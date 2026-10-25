//! Original byte stream and half-close cases from fixtures/test_mcp_relay.py.
//! These exercise the actual binary; no protocol object or Python shim is used.
#![cfg(unix)]

use std::{
    io::{Read, Write},
    os::unix::net::{UnixListener, UnixStream},
    path::Path,
    process::{Child, Command, ExitStatus, Stdio},
    sync::mpsc,
    thread,
    time::{Duration, Instant},
};

struct Relay(Child);
impl Relay {
    fn start(endpoint: &Path) -> Self {
        Self(
            Command::new(env!("CARGO_BIN_EXE_clinx-mcp-relay"))
                .arg("--socket")
                .arg(endpoint)
                .stdin(Stdio::piped())
                .stdout(Stdio::piped())
                .stderr(Stdio::piped())
                .spawn()
                .unwrap(),
        )
    }
    fn wait(&mut self, timeout: Duration) -> ExitStatus {
        let deadline = Instant::now() + timeout;
        loop {
            if let Some(status) = self.0.try_wait().unwrap() {
                return status;
            }
            assert!(
                Instant::now() < deadline,
                "relay did not exit before deadline"
            );
            thread::sleep(Duration::from_millis(5));
        }
    }
}
impl Drop for Relay {
    fn drop(&mut self) {
        if self.0.try_wait().unwrap().is_none() {
            let _ = self.0.kill();
        }
        let _ = self.0.wait();
    }
}
fn accept(listener: &UnixListener) -> UnixStream {
    listener.set_nonblocking(true).unwrap();
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        match listener.accept() {
            Ok((peer, _)) => {
                peer.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
                peer.set_write_timeout(Some(Duration::from_secs(5)))
                    .unwrap();
                return peer;
            }
            Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                assert!(Instant::now() < deadline, "relay did not connect");
                thread::sleep(Duration::from_millis(5));
            }
            Err(e) => panic!("accept failed: {e}"),
        }
    }
}

#[test]
fn test_large_binary_stream_and_reply_after_stdin_eof() {
    let tmp = tempfile::tempdir().unwrap();
    let endpoint = tmp.path().join("relay.sock");
    let listener = UnixListener::bind(&endpoint).unwrap();
    let mut sent: Vec<u8> = (0..8192).flat_map(|_| 0..=255u8).collect();
    sent.extend_from_slice(b"\x00no-newline");
    let reply: Vec<u8> = sent.iter().rev().copied().collect();
    let expected_input = sent.clone();
    let expected_reply = reply.clone();
    let worker = thread::spawn(move || {
        let mut peer = accept(&listener);
        let mut received = Vec::new();
        peer.read_to_end(&mut received).unwrap();
        assert_eq!(received, expected_input);
        peer.write_all(&expected_reply).unwrap();
    });
    let mut child = Relay::start(&endpoint);
    let mut input = child.0.stdin.take().unwrap();
    let writer = thread::spawn(move || {
        input.write_all(&sent).unwrap();
        drop(input);
    });
    let mut output = child.0.stdout.take().unwrap();
    let (tx, rx) = mpsc::channel();
    let reader = thread::spawn(move || {
        let mut received = Vec::new();
        output.read_to_end(&mut received).unwrap();
        tx.send(received).unwrap();
    });
    assert_eq!(rx.recv_timeout(Duration::from_secs(10)).unwrap(), reply);
    assert!(child.wait(Duration::from_secs(3)).success());
    let mut errors = Vec::new();
    child
        .0
        .stderr
        .take()
        .unwrap()
        .read_to_end(&mut errors)
        .unwrap();
    assert!(errors.is_empty());
    // Joining also checks the Python worker's original not-alive/no-errors assertions.
    worker.join().unwrap();
    writer.join().unwrap();
    reader.join().unwrap();
}

#[test]
fn test_remote_eof_exits_while_parent_keeps_stdin_open() {
    let tmp = tempfile::tempdir().unwrap();
    let endpoint = tmp.path().join("relay.sock");
    let listener = UnixListener::bind(&endpoint).unwrap();
    let mut child = Relay::start(&endpoint);
    let mut peer = accept(&listener);
    peer.write_all(b"final bytes").unwrap();
    drop(peer);
    assert!(child.0.stdin.is_some(), "parent must still own open stdin");
    assert!(child.wait(Duration::from_secs(3)).success());
    let mut output = Vec::new();
    child
        .0
        .stdout
        .take()
        .unwrap()
        .read_to_end(&mut output)
        .unwrap();
    assert_eq!(output, b"final bytes");
}

#[test]
fn test_unavailable_peer_fails_without_reply_or_replay() {
    let tmp = tempfile::tempdir().unwrap();
    let mut child = Relay::start(&tmp.path().join("missing.sock"));
    let _ = child.0.stdin.take().unwrap().write_all(b"never delivered");
    assert!(!child.wait(Duration::from_secs(3)).success());
    let mut output = Vec::new();
    child
        .0
        .stdout
        .take()
        .unwrap()
        .read_to_end(&mut output)
        .unwrap();
    assert!(output.is_empty());
}

#[test]
fn test_partial_response_is_visible_before_newline_or_eof() {
    let tmp = tempfile::tempdir().unwrap();
    let endpoint = tmp.path().join("stream.sock");
    let listener = UnixListener::bind(&endpoint).unwrap();
    let mut child = Relay::start(&endpoint);
    let mut peer = accept(&listener);
    peer.set_read_timeout(Some(Duration::from_secs(3))).unwrap();
    child.0.stdin.as_mut().unwrap().write_all(b"input").unwrap();
    child.0.stdin.as_mut().unwrap().flush().unwrap();
    let mut input = [0; 5];
    peer.read_exact(&mut input).unwrap();
    assert_eq!(&input, b"input");
    peer.write_all(b"partial").unwrap();
    let mut output = child.0.stdout.take().unwrap();
    let (tx, rx) = mpsc::channel();
    let reader = thread::spawn(move || {
        let mut bytes = [0; 7];
        output.read_exact(&mut bytes).unwrap();
        tx.send(bytes).unwrap();
    });
    // The peer and the parent's stdin remain open throughout this deadline.
    assert_eq!(
        &rx.recv_timeout(Duration::from_secs(2)).unwrap(),
        b"partial"
    );
    drop(child.0.stdin.take());
    assert_eq!(peer.read(&mut [0; 1]).unwrap(), 0);
    drop(peer);
    assert!(child.wait(Duration::from_secs(3)).success());
    reader.join().unwrap();
}
