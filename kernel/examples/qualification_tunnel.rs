//! Private test child, never a release member. Native replacement for the old
//! embedded Python HTTP fixture and TERM-resistant descendant.
use std::{
    fs,
    io::{BufRead, BufReader, Read, Write},
    net::TcpListener,
    process::{Child, Command, Stdio},
    sync::{Arc, atomic::AtomicBool},
    time::{Duration, SystemTime, UNIX_EPOCH},
};
// The supervisor owns signal teardown of this whole group. This guard also
// reaps the private descendant if the fixture exits normally or unwinds.
struct Descendant(Child);
impl Drop for Descendant {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}
fn main() {
    let args: Vec<_> = std::env::args().skip(1).collect();
    if args.first().is_some_and(|a| a == "--descendant") {
        signal_hook::flag::register(
            signal_hook::consts::SIGTERM,
            Arc::new(AtomicBool::new(false)),
        )
        .unwrap();
        println!("TERM_HANDLER_READY");
        std::io::stdout().flush().unwrap();
        loop {
            std::thread::sleep(Duration::from_secs(1));
        }
    }
    let get = |key: &str| {
        args.windows(2)
            .find(|p| p[0] == key)
            .map(|p| p[1].clone())
            .unwrap()
    };
    let mut child = Descendant(
        Command::new(std::env::current_exe().unwrap())
            .arg("--descendant")
            .stdout(Stdio::piped())
            .spawn()
            .unwrap(),
    );
    let output = child.0.stdout.take().unwrap();
    let (tx, rx) = std::sync::mpsc::channel();
    let reader = std::thread::spawn(move || {
        let mut ready = String::new();
        let result = BufReader::new(output)
            .take(64)
            .read_line(&mut ready)
            .map(|_| ready);
        let _ = tx.send(result);
    });
    let ready = rx
        .recv_timeout(Duration::from_secs(2))
        .expect("descendant TERM handler readiness deadline")
        .unwrap();
    reader.join().unwrap();
    assert_eq!(ready, "TERM_HANDLER_READY\n");
    fs::write(get("--pid-file"), child.0.id().to_string()).unwrap();
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    fs::write(
        get("--health.url-file"),
        format!("http://{}", listener.local_addr().unwrap()),
    )
    .unwrap();
    for peer in listener.incoming() {
        let mut peer = peer.unwrap();
        peer.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
        let mut req = [0; 4096];
        let _ = peer.read(&mut req);
        let body = format!(
            "commands_poll_last_successful_timestamp_seconds {}\n",
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_secs_f64()
        );
        let _ = write!(
            peer,
            "HTTP/1.0 200 OK\r\nContent-Length: {}\r\n\r\n{body}",
            body.len()
        );
    }
}
