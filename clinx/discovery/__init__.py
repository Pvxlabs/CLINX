"""LAN device identity/authentication. This package grants no execution authority."""

PROTOCOL_VERSION = 1
SERVICE_TYPE = "_clinx._tcp.local."
# Backend availability must be checked at runtime; this is not an audit badge.
SECURITY_STATUS = "REQUIRES_BACKEND_CHECK"
