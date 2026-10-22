#!/usr/bin/env python3
"""Read-only tunnel health gate: exact systemd PID and recent successful poll."""

import argparse
import json
import math
import re
import subprocess
import time
import urllib.request


def evaluate(metrics, *, now, max_age):
    pattern = r"^commands_poll_last_successful_timestamp_seconds(?:\{[^}]*\})?\s+(\S+)$"
    try:
        values = [
            float(m.group(1))
            for line in metrics.splitlines()
            if (m := re.fullmatch(pattern, line))
        ]
    except ValueError:
        return False
    return (
        len(values) == 1
        and math.isfinite(values[0])
        and values[0] > 0
        and 0 <= now - values[0] <= max_age
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url-file", required=True)
    parser.add_argument("--max-age", type=float, default=120)
    args = parser.parse_args()
    state = subprocess.run(
        [
            "systemctl",
            "--user",
            "show",
            "clinx-tunnel.service",
            "--property=ActiveState",
            "--property=MainPID",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    fields = dict(line.split("=", 1) for line in state.splitlines())
    if fields.get("ActiveState") != "active" or int(fields.get("MainPID", "0")) <= 0:
        raise RuntimeError("TUNNEL_NOT_ACTIVE")
    probe = subprocess.run(
        [
            "/home/pvxlabs/.local/bin/tunnel-client",
            "health",
            "--url-file",
            args.url_file,
            "--pid",
            fields["MainPID"],
            "--require-control-plane-poll",
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    report = json.loads(probe.stdout)
    with urllib.request.urlopen(report["base_url"] + "/metrics", timeout=5) as response:
        metrics = response.read(1048576).decode()
    if not evaluate(metrics, now=time.time(), max_age=args.max_age):
        raise RuntimeError("TUNNEL_CONTROL_POLL_STALE")
    print(
        json.dumps(
            dict(
                status="PASS",
                pid=int(fields["MainPID"]),
                poll_fresh=True,
                action="READ_ONLY",
            )
        )
    )


if __name__ == "__main__":
    main()
