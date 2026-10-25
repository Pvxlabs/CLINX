//! Small OS adapters shared by the native Mac helpers and Linux runtime.
//! They do not change peer authentication or task ownership.
use rustix::{
    fd::OwnedFd,
    io,
    net::{AddressFamily, Protocol, SocketType},
};

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
