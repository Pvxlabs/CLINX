//! Bounded IPv4 DNS-SD presence IO. PTR/SRV/TXT/A records retain their actual
//! expiry; stale service metadata never extends TTL. Presence never grants trust.
use crate::{discovery_index, node_tls::Result, read_projection::text};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, BTreeSet},
    net::{Ipv4Addr, SocketAddrV4, UdpSocket},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, AtomicU64, Ordering},
    },
    thread,
    time::{Duration, Instant},
};
const SERVICE: &str = "_clinx._tcp.local.";
const GROUP: Ipv4Addr = Ipv4Addr::new(224, 0, 0, 251);
const CACHE_BOUND: usize = 4096;
#[derive(Clone, Debug, PartialEq)]
enum Data {
    A(Ipv4Addr),
    Ptr(String),
    Srv(u16, String),
    Txt(Vec<(Vec<u8>, Vec<u8>)>),
}
#[derive(Clone, Debug)]
struct Record {
    name: String,
    kind: u16,
    flush: bool,
    ttl: u32,
    data: Data,
    expires: Instant,
    received: Instant,
}
fn u16_at(b: &[u8], at: usize) -> Result<u16> {
    Ok(u16::from_be_bytes(
        b.get(at..at + 2)
            .ok_or("INVALID_MDNS_PACKET")?
            .try_into()
            .unwrap(),
    ))
}
fn name(b: &[u8], cursor: &mut usize) -> Result<String> {
    let mut at = *cursor;
    let mut jumped = false;
    let mut seen = BTreeSet::new();
    let mut labels = Vec::new();
    let mut bytes = 0;
    for _ in 0..64 {
        if !seen.insert(at) {
            return Err("INVALID_MDNS_PACKET".into());
        }
        let n = *b.get(at).ok_or("INVALID_MDNS_PACKET")?;
        at += 1;
        if n & 0xc0 == 0xc0 {
            let tail = *b.get(at).ok_or("INVALID_MDNS_PACKET")?;
            at += 1;
            if !jumped {
                *cursor = at;
                jumped = true;
            }
            at = (((n & 0x3f) as usize) << 8) | tail as usize;
            continue;
        }
        if n & 0xc0 != 0 {
            return Err("INVALID_MDNS_PACKET".into());
        }
        if n == 0 {
            if !jumped {
                *cursor = at;
            }
            return Ok(if labels.is_empty() {
                ".".into()
            } else {
                labels.join(".") + "."
            });
        }
        let label = b.get(at..at + n as usize).ok_or("INVALID_MDNS_PACKET")?;
        if !label.is_ascii() || label.iter().any(|c| *c <= 32 || *c == b'.' || *c >= 127) {
            return Err("INVALID_MDNS_PACKET".into());
        }
        bytes += label.len() + 1;
        if bytes > 255 {
            return Err("INVALID_MDNS_PACKET".into());
        }
        labels.push(
            String::from_utf8(label.to_vec())
                .unwrap()
                .to_ascii_lowercase(),
        );
        at += n as usize;
    }
    Err("INVALID_MDNS_PACKET".into())
}
fn parse(b: &[u8], now: Instant) -> Result<(Vec<String>, Vec<Record>)> {
    if b.len() < 12 || b.len() > 9000 {
        return Err("INVALID_MDNS_PACKET".into());
    }
    let flags = u16_at(b, 2)?;
    if flags & 0x780f != 0 {
        return Err("INVALID_MDNS_PACKET".into());
    }
    let q = u16_at(b, 4)? as usize;
    let count = (u16_at(b, 6)? as usize) + (u16_at(b, 8)? as usize) + (u16_at(b, 10)? as usize);
    if q > 64 || count > 256 {
        return Err("INVALID_MDNS_PACKET".into());
    }
    let mut cursor = 12;
    let mut questions = Vec::new();
    for _ in 0..q {
        let n = name(b, &mut cursor)?;
        let kind = u16_at(b, cursor)?;
        let class = u16_at(b, cursor + 2)?;
        cursor += 4;
        if flags & 0x8000 == 0 && class & 0x7fff == 1 && matches!(kind, 1 | 12 | 16 | 33 | 255) {
            questions.push(n);
        }
    }
    let mut records = Vec::new();
    for _ in 0..count {
        let n = name(b, &mut cursor)?;
        let kind = u16_at(b, cursor)?;
        let class = u16_at(b, cursor + 2)?;
        let ttl = u32::from_be_bytes(
            b.get(cursor + 4..cursor + 8)
                .ok_or("INVALID_MDNS_PACKET")?
                .try_into()
                .unwrap(),
        );
        let size = u16_at(b, cursor + 8)? as usize;
        cursor += 10;
        let end = cursor
            .checked_add(size)
            .filter(|end| *end <= b.len())
            .ok_or("INVALID_MDNS_PACKET")?;
        if class & 0x7fff != 1 {
            cursor = end;
            continue;
        }
        let data = match kind {
            1 if size == 4 => Some(Data::A(Ipv4Addr::from(
                <[u8; 4]>::try_from(&b[cursor..end]).unwrap(),
            ))),
            12 => {
                let mut p = cursor;
                let target = name(b, &mut p)?;
                if p != end {
                    return Err("INVALID_MDNS_PACKET".into());
                }
                Some(Data::Ptr(target))
            }
            33 if size >= 7 => {
                let port = u16_at(b, cursor + 4)?;
                let mut p = cursor + 6;
                let host = name(b, &mut p)?;
                if p != end {
                    return Err("INVALID_MDNS_PACKET".into());
                }
                Some(Data::Srv(port, host))
            }
            16 => {
                let mut p = cursor;
                let mut props = Vec::new();
                while p < end {
                    let size = b[p] as usize;
                    p += 1;
                    let v = b
                        .get(p..p + size)
                        .filter(|_| p + size <= end)
                        .ok_or("INVALID_MDNS_PACKET")?;
                    p += size;
                    if let Some(i) = v.iter().position(|c| *c == b'=') {
                        props.push((v[..i].to_vec(), v[i + 1..].to_vec()));
                    } else {
                        props.push((v.to_vec(), Vec::new()));
                    }
                }
                Some(Data::Txt(props))
            }
            _ => None,
        };
        if let Some(data) = data {
            records.push(Record {
                name: n,
                kind,
                flush: class & 0x8000 != 0,
                ttl,
                data,
                expires: now + Duration::from_secs(ttl.min(86400) as u64),
                received: now,
            });
        }
        cursor = end;
    }
    // Query known answers/probe authority records are not new observations.
    Ok((
        questions,
        if flags & 0x8000 != 0 {
            records
        } else {
            Vec::new()
        },
    ))
}
#[derive(Default)]
struct Cache(Vec<Record>);
impl Cache {
    fn ingest(&mut self, records: Vec<Record>, now: Instant) {
        self.0.retain(|r| r.expires > now);
        for record in &records {
            if record.flush {
                // RFC6762 10.2: split RRsets arriving within a second must not
                // flush each other. Older conflicting records get a one-second
                // grace period in which a subsequent packet can refresh them.
                for old in &mut self.0 {
                    if old.name == record.name
                        && old.kind == record.kind
                        && now.saturating_duration_since(old.received) >= Duration::from_secs(1)
                        && !records
                            .iter()
                            .any(|r| r.name == old.name && r.kind == old.kind && r.data == old.data)
                    {
                        old.expires = old.expires.min(now + Duration::from_secs(1));
                    }
                }
            }
        }
        for record in records {
            self.0.retain(|r| {
                r.name != record.name || r.kind != record.kind || r.data != record.data
            });
            if record.ttl > 0 && self.0.len() < CACHE_BOUND {
                self.0.push(record);
            }
        }
    }
    fn needed(&mut self, now: Instant) -> Vec<String> {
        self.0.retain(|r| r.expires > now);
        let mut missing = BTreeSet::new();
        for pointer in self
            .0
            .iter()
            .filter(|r| r.name == SERVICE && r.kind == 12)
            .take(256)
        {
            let Data::Ptr(service) = &pointer.data else {
                continue;
            };
            let srv = self.0.iter().rfind(|r| r.name == *service && r.kind == 33);
            let txt = self.0.iter().rfind(|r| r.name == *service && r.kind == 16);
            if [srv, txt]
                .iter()
                .any(|r| r.is_none_or(|r| r.expires <= now + Duration::from_secs(5)))
            {
                missing.insert(service.clone());
            }
            if let Some(Record {
                data: Data::Srv(_, host),
                ..
            }) = srv
            {
                let address = self
                    .0
                    .iter()
                    .filter(|r| r.name == *host && r.kind == 1)
                    .collect::<Vec<_>>();
                if address.is_empty()
                    || address
                        .iter()
                        .any(|r| r.expires <= now + Duration::from_secs(5))
                {
                    missing.insert(host.clone());
                }
            }
        }
        missing.into_iter().take(512).collect()
    }
    fn candidates(&mut self, now: Instant) -> Result<Value> {
        self.0.retain(|r| r.expires > now);
        let mut rows = json!([]);
        for pointer in self
            .0
            .iter()
            .filter(|r| r.name == SERVICE && r.kind == 12)
            .take(256)
        {
            let Data::Ptr(service) = &pointer.data else {
                continue;
            };
            let srv = self.0.iter().rfind(|r| r.name == *service && r.kind == 33);
            let txt = self.0.iter().rfind(|r| r.name == *service && r.kind == 16);
            let (Some(srv), Some(txt)) = (srv, txt) else {
                continue;
            };
            let Data::Srv(port, host) = &srv.data else {
                continue;
            };
            let Data::Txt(properties) = &txt.data else {
                continue;
            };
            let addresses = self
                .0
                .iter()
                .filter(|r| r.name == *host && r.kind == 1)
                .collect::<Vec<_>>();
            if addresses.is_empty() {
                continue;
            }
            let ttl = addresses
                .iter()
                .map(|r| r.expires)
                .chain([pointer.expires, srv.expires, txt.expires])
                .min()
                .unwrap()
                .duration_since(now)
                .as_secs_f64();
            let address = addresses
                .iter()
                .filter_map(|r| {
                    if let Data::A(ip) = r.data {
                        Some(ip.to_string())
                    } else {
                        None
                    }
                })
                .collect::<Vec<_>>();
            let props = properties
                .iter()
                .map(|(k, v)| json!([hex(k), hex(v)]))
                .collect::<Vec<_>>();
            if let Ok(updated) = discovery_index::invoke(
                "discovery_index.update",
                &json!({"records":rows,"now":0,"service":service,"properties":props,"addresses":address,"port":port,"ttl":ttl}),
            ) {
                rows = updated;
            }
        }
        discovery_index::invoke(
            "discovery_index.candidates",
            &json!({"records":rows,"now":0}),
        )
    }
}
#[derive(Default)]
struct Resolver {
    recent: BTreeMap<String, Instant>,
    cursor: usize,
}
impl Resolver {
    fn due(&mut self, needed: &[String], now: Instant) -> Vec<String> {
        // One query per name and at most sixteen directed queries in any
        // rolling second, across all peers. New dependencies need not wait
        // for a periodic tick to consume the remaining budget.
        self.recent
            .retain(|_, at| now.saturating_duration_since(*at) < Duration::from_secs(1));
        let mut due = Vec::new();
        if needed.is_empty() {
            return due;
        }
        self.cursor %= needed.len();
        for _ in 0..needed.len() {
            if self.recent.len() >= 16 {
                break;
            }
            let name = &needed[self.cursor];
            self.cursor = (self.cursor + 1) % needed.len();
            if !self.recent.contains_key(name) {
                self.recent.insert(name.clone(), now);
                due.push(name.clone());
            }
        }
        due
    }
}
fn hex(b: &[u8]) -> String {
    b.iter().map(|b| format!("{b:02x}")).collect()
}
fn wire_name(s: &str, b: &mut Vec<u8>) -> Result<()> {
    for label in s.trim_end_matches('.').split('.') {
        if label.is_empty() || label.len() > 63 || !label.is_ascii() {
            return Err("INVALID_DISCOVERY_METADATA".into());
        }
        b.push(label.len() as u8);
        b.extend(label.as_bytes());
    }
    b.push(0);
    Ok(())
}
fn question(q: &str) -> Result<Vec<u8>> {
    let mut b = vec![0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0];
    wire_name(q, &mut b)?;
    b.extend([0, 255, 0, 1]);
    Ok(b)
}
#[derive(Clone)]
struct Advertisement {
    fullname: String,
    host: String,
    port: u16,
    properties: Vec<(Vec<u8>, Vec<u8>)>,
    addresses: Vec<Ipv4Addr>,
}
impl Advertisement {
    fn new(a: &Value, interfaces: &[Ipv4Addr]) -> Result<Self> {
        let id = &a["identity"];
        let addresses = if let Some(rows) = a["addresses"].as_array() {
            rows.iter().map(lan).collect::<Result<Vec<_>>>()?
        } else {
            interfaces.to_vec()
        };
        let props = ["node_id", "display_name", "protocol_version", "fingerprint"]
            .into_iter()
            .map(|k| {
                (
                    k.as_bytes().to_vec(),
                    if let Some(s) = id[k].as_str() {
                        s.as_bytes().to_vec()
                    } else {
                        id[k].to_string().into_bytes()
                    },
                )
            })
            .collect::<Vec<_>>();
        discovery_index::invoke(
            "discovery_index.update",
            &json!({"records":[],"now":0,"service":"validate","properties":props.iter().map(|(k,v)|json!([hex(k),hex(v)])).collect::<Vec<_>>(),"addresses":addresses.iter().map(ToString::to_string).collect::<Vec<_>>(),"port":a["port"],"ttl":30}),
        )?;
        let label = text(&id["node_id"]).to_ascii_lowercase();
        Ok(Self {
            fullname: format!("{label}.{SERVICE}"),
            host: format!("{label}.local."),
            port: a["port"].as_u64().unwrap() as u16,
            properties: props,
            addresses,
        })
    }
    fn packet(&self, ttl: u32) -> Result<Vec<u8>> {
        let mut records = Vec::new();
        let mut data = Vec::new();
        wire_name(&self.fullname, &mut data)?;
        records.push((SERVICE, 12u16, false, data));
        let mut data = vec![0, 0, 0, 0];
        data.extend(self.port.to_be_bytes());
        wire_name(&self.host, &mut data)?;
        records.push((&self.fullname, 33, true, data));
        let mut data = Vec::new();
        for (k, v) in &self.properties {
            let size = k.len() + 1 + v.len();
            if size > 255 {
                return Err("INVALID_DISCOVERY_METADATA".into());
            }
            data.push(size as u8);
            data.extend(k);
            data.push(b'=');
            data.extend(v);
        }
        records.push((&self.fullname, 16, true, data));
        for ip in &self.addresses {
            records.push((&self.host, 1, true, ip.octets().to_vec()));
        }
        let mut b = vec![0, 0, 0x84, 0, 0, 0];
        b.extend((records.len() as u16).to_be_bytes());
        b.extend([0, 0, 0, 0]);
        for (n, k, flush, data) in records {
            wire_name(n, &mut b)?;
            b.extend(k.to_be_bytes());
            b.extend((if flush { 0x8001u16 } else { 1 }).to_be_bytes());
            b.extend(ttl.to_be_bytes());
            b.extend((data.len() as u16).to_be_bytes());
            b.extend(data);
        }
        Ok(b)
    }
}
fn lan(v: &Value) -> Result<Ipv4Addr> {
    let s = discovery_index::invoke("discovery_index.lan_address", &json!({"value":v}))?;
    text(&s).parse().map_err(|_| "INVALID_LAN_ADDRESS".into())
}
pub fn local_addresses() -> Result<Value> {
    let rows = if_addrs::get_if_addrs()
        .map_err(|_| "NO_PRIVATE_LAN_INTERFACE")?
        .into_iter()
        .map(|i| json!({"name":i.name,"ips":[i.addr.ip().to_string()]}))
        .collect::<Vec<_>>();
    discovery_index::invoke("discovery_index.local_addresses", &json!({"adapters":rows}))
}
pub struct Browser {
    interfaces: Vec<Ipv4Addr>,
    sockets: Vec<UdpSocket>,
    target: SocketAddrV4,
    cache: Mutex<Cache>,
    advertisement: Mutex<Option<Advertisement>>,
    stopped: AtomicBool,
    thread: Mutex<Option<thread::JoinHandle<()>>>,
    conflict: AtomicBool,
}
impl Browser {
    pub fn start(interfaces: Option<Vec<String>>) -> Result<Arc<Self>> {
        let values = match interfaces {
            Some(v) => json!(v),
            None => local_addresses()?,
        };
        let interfaces = values
            .as_array()
            .ok_or("INVALID_DISCOVERY_ENDPOINT")?
            .iter()
            .map(lan)
            .collect::<Result<Vec<_>>>()?;
        if interfaces.len() > 16 {
            return Err("INVALID_DISCOVERY_ENDPOINT".into());
        }
        let mut sockets = Vec::new();
        for ip in &interfaces {
            use rustix::net::{self, AddressFamily, SocketFlags, SocketType};
            let fd = net::socket_with(
                AddressFamily::INET,
                SocketType::DGRAM,
                SocketFlags::CLOEXEC,
                Some(net::ipproto::UDP),
            )
            .map_err(|_| "MDNS_IO_UNAVAILABLE")?;
            net::sockopt::set_socket_reuseaddr(&fd, true).map_err(|_| "MDNS_IO_UNAVAILABLE")?;
            net::sockopt::set_socket_reuseport(&fd, true).map_err(|_| "MDNS_IO_UNAVAILABLE")?;
            net::bind(&fd, &SocketAddrV4::new(Ipv4Addr::UNSPECIFIED, 5353))
                .map_err(|_| "MDNS_IO_UNAVAILABLE")?;
            net::sockopt::set_ip_multicast_if(&fd, ip).map_err(|_| "MDNS_IO_UNAVAILABLE")?;
            let socket = UdpSocket::from(fd);
            socket
                .join_multicast_v4(&GROUP, ip)
                .map_err(|_| "MDNS_IO_UNAVAILABLE")?;
            socket
                .set_multicast_ttl_v4(255)
                .map_err(|_| "MDNS_IO_UNAVAILABLE")?;
            socket
                .set_nonblocking(true)
                .map_err(|_| "MDNS_IO_UNAVAILABLE")?;
            sockets.push(socket);
        }
        Self::spawn(interfaces, sockets, SocketAddrV4::new(GROUP, 5353))
    }
    fn spawn(
        interfaces: Vec<Ipv4Addr>,
        sockets: Vec<UdpSocket>,
        target: SocketAddrV4,
    ) -> Result<Arc<Self>> {
        let result = Arc::new(Self {
            interfaces,
            sockets,
            target,
            cache: Default::default(),
            advertisement: Default::default(),
            stopped: AtomicBool::new(false),
            thread: Default::default(),
            conflict: AtomicBool::new(false),
        });
        if !result.sockets.is_empty() {
            let owner = result.clone();
            *result.thread.lock().map_err(|_| "MDNS_IO_UNAVAILABLE")? =
                Some(thread::spawn(move || owner.run()));
        }
        Ok(result)
    }
    fn send(&self, b: &[u8]) {
        for socket in &self.sockets {
            let _ = socket.send_to(b, self.target);
        }
    }
    fn run(&self) {
        let mut query_at = Instant::now();
        let mut announce_at = Instant::now();
        let mut resolve_at = Instant::now();
        let mut resolver = Resolver::default();
        let mut buffer = [0u8; 9001];
        while !self.stopped.load(Ordering::Acquire) {
            let now = Instant::now();
            if now >= query_at {
                if let Ok(b) = question(SERVICE) {
                    self.send(&b);
                }
                query_at = now + Duration::from_secs(5);
            }
            if now >= announce_at {
                if !self.conflict.load(Ordering::Acquire)
                    && let Ok(ad) = self.advertisement.lock()
                    && let Some(ad) = ad.as_ref()
                    && let Ok(b) = ad.packet(30)
                {
                    self.send(&b);
                }
                announce_at = now + Duration::from_secs(10);
            }
            let mut received = false;
            for socket in &self.sockets {
                for _ in 0..32 {
                    match socket.recv_from(&mut buffer) {
                        Ok((size, source)) => {
                            if !source.is_ipv4() {
                                continue;
                            }
                            let Ok((queries, records)) = parse(&buffer[..size], Instant::now())
                            else {
                                continue;
                            };
                            received |= !records.is_empty();
                            if let Ok(ad) = self.advertisement.lock()
                                && let Some(ad) = ad.as_ref()
                            {
                                if records.iter().any(|r|r.name==ad.fullname && matches!(&r.data,Data::Txt(v) if v.iter().collect::<BTreeSet<_>>()!=ad.properties.iter().collect::<BTreeSet<_>>())) {
                                    self.conflict.store(true,Ordering::Release);
                                }
                                if !self.conflict.load(Ordering::Acquire)
                                    && queries
                                        .iter()
                                        .any(|q| q == SERVICE || q == &ad.fullname || q == &ad.host)
                                    && let Ok(b) = ad.packet(30)
                                {
                                    self.send(&b);
                                }
                            }
                            if let Ok(mut cache) = self.cache.lock() {
                                cache.ingest(records, Instant::now());
                            }
                        }
                        Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => break,
                        Err(_) => break,
                    }
                }
            }
            let now = Instant::now();
            if received || now >= resolve_at {
                let needed = self
                    .cache
                    .lock()
                    .map(|mut c| c.needed(now))
                    .unwrap_or_default();
                for name in resolver.due(&needed, now) {
                    if let Ok(b) = question(&name) {
                        self.send(&b);
                    }
                }
                resolve_at = now + Duration::from_millis(100);
            }
            thread::sleep(Duration::from_millis(20));
        }
    }
    pub fn candidates(&self) -> Result<Value> {
        self.cache
            .lock()
            .map_err(|_| "MDNS_IO_UNAVAILABLE")?
            .candidates(Instant::now())
    }
    pub fn advertise(&self, a: &Value) -> Result<String> {
        if self.stopped.load(Ordering::Acquire) {
            return Err("MDNS_IO_UNAVAILABLE".into());
        }
        if self.interfaces.is_empty() || self.sockets.is_empty() {
            return Err("NO_PRIVATE_LAN_INTERFACE".into());
        }
        let ad = Advertisement::new(a, &self.interfaces)?;
        for _ in 0..3 {
            self.send(&question(&ad.fullname)?);
            thread::sleep(Duration::from_millis(250));
        }
        if self
            .cache
            .lock()
            .map_err(|_| "MDNS_IO_UNAVAILABLE")?
            .0
            .iter()
            .any(|r| r.name == ad.fullname && r.expires > Instant::now())
        {
            return Err("NON_UNIQUE_SERVICE_NAME".into());
        }
        self.conflict.store(false, Ordering::Release);
        let packet = ad.packet(30)?;
        let name = ad.fullname.clone();
        *self
            .advertisement
            .lock()
            .map_err(|_| "MDNS_IO_UNAVAILABLE")? = Some(ad);
        self.send(&packet);
        Ok(name)
    }
    pub fn unregister(&self) -> Result<()> {
        if let Some(ad) = self
            .advertisement
            .lock()
            .map_err(|_| "MDNS_IO_UNAVAILABLE")?
            .take()
        {
            self.send(&ad.packet(0)?);
        }
        Ok(())
    }
    pub fn close(&self) -> Result<()> {
        self.unregister()?;
        self.stopped.store(true, Ordering::Release);
        if let Some(thread) = self
            .thread
            .lock()
            .map_err(|_| "MDNS_IO_UNAVAILABLE")?
            .take()
        {
            let _ = thread.join();
        }
        Ok(())
    }
}
#[derive(Default)]
pub struct Runtime {
    next: AtomicU64,
    browsers: Mutex<BTreeMap<u64, Arc<Browser>>>,
}
impl Runtime {
    pub fn invoke(&self, op: &str, a: &Value) -> Result<Value> {
        if op == "mdns.local_addresses" {
            return local_addresses();
        }
        if op == "mdns.start" {
            let interfaces = if a["interfaces"].is_null() {
                None
            } else {
                Some(
                    a["interfaces"]
                        .as_array()
                        .ok_or("INVALID_DISCOVERY_ENDPOINT")?
                        .iter()
                        .map(|v| text(v).to_owned())
                        .collect(),
                )
            };
            let browser = Browser::start(interfaces)?;
            let id = self.next.fetch_add(1, Ordering::Relaxed) + 1;
            let interfaces = browser
                .interfaces
                .iter()
                .map(ToString::to_string)
                .collect::<Vec<_>>();
            self.browsers
                .lock()
                .map_err(|_| "MDNS_IO_UNAVAILABLE")?
                .insert(id, browser);
            return Ok(json!({"id":id,"interfaces":interfaces}));
        }
        let id = a["id"].as_u64().ok_or("MDNS_IO_UNAVAILABLE")?;
        let browser = self
            .browsers
            .lock()
            .map_err(|_| "MDNS_IO_UNAVAILABLE")?
            .get(&id)
            .cloned()
            .ok_or("MDNS_IO_UNAVAILABLE")?;
        match op {
            "mdns.candidates" => browser.candidates(),
            "mdns.advertise" => browser.advertise(a).map(|v| json!(v)),
            "mdns.unregister" => browser.unregister().map(|_| Value::Null),
            "mdns.close" => {
                browser.close()?;
                self.browsers
                    .lock()
                    .map_err(|_| "MDNS_IO_UNAVAILABLE")?
                    .remove(&id);
                Ok(Value::Null)
            }
            _ => Err("MDNS_OPERATION_UNKNOWN".into()),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn ad() -> Advertisement {
        Advertisement::new(&json!({"identity":{"node_id":"fixture-mdns","display_name":"Private fixture","protocol_version":1,"fingerprint":"a".repeat(64)},"port":9000}),&[Ipv4Addr::LOCALHOST]).unwrap()
    }
    #[test]
    fn actual_record_expiry_not_metadata_refresh_controls_presence() {
        let start = Instant::now();
        let packet = ad().packet(30).unwrap();
        let (_, records) = parse(&packet, start).unwrap();
        let mut cache = Cache::default();
        cache.ingest(records, start);
        assert!(
            cache.candidates(start + Duration::from_secs(1)).unwrap()["candidates"]["fixture-mdns"]
                .is_object()
        );
        let (_, records) = parse(&packet, start + Duration::from_secs(20)).unwrap();
        cache.ingest(
            records.into_iter().filter(|r| r.kind == 12).collect(),
            start + Duration::from_secs(20),
        );
        assert!(
            cache.candidates(start + Duration::from_secs(31)).unwrap()["candidates"]
                .as_object()
                .unwrap()
                .is_empty()
        );
        let (_, records) = parse(&packet, start + Duration::from_secs(40)).unwrap();
        cache.ingest(records, start + Duration::from_secs(40));
        assert!(cache.candidates(start+Duration::from_secs(41)).unwrap()["candidates"]["fixture-mdns"].is_object());
        let (_, records) =
            parse(&ad().packet(0).unwrap(), start + Duration::from_secs(42)).unwrap();
        cache.ingest(records, start + Duration::from_secs(42));
        assert!(
            cache.candidates(start + Duration::from_secs(42)).unwrap()["candidates"]
                .as_object()
                .unwrap()
                .is_empty()
        );
    }
    #[test]
    fn dns_compression_loops_truncation_and_query_known_answers_are_bounded() {
        let now = Instant::now();
        let mut packet = ad().packet(30).unwrap();
        for size in 0..packet.len() {
            assert!(parse(&packet[..size], now).is_err());
        }
        packet[12] = 0xc0;
        packet[13] = 12;
        assert!(parse(&packet, now).is_err());
        let mut packet = ad().packet(30).unwrap();
        packet[2] = 0;
        packet[3] = 0;
        assert!(parse(&packet, now).unwrap().1.is_empty());
        let mut seed = 19u64;
        for len in 0..1024 {
            let mut bytes = Vec::new();
            for _ in 0..len {
                seed = seed.wrapping_mul(6364136223846793005).wrapping_add(1);
                bytes.push((seed >> 32) as u8);
            }
            let _ = parse(&bytes, now);
        }
    }
    #[test]
    fn partial_record_flush_and_invalid_metadata_cannot_fabricate_presence() {
        let now = Instant::now();
        let (_, mut records) = parse(&ad().packet(30).unwrap(), now).unwrap();
        let mut cache = Cache::default();
        let address = records.pop().unwrap();
        cache.ingest(records, now);
        assert!(
            cache.candidates(now).unwrap()["candidates"]
                .as_object()
                .unwrap()
                .is_empty()
        );
        cache.ingest(vec![address], now);
        assert!(cache.candidates(now).unwrap()["candidates"]["fixture-mdns"].is_object());
        let bad = Record {
            name: ad().fullname,
            kind: 16,
            flush: true,
            ttl: 30,
            data: Data::Txt(vec![(b"password".to_vec(), b"bad".to_vec())]),
            expires: now + Duration::from_secs(30),
            received: now,
        };
        cache.ingest(vec![bad], now);
        assert!(
            cache.candidates(now).unwrap()["candidates"]
                .as_object()
                .unwrap()
                .is_empty()
        );
    }
    #[test]
    fn private_unicast_datagrams_cover_announce_conflict_goodbye_and_close() {
        // No multicast membership, LAN packets or production daemon. Each actual
        // UDP listener is bound to a task-private ephemeral loopback endpoint.
        let a = UdpSocket::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
        let b = UdpSocket::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
        a.set_nonblocking(true).unwrap();
        b.set_nonblocking(true).unwrap();
        let std::net::SocketAddr::V4(aa) = a.local_addr().unwrap() else {
            panic!()
        };
        let std::net::SocketAddr::V4(ba) = b.local_addr().unwrap() else {
            panic!()
        };
        let advertiser = Browser::spawn(vec![Ipv4Addr::LOCALHOST], vec![a], ba).unwrap();
        let browser = Browser::spawn(vec![Ipv4Addr::LOCALHOST], vec![b], aa).unwrap();
        let args = json!({"identity":{"node_id":"fixture-mdns","display_name":"Private fixture","protocol_version":1,"fingerprint":"a".repeat(64)},"port":9000});
        advertiser.advertise(&args).unwrap();
        let deadline = Instant::now() + Duration::from_secs(2);
        while !browser.candidates().unwrap()["candidates"]["fixture-mdns"].is_object() {
            assert!(Instant::now() < deadline);
            thread::sleep(Duration::from_millis(10));
        }
        assert_eq!(
            browser.advertise(&args).unwrap_err(),
            "NON_UNIQUE_SERVICE_NAME"
        );
        advertiser.unregister().unwrap();
        let deadline = Instant::now() + Duration::from_secs(2);
        while browser.candidates().unwrap()["candidates"]["fixture-mdns"].is_object() {
            assert!(Instant::now() < deadline);
            thread::sleep(Duration::from_millis(10));
        }
        advertiser.close().unwrap();
        browser.close().unwrap();
    }
    fn select(packet: &[u8], wanted: u16, which: Option<usize>) -> Vec<u8> {
        let mut cursor = 12;
        let mut found = 0;
        let mut selected = Vec::new();
        for _ in 0..u16_at(packet, 6).unwrap() {
            let start = cursor;
            name(packet, &mut cursor).unwrap();
            let kind = u16_at(packet, cursor).unwrap();
            let size = u16_at(packet, cursor + 8).unwrap() as usize;
            cursor += 10 + size;
            if kind == wanted {
                if which.is_none_or(|i| i == found) {
                    selected.push(&packet[start..cursor]);
                }
                found += 1;
            }
        }
        let mut out = vec![0, 0, 0x84, 0, 0, 0];
        out.extend((selected.len() as u16).to_be_bytes());
        out.extend([0, 0, 0, 0]);
        for record in selected {
            out.extend(record);
        }
        out
    }
    #[test]
    fn cache_flush_grace_and_recent_split_rrsets_follow_actual_receive_time() {
        let start = Instant::now();
        let (_, initial) = parse(&ad().packet(30).unwrap(), start).unwrap();
        let mut second = ad();
        second.addresses = vec![Ipv4Addr::new(127, 0, 0, 2)];
        let mut cache = Cache::default();
        cache.ingest(initial.clone(), start);
        assert!(cache.needed(start).is_empty());
        let (_, changed) = parse(
            &select(&second.packet(30).unwrap(), 1, None),
            start + Duration::from_secs(2),
        )
        .unwrap();
        cache.ingest(changed.clone(), start + Duration::from_secs(2));
        assert_eq!(
            cache
                .candidates(start + Duration::from_millis(2100))
                .unwrap()["candidates"]["fixture-mdns"]["endpoints"]
                .as_array()
                .unwrap()
                .len(),
            2
        );
        assert_eq!(
            cache
                .candidates(start + Duration::from_millis(3100))
                .unwrap()["candidates"]["fixture-mdns"]["endpoints"]
                .as_array()
                .unwrap()
                .len(),
            1
        );
        let mut cache = Cache::default();
        cache.ingest(initial, start);
        cache.ingest(changed, start + Duration::from_secs(2));
        let (_, refresh) = parse(
            &select(&ad().packet(30).unwrap(), 1, None),
            start + Duration::from_millis(2500),
        )
        .unwrap();
        cache.ingest(refresh, start + Duration::from_millis(2500));
        assert_eq!(
            cache
                .candidates(start + Duration::from_millis(3200))
                .unwrap()["candidates"]["fixture-mdns"]["endpoints"]
                .as_array()
                .unwrap()
                .len(),
            2
        );
        let pending = cache.needed(start + Duration::from_secs(28));
        assert!(pending.contains(&ad().fullname));
        assert!(pending.contains(&ad().host));
    }
    #[test]
    fn directed_queries_resolve_new_dependencies_without_unbounded_retries() {
        let now = Instant::now();
        let mut resolver = Resolver::default();
        let fullname = vec!["instance._clinx._tcp.local.".to_owned()];
        let host = vec!["host.local.".to_owned()];
        assert_eq!(resolver.due(&fullname, now), fullname);
        assert!(
            resolver
                .due(&fullname, now + Duration::from_millis(20))
                .is_empty()
        );
        assert_eq!(resolver.due(&host, now + Duration::from_millis(20)), host);
        let flooded = (0..512).map(|i| format!("{i}.local.")).collect::<Vec<_>>();
        assert_eq!(
            resolver
                .due(&flooded, now + Duration::from_millis(30))
                .len(),
            14
        );
        assert!(
            resolver
                .due(&flooded, now + Duration::from_millis(999))
                .is_empty()
        );
        assert_eq!(
            resolver.due(&flooded, now + Duration::from_secs(1)).len(),
            1
        );
        assert_eq!(
            resolver
                .due(&flooded, now + Duration::from_millis(1020))
                .len(),
            1
        );
        assert!(resolver.due(&[], now + Duration::from_secs(3)).is_empty());
        assert!(resolver.recent.is_empty());
    }
    #[test]
    fn private_udp_resolves_ptr_only_and_preserves_split_multiple_addresses() {
        let server = UdpSocket::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
        server
            .set_read_timeout(Some(Duration::from_millis(100)))
            .unwrap();
        let client = UdpSocket::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
        client.set_nonblocking(true).unwrap();
        let std::net::SocketAddr::V4(target) = server.local_addr().unwrap() else {
            panic!()
        };
        let stopped = Arc::new(AtomicBool::new(false));
        let seen = Arc::new(Mutex::new(BTreeSet::new()));
        let done = stopped.clone();
        let observed = seen.clone();
        let mut advertisement = ad();
        advertisement.addresses.push(Ipv4Addr::new(127, 0, 0, 2));
        let packet = advertisement.packet(30).unwrap();
        let responder = thread::spawn(move || {
            let mut buffer = [0; 9001];
            while !done.load(Ordering::Acquire) {
                let Ok((n, peer)) = server.recv_from(&mut buffer) else {
                    continue;
                };
                let (queries, _) = parse(&buffer[..n], Instant::now()).unwrap();
                for query in queries {
                    observed.lock().unwrap().insert(query.clone());
                    if query == SERVICE {
                        server.send_to(&select(&packet, 12, None), peer).unwrap();
                    } else if query == advertisement.fullname {
                        server.send_to(&select(&packet, 33, None), peer).unwrap();
                        server.send_to(&select(&packet, 16, None), peer).unwrap();
                    } else if query == advertisement.host {
                        server.send_to(&select(&packet, 1, Some(0)), peer).unwrap();
                        thread::sleep(Duration::from_millis(100));
                        server.send_to(&select(&packet, 1, Some(1)), peer).unwrap();
                    }
                }
            }
        });
        let browser = Browser::spawn(vec![Ipv4Addr::LOCALHOST], vec![client], target).unwrap();
        // The real GUI closes its browser after two seconds. Resolve each
        // newly observed dependency promptly, including split address RRs.
        let deadline = Instant::now() + Duration::from_millis(1500);
        let result = loop {
            let rows = browser.candidates().unwrap();
            if rows["candidates"]["fixture-mdns"]["endpoints"]
                .as_array()
                .is_some_and(|v| v.len() == 2)
            {
                break true;
            }
            if Instant::now() >= deadline {
                break false;
            }
            thread::sleep(Duration::from_millis(10));
        };
        stopped.store(true, Ordering::Release);
        responder.join().unwrap();
        browser.close().unwrap();
        assert!(
            result,
            "PTR-only peer must resolve fullname and host; split A packets must survive cache flush"
        );
        let queries = seen.lock().unwrap();
        assert!(queries.contains(&ad().fullname));
        assert!(queries.contains(&ad().host));
    }
}
