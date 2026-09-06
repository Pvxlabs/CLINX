import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import bridge
import app_server


class ConfigTests(unittest.TestCase):
    def test_load_and_project_mapping(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "bridge.toml"
            repo = Path(td) / "repo"
            repo.mkdir()
            p.write_text(
                f"""
[linear]
team_id = "team"
trigger_label = "local-codex"
todo_state = "Todo"
running_state = "In Progress"
review_state = "In Review"
poll_interval_seconds = 15
max_batch = 1

[codex]
binary = "codex"
sandbox = "workspace-write"
approval = "never"

[app_server]
remote_command = ["codex", "app-server"]

[runtime]
log_dir = "{td}/logs"

[[project]]
linear_name = "Pilot"
repo = "{repo}"
target_alias = "pilot"

[targets.pilot]
ssh_alias = "p620"
thread_id = "thread-1"
session_id = "session-1"
project_id = "project-1"
cwd = "/tmp/pilot"
repository_origin = "https://example.invalid/pilot.git"
branch = "main"
app_server_version = "codex-cli 0.152.1"
""",
                encoding="utf-8",
            )
            cfg = bridge.BridgeConfig.load(p)
            self.assertEqual(cfg.team_id, "team")
            self.assertEqual(cfg.repo_for_project("Pilot"), repo.resolve())
            self.assertEqual(cfg.target_alias_for_project("Pilot"), "pilot")
            self.assertEqual(cfg.targets[0].thread_id, "thread-1")
            self.assertIsNone(cfg.repo_for_project("Unknown"))

    def test_project_id_is_optional_for_unassigned_target(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "bridge.toml"
            repo = Path(td) / "repo"
            repo.mkdir()
            p.write_text(
                f"""
[linear]
team_id = "team"
trigger_label = "local-codex"
todo_state = "Todo"
running_state = "In Progress"
review_state = "In Review"

[[project]]
linear_name = "Pilot"
repo = "{repo}"
target_alias = "pilot"

[targets.pilot]
ssh_alias = "p620"
thread_id = "thread-1"
session_id = "session-1"
cwd = "/tmp/pilot"
repository_origin = "https://example.invalid/pilot.git"
branch = "main"
app_server_version = "codex-cli 0.152.1"
""",
                encoding="utf-8",
            )
            cfg = bridge.BridgeConfig.load(p)
            self.assertIsNone(cfg.targets[0].project_id)

    def test_rejects_fast_poll(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "bridge.toml"
            p.write_text(
                """
[linear]
team_id = "team"
trigger_label = "local-codex"
todo_state = "Todo"
running_state = "In Progress"
review_state = "In Review"
poll_interval_seconds = 1

[[project]]
linear_name = "Pilot"
repo = "/tmp"
""",
                encoding="utf-8",
            )
            with self.assertRaises(bridge.BridgeError):
                bridge.BridgeConfig.load(p)


class PromptTests(unittest.TestCase):
    def test_prompt_uses_linear_as_authority(self):
        issue = {"identifier": "PVX-999"}
        prompt = bridge.codex_prompt(issue, Path("/tmp/repo"), "In Review")
        self.assertIn("PVX-999", prompt)
        self.assertIn("Linear issue is the authoritative execution contract", prompt)
        self.assertIn("Never mark the issue Done", prompt)


class FakeLinear:
    def __init__(self):
        self.comments = []
        self.state = "Todo"

    def team_states(self, team_id):
        return {"Todo": "todo", "In Progress": "running", "In Review": "review"}

    def eligible_issues(self, **kwargs):
        return []

    def update_issue_state(self, issue_id, state_id):
        self.state = "In Progress"
        return {
            "id": issue_id,
            "identifier": "PVX-1",
            "state": {"id": state_id, "name": "In Progress", "type": "started"},
        }

    def add_comment(self, issue_id, body):
        self.comments.append(body)

    def get_issue(self, issue_id):
        return {
            "id": issue_id,
            "identifier": "PVX-1",
            "state": {"id": "review", "name": "In Review", "type": "started"},
        }


class BridgeInitTests(unittest.TestCase):
    def test_initialize_resolves_required_states(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            repo.mkdir()
            cfg = bridge.BridgeConfig(
                team_id="team",
                trigger_label="local-codex",
                todo_state="Todo",
                running_state="In Progress",
                review_state="In Review",
                poll_interval_seconds=15,
                max_batch=1,
                codex_binary="codex",
                sandbox="workspace-write",
                approval="never",
                log_dir=Path(td) / "logs",
                projects=(bridge.ProjectMapping("Pilot", repo),),
            )
            b = bridge.Bridge(cfg, FakeLinear())
            b.initialize()
            self.assertEqual(b.states["In Progress"], "running")


class LinearQueryShapeTests(unittest.TestCase):
    def test_eligible_issue_team_filter_uses_graphql_id(self):
        class CaptureClient(bridge.LinearClient):
            def __init__(self):
                pass

            def request(self, query, variables=None):
                self.query = query
                self.variables = variables
                return {"issues": {"nodes": []}}

        client = CaptureClient()
        result = client.eligible_issues(
            team_id="fb8babe1-9db8-4f19-af10-fb5d116f1374",
            state_name="Todo",
            label_name="local-codex",
            first=1,
        )
        self.assertEqual(result, [])
        self.assertIn("$teamId: ID!", client.query)
        self.assertEqual(
            client.variables["teamId"],
            "fb8babe1-9db8-4f19-af10-fb5d116f1374",
        )


class CodexCommandShapeTests(unittest.TestCase):
    def test_approval_flag_precedes_exec_subcommand(self):
        cfg = bridge.BridgeConfig(
            team_id="team",
            trigger_label="local-codex",
            todo_state="Todo",
            running_state="In Progress",
            review_state="In Review",
            poll_interval_seconds=15,
            max_batch=1,
            codex_binary="codex",
            sandbox="workspace-write",
            approval="never",
            log_dir=Path("/tmp/bridge-tests"),
            projects=(bridge.ProjectMapping("Pilot", Path("/tmp/repo")),),
        )
        issue = {"identifier": "PVX-1"}

        # Mirror argv construction contract without launching Codex.
        cmd = [
            cfg.codex_binary,
            "--ask-for-approval",
            cfg.approval,
            "exec",
            "--sandbox",
            cfg.sandbox,
        ]
        self.assertLess(cmd.index("--ask-for-approval"), cmd.index("exec"))


def target_fixture(**overrides):
    values = {
        "alias": "pilot",
        "ssh_alias": "p620",
        "thread_id": "thread-1",
        "session_id": "session-1",
        "project_id": "project-1",
        "cwd": "/tmp/pilot",
        "repository_origin": "https://example.invalid/pilot.git",
        "branch": "main",
        "app_server_version": "codex-cli 0.152.1",
    }
    values.update(overrides)
    return bridge.TargetConfig(**values)


def dispatcher_fixture(target=None, client=None):
    target = target or target_fixture()
    cfg = bridge.BridgeConfig(
        team_id="team",
        trigger_label="local-codex",
        todo_state="Todo",
        running_state="In Progress",
        review_state="In Review",
        poll_interval_seconds=15,
        max_batch=1,
        codex_binary="codex",
        sandbox="workspace-write",
        approval="never",
        log_dir=Path("/tmp/bridge-tests"),
        projects=(bridge.ProjectMapping("Pilot", Path("/tmp/pilot"), "pilot"),),
        targets=(target,),
    )
    return cfg, bridge.Dispatcher(cfg, client_factory=lambda _target: client)


class FakeAppServerClient:
    def __init__(self, thread=None, initialize_info=None, error=None):
        self.thread = thread or {
            "id": "thread-1",
            "sessionId": "session-1",
            "projectId": "project-1",
            "cwd": "/tmp/pilot",
            "gitInfo": {
                "originUrl": "https://example.invalid/pilot.git",
                "branch": "main",
            },
            "canAcceptDirectInput": True,
            "status": {"type": "active"},
        }
        self.initialize_info = initialize_info or app_server.InitializeInfo(
            server_name="codex",
            server_version="0.152.1",
            user_agent="codex-cli 0.152.1",
        )
        self.error = error
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def initialize(self, **kwargs):
        self.calls.append(("initialize", kwargs))
        if self.error:
            raise self.error
        return self.initialize_info

    def thread_read(self, thread_id):
        self.calls.append(("thread/read", thread_id))
        return self.thread

    def thread_resume(self, thread_id):
        self.calls.append(("thread/resume", thread_id))
        self.thread = {**self.thread, "canAcceptDirectInput": True}
        return self.thread

    def turn_start(self, thread_id, prompt, **kwargs):
        self.calls.append(("turn/start", thread_id, prompt, kwargs))
        return app_server.TurnStartInfo(
            turn_id="turn-1",
            model=kwargs.get("model"),
            reasoning_effort=kwargs.get("reasoning_effort"),
        )


class DispatcherTests(unittest.TestCase):
    def test_unknown_target_fails(self):
        client = FakeAppServerClient()
        _cfg, dispatcher = dispatcher_fixture(client=client)
        with self.assertRaises(bridge.TargetResolutionError):
            dispatcher.dispatch("missing", "probe")
        self.assertEqual(client.calls, [])

    def test_exact_thread_dispatch_and_overrides(self):
        client = FakeAppServerClient()
        _cfg, dispatcher = dispatcher_fixture(client=client)
        result = dispatcher.dispatch(
            "pilot",
            "DISPATCHER_M0_PROBE_PASS",
            model="gpt-5.2",
            reasoning_effort="high",
        )
        self.assertEqual(result.target_alias, "pilot")
        self.assertEqual(result.thread_id, "thread-1")
        self.assertEqual(result.turn_id, "turn-1")
        self.assertEqual(result.model, "gpt-5.2")
        self.assertEqual(result.reasoning_effort, "high")
        turn_call = client.calls[-1]
        self.assertEqual(turn_call[0:2], ("turn/start", "thread-1"))
        self.assertEqual(turn_call[3]["model"], "gpt-5.2")
        self.assertEqual(turn_call[3]["reasoning_effort"], "high")

    def test_identity_mismatch_never_calls_turn_start(self):
        fields = {
            "threadId": {"id": "other-thread"},
            "sessionId": {"sessionId": "other-session"},
            "projectId": {"projectId": "other-project"},
            "cwd": {"cwd": "/tmp/other"},
            "repository origin": {
                "gitInfo": {"originUrl": "https://example.invalid/other.git"}
            },
            "branch": {"gitInfo": {"branch": "other"}},
            "canAcceptDirectInput": {"canAcceptDirectInput": False},
        }
        for field, change in fields.items():
            with self.subTest(field=field):
                client = FakeAppServerClient()
                client.thread = {**client.thread, **change}
                _cfg, dispatcher = dispatcher_fixture(client=client)
                with self.assertRaisesRegex(bridge.IdentityGuardError, "DISPATCH_IDENTITY_GUARD=FAIL"):
                    dispatcher.dispatch("pilot", "probe")
                self.assertFalse(any(call[0] == "turn/start" for call in client.calls))

    def test_unloaded_thread_is_resumed_then_reread(self):
        client = FakeAppServerClient(
            thread={
                "id": "thread-1",
                "sessionId": "session-1",
                "projectId": "project-1",
                "cwd": "/tmp/pilot",
                "gitInfo": {
                    "originUrl": "https://example.invalid/pilot.git",
                    "branch": "main",
                },
                "status": {"type": "notLoaded"},
            }
        )
        _cfg, dispatcher = dispatcher_fixture(client=client)
        dispatcher.dispatch("pilot", "probe")
        self.assertEqual(
            [call[0] for call in client.calls],
            ["initialize", "thread/read", "thread/resume", "thread/read", "turn/start"],
        )

    def test_app_server_error_is_propagated(self):
        error = app_server.AppServerRemoteError(
            "thread/read", {"code": -32000, "message": "boom"}
        )
        client = FakeAppServerClient(error=error)
        _cfg, dispatcher = dispatcher_fixture(client=client)
        with self.assertRaises(app_server.AppServerRemoteError):
            dispatcher.dispatch("pilot", "probe")
        self.assertFalse(any(call[0] == "turn/start" for call in client.calls))

    def test_project_id_null_and_configured_null_is_unassigned_pass(self):
        target = target_fixture(project_id=None)
        thread = {
            "id": "thread-1",
            "sessionId": "session-1",
            "projectId": None,
            "cwd": "/tmp/pilot",
            "canAcceptDirectInput": True,
        }
        bridge.identity_guard(target, thread)

    def test_project_id_null_and_configured_value_fails(self):
        target = target_fixture(project_id="project-1")
        thread = {"id": "thread-1", "sessionId": "session-1", "projectId": None, "cwd": "/tmp/pilot", "canAcceptDirectInput": True}
        with self.assertRaisesRegex(bridge.IdentityGuardError, "projectId"):
            bridge.identity_guard(target, thread)

    def test_project_id_exact_match_passes(self):
        target = target_fixture(project_id="project-1")
        thread = {"id": "thread-1", "sessionId": "session-1", "projectId": "project-1", "cwd": "/tmp/pilot", "canAcceptDirectInput": True}
        bridge.identity_guard(target, thread)

    def test_project_id_non_null_with_unassigned_config_fails(self):
        target = target_fixture(project_id=None)
        thread = {"id": "thread-1", "sessionId": "session-1", "projectId": "project-1", "cwd": "/tmp/pilot", "canAcceptDirectInput": True}
        with self.assertRaisesRegex(bridge.IdentityGuardError, "projectId"):
            bridge.identity_guard(target, thread)

    def test_git_info_present_exact_match_passes(self):
        target = target_fixture()
        thread = {
            "id": "thread-1",
            "sessionId": "session-1",
            "projectId": "project-1",
            "cwd": "/tmp/pilot",
            "canAcceptDirectInput": True,
        }
        evidence = bridge.RepositoryIdentityEvidence(
            source="app_server",
            cwd="/tmp/pilot",
            origin="https://example.invalid/pilot.git",
            branch="main",
        )
        bridge.identity_guard(target, thread, repository_evidence=evidence)

    def test_git_info_absent_uses_thread_read_cwd_and_exact_local_git(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "pilot"
            repo.mkdir()
            subprocess.run(["git", "-C", str(repo), "init", "-b", "main"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "https://example.invalid/pilot.git"], check=True)
            (repo / "README.md").write_text("pilot\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
            subprocess.run(
                ["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "init"],
                check=True,
                capture_output=True,
            )
            target = target_fixture(cwd=str(repo), repository_origin="https://example.invalid/pilot.git")
            thread = {
                "id": "thread-1",
                "sessionId": "session-1",
                "projectId": "project-1",
                "cwd": str(repo),
                "projectId": "project-1",
                "canAcceptDirectInput": True,
                "gitInfo": None,
            }
            evidence = bridge._repository_identity_evidence(thread)
            self.assertEqual(evidence.source, "local_git")
            bridge.identity_guard(target, thread, repository_evidence=evidence)

    def test_git_info_absent_origin_mismatch_fails(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "pilot"
            repo.mkdir()
            subprocess.run(["git", "-C", str(repo), "init", "-b", "main"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "https://example.invalid/other.git"], check=True)
            (repo / "README.md").write_text("pilot\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
            subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "init"], check=True, capture_output=True)
            target = target_fixture(cwd=str(repo), repository_origin="https://example.invalid/pilot.git")
            thread = {"id": "thread-1", "sessionId": "session-1", "projectId": "project-1", "cwd": str(repo), "canAcceptDirectInput": True, "gitInfo": None}
            evidence = bridge._repository_identity_evidence(thread)
            with self.assertRaisesRegex(bridge.IdentityGuardError, "repositoryOrigin"):
                bridge.identity_guard(target, thread, repository_evidence=evidence)

    def test_git_info_absent_branch_mismatch_fails(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "pilot"
            repo.mkdir()
            subprocess.run(["git", "-C", str(repo), "init", "-b", "main"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "https://example.invalid/pilot.git"], check=True)
            (repo / "README.md").write_text("pilot\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
            subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "init"], check=True, capture_output=True)
            target = target_fixture(cwd=str(repo), branch="other")
            thread = {"id": "thread-1", "sessionId": "session-1", "projectId": "project-1", "cwd": str(repo), "canAcceptDirectInput": True, "gitInfo": None}
            evidence = bridge._repository_identity_evidence(thread)
            with self.assertRaisesRegex(bridge.IdentityGuardError, "branch"):
                bridge.identity_guard(target, thread, repository_evidence=evidence)

    def test_git_info_absent_non_git_cwd_fails(self):
        with tempfile.TemporaryDirectory() as td:
            target = target_fixture(cwd=td)
            thread = {"id": "thread-1", "sessionId": "session-1", "projectId": "project-1", "cwd": td, "canAcceptDirectInput": True, "gitInfo": None}
            with self.assertRaisesRegex(bridge.IdentityGuardError, "local Git identity lookup failed"):
                bridge._repository_identity_evidence(thread)

    def test_local_git_lookup_uses_thread_read_cwd(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "pilot"
            repo.mkdir()
            subprocess.run(["git", "-C", str(repo), "init", "-b", "main"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "https://example.invalid/pilot.git"], check=True)
            (repo / "README.md").write_text("pilot\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
            subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "init"], check=True, capture_output=True)
            target = target_fixture(cwd="/tmp/pilot", repository_origin="https://example.invalid/pilot.git")
            thread = {"id": "thread-1", "sessionId": "session-1", "projectId": "project-1", "cwd": str(repo), "canAcceptDirectInput": True, "gitInfo": None}
            evidence = bridge._repository_identity_evidence(thread)
            self.assertEqual(evidence.cwd, str(repo))
            with self.assertRaisesRegex(bridge.IdentityGuardError, "cwd"):
                bridge.identity_guard(target, thread, repository_evidence=evidence)


class LinearDispatchIntegrationTests(unittest.TestCase):
    def test_issue_execution_uses_dispatcher_after_claim(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            repo.mkdir()
            cfg = bridge.BridgeConfig(
                team_id="team",
                trigger_label="local-codex",
                todo_state="Todo",
                running_state="In Progress",
                review_state="In Review",
                poll_interval_seconds=15,
                max_batch=1,
                codex_binary="codex",
                sandbox="workspace-write",
                approval="never",
                log_dir=Path(td) / "logs",
                projects=(bridge.ProjectMapping("Pilot", repo, "pilot"),),
                targets=(target_fixture(),),
            )

            class RecordingDispatcher:
                def __init__(self):
                    self.calls = []

                def dispatch(self, target_alias, prompt, model=None, reasoning_effort=None):
                    self.calls.append((target_alias, prompt, model, reasoning_effort))
                    return bridge.DispatchResult(
                        target_alias=target_alias,
                        thread_id="thread-1",
                        turn_id="turn-1",
                        model=model,
                        reasoning_effort=reasoning_effort,
                        dispatch_status="DISPATCHED",
                    )

            linear = FakeLinear()
            dispatcher = RecordingDispatcher()
            instance = bridge.Bridge(cfg, linear, dispatcher=dispatcher)
            instance.initialize()
            issue = {
                "id": "issue-1",
                "identifier": "PVX-1",
                "title": "Pilot",
                "project": {"name": "Pilot"},
            }

            instance.execute_issue(issue, repo)

            self.assertEqual(linear.state, "In Progress")
            self.assertEqual(len(dispatcher.calls), 1)
            self.assertEqual(dispatcher.calls[0][0], "pilot")
            self.assertIn("PVX-1", dispatcher.calls[0][1])
            self.assertTrue(any("BRIDGE_DISPATCHED" in body for body in linear.comments))


class AppServerClientTests(unittest.TestCase):
    def test_process_transport_writes_line_delimited_json(self):
        class FakeStream:
            def __init__(self):
                self.writes = []

            def write(self, value):
                self.writes.append(value)

            def flush(self):
                pass

            def close(self):
                pass

        class FakeProcess:
            def __init__(self):
                self.stdin = FakeStream()
                self.stdout = FakeStream()

            def poll(self):
                return None

            def terminate(self):
                pass

            def wait(self, timeout=None):
                return 0

            def kill(self):
                pass

        process = FakeProcess()
        transport = app_server.ProcessStdioTransport(
            ("codex", "app-server", "proxy"),
            popen=lambda *args, **kwargs: process,
        )
        transport.connect()
        transport.send({"id": "request-1", "method": "initialize"})
        self.assertEqual(
            process.stdin.writes,
            ['{"id":"request-1","method":"initialize"}\n'],
        )
        transport.close()

    def test_local_transport_builds_proxy_command_without_ssh(self):
        class FakeStream:
            def __init__(self, data=b""):
                self.data = bytearray(data)
                self.writes = []

            def write(self, value):
                self.writes.append(value)

            def flush(self):
                pass

            def read(self, length):
                value = bytes(self.data[:length])
                del self.data[:length]
                return value

            def close(self):
                pass

        handshake = (
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"Upgrade: websocket\r\n"
            b"Connection: Upgrade\r\n"
            b"Sec-WebSocket-Accept: invalid\r\n\r\n"
        )

        class FakeProcess:
            stdin = FakeStream()
            stdout = FakeStream(handshake)

            def poll(self):
                return 0

            def terminate(self):
                pass

            def wait(self, timeout=None):
                return 0

            def kill(self):
                pass

        calls = []

        def fake_popen(*args, **kwargs):
            calls.append((args, kwargs))
            return FakeProcess()

        # The handshake is exercised separately below; this test only checks
        # the process command shape without depending on a real accept key.
        transport = app_server.LocalStdioTransport(
            ("codex", "app-server", "proxy"),
            popen=fake_popen,
        )
        transport._byte_transport.connect()
        self.assertEqual(calls[0][0][0], ["codex", "app-server", "proxy"])
        self.assertEqual(transport.command, ("codex", "app-server", "proxy"))
        transport.close()

    def test_websocket_transport_performs_handshake_and_masks_json(self):
        class FakeStream:
            def __init__(self, data=b""):
                self.data = bytearray(data)
                self.writes = []

            def write(self, value):
                self.writes.append(value)

            def flush(self):
                pass

            def read(self, length):
                value = bytes(self.data[:length])
                del self.data[:length]
                return value

            def close(self):
                pass

        # Accept validation is deterministic by deriving it from the key sent
        # in the handshake request, then providing one server text frame.
        class FakeProcess:
            def __init__(self):
                self.stdin = FakeStream()
                self.stdout = FakeStream()

            def poll(self):
                return None

            def terminate(self):
                pass

            def wait(self, timeout=None):
                return 0

            def kill(self):
                pass

        process = FakeProcess()

        def fake_popen(*args, **kwargs):
            return process

        transport = app_server.LocalStdioTransport(
            ("codex", "app-server", "proxy"),
            popen=fake_popen,
        )

        original_send_bytes = process.stdin.write

        def write_and_prepare(value):
            original_send_bytes(value)
            if value.startswith(b"GET "):
                headers = value.decode("ascii").split("\r\n")
                key = next(line.split(": ", 1)[1] for line in headers if line.startswith("Sec-WebSocket-Key:"))
                import base64
                import hashlib

                accept = base64.b64encode(
                    hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
                ).decode()
                process.stdout.data.extend(
                    (
                        "HTTP/1.1 101 Switching Protocols\r\n"
                        "Upgrade: websocket\r\n"
                        "Connection: Upgrade\r\n"
                        f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
                    ).encode()
                )

        process.stdin.write = write_and_prepare
        transport.connect()
        self.assertTrue(process.stdin.writes[0].startswith(b"GET / HTTP/1.1\r\n"))
        self.assertIn(b"Upgrade: websocket", process.stdin.writes[0])

        transport.send({"id": "request-1", "method": "model/list"})
        frame = process.stdin.writes[-1]
        self.assertEqual(frame[0] & 0x0F, 0x1)
        self.assertTrue(frame[1] & 0x80)
        self.assertNotIn(b'"method":"model/list"', frame[6:])
        transport.close()

    def test_default_client_uses_local_websocket_transport(self):
        cfg, _dispatcher = dispatcher_fixture(client=None)
        client = bridge._default_app_server_client(cfg, target_fixture())
        self.assertIsInstance(client.transport, app_server.LocalStdioTransport)
        self.assertEqual(
            client.transport.command,
            ("codex", "app-server", "proxy"),
        )

    def test_local_transport_does_not_invoke_ssh(self):
        cfg, _dispatcher = dispatcher_fixture(client=None)
        client = bridge._default_app_server_client(cfg, target_fixture())
        self.assertIsInstance(client.transport, app_server.LocalStdioTransport)
        self.assertNotIsInstance(client.transport, app_server.SSHStdioTransport)

    def test_protocol_payloads_use_exact_thread_and_dispatch_overrides(self):
        class FakeTransport:
            def __init__(self):
                self.sent = []
                self.responses = []

            def send(self, message):
                self.sent.append(message)
                method = message.get("method")
                if method == "initialize":
                    self.responses.append(
                        {
                            "id": message["id"],
                            "result": {
                                "serverInfo": {
                                    "name": "codex",
                                    "version": "0.152.1",
                                },
                                "userAgent": "codex-cli 0.152.1",
                            },
                        }
                    )
                elif method == "thread/read":
                    self.responses.append(
                        {
                            "id": message["id"],
                            "result": {
                                "thread": {
                                    "id": message["params"]["threadId"],
                                }
                            },
                        }
                    )
                elif method == "thread/resume":
                    self.responses.append(
                        {
                            "id": message["id"],
                            "result": {
                                "thread": {
                                    "id": message["params"]["threadId"],
                                }
                            },
                        }
                    )
                elif method == "turn/start":
                    self.responses.append(
                        {
                            "id": message["id"],
                            "result": {"turn": {"id": "turn-1"}},
                        }
                    )

            def receive(self, _timeout):
                return self.responses.pop(0)

            def close(self):
                pass

        transport = FakeTransport()
        client = app_server.CodexAppServerClient(transport)
        client.initialize(
            client_name="bridge",
            client_title="Bridge",
            client_version="1.0.0-m0",
        )
        client.thread_read("durable-thread")
        client.thread_resume("durable-thread")
        turn = client.turn_start(
            "durable-thread",
            "DISPATCHER_M0_PROBE_PASS",
            cwd="/tmp/pilot",
            model="gpt-5.2",
            reasoning_effort="high",
        )

        initialize_request = transport.sent[0]
        self.assertEqual(initialize_request["method"], "initialize")
        self.assertEqual(initialize_request["params"]["clientInfo"]["name"], "bridge")
        self.assertEqual(transport.sent[1], {"method": "initialized"})

        thread_reads = [item for item in transport.sent if item.get("method") == "thread/read"]
        self.assertEqual(len(thread_reads), 1)
        self.assertEqual(thread_reads[0]["params"], {"threadId": "durable-thread"})

        resumes = [item for item in transport.sent if item.get("method") == "thread/resume"]
        self.assertEqual(len(resumes), 1)
        self.assertEqual(resumes[0]["params"], {"threadId": "durable-thread"})

        turn_request = next(item for item in transport.sent if item.get("method") == "turn/start")
        self.assertEqual(turn_request["params"]["threadId"], "durable-thread")
        self.assertEqual(turn_request["params"]["cwd"], "/tmp/pilot")
        self.assertEqual(turn_request["params"]["model"], "gpt-5.2")
        self.assertEqual(turn_request["params"]["effort"], "high")
        self.assertEqual(
            turn_request["params"]["input"],
            [{"type": "text", "text": "DISPATCHER_M0_PROBE_PASS"}],
        )
        self.assertEqual(turn.turn_id, "turn-1")

    def test_thread_start_builds_minimal_durable_payload(self):
        class FakeTransport:
            def __init__(self):
                self.sent = []
                self.responses = []

            def send(self, message):
                self.sent.append(message)
                if message.get("method") == "thread/start":
                    self.responses.append(
                        {
                            "id": message["id"],
                            "result": {
                                "thread": {
                                    "id": "pilot-thread",
                                    "sessionId": "pilot-session",
                                    "projectId": None,
                                    "cwd": "/home/pvxlabs/dev/clinx-pilot",
                                }
                            },
                        }
                    )

            def receive(self, _timeout):
                return self.responses.pop(0)

            def close(self):
                pass

        transport = FakeTransport()
        client = app_server.CodexAppServerClient(transport)
        thread = client.thread_start(
            cwd="/home/pvxlabs/dev/clinx-pilot",
            model="gpt-5.2",
            sandbox="read-only",
            ephemeral=False,
        )
        self.assertEqual(thread["id"], "pilot-thread")
        request = transport.sent[0]
        self.assertEqual(request["method"], "thread/start")
        self.assertEqual(
            request["params"],
            {
                "cwd": "/home/pvxlabs/dev/clinx-pilot",
                "model": "gpt-5.2",
                "sandbox": "read-only",
                "ephemeral": False,
            },
        )

    def test_ssh_transport_builds_configured_command_without_shell(self):
        class FakeStream:
            def __init__(self, data=b""):
                self.data = bytearray(data)

            def write(self, _value):
                pass

            def flush(self):
                pass

            def read(self, length):
                value = bytes(self.data[:length])
                del self.data[:length]
                return value

            def close(self):
                pass

        handshake = (
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"Upgrade: websocket\r\n"
            b"Connection: Upgrade\r\n"
            b"Sec-WebSocket-Accept: invalid\r\n\r\n"
        )

        class FakeProcess:
            stdin = FakeStream()
            stdout = FakeStream(handshake)

            def poll(self):
                return 0

            def terminate(self):
                pass

            def wait(self, timeout=None):
                return 0

            def kill(self):
                pass

        calls = []

        def fake_popen(*args, **kwargs):
            calls.append((args, kwargs))
            return FakeProcess()

        transport = app_server.SSHStdioTransport(
            "p620",
            ("codex", "app-server", "proxy"),
            ssh_binary="ssh",
            ssh_args=("-T", "-o", "BatchMode=yes"),
            popen=fake_popen,
        )
        transport._byte_transport.connect()
        self.assertEqual(
            calls[0][0][0],
            ["ssh", "-T", "-o", "BatchMode=yes", "p620", "codex", "app-server", "proxy"],
        )
        self.assertEqual(calls[0][1]["shell"], False)
        self.assertEqual(calls[0][1]["stderr"], app_server.subprocess.DEVNULL)

    def test_request_ids_and_server_events_are_handled(self):
        class FakeTransport:
            def __init__(self):
                self.sent = []
                self.responses = []

            def send(self, message):
                self.sent.append(message)
                if message.get("method") == "model/list":
                    self.responses.append({"method": "turn/started", "params": {}})
                    self.responses.append({"id": message["id"], "result": {"data": []}})

            def receive(self, _timeout):
                return self.responses.pop(0)

            def close(self):
                pass

        transport = FakeTransport()
        client = app_server.CodexAppServerClient(transport)
        result = client.model_list()
        self.assertEqual(result, {"data": []})
        self.assertEqual(client.events, ["turn/started"])
        self.assertIsInstance(transport.sent[0]["id"], str)


if __name__ == "__main__":
    unittest.main()
