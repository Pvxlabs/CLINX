"""Device commands integrated into the existing bridge argparse entry point."""

from __future__ import annotations

import argparse
import getpass
import json
import socket
import sys
import time
import tomllib
from dataclasses import asdict
from pathlib import Path
from typing import Any

COMMANDS = {"devices", "pair", "serve"}


def add_commands(sub: Any) -> None:
    for name, help_text in (
        ("devices", "Discover nearby devices and reconnect trusted devices"),
        ("pair", "Select a nearby device, or 'pair accept' to display a one-time code"),
        ("serve", "Advertise this device and accept authenticated reconnections"),
    ):
        parser = sub.add_parser(name, help=help_text)
        parser.add_argument(
            "--state-dir", type=Path, help="Advanced: private device state directory"
        )
        parser.add_argument("--display-name", help="Friendly name on first initialization")
        parser.add_argument(
            "--allow-dev-pairing",
            action="store_true",
            help="DEV_ONLY: opt into non-constant-time SPAKE2 and development trust",
        )
        parser.add_argument(
            "--wait", type=float, default=2.0, help="Discovery interval in seconds (0–30)"
        )
        parser.add_argument("--json", action="store_true")
        if name == "pair":
            parser.add_argument("device", nargs="?", help="Device name/node ID, or accept")
        if name in {"pair", "serve"}:
            parser.add_argument(
                "--port", type=int, default=0, help="Advanced: listener port (default automatic)"
            )


def _settings(args: argparse.Namespace) -> tuple[Path, str]:
    from bridge import detect_runtime_host

    config_path = Path(args.config).expanduser()
    raw = tomllib.loads(config_path.read_text()) if config_path.exists() else {}
    runtime = raw.get("runtime", {})
    host = detect_runtime_host(runtime.get("runtime_host"), hostname=socket.gethostname())
    default = (
        Path(runtime.get("task_db_path", "~/.local/state/clinx/tasks.sqlite3")).expanduser().parent
        / "devices"
    )
    return args.state_dir or default, host


def _select(candidates: dict[str, Any], identity: Any, selector: str | None) -> Any:
    choices = [c for c in candidates.values() if c.node_id != identity.public["node_id"]]
    if selector:
        choices = [c for c in choices if selector in {c.node_id, c.display_name}]
        if len(choices) != 1:
            raise ValueError("DEVICE_SELECTION_NOT_UNIQUE")
        return choices[0]
    if not choices:
        raise ValueError("NO_NEARBY_DEVICES")
    choices.sort(key=lambda c: c.node_id)
    for i, candidate in enumerate(choices, 1):
        print(f"{i}. {candidate.display_name} ({candidate.node_id})")
    if not sys.stdin.isatty():
        raise ValueError("INTERACTIVE_DEVICE_SELECTION_REQUIRED")
    answer = input("Select device: ")
    if not answer.isdecimal() or not 1 <= int(answer) <= len(choices):
        raise ValueError("INVALID_DEVICE_SELECTION")
    return choices[int(answer) - 1]


def handle(args: argparse.Namespace) -> int:
    try:
        from .discovery import DiscoveryIndex, LanDiscovery, device_rows
        from .identity import DeviceError, NodeIdentity, PrivateStore, TrustedPeerStore
        from .pairing import backend_status
        from .transport import DeviceServer, LanTransport, reconnect_discovered

        if not 0 <= args.wait <= 30:
            raise DeviceError("INVALID_DISCOVERY_INTERVAL")
        accepting = args.command == "pair" and args.device == "accept"
        # No PIN on argv, pipes, JSON, log files, or a non-interactive console.
        if accepting and (args.json or not sys.stdout.isatty()):
            raise DeviceError("PAIRING_REQUIRES_LOCAL_TTY")
        if args.command == "pair" and not args.allow_dev_pairing:
            from .pairing import production_backend

            production_backend()
        root, host = _settings(args)
        store = PrivateStore(root)
        identity = NodeIdentity(store, host, args.display_name)
        peers = TrustedPeerStore(store)
        index = DiscoveryIndex()
        if args.allow_dev_pairing:
            print(
                "DEV_ONLY / SECURITY_BLOCKER: SPAKE2 backend is not constant-time.", file=sys.stderr
            )
        if args.command == "serve" or accepting:
            with DeviceServer(
                identity, peers, port=args.port, allow_dev=args.allow_dev_pairing
            ) as server:
                with LanDiscovery(index) as discovery:
                    discovery.advertise(identity, server.port)
                    print(
                        f"This Device: {identity.public['display_name']} — listening for nearby devices",
                        flush=True,
                    )
                    if accepting:
                        code = server.window.open()
                        print(
                            f"Pairing code: {code} (60 seconds; one use; at most 3 failures)",
                            flush=True,
                        )
                        code = ""
                    last_attempt: dict[str, float] = {}
                    while True:
                        candidates = index.candidates()
                        due = {
                            n: c
                            for n, c in candidates.items()
                            if time.monotonic() - last_attempt.get(n, -60.0) >= 10.0
                        }
                        reconnect_discovered(identity, peers, due, allow_dev=args.allow_dev_pairing)
                        for node_id in due:
                            last_attempt[node_id] = time.monotonic()
                        last_attempt = {n: t for n, t in last_attempt.items() if n in candidates}
                        time.sleep(0.5)
        with LanDiscovery(index):
            time.sleep(args.wait)
            candidates = index.candidates()
            if args.command == "devices":
                states = reconnect_discovered(
                    identity, peers, candidates, allow_dev=args.allow_dev_pairing
                )
                rows = device_rows(identity, peers, index)
                for row in rows:
                    row["authenticated"] = states.get(row["node_id"]) == "AUTHENTICATED"
                    if states.get(row["node_id"]) == "IDENTITY_MISMATCH":
                        row["state"] = "Identity Mismatch"
                if args.json:
                    print(
                        json.dumps(
                            dict(
                                devices=rows,
                                security_status=backend_status(allow_dev=args.allow_dev_pairing),
                            ),
                            sort_keys=True,
                        )
                    )
                else:
                    for row in rows:
                        auth = " / authenticated" if row["authenticated"] else ""
                        print(f"{row['state']}: {row['display_name']} ({row['node_id']}){auth}")
                return 0
            candidate = _select(candidates, identity, args.device)
            transport = LanTransport(identity, peers, allow_dev=args.allow_dev_pairing)
            old = peers.all().get(candidate.node_id)
            needs_secure_repair = (
                old and old["security_status"] == "DEV_ONLY" and not args.allow_dev_pairing
            )
            if old and not needs_secure_repair:
                session = transport.reconnect(candidate)
            else:
                if not sys.stdin.isatty():
                    raise DeviceError("LOCAL_TTY_REQUIRED_FOR_PIN")
                code = getpass.getpass("4-digit code shown on the other device: ")
                try:
                    session = transport.pair(candidate, code)
                finally:
                    code = ""
            print(
                json.dumps(asdict(session), sort_keys=True)
                if args.json
                else f"Trusted device connected: {candidate.display_name}; execution authorization still required"
            )
            return 0
    except KeyboardInterrupt:
        return 0
    except ImportError:
        print(
            "DISCOVERY_DEPENDENCIES_MISSING: install requirements-discovery.txt in an isolated environment",
            file=sys.stderr,
        )
        return 2
    except Exception as exc:
        # Only our fixed codes are safe for terminal/log output.
        from .identity import DeviceError

        message = str(exc) if isinstance(exc, DeviceError) else type(exc).__name__
        print("DEVICE_ERROR: " + message, file=sys.stderr)
        return 2
