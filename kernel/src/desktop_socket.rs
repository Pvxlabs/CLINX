//! Explicit maintenance of the existing Desktop socket alias. Never starts a
//! daemon, scans endpoints, writes task state, or retries a Provider request.
use crate::{
    native_transport::connect_local,
    provider::{Client, Provider, Result},
};
use serde_json::{Value, json};
use std::{
    fs,
    os::unix::fs::{FileTypeExt, MetadataExt, symlink},
    path::{Path, PathBuf},
    process::Command,
    time::Duration,
};

const ALIAS: &str = "/run/clinx-rust-provider/provider.sock";
const TIMEOUT: Duration = Duration::from_secs(3);

fn identity(path: &Path) -> Result<(u64, u64)> {
    let meta = fs::metadata(path).map_err(|_| "DESKTOP_SOCKET_IDENTITY_REQUIRED")?;
    Ok((meta.dev(), meta.ino()))
}
fn peer(path: &Path, uid: u32) -> Result<i32> {
    let socket = connect_local(path, TIMEOUT).map_err(|_| "DESKTOP_SOCKET_UNAVAILABLE")?;
    #[cfg(target_os = "linux")]
    {
        let credentials = rustix::net::sockopt::socket_peercred(&socket)
            .map_err(|_| "DESKTOP_SOCKET_PEER_CONFLICT")?;
        if uid == 0 || credentials.uid.as_raw() != uid {
            return Err("DESKTOP_SOCKET_PEER_CONFLICT".into());
        }
        Ok(credentials.pid.as_raw_nonzero().get())
    }
    #[cfg(not(target_os = "linux"))]
    {
        let _ = (socket, uid);
        Err("DESKTOP_SOCKET_PEER_CREDENTIALS_UNAVAILABLE".into())
    }
}
fn inspect_at(path: &Path, witness: &str, root: &Path, uid: u32) -> Result<Value> {
    let meta = path
        .symlink_metadata()
        .map_err(|_| "DESKTOP_SOCKET_IDENTITY_REQUIRED")?;
    let parent = root
        .symlink_metadata()
        .map_err(|_| "DESKTOP_SOCKET_IDENTITY_REQUIRED")?;
    if !path.is_absolute()
        || path.parent() != Some(root)
        || meta.file_type().is_symlink()
        || parent.file_type().is_symlink()
        || parent.uid() != uid
        || meta.uid() != uid
        || !meta.file_type().is_socket()
    {
        return Err("DESKTOP_SOCKET_IDENTITY_REQUIRED".into());
    }
    let pid = peer(path, uid)?;
    let before = identity(path)?;
    let mut client = Client::connect_uninitialized(path, TIMEOUT)?;
    let initialized = client.request(
        "initialize",
        json!({"clientInfo":{
        "name":"clinx-desktop-socket", "title":"CLINX Desktop socket verification", "version":"1"},
        "capabilities":{"experimentalApi":true}}),
    )?;
    if !initialized.is_object() {
        return Err("PROVIDER_PROTOCOL_INVALID".into());
    }
    client.send_message(&json!({"method":"initialized"}))?;
    let result = client.request(
        "thread/read",
        json!({"threadId":witness,"includeTurns":false}),
    )?;
    let thread = &result["thread"];
    if thread["id"] != witness || before != identity(path)? {
        return Err("DESKTOP_WITNESS_CONFLICT".into());
    }
    Ok(
        json!({"socket":path,"daemon_pid":pid,"uid":uid,"socket_device":before.0,
        "socket_inode":before.1,"witness_thread_id":witness,"witness_cwd":thread["cwd"],"daemon_started":false}),
    )
}
fn publish_with(
    path: &Path,
    alias: &Path,
    expected_alias: &Path,
    uid: u32,
    apply: bool,
    mut inspect: impl FnMut() -> Result<Value>,
    mut grant: impl FnMut(&Path) -> Result<()>,
) -> Result<Value> {
    let mut result = inspect()?;
    let parent = alias.parent().ok_or("DESKTOP_ALIAS_IDENTITY_REQUIRED")?;
    let meta = parent
        .symlink_metadata()
        .map_err(|_| "DESKTOP_ALIAS_IDENTITY_REQUIRED")?;
    if alias != expected_alias
        || meta.file_type().is_symlink()
        || meta.uid() != uid
        || (alias.exists() && !alias.is_symlink())
    {
        return Err("DESKTOP_ALIAS_IDENTITY_REQUIRED".into());
    }
    result["previous_alias"] = if alias.is_symlink() {
        json!(fs::read_link(alias).map_err(|_| "DESKTOP_ALIAS_IDENTITY_REQUIRED")?)
    } else {
        Value::Null
    };
    result["alias"] = json!(alias);
    result["applied"] = json!(false);
    if apply {
        grant(path)?;
        let verified = inspect()?;
        if ["daemon_pid", "socket_device", "socket_inode"]
            .iter()
            .any(|key| result[*key] != verified[*key])
        {
            return Err("DESKTOP_SOCKET_CHANGED".into());
        }
        let temporary = alias.with_file_name(format!(
            "{}.desktop-{}",
            alias.file_name().unwrap().to_string_lossy(),
            std::process::id()
        ));
        // Keep the old alias until the final atomic rename; never replace a
        // pre-existing temporary path on an uncertain maintenance result.
        symlink(path, &temporary).map_err(|_| "DESKTOP_ALIAS_PUBLISH_FAILED")?;
        let renamed: Result<()> =
            fs::rename(&temporary, alias).map_err(|_| "DESKTOP_ALIAS_PUBLISH_FAILED".into());
        let _ = fs::remove_file(&temporary);
        renamed?;
        result["applied"] = json!(true);
    }
    Ok(result)
}
fn grant(path: &Path) -> Result<()> {
    let ids = ["clinx-api", "clinx-transport", "clinx-authority"]
        .into_iter()
        .map(|name| {
            nix::unistd::User::from_name(name)
                .map_err(|_| "DESKTOP_ACCOUNT_UNAVAILABLE")?
                .map(|user| user.uid.as_raw())
                .ok_or("DESKTOP_ACCOUNT_UNAVAILABLE")
        })
        .collect::<std::result::Result<Vec<_>, _>>()?;
    for (target, rights) in [
        (
            path.parent().ok_or("DESKTOP_SOCKET_IDENTITY_REQUIRED")?,
            "--x",
        ),
        (path, "rw-"),
    ] {
        let acl = ids
            .iter()
            .map(|uid| format!("u:{uid}:{rights}"))
            .collect::<Vec<_>>()
            .join(",");
        if !Command::new("/usr/bin/setfacl")
            .args(["-m", &acl])
            .arg(target)
            .status()
            .map_err(|_| "DESKTOP_ACL_UNAVAILABLE")?
            .success()
        {
            return Err("DESKTOP_ACL_UNAVAILABLE".into());
        }
    }
    Ok(())
}
pub fn publish(path: &Path, witness: &str, alias: &Path, apply: bool) -> Result<Value> {
    let uid = nix::unistd::getuid().as_raw();
    let root = PathBuf::from(format!("/tmp/codex-daemon-{uid}"));
    publish_with(
        path,
        alias,
        Path::new(ALIAS),
        uid,
        apply,
        || inspect_at(path, witness, &root, uid),
        grant,
    )
}
pub fn cli(args: impl IntoIterator<Item = String>) -> Result<Value> {
    let mut args = args.into_iter();
    let (mut path, mut witness, mut alias, mut apply) = (None, None, PathBuf::from(ALIAS), false);
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--socket" => {
                path = Some(PathBuf::from(
                    args.next().ok_or("DESKTOP_SOCKET_ARGUMENT_REQUIRED")?,
                ))
            }
            "--witness-thread-id" => witness = Some(args.next().ok_or("DESKTOP_WITNESS_REQUIRED")?),
            "--alias" => {
                alias = PathBuf::from(args.next().ok_or("DESKTOP_ALIAS_IDENTITY_REQUIRED")?)
            }
            "--apply" => apply = true,
            _ => return Err("DESKTOP_SOCKET_ARGUMENT_INVALID".into()),
        }
    }
    publish(
        &path.ok_or("DESKTOP_SOCKET_ARGUMENT_REQUIRED")?,
        &witness.ok_or("DESKTOP_WITNESS_REQUIRED")?,
        &alias,
        apply,
    )
}

