#!/usr/bin/env python3
"""CLINX centre registration service and explicit owner control commands."""

from __future__ import annotations

import argparse
import getpass
import json
import signal
import threading
from pathlib import Path

from local_discovery.discovery import Candidate
from local_discovery.identity import NodeIdentity, PrivateStore, TrustedPeerStore
from local_discovery.transport import DeviceServer, LanTransport
from node_protocol import NodeAuthorizationStore, NodeRegistry
from node_runtime import CentreService, PairedTLS, endpoint_address


def parser():
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--state", type=Path, required=True)
    value.add_argument("--node-id", required=True)
    commands = value.add_subparsers(dest="command", required=True)
    commands.add_parser("bootstrap")
    serve = commands.add_parser("serve")
    serve.add_argument("--registry", type=Path, required=True)
    serve.add_argument("--bind", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8771)
    serve.add_argument("--stale-after", type=int, default=45)
    share = commands.add_parser("share")
    share.add_argument("--peer", required=True)
    share.add_argument("--user-scope", required=True)
    share.add_argument("--read-sessions", action="store_true")
    share.add_argument("--execute-tasks", action="store_true")
    share.add_argument("--revoke", action="store_true")
    share.add_argument("--approved", action="store_true", required=True)
    pair = commands.add_parser("pair-listen")
    pair.add_argument("--bind", default="127.0.0.1")
    pair.add_argument("--port", type=int, default=8770)
    pair.add_argument("--seconds", type=int, default=120)
    connect = commands.add_parser("pair-connect")
    connect.add_argument("--peer", required=True)
    connect.add_argument("--fingerprint", required=True)
    connect.add_argument("--endpoint", required=True)
    configure = commands.add_parser("configure-node")
    configure.add_argument("--centre", required=True)
    configure.add_argument("--endpoint", required=True)
    configure.add_argument("--bind", default="0.0.0.0")
    configure.add_argument("--port", type=int, default=8772)
    configure.add_argument("--execution-config", type=Path)
    return value


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    private = PrivateStore(args.state)
    if args.command != "bootstrap" and private.read("identity.json") is None:
        raise ValueError("IDENTITY_BOOTSTRAP_REQUIRED")
    identity = NodeIdentity(private, args.node_id)
    peers = TrustedPeerStore(private)
    if args.command == "bootstrap":
        print(json.dumps(identity.public))
        return 0
    if args.command == "share":
        PairedTLS(identity, peers).peer(args.peer)
        approvals = NodeAuthorizationStore(args.state)
        scope = approvals.revoke(args.peer, user_scope=args.user_scope) if args.revoke else approvals.grant(
            args.peer, user_scope=args.user_scope, read_sessions=args.read_sessions, execute_tasks=args.execute_tasks)
        print(json.dumps({"peer_id": args.peer, "scope": scope.as_dict()}))
        return 0
    if args.command == "configure-node":
        PairedTLS(identity, peers).peer(args.centre)
        endpoint_address(args.endpoint)
        with private.lock():
            private.write("node-config.json", dict(schema_version=1, centre_id=args.centre,
                centre_endpoint=args.endpoint, bind_address=args.bind, port=args.port,
                execution_config=str(args.execution_config.resolve()) if args.execution_config else None))
        print(json.dumps({"configured": True, "centre_id": args.centre}))
        return 0
    if args.command == "pair-connect":
        address = endpoint_address(args.endpoint)
        candidate = Candidate(args.peer, args.peer, args.fingerprint, (address,))
        session = LanTransport(identity, peers).pair(candidate, getpass.getpass("Pairing code: "))
        print(json.dumps({"paired": True, "node_id": session.node_id, "authority_granted": False}))
        return 0
    if args.command == "pair-listen":
        with DeviceServer(identity, peers, address=args.bind, port=args.port) as server:
            code = server.window.open()
            print(json.dumps({"identity": identity.public, "port": server.port, "pairing_code": code}), flush=True)
            threading.Event().wait(min(120, max(1, args.seconds)))
        return 0
    registry = NodeRegistry(args.registry)
    service = CentreService(PairedTLS(identity, peers), registry, NodeAuthorizationStore(args.state), stale_after=args.stale_after)
    server = service.server(address=args.bind, port=args.port)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    print(json.dumps({"ready": True, "address": server.address, "registry": str(args.registry)}), flush=True)
    try:
        while not stop.wait(1):
            registry.refresh_states()
    finally:
        server.close()
        registry.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
