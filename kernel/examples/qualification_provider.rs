//! Disposable protocol oracle for integration qualification only. Not a runtime
//! binary or release payload. It never opens a socket or business data file.
use serde_json::{Value, json};
use std::io::{BufRead, Read, Write};
fn main() {
    let mode = std::env::args().nth(1).expect("qualification mode");
    let mut input = std::io::BufReader::new(std::io::stdin().lock());
    let mut output = std::io::stdout().lock();
    if mode == "jsonl" {
        let mut line = String::new();
        input.read_line(&mut line).unwrap();
        let value: Value = serde_json::from_str(&line).unwrap();
        writeln!(output,"{}",json!({"value":value,"pid":std::process::id(),"sid":rustix::process::getsid(None).unwrap().as_raw_nonzero().get(),"pgid":rustix::process::getpgrp().as_raw_nonzero().get()})).unwrap();
        output.flush().unwrap();
        return;
    }
    assert_eq!(mode, "websocket");
    let mut header = vec![];
    while !header.ends_with(b"\r\n\r\n") {
        let mut b = [0];
        input.read_exact(&mut b).unwrap();
        header.push(b[0]);
        assert!(header.len() < 16384);
    }
    let header = String::from_utf8(header).unwrap();
    let key = header
        .lines()
        .find_map(|line| {
            line.split_once(':')
                .filter(|(name, _)| name.eq_ignore_ascii_case("Sec-WebSocket-Key"))
                .map(|(_, v)| v.trim())
        })
        .unwrap();
    use base64::{Engine, engine::general_purpose::STANDARD};
    use sha1::{Digest, Sha1};
    let accept = STANDARD.encode(Sha1::digest(
        format!("{key}258EAFA5-E914-47DA-95CA-C5AB0DC85B11").as_bytes(),
    ));
    write!(
        output,
        "HTTP/1.1 101 Switching Protocols\r\nSec-WebSocket-Accept: {accept}\r\n\r\n"
    )
    .unwrap();
    output.flush().unwrap();
    let mut h = [0; 2];
    input.read_exact(&mut h).unwrap();
    assert_eq!(h[0], 129);
    assert_eq!(h[1] & 128, 128);
    let size = match h[1] & 127 {
        126 => {
            let mut b = [0; 2];
            input.read_exact(&mut b).unwrap();
            u16::from_be_bytes(b) as u64
        }
        127 => {
            let mut b = [0; 8];
            input.read_exact(&mut b).unwrap();
            u64::from_be_bytes(b)
        }
        n => u64::from(n),
    };
    assert!(size < 1_048_576);
    let mut mask = [0; 4];
    input.read_exact(&mut mask).unwrap();
    let mut payload = vec![0; size as usize];
    input.read_exact(&mut payload).unwrap();
    for (i, b) in payload.iter_mut().enumerate() {
        *b ^= mask[i % 4];
    }
    let value: Value = serde_json::from_slice(&payload).unwrap();
    assert_eq!(value["method"], "thread/read");
    assert_eq!(
        value["params"],
        json!({"threadId":"exact-thread","includeTurns":false})
    );
    let reply = serde_json::to_vec(
        &json!({"id":value["id"],"result":{"thread":{"id":value["params"]["threadId"]}}}),
    )
    .unwrap();
    assert!(reply.len() < 126);
    output.write_all(&[129, reply.len() as u8]).unwrap();
    output.write_all(&reply).unwrap();
    output.flush().unwrap();
}
