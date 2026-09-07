import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parent
LAUNCHER = ROOT / "bin" / "clinx-context-mcp"
PYTHON = Path("/usr/bin/python3.12")


def _request(process, payload):
    process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
    process.stdin.flush()
    line = process.stdout.readline()
    if not line:
        raise AssertionError(f"MCP child closed stdout; poll={process.poll()}")
    return json.loads(line), line


class TunnelChildCompatibilityTests(unittest.TestCase):
    def _start(self, cwd="/"):
        env = os.environ.copy()
        env.update(
            {
                "CONTROL_PLANE_API_KEY": "must-not-reach-child",
                "LINEAR_API_KEY": "must-not-reach-child",
                "PYTHONPATH": "/tmp/must-not-reach-child",
            }
        )
        return subprocess.Popen(
            [str(LAUNCHER)],
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )

    def test_launcher_pins_absolute_python_and_paths(self):
        self.assertTrue(PYTHON.is_file())
        self.assertTrue(os.access(PYTHON, os.X_OK))
        text = LAUNCHER.read_text(encoding="utf-8")
        self.assertIn("/usr/bin/python3.12", text)
        self.assertIn("$repo_root/mcp_server.py", text)
        self.assertIn("$repo_root/bridge.toml", text)
        self.assertIn("/usr/bin/env -i", text)
        self.assertNotIn("CONTROL_PLANE_API_KEY", text)
        self.assertNotIn("LINEAR_API_KEY", text)

    def test_multiple_requests_keep_child_alive_and_stdout_is_protocol_only(self):
        process = self._start(cwd="/")
        try:
            initialize, initialize_line = _request(
                process,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "tunnel-child-harness", "version": "1"},
                    },
                },
            )
            self.assertEqual(initialize["id"], 1)
            self.assertIn("result", initialize)
            self.assertIsNone(process.poll())

            discover, discover_line = _request(
                process,
                {
                    "jsonrpc": "2.0",
                    "id": 0,
                    "method": "server/discover",
                    "params": {
                        "_meta": {
                            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                            "io.modelcontextprotocol/clientInfo": {"name": "modern", "version": "1"},
                            "io.modelcontextprotocol/clientCapabilities": {},
                        },
                    },
                },
            )
            self.assertEqual(discover["result"]["resultType"], "complete")
            self.assertEqual(discover["result"]["supportedVersions"], ["2026-07-28"])
            self.assertEqual(discover["result"]["capabilities"], {"tools": {}})
            self.assertIsNone(process.poll())

            notification = {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            }
            process.stdin.write(json.dumps(notification, separators=(",", ":")) + "\n")
            process.stdin.flush()
            time.sleep(0.05)
            self.assertIsNone(process.poll())

            tools, tools_line = _request(
                process,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/list",
                    "params": {
                        "_meta": {
                            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                            "io.modelcontextprotocol/clientInfo": {"name": "modern", "version": "1"},
                            "io.modelcontextprotocol/clientCapabilities": {},
                        },
                    },
                },
            )
            self.assertEqual(tools["result"]["resultType"], "complete")
            names = [tool["name"] for tool in tools["result"]["tools"]]
            self.assertEqual(
                names,
                [
                    "clinx_find_task",
                    "clinx_get_context",
                    "clinx_get_topic_status",
                    "clinx_list_projects",
                    "clinx_get_status",
                    "clinx_get_capabilities",
                    "clinx_prepare_execution",
                ],
            )
            self.assertNotIn("clinx_execute", names)
            self.assertIsNone(process.poll())

            find_task, find_task_line = _request(
                process,
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "clinx_find_task",
                        "arguments": {"query": "__tunnel_child_harness__"},
                        "_meta": {
                            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                            "io.modelcontextprotocol/clientInfo": {"name": "modern", "version": "1"},
                            "io.modelcontextprotocol/clientCapabilities": {},
                        },
                    },
                },
            )
            self.assertEqual(find_task["id"], 3)
            self.assertIn("result", find_task)
            self.assertEqual(find_task["result"]["resultType"], "complete")
            self.assertIsNone(process.poll())

            get_status, get_status_line = _request(
                process,
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {
                        "name": "clinx_get_status",
                        "arguments": {
                            "query": "__tunnel_child_harness__",
                            "project": "CLINX",
                        },
                    },
                },
            )
            self.assertEqual(get_status["id"], 4)
            self.assertIn("result", get_status)
            self.assertIsNone(process.poll())

            for line in (discover_line, initialize_line, tools_line, find_task_line, get_status_line):
                self.assertIsInstance(json.loads(line), dict)
                self.assertTrue(line.endswith("\n"))
        finally:
            process.stdin.close()
            self.assertEqual(process.wait(timeout=3), 0)
            self.assertEqual(process.stdout.read(), "")
            self.assertEqual(process.stderr.read(), "")
            process.stdout.close()
            process.stderr.close()

    def test_cwd_independence_from_supported_launch_locations(self):
        for cwd in ("/", "/tmp", "/home/pvxlabs"):
            process = self._start(cwd=cwd)
            try:
                response, _ = _request(
                    process,
                    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                )
                self.assertIn("result", response)
                self.assertIsNone(process.poll())
            finally:
                process.stdin.close()
                self.assertEqual(process.wait(timeout=3), 0)
                self.assertEqual(process.stdout.read(), "")
                self.assertEqual(process.stderr.read(), "")
                process.stdout.close()
                process.stderr.close()

    def test_missing_config_fails_on_stderr_without_stdout_protocol(self):
        with tempfile.TemporaryDirectory() as td:
            process = subprocess.Popen(
                [
                    str(PYTHON),
                    str(ROOT / "mcp_server.py"),
                    "--config",
                    str(Path(td) / "missing.toml"),
                    "--stdio",
                ],
                cwd="/",
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            process.stdin.close()
            self.assertNotEqual(process.wait(timeout=3), 0)
            self.assertEqual(process.stdout.read(), "")
            self.assertIn("MCP_SERVER=BLOCKED", process.stderr.read())
            process.stdout.close()
            process.stderr.close()

    def test_parent_eof_is_clean_exit(self):
        process = self._start()
        process.stdin.close()
        self.assertEqual(process.wait(timeout=3), 0)
        self.assertEqual(process.stdout.read(), "")
        self.assertEqual(process.stderr.read(), "")
        process.stdout.close()
        process.stderr.close()


if __name__ == "__main__":
    unittest.main()
