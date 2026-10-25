"""Explicit host opt-in for sharing read-only Monitor configuration over mTLS."""

from pathlib import Path
from urllib.parse import urlsplit

from .discovery import _command
from .identity import DeviceError, PrivateStore


def validate_connection(raw):
    # urllib remains the URL wire parser. Rust owns all endpoint/token policy.
    try:
        endpoint, credential = raw["endpoint"], raw["credential"]
        url = urlsplit(endpoint)
        return _command(
            "validate_connection",
            endpoint=endpoint,
            credential=credential,
            url=dict(
                scheme=url.scheme,
                hostname=url.hostname,
                username=url.username,
                password=url.password,
                path=url.path,
                query=url.query,
                fragment=url.fragment,
                port=url.port,
            ),
        )
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise DeviceError("INVALID_OBSERVER_CONNECTION") from exc


class ObserverBootstrap:
    def __init__(self, config: Path):
        self.config = config.expanduser().absolute()

    def connection(self):
        # Local owner-only files, no shell expansion or arbitrary environment loading.
        PrivateStore._check(self.config)
        with self.config.open() as stream:
            text = stream.read(4097)
        raw = _command("bootstrap_config", size=self.config.stat().st_size, text=text)
        credential_file = Path(raw["credential_file"]).expanduser().absolute()
        PrivateStore._check(credential_file)
        with credential_file.open() as stream:
            text = stream.read(16385)
        token = _command(
            "bootstrap_token", size=credential_file.stat().st_size, text=text
        )
        return validate_connection({"endpoint": raw["endpoint"], "credential": token})
