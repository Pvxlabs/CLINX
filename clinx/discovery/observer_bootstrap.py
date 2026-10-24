"""Explicit host opt-in for sharing read-only Monitor configuration over mTLS."""

import ipaddress
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from .identity import DeviceError, PrivateStore


def validate_connection(raw):
    try:
        endpoint, credential = raw["endpoint"], raw["credential"]
        url = urlsplit(endpoint)
        host = url.hostname or ""
        private = host.endswith((".ts.net", ".local"))
        try:
            address = ipaddress.ip_address(host)
            private = address.is_private and not address.is_unspecified and not address.is_multicast
        except ValueError:
            pass
        if (url.scheme != "https" or not private or url.username is not None
                or url.password is not None or url.path not in ("", "/")
                or url.query or url.fragment or len(endpoint) > 2048
                or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", credential)):
            raise ValueError()
        if url.port is not None and not 1 <= url.port <= 65535:
            raise ValueError()
        return {"endpoint": endpoint, "credential": credential, "read_only": True}
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise DeviceError("INVALID_OBSERVER_CONNECTION") from exc


class ObserverBootstrap:
    def __init__(self, config: Path):
        self.config = config.expanduser().absolute()

    def connection(self):
        # Local owner-only files, no shell expansion or arbitrary environment loading.
        PrivateStore._check(self.config)
        if self.config.stat().st_size > 4096:
            raise DeviceError("INVALID_OBSERVER_CONFIGURATION")
        raw = json.loads(self.config.read_text())
        if raw.get("share_read_only_observer") is not True:
            raise DeviceError("OBSERVER_SHARING_DISABLED")
        credential_file = Path(raw["credential_file"]).expanduser().absolute()
        PrivateStore._check(credential_file)
        if credential_file.stat().st_size > 16384:
            raise DeviceError("INVALID_OBSERVER_CONFIGURATION")
        tokens = re.findall(r"^CLINX_OBSERVER_TOKEN=([A-Za-z0-9_-]{32,256})$",
                            credential_file.read_text(), re.MULTILINE)
        if len(tokens) != 1:
            raise DeviceError("INVALID_OBSERVER_CONFIGURATION")
        return validate_connection({"endpoint": raw["endpoint"], "credential": tokens[0]})
