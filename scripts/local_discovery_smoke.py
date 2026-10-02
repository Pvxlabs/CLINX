"""Opt-in real P620 two-process mDNS / OPAQUE / mTLS qualification.

No shared daemon, no remote execution API. PIN travels only through private
multiprocessing pipes in RAM; neither command lines nor evidence contain it.
Run from the repository: python3 scripts/local_discovery_smoke.py
"""

from __future__ import annotations

import json
import multiprocessing as mp
import secrets
import ssl
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local_discovery.discovery import DiscoveryIndex, LanDiscovery
from local_discovery.identity import DeviceError, NodeIdentity, PrivateStore, TrustedPeerStore
from local_discovery.transport import DeviceServer, LanTransport


def node(pipe, root, name):
    try:
        identity = NodeIdentity(PrivateStore(Path(root)), name)
        peers = TrustedPeerStore(identity.store)
        index = DiscoveryIndex()
        with DeviceServer(identity, peers) as server, LanDiscovery(index) as discovery:
            discovery.advertise(identity, server.port)
            transport = LanTransport(identity, peers)
            pipe.send({"ready": True, "port": server.port})
            while True:
                command = pipe.recv()
                if command["op"] == "stop":
                    return
                if command["op"] == "open":
                    pipe.send(server.window.open())
                    continue
                peer_id = command["peer"]
                deadline = time.monotonic() + 12
                while peer_id not in index.candidates() and time.monotonic() < deadline:
                    time.sleep(0.1)
                candidate = index.candidates()[peer_id]
                if command["op"] == "pair":
                    session = transport.pair(candidate, command.pop("code"))
                elif command["op"] == "wrong_server":
                    # Preserve the legitimate advertised fingerprint, redirect
                    # only the endpoint to a real server with a different key.
                    candidate = replace(
                        candidate,
                        endpoints=((candidate.endpoints[0][0], command["port"]),),
                    )
                    try:
                        transport.reconnect(candidate)
                    except (DeviceError, ssl.SSLError):
                        pipe.send({"wrong_identity_rejected": True})
                        continue
                    raise AssertionError("wrong TLS identity accepted")
                else:
                    session = transport.reconnect(candidate)
                pipe.send(
                    {
                        "authenticated": True,
                        "capabilities": list(session.capabilities),
                        "authority_granted": session.authority_granted,
                        "security_status": peers.all()[peer_id]["security_status"],
                    }
                )
    except Exception as exc:
        # Fixed exception type only: no repr, arguments, traceback or PIN.
        pipe.send({"error_type": type(exc).__name__})
    finally:
        pipe.close()


def start(ctx, root, name):
    parent, child = ctx.Pipe()
    process = ctx.Process(target=node, args=(child, str(root), name))
    process.start()
    child.close()
    result = receive(parent)
    assert result.get("ready"), result
    return process, parent, result["port"]


def receive(pipe):
    if not pipe.poll(30):
        raise RuntimeError("SMOKE_TIMEOUT")
    result = pipe.recv()
    if isinstance(result, dict) and "error_type" in result:
        raise RuntimeError("SMOKE_CHILD_FAILED_" + result["error_type"])
    return result


def stop(process, pipe, port):
    del port
    try:
        if process.is_alive():
            pipe.send({"op": "stop"})
        process.join(10)
    finally:
        if process.is_alive():
            process.terminate()  # Only this test's own temporary child.
            process.join(5)
        pipe.close()
    assert process.exitcode == 0


def main():
    ctx = mp.get_context("spawn")
    suffix = secrets.token_hex(4)
    alpha, beta = "clinx-prod-a-" + suffix, "clinx-prod-b-" + suffix
    with tempfile.TemporaryDirectory(prefix="clinx-pake-") as temporary:
        root = Path(temporary)
        children = []
        try:
            a = start(ctx, root / "a", alpha)
            children.append(a)
            b = start(ctx, root / "b", beta)
            children.append(b)
            b[1].send({"op": "open"})
            code = receive(b[1])
            a[1].send({"op": "pair", "peer": beta, "code": code})
            code = None
            paired = receive(a[1])
            assert paired == dict(
                authenticated=True,
                capabilities=[],
                authority_granted=False,
                security_status="OPAQUE_V1",
            )
            print("P620_REAL_PAIRING=PASS（两个独立临时进程，真实组播与 OPAQUE）", flush=True)
            for child in children:
                stop(*child)
            children.clear()
            # Both processes are fresh and reload only stable identity/trust.
            # No pairing window is opened and no PIN exists in these processes.
            a = start(ctx, root / "a", alpha)
            children.append(a)
            b = start(ctx, root / "b", beta)
            children.append(b)
            for local, remote in [(a, beta), (b, alpha)]:
                local[1].send({"op": "reconnect", "peer": remote})
                assert receive(local[1]) == paired
            print("P620_REAL_RECONNECT=PASS（双进程重启、重新发现、双向 mTLS、无 PIN）", flush=True)
            imposter = start(ctx, root / "imposter", "clinx-imposter-" + suffix)
            children.append(imposter)
            a[1].send({"op": "wrong_server", "peer": beta, "port": imposter[2]})
            assert receive(a[1]) == {"wrong_identity_rejected": True}
            print("P620_WRONG_IDENTITY=PASS（实际错误 TLS 公钥被拒绝）", flush=True)
            print(
                json.dumps(
                    {
                        "AUTHORIZATION_ISOLATION": "PASS",
                        "REAL_DEVICE_VALIDATION": "未验证；这是 P620 同机双进程",
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        finally:
            for child in children:
                stop(*child)


if __name__ == "__main__":
    main()
