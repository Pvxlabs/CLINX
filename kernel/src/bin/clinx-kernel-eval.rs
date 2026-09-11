#![forbid(unsafe_code)]

use clinx_decision_kernel::{
    AUTHORITY, MAX_FRAME_BYTES, MAX_REQUESTS_PER_PROCESS, PROTOCOL_VERSION, ProtocolResponse,
    execute_request, parse_protocol_request,
};
use std::io::{self, BufRead, Write};

fn write_response(response: &ProtocolResponse) -> io::Result<()> {
    let stdout = io::stdout();
    let mut output = stdout.lock();
    serde_json::to_writer(&mut output, response)?;
    output.write_all(b"\n")?;
    output.flush()
}

fn main() -> io::Result<()> {
    let stdin = io::stdin();
    let mut input = stdin.lock();
    let mut frame = Vec::new();
    for _ in 0..MAX_REQUESTS_PER_PROCESS {
        frame.clear();
        let read = input.read_until(b'\n', &mut frame)?;
        if read == 0 {
            return Ok(());
        }
        if frame.len() > MAX_FRAME_BYTES {
            write_response(&ProtocolResponse::failure(
                "UNKNOWN",
                "FRAME_TOO_LARGE",
                format!("request frame exceeds {MAX_FRAME_BYTES} bytes"),
            ))?;
            continue;
        }
        if frame.last() == Some(&b'\n') {
            frame.pop();
        }
        if frame.last() == Some(&b'\r') {
            frame.pop();
        }
        let response = match parse_protocol_request(&frame) {
            Ok(request) => execute_request(request),
            Err(error) => ProtocolResponse {
                request_id: "UNKNOWN".to_owned(),
                protocol_version: PROTOCOL_VERSION.to_owned(),
                ok: false,
                authority: AUTHORITY.to_owned(),
                result: None,
                error: Some(clinx_decision_kernel::ProtocolError {
                    code: "INVALID_JSON".to_owned(),
                    message: format!("request is not valid protocol JSON: {error}"),
                }),
            },
        };
        write_response(&response)?;
    }
    write_response(&ProtocolResponse::failure(
        "UNKNOWN",
        "REQUEST_LIMIT_REACHED",
        format!("process request limit is {MAX_REQUESTS_PER_PROCESS}"),
    ))
}
