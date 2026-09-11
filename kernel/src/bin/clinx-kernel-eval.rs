#![forbid(unsafe_code)]

use clinx_decision_kernel::{
    AUTHORITY, MAX_FRAME_BYTES, MAX_REQUESTS_PER_PROCESS, PROTOCOL_VERSION, ProtocolResponse,
    execute_request, parse_protocol_request,
};
use std::io::{self, BufRead, Write};

enum FrameRead {
    Eof,
    Complete,
    TooLarge,
}

fn read_bounded_frame<R: BufRead>(
    reader: &mut R,
    frame: &mut Vec<u8>,
    max: usize,
) -> io::Result<FrameRead> {
    frame.clear();
    loop {
        let available = reader.fill_buf()?;
        if available.is_empty() {
            return Ok(if frame.is_empty() {
                FrameRead::Eof
            } else {
                FrameRead::Complete
            });
        }
        let newline = available.iter().position(|byte| *byte == b'\n');
        let content_len = newline.unwrap_or(available.len());
        let total_len = frame.len().saturating_add(content_len);
        if newline.is_some() && total_len.saturating_add(1) > max {
            reader.consume(content_len + 1);
            return Ok(FrameRead::TooLarge);
        }
        if newline.is_none() && total_len > max {
            let available_len = available.len();
            reader.consume(available_len);
            return Ok(FrameRead::TooLarge);
        }
        frame.extend_from_slice(&available[..content_len]);
        reader.consume(content_len + usize::from(newline.is_some()));
        if newline.is_some() {
            return Ok(FrameRead::Complete);
        }
    }
}

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
    let mut frame = Vec::with_capacity(MAX_FRAME_BYTES);
    for _ in 0..MAX_REQUESTS_PER_PROCESS {
        frame.clear();
        match read_bounded_frame(&mut input, &mut frame, MAX_FRAME_BYTES)? {
            FrameRead::Eof => return Ok(()),
            FrameRead::TooLarge => {
                write_response(&ProtocolResponse::failure(
                    "UNKNOWN",
                    "FRAME_TOO_LARGE",
                    format!("request frame exceeds {MAX_FRAME_BYTES} bytes"),
                ))?;
                return Ok(());
            }
            FrameRead::Complete => {}
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

#[cfg(test)]
mod tests {
    use super::{FrameRead, read_bounded_frame};
    use std::io::Cursor;

    #[test]
    fn accepts_delimiter_at_inclusive_boundary() {
        let mut frame = Vec::new();
        let mut input = Cursor::new(b"abc\n".to_vec());
        assert!(matches!(
            read_bounded_frame(&mut input, &mut frame, 4),
            Ok(FrameRead::Complete)
        ));
        assert_eq!(frame, b"abc");
    }

    #[test]
    fn rejects_content_beyond_boundary_without_growing_frame() {
        let mut frame = Vec::new();
        let mut input = Cursor::new(b"abcd\nmore".to_vec());
        assert!(matches!(
            read_bounded_frame(&mut input, &mut frame, 4),
            Ok(FrameRead::TooLarge)
        ));
        assert!(frame.len() <= 4);
    }

    #[test]
    fn eof_returns_partial_frame_without_unbounded_read() {
        let mut frame = Vec::new();
        let mut input = Cursor::new(b"abc".to_vec());
        assert!(matches!(
            read_bounded_frame(&mut input, &mut frame, 4),
            Ok(FrameRead::Complete)
        ));
        assert_eq!(frame, b"abc");
    }

    #[test]
    fn preserves_the_next_frame_after_a_delimiter() {
        let mut frame = Vec::new();
        let mut input = Cursor::new(b"a\nb\n".to_vec());
        assert!(matches!(
            read_bounded_frame(&mut input, &mut frame, 2),
            Ok(FrameRead::Complete)
        ));
        assert_eq!(frame, b"a");
        assert!(matches!(
            read_bounded_frame(&mut input, &mut frame, 2),
            Ok(FrameRead::Complete)
        ));
        assert_eq!(frame, b"b");
    }
}
