//! Dedicated Transport-principal native worker. Never opens an authority DB.
#![forbid(unsafe_code)]
use clinx_decision_kernel::native_transport::{NativeTransport, UnixAuthority, UnixProviders};
use clinx_decision_kernel::remote_provider::RoutedProviders;
use std::{path::PathBuf, time::Duration};
fn main() -> Result<(), String> {
    let mut args = std::env::args().skip(1);
    let mut socket = None;
    let mut uid = None;
    let mut once = false;
    let mut timeout = 30;
    let mut routes = std::collections::BTreeMap::new();
    let mut gateway = None;
    let mut gateway_uid = None;
    let mut remote_hosts = std::collections::BTreeSet::new();
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--socket"=>socket=args.next().map(PathBuf::from),
            "--authority-uid"=>uid=Some(args.next().ok_or("AUTHORITY_UID_REQUIRED")?.parse().map_err(|_|"AUTHORITY_UID_INVALID")?),
            "--provider-gateway"=>gateway=args.next().map(PathBuf::from),
            "--provider-gateway-uid"=>gateway_uid=Some(args.next().ok_or("GATEWAY_UID_REQUIRED")?.parse::<u32>().map_err(|_|"GATEWAY_UID_INVALID")?),
            "--provider-route"=>{let host=args.next().ok_or("REMOTE_HOST_REQUIRED")?;let path=PathBuf::from(args.next().ok_or("GATEWAY_PATH_REQUIRED")?);if host.is_empty()||!path.is_absolute()||routes.insert(host,path).is_some(){return Err("INVALID_PROVIDER_ROUTE".into());}},
            "--remote-host"=>{remote_hosts.insert(args.next().ok_or("REMOTE_HOST_REQUIRED")?);},
            "--once"=>once=true,
            "--timeout-seconds"=>timeout=args.next().ok_or("TIMEOUT_REQUIRED")?.parse::<u64>().map_err(|_|"TIMEOUT_INVALID")?,
            _=>return Err("usage: clinx-native-transport --socket PATH --authority-uid UID [--once] [--timeout-seconds 1..90]".into()),
        }
    }
    if !(1..=90).contains(&timeout) {
        return Err("TIMEOUT_INVALID".into());
    }
    if (!remote_hosts.is_empty() && gateway.is_none())
        || ((!remote_hosts.is_empty() || !routes.is_empty()) && gateway_uid.is_none())
    {
        return Err("REMOTE_PROVIDER_GATEWAY_REQUIRED".into());
    }
    let mut worker = NativeTransport::new(
        UnixAuthority {
            endpoint: socket.ok_or("AUTHORITY_SOCKET_REQUIRED")?,
            uid: uid.ok_or("AUTHORITY_UID_REQUIRED")?,
        },
        RoutedProviders {
            local: UnixProviders {
                timeout: Duration::from_secs(timeout),
            },
            gateway,
            gateway_uid: gateway_uid.unwrap_or(u32::MAX),
            remote_hosts,
            routes,
        },
    );
    loop {
        println!("{}", worker.tick()?);
        if once {
            return Ok(());
        }
        std::thread::sleep(Duration::from_secs(2));
    }
}
