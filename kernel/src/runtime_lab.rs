//! Opt-in local journal runtime. No Provider, Host, TaskRegistry or network imports.
use crate::ProtocolRequest;
use crate::execution_core as core;
use serde::Deserialize;
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    path::Path,
};

pub const PROTOCOL: &str = "CLINX_RUNTIME_LAB_V1";
const MARKER: &str = "CLINX_RUNTIME_LAB_V1 NON_AUTHORITATIVE 1.0.0\n";
const LIMIT: usize = 1024;
#[derive(Clone, Default)]
pub struct State {
    events: Vec<Value>,
    deliveries: BTreeMap<String, Value>,
    checkpoints: BTreeMap<String, Value>,
    receipts: BTreeMap<String, (String, Value)>,
}
fn err() -> String {
    "REJECT".into()
}
fn present(v: &Value) -> bool {
    v.as_str().is_some_and(|s| !s.is_empty())
}
fn key(row: &Value) -> String {
    json!([row["execution_id"], row["attempt_id"], row["call_id"]]).to_string()
}
fn utc(v: &Value) -> bool {
    let Some(s) = v.as_str() else {
        return false;
    };
    let b = s.as_bytes();
    if b.len() != 27
        || [4, 7, 10, 13, 16, 19, 26]
            .iter()
            .zip(b"--T::.Z")
            .any(|(i, c)| b[*i] != *c)
        || b.iter()
            .enumerate()
            .any(|(i, c)| ![4, 7, 10, 13, 16, 19, 26].contains(&i) && !c.is_ascii_digit())
    {
        return false;
    }
    let n = |a: usize, z: usize| s[a..z].parse::<u32>().unwrap_or(9999);
    let (y, m, d) = (n(0, 4), n(5, 7), n(8, 10));
    let leap = y.is_multiple_of(4) && (!y.is_multiple_of(100) || y.is_multiple_of(400));
    let days = match m {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 => {
            if leap {
                29
            } else {
                28
            }
        }
        _ => 0,
    };
    y > 0 && d > 0 && d <= days && n(11, 13) < 24 && n(14, 16) < 60 && n(17, 19) < 60
}
/// Full Stage-3 Event envelope; the Stage-2 offline format is never a live wire reader.
pub fn offline(e: &Value) -> Result<Value, String> {
    if !core::keys(
        e,
        "event_id family contract_version schema_version aggregate_type aggregate_id sequence aggregate_version actor causation_id correlation_id transaction_id idempotency_scope idempotency_key payload payload_hash occurred_at recorded_at",
    ) || ["causation_id", "correlation_id", "transaction_id"]
        .iter()
        .any(|k| !present(&e[k]))
        || e["aggregate_type"] != e["payload"]["object"]
        || e["aggregate_id"] != e["payload"]["id"]
        || e["aggregate_version"] != e["payload"]["next_version"]
        || e["actor"] != e["payload"]["actor"]
        || !utc(&e["occurred_at"])
        || !utc(&e["recorded_at"])
        || e["recorded_at"].as_str() < e["occurred_at"].as_str()
    {
        return Err(err());
    }
    Ok(
        json!({"family":e["family"],"contract_version":e["contract_version"],"schema_version":e["schema_version"],"event_id":e["event_id"],"stream_id":[e["aggregate_type"],e["aggregate_id"]],"sequence":e["sequence"],"payload":e["payload"],"payload_hash":e["payload_hash"],"scope":e["idempotency_scope"],"key":e["idempotency_key"]}),
    )
}
// Cross-stream checks for the minimal one-attempt runtime slice. No takeover is inferred.
fn validate_ledger(
    events: &[Value],
    old_len: usize,
    deliveries: &BTreeMap<String, Value>,
) -> Result<(), String> {
    let mut owners: BTreeMap<String, Value> = BTreeMap::new();
    let mut workers: BTreeMap<String, Value> = BTreeMap::new();
    let mut epochs: BTreeMap<String, i64> = BTreeMap::new();
    let mut terminals: BTreeMap<String, Value> = BTreeMap::new();
    let mut allocations: BTreeMap<String, Value> = BTreeMap::new();
    for (index, e) in events.iter().enumerate() {
        let r = &e["payload"];
        let f = &r["facts"];
        if let Some(o) = f.get("stored_owner").filter(|o| o.is_object()) {
            let id = o["execution_id"].as_str().ok_or_else(err)?.to_owned();
            if owners.get(&id).is_some_and(|old| old != o) {
                return Err(err());
            }
            if workers
                .get(o["worker_id"].as_str().unwrap_or(""))
                .is_some_and(|inc| *inc != o["incarnation_id"])
            {
                return Err(err());
            }
            owners.insert(id, o.clone());
            if r["object"] == "ResourceAllocation" {
                allocations.insert(r["id"].as_str().unwrap().into(), o.clone());
            }
        }
        if r["object"] == "RuntimeWorker" {
            let id = r["id"].as_str().unwrap();
            if r["action"] == "register_new_incarnation" {
                if workers
                    .get(id)
                    .is_some_and(|inc| *inc != f["previous_incarnation"])
                {
                    return Err(err());
                }
                workers.insert(id.into(), f["presented_incarnation"].clone());
            } else if let Some(inc) = f.get("current_incarnation") {
                if workers.get(id).is_some_and(|old| old != inc) {
                    return Err(err());
                }
                workers.insert(id.into(), inc.clone());
            }
        }
        if r["object"] == "ResourceAllocation" && r["action"] == "grant" {
            let o = &f["presented_owner"];
            let resource = o["resource_key"].as_str().ok_or_else(err)?;
            if epochs
                .get(resource)
                .is_some_and(|epoch| f["previous_epoch"].as_i64() != Some(*epoch))
            {
                return Err(err());
            }
            epochs.insert(
                resource.into(),
                o["resource_epoch"].as_i64().ok_or_else(err)?,
            );
        }
        if r["object"] == "Execution"
            && matches!(
                r["to"].as_str(),
                Some("SUCCEEDED" | "BLOCKED" | "FAILED" | "CANCELLED")
            )
        {
            if index >= old_len {
                for row in deliveries
                    .values()
                    .filter(|row| row["execution_id"] == r["id"])
                {
                    let matched = f["deliveries"].as_array().and_then(|rows| {
                        rows.iter()
                            .find(|candidate| candidate["call_id"] == row["call_id"])
                    });
                    if matched.is_none_or(|candidate| {
                        [
                            "execution_id",
                            "attempt_id",
                            "host_ref",
                            "host_completion",
                            "delivery_state",
                            "response_sent",
                        ]
                        .iter()
                        .any(|k| candidate[k] != row[k])
                    }) {
                        return Err("HOLD_UNKNOWN".into());
                    }
                }
            }
            terminals.insert(r["id"].as_str().unwrap().into(), r.clone());
        }
        if r["object"] == "ResourceAllocation"
            && matches!(r["action"].as_str(), Some("release" | "commit_release"))
        {
            let id = f["lease_execution_id"].as_str().unwrap_or("");
            if terminals.get(id).is_none_or(|t| {
                t["to"] != f["committed_outcome"]
                    || t["facts"]["binding"]["attempt_id"] != f["lease_attempt_id"]
            }) {
                return Err("HOLD_UNKNOWN".into());
            }
            if allocations.get(r["id"].as_str().unwrap()).is_some_and(|o| {
                o["execution_id"] != f["lease_execution_id"]
                    || o["attempt_id"] != f["lease_attempt_id"]
            }) {
                return Err(err());
            }
        }
    }
    Ok(())
}
impl State {
    pub fn snapshot(&self) -> Value {
        let es: Vec<_> = self.events.iter().map(|e| offline(e).unwrap()).collect();
        json!({"projection":core::replay(&es).expect("validated events"),"deliveries":self.deliveries,"checkpoints":self.checkpoints,"event_count":self.events.len()})
    }
    pub fn apply(&mut self, req: &ProtocolRequest) -> Result<(Value, bool), String> {
        let p = &req.payload;
        match req.operation.as_str() {
            "evaluate" if core::keys(p, "record") => {
                return Ok((json!({"verdict":core::transition(&p["record"])}), false));
            }
            "replay" if core::keys(p, "events") => {
                let es = p["events"]
                    .as_array()
                    .filter(|a| a.len() <= LIMIT)
                    .ok_or_else(err)?;
                return Ok((
                    json!({"verdict":"APPLY","projection":core::replay(es)?}),
                    false,
                ));
            }
            "ack" if core::keys(p, "row ack") => {
                return Ok((
                    json!({"verdict":core::reconcile_ack(&p["row"],&p["ack"])}),
                    false,
                ));
            }
            "receipt" if core::keys(p, "original request") => {
                return Ok((
                    json!({"verdict":core::receipt_retry(&p["original"],&p["request"])}),
                    false,
                ));
            }
            "inspect" if core::keys(p, "") => {
                return Ok((json!({"verdict":"APPLY","snapshot":self.snapshot()}), false));
            }
            "recovery" if core::keys(p, "record") => {
                let r = &p["record"];
                let v = core::transition(r);
                let class = if v != "APPLY" {
                    v
                } else if r["object"] == "ResourceAllocation" {
                    "EXACT_RELEASE_ONLY"
                } else {
                    "COMMIT_ORIGINAL_TERMINAL_ONLY"
                };
                return Ok((json!({"verdict":v,"classification":class}), false));
            }
            _ => {}
        }
        let h = core::digest(&json!({"operation":req.operation,"payload":p}));
        if let Some((prior, receipt)) = self.receipts.get(&req.request_id) {
            if *prior != h {
                return Err(err());
            }
            return Ok((json!({"verdict":"DUPLICATE","receipt":receipt}), false));
        }
        let mut result_verdict = "APPLY";
        match req.operation.as_str() {
            "append" if core::keys(p, "events") => {
                let es = p["events"]
                    .as_array()
                    .filter(|a| !a.is_empty() && a.len() + self.events.len() <= LIMIT)
                    .ok_or_else(err)?;
                let mut next = self.events.clone();
                for e in es {
                    offline(e)?;
                    if let Some(old) = next.iter().find(|old| old["event_id"] == e["event_id"]) {
                        if core::canonical(old) != core::canonical(e) {
                            return Err(err());
                        }
                        continue;
                    }
                    if self
                        .checkpoints
                        .get(e["aggregate_id"].as_str().unwrap_or(""))
                        .is_some_and(|prior| {
                            e["aggregate_type"] == "Execution"
                                && e["payload"]["facts"]
                                    .get("result")
                                    .is_some_and(|r| *r != prior["facts"]["result"])
                        })
                    {
                        return Err("HOLD_UNKNOWN".into());
                    }
                    next.push(e.clone());
                }
                let offline_events: Vec<_> = next.iter().map(offline).collect::<Result<_, _>>()?;
                core::replay(&offline_events)?;
                validate_ledger(&next, self.events.len(), &self.deliveries)?;
                if next.len() == self.events.len() {
                    result_verdict = "DUPLICATE";
                }
                self.events = next;
            }
            "record_delivery" if core::keys(p, "row") => {
                let row = &p["row"];
                // Initial rows are input evidence, never a Host invocation.
                let mut ack = row.clone();
                if !ack.is_object() {
                    return Err(err());
                }
                ack["success"] = json!(true);
                if core::reconcile_ack(row, &ack) != "APPLY"
                    || !matches!(row["delivery_state"].as_str(), Some("PENDING" | "FAILED"))
                {
                    return Err(err());
                }
                let k = key(row);
                if let Some(old) = self.deliveries.get(&k) {
                    if old != row {
                        return Err(err());
                    }
                    result_verdict = "DUPLICATE";
                } else {
                    self.deliveries.insert(k, row.clone());
                }
            }
            "late_ack" if core::keys(p, "ack") => {
                let k = key(&p["ack"]);
                let row = self.deliveries.get_mut(&k).ok_or_else(err)?;
                let v = core::reconcile_ack(row, &p["ack"]);
                if !matches!(v, "APPLY" | "DUPLICATE") {
                    return Err(v.into());
                }
                result_verdict = v;
                if v == "APPLY" {
                    row["delivery_state"] = json!("DELIVERED");
                    row["ack_host_ref"] = row["host_ref"].clone();
                }
            }
            "checkpoint" if core::keys(p, "record") => {
                let r = &p["record"];
                let f = &r["facts"];
                let id = r["id"].as_str().ok_or_else(err)?;
                if r["object"] != "Execution"
                    || r["from"] != "FINALIZING"
                    || r["actor"] != "finalizer"
                    || f["finalization_phase"] != "RESULT_COMMITTED"
                    || core::transition(r) != "HOLD_UNKNOWN"
                {
                    return Err(err());
                }
                // A result checkpoint is evidence, not a terminal bundle or a fresh observation.
                let result = &f["result"];
                if core::guard("binding", f, &r["to"]) != "APPLY"
                    || !present(&result["result_ref"])
                    || result["execution_id"] != f["binding"]["execution_id"]
                    || result["attempt_id"] != f["binding"]["attempt_id"]
                    || result["outcome"] != r["to"]
                {
                    return Err("HOLD_UNKNOWN".into());
                }
                let snapshot = self.snapshot();
                if !snapshot["projection"].as_array().unwrap().iter().any(|s| {
                    s["object"] == "Execution"
                        && s["id"] == id
                        && s["lifecycle"] == r["from"]
                        && s["version"] == r["version"]
                }) {
                    return Err(err());
                }
                if self
                    .checkpoints
                    .get(id)
                    .is_some_and(|prior| prior["facts"]["result"] != f["result"])
                {
                    return Err("HOLD_UNKNOWN".into());
                }
                self.checkpoints.insert(id.to_owned(), r.clone());
                result_verdict = "HOLD_UNKNOWN";
            }
            _ => return Err(err()),
        }
        let result = json!({"verdict":result_verdict,"snapshot":self.snapshot()});
        self.receipts
            .insert(req.request_id.clone(), (h, result.clone()));
        Ok((result, true))
    }
}

