"""The Rust relay preserves the existing byte stream, half-close and exit rules."""
import os
from pathlib import Path
import socket
import select
import subprocess
import threading

import pytest


@pytest.fixture
def binary():
    path = Path(os.environ['CLINX_MCP_RELAY'])
    assert path.is_file() and os.access(path, os.X_OK)
    return path


def test_large_binary_stream_and_reply_after_stdin_eof(binary, tmp_path):
    endpoint = tmp_path / 'relay.sock'
    sent = bytes(range(256)) * 8192 + b'\x00no-newline'
    reply = sent[::-1]
    errors = []
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(endpoint)); listener.listen(1); listener.settimeout(5)

        def serve():
            try:
                peer, _ = listener.accept()
                with peer:
                    peer.settimeout(5)
                    chunks = []
                    while chunk := peer.recv(65536):
                        chunks.append(chunk)
                    assert b''.join(chunks) == sent
                    peer.sendall(reply)
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=serve)
        worker.start()
        result = subprocess.run([str(binary), '--socket', str(endpoint)], input=sent,
                                capture_output=True, timeout=10)
        worker.join(timeout=6)
        assert not worker.is_alive() and not errors
        assert result.returncode == 0 and result.stdout == reply and not result.stderr


def test_remote_eof_exits_while_parent_keeps_stdin_open(binary, tmp_path):
    endpoint = tmp_path / 'relay.sock'
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(endpoint)); listener.listen(1); listener.settimeout(5)
        child = subprocess.Popen([str(binary), '--socket', str(endpoint)], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            peer, _ = listener.accept()
            peer.sendall(b'final bytes'); peer.close()
            assert child.wait(timeout=3) == 0
            assert child.stdout.read() == b'final bytes'
        finally:
            if child.poll() is None:
                child.kill(); child.wait()
            child.stdin.close(); child.stdout.close(); child.stderr.close()


def test_unavailable_peer_fails_without_reply_or_replay(binary, tmp_path):
    result = subprocess.run([str(binary), '--socket', str(tmp_path / 'missing.sock')],
                            input=b'never delivered', capture_output=True, timeout=3)
    assert result.returncode != 0 and result.stdout == b''


def test_partial_response_is_visible_before_newline_or_eof(binary, tmp_path):
    endpoint = tmp_path / 'stream.sock'
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(endpoint)); listener.listen(1); listener.settimeout(5)
        child = subprocess.Popen([str(binary), '--socket', str(endpoint)], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            peer, _ = listener.accept()
            with peer:
                peer.settimeout(3)
                child.stdin.write(b'input'); child.stdin.flush()
                assert peer.recv(5) == b'input'
                peer.sendall(b'partial')
                assert select.select([child.stdout], [], [], 2)[0]
                assert os.read(child.stdout.fileno(), 7) == b'partial'
                child.stdin.close()
                assert peer.recv(1) == b''
            assert child.wait(timeout=3) == 0
        finally:
            if child.poll() is None:
                child.kill(); child.wait()
            child.stdin.close(); child.stdout.close(); child.stderr.close()
