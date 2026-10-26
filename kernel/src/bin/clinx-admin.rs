//! Root-owned CLINX operator entry. No arbitrary command, path, SQL or shell API.
#[path = "../admin_tools.rs"]
mod admin;
#[path = "../build_release.rs"]
#[allow(dead_code)]
mod build;
#[path = "../install_tools.rs"]
#[allow(dead_code)]
mod install;
fn main() {
    let args: Vec<_> = std::env::args().skip(1).collect();
    if args == ["--help"] {
        println!(
            "clinx-admin status | nodes | journal SERVICE [LINES] | service start|stop|restart SERVICE | pair-listen | share PEER USER_SCOPE read|execute|revoke | probe HOST PROJECT | stage CANDIDATE_DIR SOURCE_HEAD | select RELEASE_ID --approved | recover RELEASE_ID EXECUTION_REF --approved | policy EXPECTED_HASH OPERATION_ID --approved < POLICY_JSON\nRoot-owned, fixed CLINX paths/roles. Sudo permission never replaces task-specific user authorization. No arbitrary shell, SQL, service or output path."
        );
        return;
    }
    match admin::run(&args) {
        Ok(value) => println!("{value}"),
        Err(error) => {
            eprintln!("CLINX_ADMIN=BLOCKED: {error}");
            std::process::exit(1);
        }
    }
}
