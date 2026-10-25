//! Byte transport to the existing MCP socket. No JSON parsing or task state.
#![forbid(unsafe_code)]

use std::{
    io::{self, Read, Write},
    net::Shutdown,
    path::Path,
    thread,
    time::Duration,
};

fn main() -> io::Result<()> {
    let args: Vec<_> = std::env::args_os().skip(1).collect();
    let endpoint = match args.as_slice() {
        [] => Path::new("/run/clinx-rust-api/mcp.sock"),
        [flag, path] if flag == "--socket" && Path::new(path).is_absolute() => Path::new(path),
        _ => {
            return Err(io::Error::other(
                "usage: clinx-mcp-relay [--socket ABSOLUTE_PATH]",
            ));
        }
    };
    let mut peer =
        clinx_decision_kernel::native_transport::connect_local(endpoint, Duration::from_secs(10))?;
    let mut writer = peer.try_clone()?;
    // stdin EOF is a half-close: replies remain readable. A remote close ends
    // the process even if the parent keeps stdin open; never wait for that thread.
    thread::spawn(move || {
        let copied = io::copy(&mut io::stdin().lock(), &mut writer);
        let _ = writer.shutdown(if copied.is_ok() {
            Shutdown::Write
        } else {
            Shutdown::Both
        });
    });
    let mut output = io::stdout().lock();
    let mut buffer = [0; 65536];
    loop {
        let count = match peer.read(&mut buffer) {
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            result => result?,
        };
        if count == 0 {
            return Ok(());
        }
        output.write_all(&buffer[..count])?;
        // A byte relay cannot wait for a newline or a socket close to expose a
        // partial response to its parent.
        output.flush()?;
    }
}
