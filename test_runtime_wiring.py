import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent


class RuntimeWiringTests(unittest.TestCase):
    def test_clinx_service_starts_after_and_wants_tunnel(self):
        service = (ROOT / "systemd" / "clinx.service").read_text()
        self.assertIn("After=network-online.target clinx-tunnel.service", service)
        self.assertIn("Wants=clinx-tunnel.service", service)
        self.assertIn("ExecStart=/usr/bin/python3 /home/pvxlabs/dev/clinx/bridge.py", service)

    def test_tunnel_service_is_part_of_clinx_restart_and_uses_canonical_profile(self):
        service = (ROOT / "systemd" / "clinx-tunnel.service").read_text()
        self.assertIn("PartOf=clinx.service", service)
        self.assertIn("/home/pvxlabs/.local/bin/tunnel-client run", service)
        self.assertIn("--profile-dir %h/.config/tunnel-client", service)
        self.assertIn("--profile clinx-p620", service)
        self.assertIn("KillMode=control-group", service)

    def test_tunnel_unit_does_not_embed_runtime_credentials(self):
        service = (ROOT / "systemd" / "clinx-tunnel.service").read_text()
        self.assertNotIn("CONTROL_PLANE_API_KEY=", service)
        self.assertNotIn("LINEAR_API_KEY=", service)
