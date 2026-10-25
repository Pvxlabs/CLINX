"""Compatibility executable; Swift launches the Rust helper directly."""
from pathlib import Path
import os
import sys

# Only for historical direct script callers; no discovery/pairing state here.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def main():
    from clinx.authority.request_pipe import binary
    path = str(binary())
    os.execv(path, [path, "--discovery-gui"])


if __name__ == "__main__":
    main()
