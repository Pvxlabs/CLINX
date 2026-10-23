#![forbid(unsafe_code)]
use clinx_decision_kernel::{
    MAX_FRAME_BYTES, parse_protocol_request,
    runtime_lab::{Journal, PROTOCOL},
};
use serde_json::json;
use std::{
    io::{self, BufRead, Write},
    path::Path,
};
fn main() -> Result<(), String> {
    let args: Vec<_> = std::env::args().collect();
    if args.len() < 3
        || args.len() > 4
        || args[1] != "--lab-dir"
        || args.len() == 4 && args[3] != "--init"
    {
        return Err("usage: clinx-runtime-lab --lab-dir NEW_ISOLATED_DIRECTORY [--init]".into());
    }
    let mut journal = Journal::open(Path::new(&args[2]), args.len() == 4)?;
    let stdin = io::stdin();
    let mut input = stdin.lock();
    let stdout = io::stdout();
    let mut output = stdout.lock();
    for _ in 0..4096 {
        let mut frame = Vec::new();
        // take() bounds memory even for a pipe that never sends a newline.
        let n = std::io::Read::take(&mut input, (MAX_FRAME_BYTES + 1) as u64)
            .read_until(b'\n', &mut frame)
            .map_err(|e| e.to_string())?;
        if n == 0 {
            return Ok(());
        }
        let mut id = "UNKNOWN".to_owned();
        let result = if n > MAX_FRAME_BYTES || frame.last() != Some(&b'\n') {
            Err("REJECT".into())
        } else {
            match parse_protocol_request(&frame) {
                Ok(req) => {
                    id = req.request_id.clone();
                    journal.execute(&req)
                }
                Err(_) => Err("REJECT".into()),
            }
        };
        let response = match result {
            Ok(result) => {
                json!({"protocol_version":PROTOCOL,"contract_family":"CLINX_EXECUTION_CORE_V1","contract_version":"1.0.0","authority":"NON_AUTHORITATIVE","request_id":id,"result":result})
            }
            Err(v) => {
                json!({"protocol_version":PROTOCOL,"contract_family":"CLINX_EXECUTION_CORE_V1","contract_version":"1.0.0","authority":"NON_AUTHORITATIVE","request_id":id,"result":{"verdict":v}})
            }
        };
        serde_json::to_writer(&mut output, &response).map_err(|e| e.to_string())?;
        output
            .write_all(b"\n")
            .and_then(|_| output.flush())
            .map_err(|e| e.to_string())?;
        if n > MAX_FRAME_BYTES {
            return Ok(());
        }
    }
    Err("request limit".into())
}
