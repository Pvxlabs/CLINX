"""Byte transport from the existing tunnel's stdio to the dedicated MCP UID."""
import os
import select
import socket


def main():
    with socket.socket(socket.AF_UNIX) as peer:
        peer.settimeout(10)
        peer.connect('/run/clinx-rust-api/mcp.sock')
        peer.settimeout(None)
        inputs = [0, peer]
        while peer in inputs:
            ready, _, _ = select.select(inputs, [], [])
            for source in ready:
                if source == 0:
                    data = os.read(0, 65536)
                    if not data:
                        inputs.remove(0)
                        peer.shutdown(socket.SHUT_WR)
                    else:
                        peer.sendall(data)
                else:
                    data = peer.recv(65536)
                    if not data:
                        return
                    while data:
                        data = data[os.write(1, data):]


if __name__ == '__main__':
    main()
