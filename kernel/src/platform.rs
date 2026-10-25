//! Small OS adapters shared by the native Mac helpers and Linux runtime.
//! They do not change peer authentication or task ownership.
use rustix::{
    fd::OwnedFd,
    io,
    net::{AddressFamily, Protocol, SocketType},
};

/// Darwin inherits O_NONBLOCK from the listening socket on accept. Our bounded
/// TLS workers use blocking IO with socket deadlines on every platform.
pub(crate) fn blocking_tcp(stream: std::net::TcpStream) -> std::io::Result<std::net::TcpStream> {
    stream.set_nonblocking(false)?;
    Ok(stream)
}

/// Return a close-on-exec socket, with nonblocking mode selected before use.
/// Linux keeps atomic socket flags. Darwin does not offer those socket flags;
/// set FD/status flags before bind, connect, publication, or returning the fd.
pub(crate) fn socket(
    family: AddressFamily,
    kind: SocketType,
    protocol: Option<Protocol>,
    nonblocking: bool,
) -> io::Result<OwnedFd> {
    #[cfg(not(target_vendor = "apple"))]
    {
        use rustix::net::SocketFlags;
        let flags = SocketFlags::CLOEXEC
            | if nonblocking {
                SocketFlags::NONBLOCK
            } else {
                SocketFlags::empty()
            };
        rustix::net::socket_with(family, kind, flags, protocol)
    }
    #[cfg(target_vendor = "apple")]
    {
        let fd = rustix::net::socket(family, kind, protocol)?;
        io::fcntl_setfd(&fd, io::FdFlags::CLOEXEC)?;
        if nonblocking {
            let flags = rustix::fs::fcntl_getfl(&fd)?;
            rustix::fs::fcntl_setfl(&fd, flags | rustix::fs::OFlags::NONBLOCK)?;
        }
        Ok(fd)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn accepted_worker_waits_for_delayed_peer_instead_of_would_block() {
        use std::{
            io::{Read, Write},
            net::{TcpListener, TcpStream},
            time::Duration,
        };
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let address = listener.local_addr().unwrap();
        let (ready, start) = std::sync::mpsc::channel();
        let peer = std::thread::spawn(move || {
            let mut stream = TcpStream::connect(address).unwrap();
            start.recv().unwrap();
            std::thread::sleep(Duration::from_millis(50));
            stream.write_all(b"delayed").unwrap();
        });
        let (accepted, _) = loop {
            match listener.accept() {
                Ok(pair) => break pair,
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                    std::thread::sleep(Duration::from_millis(1))
                }
                Err(e) => panic!("{e}"),
            }
        };
        // Exercise Darwin's inherited state deterministically on Linux too.
        accepted.set_nonblocking(true).unwrap();
        let mut accepted = blocking_tcp(accepted).unwrap();
        accepted
            .set_read_timeout(Some(Duration::from_secs(2)))
            .unwrap();
        ready.send(()).unwrap();
        let mut bytes = [0; 7];
        accepted.read_exact(&mut bytes).unwrap();
        assert_eq!(&bytes, b"delayed");
        peer.join().unwrap();
    }
    #[test]
    fn sockets_preserve_close_on_exec_and_requested_blocking_mode() {
        for nonblocking in [false, true] {
            for (family, kind, protocol) in [
                (AddressFamily::UNIX, SocketType::STREAM, None),
                (
                    AddressFamily::INET,
                    SocketType::STREAM,
                    Some(rustix::net::ipproto::TCP),
                ),
                (
                    AddressFamily::INET,
                    SocketType::DGRAM,
                    Some(rustix::net::ipproto::UDP),
                ),
            ] {
                let fd = socket(family, kind, protocol, nonblocking).unwrap();
                assert!(io::fcntl_getfd(&fd).unwrap().contains(io::FdFlags::CLOEXEC));
                assert_eq!(
                    rustix::fs::fcntl_getfl(&fd)
                        .unwrap()
                        .contains(rustix::fs::OFlags::NONBLOCK),
                    nonblocking
                );
            }
        }
    }
}
