//! Byte transport to the existing MCP socket. No JSON parsing or task state.
#![forbid(unsafe_code)]

use rustix::{
    event::{PollFd, PollFlags, Timespec, poll},
    io::Errno,
    net::{self, AddressFamily, SocketAddrUnix, SocketFlags, SocketType},
};
use std::{
    io::{self, Read, Write},
    net::Shutdown,
    os::unix::net::UnixStream,
    path::Path,
    thread,
    time::{Duration, Instant},
};

fn connect(endpoint: &Path) -> io::Result<UnixStream> {
    let fd = net::socket_with(
        AddressFamily::UNIX,
        SocketType::STREAM,
        SocketFlags::CLOEXEC | SocketFlags::NONBLOCK,
        None,
    )?;
    let address = SocketAddrUnix::new(endpoint)?;
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        match net::connect(&fd, &address) {
            Ok(()) => break,
            Err(Errno::INTR) => continue,
            // A full AF_UNIX backlog has not accepted this connection. No bytes
            // have been sent; retry connection only within the original timeout.
            Err(Errno::AGAIN) => {
                if Instant::now() >= deadline {
                    return Err(io::ErrorKind::TimedOut.into());
                }
                thread::sleep(Duration::from_millis(10));
            }
            Err(Errno::INPROGRESS) => {
                let timeout =
                    Timespec::try_from(deadline.saturating_duration_since(Instant::now()))
                        .map_err(io::Error::other)?;
                if poll(&mut [PollFd::new(&fd, PollFlags::OUT)], Some(&timeout))? == 0 {
                    return Err(io::ErrorKind::TimedOut.into());
                }
                net::sockopt::socket_error(&fd)??;
                break;
            }
            Err(error) => return Err(error.into()),
        }
    }
    let stream = UnixStream::from(fd);
    stream.set_nonblocking(false)?;
    Ok(stream)
}

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
    let mut peer = connect(endpoint)?;
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
