//! Private Helper request process, never a listener or a task-state writer.
fn main() {
    let input = std::io::stdin();
    let output = std::io::stdout();
    if clinx_decision_kernel::request_pipe::serve(input.lock(), output.lock()).is_err() {
        eprintln!("REQUEST_PIPE_UNAVAILABLE");
        std::process::exit(1);
    }
}
