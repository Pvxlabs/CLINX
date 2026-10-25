//! Independent Unix WebSocket oracle. Only temporary sockets and child probes.
#![allow(dead_code)]
use base64::{Engine, engine::general_purpose::STANDARD};
use serde_json::Value;
use sha1::{Digest, Sha1};
use std::{
    io::{Read, Write},
    os::unix::net::{UnixListener, UnixStream},
    path::{Path, PathBuf},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    thread::{self, JoinHandle},
    time::Duration,
};

fn handshake(peer: &mut UnixStream) -> std::io::Result<()> {
    let mut header = vec![];
    while !header.ends_with(b"\r\n\r\n") {
        let mut byte = [0];
        peer.read_exact(&mut byte)?;
        header.push(byte[0]);
        assert!(header.len() < 16384);
    }
    let header = String::from_utf8(header).unwrap();
    let key = header
        .lines()
        .find_map(|line| {
            line.split_once(':')
                .filter(|(name, _)| name.eq_ignore_ascii_case("Sec-WebSocket-Key"))
                .map(|(_, key)| key.trim())
        })
        .unwrap();
    let accept = STANDARD.encode(Sha1::digest(
        format!("{key}258EAFA5-E914-47DA-95CA-C5AB0DC85B11").as_bytes(),
    ));
    write!(
        peer,
        "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n"
    )?;
    peer.flush()
}
fn receive(peer: &mut UnixStream) -> std::io::Result<Value> {
    let mut header = [0; 2];
    peer.read_exact(&mut header)?;
    assert_eq!(header[0], 129);
    assert_eq!(header[1] & 128, 128);
    let length = match header[1] & 127 {
        126 => {
            let mut b = [0; 2];
            peer.read_exact(&mut b)?;
            u16::from_be_bytes(b) as usize
        }
        127 => {
            let mut b = [0; 8];
            peer.read_exact(&mut b)?;
            u64::from_be_bytes(b) as usize
        }
        n => n as usize,
    };
    assert!(length <= 16 * 1024 * 1024);
    let mut mask = [0; 4];
    peer.read_exact(&mut mask)?;
    let mut payload = vec![0; length];
    peer.read_exact(&mut payload)?;
    for (i, b) in payload.iter_mut().enumerate() {
        *b ^= mask[i % 4];
    }
    Ok(serde_json::from_slice(&payload).unwrap())
}
fn send(peer: &mut UnixStream, value: &Value) -> std::io::Result<()> {
    let payload = serde_json::to_vec(value).unwrap();
    if payload.len() < 126 {
        peer.write_all(&[129, payload.len() as u8])?;
    } else if payload.len() <= u16::MAX as usize {
        peer.write_all(&[129, 126])?;
        peer.write_all(&(payload.len() as u16).to_be_bytes())?;
    } else {
        peer.write_all(&[129, 127])?;
        peer.write_all(&(payload.len() as u64).to_be_bytes())?;
    }
    peer.write_all(&payload)?;
    peer.flush()
}
pub struct Provider {
    pub path: PathBuf,
    pub calls: Arc<Mutex<Vec<Value>>>,
    stop: Arc<AtomicBool>,
    peers: Arc<Mutex<Vec<UnixStream>>>,
    thread: Option<JoinHandle<()>>,
}
impl Provider {
    pub fn new(
        path: &Path,
        reply: impl Fn(Value) -> Option<Vec<Value>> + Send + Sync + 'static,
    ) -> Self {
        let listener = UnixListener::bind(path).unwrap();
        listener.set_nonblocking(true).unwrap();
        let calls = Arc::new(Mutex::new(vec![]));
        let stop = Arc::new(AtomicBool::new(false));
        let peers = Arc::new(Mutex::new(vec![]));
        let (calls2, stop2, peers2) = (calls.clone(), stop.clone(), peers.clone());
        let reply = Arc::new(reply);
        let thread = thread::spawn(move || {
            let mut workers = vec![];
            while !stop2.load(Ordering::Relaxed) {
                match listener.accept() {
                    Ok((mut peer, _)) => {
                        peer.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
                        peer.set_write_timeout(Some(Duration::from_secs(5)))
                            .unwrap();
                        peers2.lock().unwrap().push(peer.try_clone().unwrap());
                        let (calls, reply) = (calls2.clone(), reply.clone());
                        workers.push(thread::spawn(move || {
                            if handshake(&mut peer).is_err() {
                                return;
                            }
                            while let Ok(message) = receive(&mut peer) {
                                calls.lock().unwrap().push(message.clone());
                                let Some(replies) = reply(message) else {
                                    let _ = peer.shutdown(std::net::Shutdown::Both);
                                    break;
                                };
                                for value in replies {
                                    if send(&mut peer, &value).is_err() {
                                        return;
                                    }
                                }
                            }
                            let _ = peer.shutdown(std::net::Shutdown::Both);
                        }));
                    }
                    Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                        thread::sleep(Duration::from_millis(1))
                    }
                    Err(e) => panic!("fixture accept: {e}"),
                }
            }
            for peer in peers2.lock().unwrap().iter() {
                let _ = peer.shutdown(std::net::Shutdown::Both);
            }
            for worker in workers {
                worker.join().unwrap();
            }
        });
        Self {
            path: path.to_owned(),
            calls,
            stop,
            peers,
            thread: Some(thread),
        }
    }
    pub fn calls(&self) -> Vec<Value> {
        self.calls.lock().unwrap().clone()
    }
    pub fn close(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        for peer in self.peers.lock().unwrap().iter() {
            let _ = peer.shutdown(std::net::Shutdown::Both);
        }
        if let Some(thread) = self.thread.take() {
            thread.join().unwrap();
        }
    }
}
impl Drop for Provider {
    fn drop(&mut self) {
        self.close();
    }
}