#[cfg(all(test, target_os = "linux"))]
mod tests {
    use super::*;
    use base64::{Engine, engine::general_purpose::STANDARD};
    use sha1::{Digest, Sha1};
    use std::{
        io::{Read, Write},
        os::unix::net::{UnixListener, UnixStream},
        sync::{Arc, Mutex},
        thread,
    };

    fn read(stream: &mut UnixStream) -> Value {
        let mut head = [0; 2];
        stream.read_exact(&mut head).unwrap();
        assert_eq!(head[0], 0x81);
        assert_ne!(head[1] & 0x80, 0);
        let mut len = (head[1] & 0x7f) as usize;
        if len == 126 {
            let mut n = [0; 2];
            stream.read_exact(&mut n).unwrap();
            len = u16::from_be_bytes(n) as usize;
        }
        assert!(len < 65536);
        let mut mask = [0; 4];
        stream.read_exact(&mut mask).unwrap();
        let mut data = vec![0; len];
        stream.read_exact(&mut data).unwrap();
        for (i, b) in data.iter_mut().enumerate() {
            *b ^= mask[i % 4];
        }
        serde_json::from_slice(&data).unwrap()
    }
    fn send(stream: &mut UnixStream, value: Value) {
        let data = value.to_string().into_bytes();
        assert!(data.len() < 126);
        stream.write_all(&[0x81, data.len() as u8]).unwrap();
        stream.write_all(&data).unwrap();
    }
    fn fixture(
        witness: &'static str,
        replace: bool,
    ) -> (tempfile::TempDir, PathBuf, thread::JoinHandle<Vec<Value>>) {
        let root = tempfile::tempdir().unwrap();
        let path = root.path().join("desktop.sock");
        let listener = UnixListener::bind(&path).unwrap();
        let target = path.clone();
        let child = thread::spawn(move || {
            drop(listener.accept().unwrap().0); // Existing first SO_PEERCRED probe.
            let (mut peer, _) = listener.accept().unwrap();
            peer.set_read_timeout(Some(Duration::from_secs(3))).unwrap();
            let mut header = vec![];
            while !header.ends_with(b"\r\n\r\n") {
                let mut b = [0];
                peer.read_exact(&mut b).unwrap();
                header.push(b[0]);
                assert!(header.len() < 65536);
            }
            let text = String::from_utf8(header).unwrap();
            let key = text
                .lines()
                .find_map(|l| l.strip_prefix("Sec-WebSocket-Key: "))
                .unwrap()
                .trim();
            let accept = STANDARD.encode(Sha1::digest(format!(
                "{key}258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
            )));
            write!(peer,"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n").unwrap();
            let init = read(&mut peer);
            send(&mut peer, json!({"id":init["id"],"result":{}}));
            let notification = read(&mut peer);
            let request = read(&mut peer);
            if replace {
                fs::remove_file(&target).unwrap();
                let _replacement = UnixListener::bind(&target).unwrap();
            }
            send(
                &mut peer,
                json!({"id":request["id"],"result":{"thread":{"id":witness,"cwd":"/exact/workspace"}}}),
            );
            vec![init, notification, request]
        });
        (root, path, child)
    }
    #[test]
    fn exact_existing_provider_witness_preserves_wire_and_never_starts_daemon() {
        let (root, path, child) = fixture("witness", false);
        let uid = nix::unistd::getuid().as_raw();
        assert_ne!(uid, 0, "qualification requires an unprivileged owner");
        let before = identity(&path).unwrap();
        let result = inspect_at(&path, "witness", root.path(), uid).unwrap();
        assert_eq!(
            result,
            json!({"socket":path,"daemon_pid":std::process::id(),"uid":uid,"socket_device":before.0,"socket_inode":before.1,"witness_thread_id":"witness","witness_cwd":"/exact/workspace","daemon_started":false})
        );
        let calls = child.join().unwrap();
        assert_eq!(
            calls[0]["params"],
            json!({"clientInfo":{"name":"clinx-desktop-socket","title":"CLINX Desktop socket verification","version":"1"},"capabilities":{"experimentalApi":true}})
        );
        assert_eq!(calls[0]["method"], "initialize");
        assert_eq!(calls[1], json!({"method":"initialized"}));
        assert_eq!(calls[2]["method"], "thread/read");
        assert_eq!(
            calls[2]["params"],
            json!({"threadId":"witness","includeTurns":false})
        );
        assert_eq!(identity(&path).unwrap(), before);
        assert_eq!(fs::read_dir(root.path()).unwrap().count(), 1);
    }
    #[test]
    fn wrong_witness_and_inode_replacement_fail_closed() {
        for (witness, replace) in [("other", false), ("witness", true)] {
            let (root, path, child) = fixture(witness, replace);
            assert_eq!(
                inspect_at(
                    &path,
                    "witness",
                    root.path(),
                    nix::unistd::getuid().as_raw()
                )
                .unwrap_err(),
                "DESKTOP_WITNESS_CONFLICT"
            );
            assert_eq!(child.join().unwrap().len(), 3);
        }
    }
    #[test]
    fn path_symlink_owner_and_root_peer_conflicts_reject_before_provider_io() {
        let root = tempfile::tempdir().unwrap();
        let path = root.path().join("s");
        let listener = UnixListener::bind(&path).unwrap();
        let uid = nix::unistd::getuid().as_raw();
        let alias = root.path().join("alias");
        symlink(&path, &alias).unwrap();
        for (candidate, owner) in [(&alias, uid), (&path, uid + 1)] {
            assert_eq!(
                inspect_at(candidate, "w", root.path(), owner).unwrap_err(),
                "DESKTOP_SOCKET_IDENTITY_REQUIRED"
            );
        }
        assert_eq!(
            inspect_at(&path, "w", Path::new("/wrong"), uid).unwrap_err(),
            "DESKTOP_SOCKET_IDENTITY_REQUIRED"
        );
        assert_eq!(peer(&path, 0).unwrap_err(), "DESKTOP_SOCKET_PEER_CONFLICT");
        drop(listener);
    }
    fn receipt() -> Value {
        json!({"daemon_pid":42,"socket_device":1,"socket_inode":2})
    }
    #[test]
    fn dry_run_never_grants_and_apply_rechecks_then_atomically_replaces_alias() {
        let root = tempfile::tempdir().unwrap();
        let alias = root.path().join("alias");
        let target = root.path().join("socket");
        symlink("/old/exact", &alias).unwrap();
        let uid = nix::unistd::getuid().as_raw();
        let calls = Arc::new(Mutex::new(vec![]));
        for apply in [false, true] {
            let events = calls.clone();
            let grants = calls.clone();
            let result = publish_with(
                &target,
                &alias,
                &alias,
                uid,
                apply,
                || {
                    events.lock().unwrap().push("inspect");
                    Ok(receipt())
                },
                |p| {
                    assert_eq!(p, target);
                    grants.lock().unwrap().push("grant");
                    Ok(())
                },
            )
            .unwrap();
            assert_eq!(result["previous_alias"], "/old/exact");
            assert_eq!(result["applied"], apply);
            assert_eq!(
                fs::read_link(&alias).unwrap(),
                if apply {
                    target.clone()
                } else {
                    PathBuf::from("/old/exact")
                }
            );
        }
        assert_eq!(
            *calls.lock().unwrap(),
            vec!["inspect", "inspect", "grant", "inspect"]
        );
        assert_eq!(fs::read_dir(root.path()).unwrap().count(), 1);
    }
    #[test]
    fn changed_evidence_failed_acl_and_invalid_alias_preserve_old_alias() {
        let root = tempfile::tempdir().unwrap();
        let alias = root.path().join("alias");
        let target = root.path().join("socket");
        let uid = nix::unistd::getuid().as_raw();
        symlink("/old/exact", &alias).unwrap();
        for key in ["daemon_pid", "socket_device", "socket_inode"] {
            let mut calls = 0;
            assert_eq!(
                publish_with(
                    &target,
                    &alias,
                    &alias,
                    uid,
                    true,
                    || {
                        calls += 1;
                        let mut r = receipt();
                        if calls == 2 {
                            r[key] = json!(999);
                        }
                        Ok(r)
                    },
                    |_| Ok(())
                )
                .unwrap_err(),
                "DESKTOP_SOCKET_CHANGED"
            );
            assert_eq!(fs::read_link(&alias).unwrap(), Path::new("/old/exact"));
        }
        assert_eq!(
            publish_with(
                &target,
                &alias,
                &alias,
                uid,
                true,
                || Ok(receipt()),
                |_| Err("ACL_FAILED".into())
            )
            .unwrap_err(),
            "ACL_FAILED"
        );
        assert_eq!(
            publish_with(
                &target,
                &alias,
                Path::new(ALIAS),
                uid,
                true,
                || Ok(receipt()),
                |_| panic!("invalid alias cannot grant")
            )
            .unwrap_err(),
            "DESKTOP_ALIAS_IDENTITY_REQUIRED"
        );
        assert_eq!(fs::read_link(&alias).unwrap(), Path::new("/old/exact"));
        assert_eq!(fs::read_dir(root.path()).unwrap().count(), 1);
    }
    #[test]
    fn existing_temporary_collision_is_preserved_and_never_published() {
        let root = tempfile::tempdir().unwrap();
        let alias = root.path().join("alias");
        let target = root.path().join("socket");
        let temp = root
            .path()
            .join(format!("alias.desktop-{}", std::process::id()));
        fs::write(&temp, "keep").unwrap();
        assert_eq!(
            publish_with(
                &target,
                &alias,
                &alias,
                nix::unistd::getuid().as_raw(),
                true,
                || Ok(receipt()),
                |_| Ok(())
            )
            .unwrap_err(),
            "DESKTOP_ALIAS_PUBLISH_FAILED"
        );
        assert_eq!(fs::read_to_string(temp).unwrap(), "keep");
        assert!(!alias.exists());
    }
}