pub struct Journal {
    file: File,
    pub state: State,
    position: u64,
    previous: String,
    poisoned: bool,
}
impl Drop for Journal {
    fn drop(&mut self) {
        // Closing the owner's FD alone leaves flock held by a concurrently
        // forked child until exec. Release this journal's lock explicitly;
        // no journal operation is possible once its owner is being dropped.
        let _ = self.file.unlock();
    }
}
impl Journal {
    pub fn open(dir: &Path, initialize: bool) -> Result<Self, String> {
        if initialize {
            fs::create_dir(dir).map_err(|e| e.to_string())?;
            let mut marker = OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(dir.join("LAB_ONLY"))
                .map_err(|e| e.to_string())?;
            marker
                .write_all(MARKER.as_bytes())
                .and_then(|_| marker.sync_all())
                .map_err(|e| e.to_string())?;
            let f = OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(dir.join("events.ndjson"))
                .map_err(|e| e.to_string())?;
            f.sync_all().map_err(|e| e.to_string())?;
            File::open(dir)
                .and_then(|f| f.sync_all())
                .map_err(|e| e.to_string())?;
            if let Some(parent) = dir.parent() {
                File::open(parent)
                    .and_then(|f| f.sync_all())
                    .map_err(|e| e.to_string())?;
            }
        }
        for p in [
            dir.to_path_buf(),
            dir.join("LAB_ONLY"),
            dir.join("events.ndjson"),
        ] {
            if fs::symlink_metadata(&p)
                .map_err(|e| e.to_string())?
                .file_type()
                .is_symlink()
            {
                return Err("symlink rejected".into());
            }
        }
        if fs::read_to_string(dir.join("LAB_ONLY")).map_err(|e| e.to_string())? != MARKER {
            return Err("not a lab journal".into());
        }
        let mut file = OpenOptions::new()
            .read(true)
            .append(true)
            .open(dir.join("events.ndjson"))
            .map_err(|e| e.to_string())?;
        file.try_lock().map_err(|e| e.to_string())?;
        let mut bytes = Vec::new();
        std::io::Read::by_ref(&mut file)
            .take(64 * 1024 * 1024 + 1)
            .read_to_end(&mut bytes)
            .map_err(|e| e.to_string())?;
        if bytes.len() > 64 * 1024 * 1024 || !bytes.is_empty() && bytes.last() != Some(&b'\n') {
            return Err("REJECT: incomplete or oversized journal; no projection published".into());
        }
        let mut state = State::default();
        let mut position = 0;
        let mut previous = String::new();
        for line in bytes
            .strip_suffix(b"\n")
            .unwrap_or(&bytes)
            .split(|b| *b == b'\n')
            .filter(|_| !bytes.is_empty())
        {
            if line.is_empty() {
                return Err(err());
            }
            let mut parser = serde_json::Deserializer::from_slice(line);
            let entry = crate::UniqueJson::deserialize(&mut parser)
                .map_err(|e| e.to_string())?
                .0;
            parser.end().map_err(|e| e.to_string())?;
            if !core::keys(&entry, "position previous request hash")
                || entry["position"] != position + 1
                || entry["previous"] != previous
            {
                return Err(err());
            }
            let body = json!({"position":entry["position"],"previous":entry["previous"],"request":entry["request"]});
            if entry["hash"] != core::digest(&body) {
                return Err(err());
            }
            let req: ProtocolRequest =
                serde_json::from_value(entry["request"].clone()).map_err(|e| e.to_string())?;
            if req.protocol_version != PROTOCOL {
                return Err(err());
            }
            let (_, persist) = state.apply(&req)?;
            if !persist {
                return Err(err());
            }
            position += 1;
            previous = entry["hash"].as_str().unwrap().to_owned();
        }
        Ok(Self {
            file,
            state,
            position,
            previous,
            poisoned: false,
        })
    }
    pub fn execute(&mut self, req: &ProtocolRequest) -> Result<Value, String> {
        if self.poisoned
            || req.protocol_version != PROTOCOL
            || req.request_id.is_empty()
            || req.request_id.len() > 128
        {
            return Err(err());
        }
        let mut next = self.state.clone();
        let (result, persist) = next.apply(req)?;
        if serde_json::to_vec(&result)
            .map_err(|e| e.to_string())?
            .len()
            > crate::MAX_FRAME_BYTES - 512
        {
            return Err(err());
        }
        if persist {
            let body = json!({"position":self.position+1,"previous":self.previous,"request":{"protocol_version":req.protocol_version,"request_id":req.request_id,"operation":req.operation,"payload":req.payload}});
            let h = core::digest(&body);
            let mut entry = body;
            entry["hash"] = json!(h);
            let mut bytes = serde_json::to_vec(&entry).map_err(|e| e.to_string())?;
            bytes.push(b'\n');
            if self.file.metadata().map_err(|e| e.to_string())?.len() + bytes.len() as u64
                > 64 * 1024 * 1024
            {
                return Err(err());
            }
            let crash = std::env::var("CLINX_RUNTIME_LAB_CRASH").unwrap_or_default();
            if crash == "BEFORE_APPEND" {
                std::process::exit(86);
            }
            if crash == "TORN_APPEND" {
                self.file
                    .write_all(&bytes[..bytes.len() / 2])
                    .and_then(|_| self.file.sync_all())
                    .map_err(|e| e.to_string())?;
                std::process::exit(86);
            }
            self.poisoned = true;
            self.file
                .write_all(&bytes)
                .and_then(|_| self.file.sync_all())
                .map_err(|e| e.to_string())?;
            if crash == "AFTER_SYNC" {
                std::process::exit(86);
            }
            self.position += 1;
            self.previous = h;
            self.state = next;
            self.poisoned = false;
        }
        Ok(result)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn journal_drop_releases_lock_even_while_a_child_inherits_the_fd() {
        let parent = tempfile::tempdir().unwrap();
        let dir = parent.path().join("journal");
        let journal = Journal::open(&dir, true).unwrap();
        // Deterministically extend the fork-to-exec inheritance window which
        // parallel command-construction tests can otherwise hit transiently.
        rustix::io::fcntl_setfd(&journal.file, rustix::io::FdFlags::empty()).unwrap();
        let mut child = std::process::Command::new("sleep")
            .arg("2")
            .spawn()
            .unwrap();
        drop(journal);
        let reopened = Journal::open(&dir, false);
        let _ = child.kill();
        child.wait().unwrap();
        assert!(reopened.is_ok(), "journal writer lock outlived its owner");
    }

    fn admission() -> Value {
        json!({"object":"Execution","id":"e1","from":"REQUESTED","to":"ADMITTED",
            "action":"admit","actor":"command","version":0,"next_version":1,
            "facts":{"commit_bundle":["execution","request","prepared","owner","event","receipt","outbox"]}})
    }

    fn versions_leave_journal_unchanged(operation: &str, records: Vec<Value>, verdict: &str) {
        static NEXT: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
        let serial = NEXT.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        let dir = std::env::temp_dir().join(format!(
            "clinx-d002-{operation}-{verdict}-{}-{serial}",
            std::process::id()
        ));
        assert!(!dir.exists());
        let mut journal = Journal::open(&dir, true).unwrap();
        // Populate a receipt and delivery before querying: rejection must preserve
        // the complete existing ledger, rather than just its public projection.
        let seed = ProtocolRequest {
            request_id: "seed".into(),
            protocol_version: PROTOCOL.into(),
            operation: "record_delivery".into(),
            payload: json!({"row":{"execution_id":"e1","attempt_id":"a1","owner_ref":"o1",
                "thread_id":"t1","turn_id":"turn1","generation":3,"call_id":1,
                "namespace":"tools","tool":"host","operation_id":"op1","host_ref":"h1",
                "response_sent":true,"delivery_state":"PENDING"}}),
        };
        journal.execute(&seed).unwrap();
        let before = journal.state.clone();
        let bytes = fs::read(dir.join("events.ndjson")).unwrap();
        for record in records {
            let request = ProtocolRequest {
                request_id: "version-query".into(),
                protocol_version: PROTOCOL.into(),
                operation: operation.into(),
                payload: json!({"record":record}),
            };
            let expected = if operation == "recovery" {
                json!({"verdict":verdict,"classification":if verdict == "APPLY" {
                    "COMMIT_ORIGINAL_TERMINAL_ONLY"
                } else { verdict }})
            } else {
                json!({"verdict":verdict})
            };
            assert_eq!(journal.execute(&request).unwrap(), expected, "{record}");
            assert_eq!(journal.state.snapshot(), before.snapshot());
            assert_eq!(journal.state.events, before.events);
            assert_eq!(journal.state.deliveries, before.deliveries);
            assert_eq!(journal.state.checkpoints, before.checkpoints);
            assert_eq!(journal.state.receipts, before.receipts);
            assert_eq!(fs::read(dir.join("events.ndjson")).unwrap(), bytes);
        }
        drop(journal);
        let reopened = Journal::open(&dir, false).unwrap();
        assert_eq!(reopened.state.snapshot(), before.snapshot());
        assert_eq!(reopened.state.receipts, before.receipts);
        assert_eq!(fs::read(dir.join("events.ndjson")).unwrap(), bytes);
        drop(reopened);
        fs::remove_dir_all(dir).unwrap();
    }

    fn overflow_record() -> Value {
        let mut r = admission();
        r["version"] = json!(i64::MAX);
        r["next_version"] = json!(i64::MAX as u64 + 1);
        r
    }

    #[test]
    fn d002_evaluate_rejects_overflow_without_mutation() {
        versions_leave_journal_unchanged("evaluate", vec![overflow_record()], "REJECT");
    }

    #[test]
    fn d002_recovery_rejects_overflow_without_mutation() {
        versions_leave_journal_unchanged("recovery", vec![overflow_record()], "REJECT");
    }

    #[test]
    fn version_types_ranges_and_missing_values_reject_in_both_operations() {
        let mut records = Vec::new();
        for field in ["version", "next_version"] {
            for value in [
                json!(i64::MIN),
                json!(-1),
                json!(i64::MAX as u64 + 1),
                json!(u64::MAX),
                serde_json::from_str("18446744073709551616").unwrap(),
                json!(1.0),
                json!(0.0),
                json!(true),
                json!(false),
                json!("1"),
                Value::Null,
                json!([]),
                json!({}),
            ] {
                let mut r = admission();
                r[field] = value;
                records.push(r);
            }
            let mut r = admission();
            r.as_object_mut().unwrap().remove(field);
            records.push(r);
        }
        for next in [
            json!(i64::MAX),
            json!(0),
            Value::Null,
            json!(true),
            json!(1.0),
            json!("1"),
            json!(u64::MAX),
        ] {
            let mut r = overflow_record();
            r["next_version"] = next;
            records.push(r);
        }
        records.push(overflow_record());
        for op in ["evaluate", "recovery"] {
            versions_leave_journal_unchanged(op, records.clone(), "REJECT");
        }
    }

    #[test]
    fn legal_integer_boundaries_keep_their_semantics() {
        let mut max = admission();
        max["version"] = json!(i64::MAX - 1);
        max["next_version"] = json!(i64::MAX);
        let mut exact = admission();
        exact["version"] = json!(9007199254740992_i64);
        exact["next_version"] = json!(9007199254740993_i64);
        for op in ["evaluate", "recovery"] {
            versions_leave_journal_unchanged(
                op,
                vec![admission(), exact.clone(), max.clone()],
                "APPLY",
            );
        }
    }

    #[test]
    fn date_validation() {
        assert!(utc(&json!("2024-02-29T00:00:00.000000Z")));
        assert!(!utc(&json!("2025-02-29T00:00:00.000000Z")));
    }
    #[test]
    fn no_path_without_explicit_marker() {
        let d = std::env::temp_dir().join(format!("clinx-lab-negative-{}", std::process::id()));
        assert!(Journal::open(&d, false).is_err());
        assert!(!d.exists());
    }
    #[test]
    fn missing_terminal_cannot_be_supplied_as_release_assertion() {
        let c = core::contract();
        let r = json!({"object":"ResourceAllocation","id":"a1","from":"ACTIVE","to":"RELEASE_PENDING","action":"release","actor":"finalizer","version":0,"next_version":1,"facts":c["contexts"]["release"]});
        assert_eq!(core::transition(&r), "APPLY");
        assert_eq!(
            validate_ledger(&[json!({"payload":r})], 0, &BTreeMap::new()),
            Err("HOLD_UNKNOWN".into())
        );
    }
    #[test]
    fn invalid_append_does_not_write_a_journal_frame() {
        let dir =
            std::env::temp_dir().join(format!("clinx-lab-journal-unit-{}", std::process::id()));
        assert!(!dir.exists());
        {
            let mut journal = Journal::open(&dir, true).unwrap();
            let request = ProtocolRequest {
                request_id: "bad".into(),
                protocol_version: PROTOCOL.into(),
                operation: "append".into(),
                payload: json!({"events":[{}]}),
            };
            assert!(journal.execute(&request).is_err());
            assert_eq!(journal.state.snapshot()["event_count"], 0);
            assert_eq!(fs::metadata(dir.join("events.ndjson")).unwrap().len(), 0);
        }
        assert!(Journal::open(&dir, false).is_ok());
        fs::remove_dir_all(&dir).unwrap();
    }
}
