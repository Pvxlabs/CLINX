import os
from pathlib import Path
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

    def test_ssh_transport_builds_configured_command_without_shell(self):
        class FakeStream:
            def close(self):
                pass

        class FakeProcess:
            stdin = FakeStream()
            stdout = FakeStream()

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
            ("codex", "app-server", "--stdio"),
            ssh_binary="ssh",
            ssh_args=("-T", "-o", "BatchMode=yes"),
            popen=fake_popen,
        )
        transport.connect()
        self.assertEqual(
            calls[0][0][0],
            ["ssh", "-T", "-o", "BatchMode=yes", "p620", "codex", "app-server", "--stdio"],
        )
        self.assertEqual(calls[0][1]["shell"], False)
        self.assertEqual(calls[0][1]["stderr"], app_server.subprocess.DEVNULL)
        transport.close()

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
