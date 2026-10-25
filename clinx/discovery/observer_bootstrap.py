"""Python binding for Rust private Observer configuration IO and URL validation."""
from pathlib import Path

from .discovery import _command
from .identity import DeviceError


def validate_connection(raw):
    try:
        return _command('connection_parse', endpoint=raw['endpoint'], credential=raw['credential'])
    except (TypeError, KeyError, AttributeError) as exc:
        raise DeviceError('INVALID_OBSERVER_CONNECTION') from exc


class ObserverBootstrap:
    def __init__(self, config: Path):
        self.config = config.expanduser().absolute()

    def connection(self):
        return _command('bootstrap_read', path=str(self.config))
