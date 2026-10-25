//! Native durable adapter for the recovered, frozen execution core.
//!
//! This owns SQLite transactions; it never invokes a Python process. Deployment
//! admission and authenticated Provider evidence are separate integration gates.
use crate::{ProtocolRequest, authority_state::State, execution_core as core};
use rusqlite::{Connection, OpenFlags, OptionalExtension, TransactionBehavior, params};
use serde_json::{Value, json};
use std::{
    fs::{self, File, OpenOptions},
    io::Read,
    os::unix::fs::MetadataExt,
    path::{Path, PathBuf},
    time::Duration,
};

pub const PROTOCOL: &str = "CLINX_RUST_AUTHORITY_V1";
const MAX_LEASE_MS: i64 = 120_000;
type Result<T> = std::result::Result<T, String>;
pub(crate) fn db(e: rusqlite::Error) -> String {
    format!("STORAGE_ERROR:{e}")
}
pub(crate) fn parse(s: String) -> Result<Value> {
    serde_json::from_str(&s).map_err(|_| "CORRUPT_AUTHORITY".into())
}
fn text<'a>(v: &'a Value, key: &str) -> Result<&'a str> {
    v[key]
        .as_str()
        .filter(|s| !s.is_empty() && s.len() <= 256)
        .ok_or_else(|| format!("INVALID_{key}"))
}
pub(crate) fn random_id() -> Result<String> {
    let mut b = [0u8; 32];
    File::open("/dev/urandom")
        .and_then(|mut f| f.read_exact(&mut b))
        .map_err(|e| e.to_string())?;
    Ok(b.iter().map(|b| format!("{b:02x}")).collect())
}
pub(crate) fn clock() -> i64 {
    // Darwin exposes its boot-relative clock as CLOCK_MONOTONIC. Linux
    // Authority lease expiry retains CLOCK_BOOTTIME, including suspend time.
    #[cfg(target_vendor = "apple")]
    let clock = rustix::time::ClockId::Monotonic;
    #[cfg(not(target_vendor = "apple"))]
    let clock = rustix::time::ClockId::Boottime;
    let t = rustix::time::clock_gettime(clock);
    t.tv_sec * 1000 + t.tv_nsec / 1_000_000
}

const SCHEMA: &str = "
CREATE TABLE identity(id TEXT PRIMARY KEY, schema_version INTEGER NOT NULL CHECK(schema_version=1));
CREATE TABLE resources(resource TEXT PRIMARY KEY, epoch INTEGER NOT NULL CHECK(epoch>0));
CREATE TABLE leases(resource TEXT NOT NULL, epoch INTEGER NOT NULL, claim_id TEXT NOT NULL UNIQUE,
 request_hash TEXT NOT NULL, owner TEXT NOT NULL, capability TEXT NOT NULL, boot TEXT NOT NULL,
 expires_ms INTEGER NOT NULL, claim_expires_ms INTEGER NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('ACTIVE','RELEASED')),
 PRIMARY KEY(resource,epoch), FOREIGN KEY(resource) REFERENCES resources(resource));
CREATE TABLE commits(resource TEXT NOT NULL, epoch INTEGER NOT NULL, position INTEGER NOT NULL,
 command_id TEXT NOT NULL, request_hash TEXT NOT NULL, request TEXT NOT NULL, previous TEXT NOT NULL,
 hash TEXT NOT NULL, receipt TEXT NOT NULL, PRIMARY KEY(resource,epoch,position),
 UNIQUE(resource,epoch,command_id), FOREIGN KEY(resource,epoch) REFERENCES leases(resource,epoch));
CREATE TABLE projections(resource TEXT NOT NULL, epoch INTEGER NOT NULL, snapshot TEXT NOT NULL,
 PRIMARY KEY(resource,epoch), FOREIGN KEY(resource,epoch) REFERENCES leases(resource,epoch));
CREATE TABLE renewals(resource TEXT NOT NULL, epoch INTEGER NOT NULL, operation_id TEXT NOT NULL,
 request_hash TEXT NOT NULL, receipt TEXT NOT NULL, PRIMARY KEY(resource,epoch,operation_id),
 FOREIGN KEY(resource,epoch) REFERENCES leases(resource,epoch));
CREATE TABLE outbox(id TEXT PRIMARY KEY, resource TEXT NOT NULL, epoch INTEGER NOT NULL,
 position INTEGER, receipt TEXT NOT NULL,
 FOREIGN KEY(resource,epoch,position) REFERENCES commits(resource,epoch,position));
CREATE TRIGGER immutable_commit_update BEFORE UPDATE ON commits BEGIN SELECT RAISE(ABORT,'immutable commit'); END;
CREATE TRIGGER immutable_commit_delete BEFORE DELETE ON commits BEGIN SELECT RAISE(ABORT,'immutable commit'); END;
CREATE TRIGGER immutable_outbox_update BEFORE UPDATE ON outbox BEGIN SELECT RAISE(ABORT,'immutable outbox'); END;
CREATE TRIGGER immutable_outbox_delete BEFORE DELETE ON outbox BEGIN SELECT RAISE(ABORT,'immutable outbox'); END;
CREATE TRIGGER immutable_renewal_update BEFORE UPDATE ON renewals BEGIN SELECT RAISE(ABORT,'immutable renewal'); END;
CREATE TRIGGER immutable_renewal_delete BEFORE DELETE ON renewals BEGIN SELECT RAISE(ABORT,'immutable renewal'); END;
";

struct WriterLock(File);

impl Drop for WriterLock {
    fn drop(&mut self) {
        // Closing just this fd does not release an OFD lock held across another
        // thread's fork. Explicit release follows the SQLite connection's drop.
        let _ = self.0.unlock();
    }
}

pub struct Store {
    // Rust drops fields in declaration order. Close SQLite before releasing
    // writer ownership, including when unrelated children inherited the fd.
    pub(crate) conn: Connection,
    path: PathBuf,
    inode: (u64, u64),
    _lock: WriterLock,
    pub(crate) boot: String,
    pub identity: String,
}


impl Store {
    /// Explicit fresh-state initialization only. No import or fallback to a legacy DB.
    pub fn open(directory: &Path, initialize: bool) -> Result<Self> {
        let uid = rustix::process::getuid().as_raw();
        let meta = directory.symlink_metadata().map_err(|e| e.to_string())?;
        if !directory.is_absolute()
            || !meta.is_dir()
            || meta.uid() != uid
            || meta.mode() & 0o077 != 0
            || directory.canonicalize().map_err(|e| e.to_string())? != directory
        {
            return Err("PRIVATE_CANONICAL_STATE_DIRECTORY_REQUIRED".into());
        }
        let lock_path = directory.join("writer.lock");
        if lock_path.exists()
            && !lock_path
                .symlink_metadata()
                .map_err(|e| e.to_string())?
                .is_file()
        {
            return Err("INVALID_LOCK_IDENTITY".into());
        }
        let lock = OpenOptions::new()
            .create(true)
            .truncate(false)
            .write(true)
            .open(lock_path)
            .map_err(|e| e.to_string())?;
        lock.try_lock().map_err(|_| "WRITER_ALREADY_ACTIVE")?;
        let lock = WriterLock(lock);
        let path = directory.join("authority.sqlite3");
        if initialize {
            // create_new prevents accidental history reuse or database overwrite.
            OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(&path)
                .and_then(|f| f.sync_all())
                .map_err(|e| e.to_string())?;
        }
        let m = path.symlink_metadata().map_err(|e| e.to_string())?;
        if !m.is_file() || m.uid() != uid || m.nlink() != 1 {
            return Err("INVALID_DATABASE_IDENTITY".into());
        }
        let mut conn = Connection::open_with_flags(
            &path,
            OpenFlags::SQLITE_OPEN_READ_WRITE
                | OpenFlags::SQLITE_OPEN_NO_MUTEX
                | OpenFlags::SQLITE_OPEN_NOFOLLOW,
        )
        .map_err(db)?;
        conn.busy_timeout(Duration::from_secs(5)).map_err(db)?;
        conn.execute_batch("PRAGMA foreign_keys=ON; PRAGMA trusted_schema=OFF; PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;").map_err(db)?;
        if initialize {
            let tx = conn
                .transaction_with_behavior(TransactionBehavior::Immediate)
                .map_err(db)?;
            tx.execute_batch(SCHEMA).map_err(db)?;
            tx.execute_batch(crate::business::SCHEMA).map_err(db)?;
            tx.execute("INSERT INTO identity VALUES(?1,1)", [random_id()?])
                .map_err(db)?;
            tx.commit().map_err(db)?;
            File::open(directory)
                .and_then(|f| f.sync_all())
                .map_err(|e| e.to_string())?;
        }
        let identity: String = conn
            .query_row("SELECT id FROM identity WHERE schema_version=1", [], |r| {
                r.get(0)
            })
            .map_err(db)?;
        if conn
            .query_row("SELECT count(*) FROM identity", [], |r| r.get::<_, i64>(0))
            .map_err(db)?
            != 1
            || conn
                .query_row("PRAGMA quick_check", [], |r| r.get::<_, String>(0))
                .map_err(db)?
                != "ok"
        {
            return Err("CORRUPT_AUTHORITY".into());
        }
        // Complete fallible initialization before moving either resource into
        // the struct: partially initialized struct temporaries drop in reverse.
        let boot =
            fs::read_to_string("/proc/sys/kernel/random/boot_id").map_err(|e| e.to_string())?;
        let store = Self {
            conn,
            path,
            inode: (m.dev(), m.ino()),
            _lock: lock,
            boot,
            identity,
        };
        // Validate every stream and every stored result before serving any projection.
        let mut stmt = store
            .conn
            .prepare("SELECT resource,epoch,state FROM leases")
            .map_err(db)?;
        let keys = stmt
            .query_map([], |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, i64>(1)?,
                    r.get::<_, String>(2)?,
                ))
            })
            .map_err(db)?
            .collect::<std::result::Result<Vec<_>, _>>()
            .map_err(db)?;
        drop(stmt);
        for (r, e, lease_state) in keys {
            let (state, position, _) = replay(&store.conn, &r, e)?;
            if position > 0 {
                let stored: String = store
                    .conn
                    .query_row(
                        "SELECT snapshot FROM projections WHERE resource=? AND epoch=?",
                        params![r, e],
                        |row| row.get(0),
                    )
                    .map_err(db)?;
                if parse(stored)? != state.snapshot()
                    || (lease_state == "RELEASED") != released(&state.snapshot())
                {
                    return Err("CORRUPT_PROJECTION_OR_RELEASE".into());
                }
            } else if lease_state != "ACTIVE" {
                return Err("CORRUPT_RELEASE".into());
            }
        }
        verify_renewals(&store.conn)?;
        Ok(store)
    }

    pub(crate) fn check_identity(&self) -> Result<()> {
        let m = self
            .path
            .symlink_metadata()
            .map_err(|_| "DATABASE_PATH_REPLACED")?;
        if !m.is_file() || (m.dev(), m.ino()) != self.inode {
            return Err("DATABASE_PATH_REPLACED".into());
        }
        Ok(())
    }

    /// Same claim key reads its exact original lease; it never extends it.
    pub fn claim(&mut self, p: &Value) -> Result<Value> {
        self.check_identity()?;
        if !core::keys(p, "resource expected_epoch claim_id owner lease_ms") {
            return Err("INVALID_CLAIM".into());
        }
        let resource = text(p, "resource")?;
        let id = text(p, "claim_id")?;
        let expected = p["expected_epoch"]
            .as_i64()
            .filter(|v| *v >= 0 && *v < i64::MAX)
            .ok_or("INVALID_EPOCH")?;
        let duration = p["lease_ms"]
            .as_i64()
            .filter(|n| *n > 0 && *n <= MAX_LEASE_MS)
            .ok_or("INVALID_LEASE")?;
        let owner = &p["owner"];
        if !core::keys(
            owner,
            "execution_id attempt_id assignment_id worker_id incarnation_id resource_key resource_epoch",
        ) || owner["resource_key"] != resource
            || owner["resource_epoch"] != expected + 1
        {
            return Err("OWNER_IDENTITY_CONFLICT".into());
        }
        for k in [
            "execution_id",
            "attempt_id",
            "assignment_id",
            "worker_id",
            "incarnation_id",
        ] {
            text(owner, k)?;
        }
        let hash = core::digest(p);
        let tx = self
            .conn
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(db)?;
        if let Some((prior,capability,epoch,expires)) = tx.query_row(
            "SELECT request_hash,capability,epoch,claim_expires_ms FROM leases WHERE claim_id=?",[id],
            |r|Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?,r.get::<_,i64>(2)?,r.get::<_,i64>(3)?))).optional().map_err(db)? {
            if prior != hash { return Err("CLAIM_ID_CONFLICT".into()); }
            return Ok(json!({"verdict":"DUPLICATE","resource":resource,"epoch":epoch,"capability":capability,"expires_ms":expires}));
        }
        let prior: Option<i64> = tx
            .query_row(
                "SELECT epoch FROM resources WHERE resource=?",
                [resource],
                |r| r.get(0),
            )
            .optional()
            .map_err(db)?;
        if prior.unwrap_or(0) != expected {
            return Err("EPOCH_CAS_CONFLICT".into());
        }
        if let Some(epoch) = prior {
            let state: String = tx
                .query_row(
                    "SELECT state FROM leases WHERE resource=? AND epoch=?",
                    params![resource, epoch],
                    |r| r.get(0),
                )
                .map_err(db)?;
            // Expiry is a denial/quarantine, never takeover or side-effect termination proof.
            if state != "RELEASED" {
                return Err("RESOURCE_STILL_HELD".into());
            }
        }
        let epoch = expected + 1;
        let capability = random_id()?;
        let expires = clock() + duration;
        tx.execute("INSERT INTO resources VALUES(?,?) ON CONFLICT(resource) DO UPDATE SET epoch=excluded.epoch",params![resource,epoch]).map_err(db)?;
        tx.execute(
            "INSERT INTO leases VALUES(?,?,?,?,?,?,?,?,?,'ACTIVE')",
            params![
                resource,
                epoch,
                id,
                hash,
                core::canonical(owner),
                capability,
                self.boot,
                expires,
                expires
            ],
        )
        .map_err(db)?;
        tx.commit().map_err(db)?;
        Ok(
            json!({"verdict":"APPLY","resource":resource,"epoch":epoch,"capability":capability,"expires_ms":expires}),
        )
    }

    pub fn execute(&mut self, p: &Value) -> Result<Value> {
        self.execute_at(p, |_| Ok(()))
    }

    /// Fault injection belongs to tests, never to production environment variables.
    pub fn execute_at(
        &mut self,
        p: &Value,
        mut fault: impl FnMut(&str) -> Result<()>,
    ) -> Result<Value> {
        self.check_identity()?;
        if !core::keys(p, "resource epoch capability command") {
            return Err("INVALID_ENVELOPE".into());
        }
        let resource = text(p, "resource")?;
        let epoch = p["epoch"]
            .as_i64()
            .filter(|e| *e > 0)
            .ok_or("INVALID_EPOCH")?;
        let capability = text(p, "capability")?;
        let req: ProtocolRequest =
            serde_json::from_value(p["command"].clone()).map_err(|_| "INVALID_COMMAND")?;
        if req.protocol_version != PROTOCOL
            || req.request_id.is_empty()
            || req.request_id.len() > 128
            || !matches!(
                req.operation.as_str(),
                "append" | "checkpoint" | "record_delivery" | "late_ack"
            )
        {
            return Err("INVALID_COMMAND".into());
        }
        let tx = self
            .conn
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(db)?;
        let (owner, token, boot, expires, lifecycle, current): (String,String,String,i64,String,i64) = tx.query_row(
            "SELECT l.owner,l.capability,l.boot,l.expires_ms,l.state,r.epoch FROM leases l JOIN resources r USING(resource) WHERE l.resource=? AND l.epoch=?",
            params![resource,epoch],|r| Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?))).optional().map_err(db)?.ok_or("FENCED")?;
        // Fence before idempotency: an old lease never becomes current by retrying.
        if token != capability
            || boot != self.boot
            || clock() >= expires
            || lifecycle != "ACTIVE"
            || current != epoch
        {
            return Err("FENCED".into());
        }
        let owner = parse(owner)?;
        validate_scope(&req, &owner)?;
        let request_hash = core::digest(&json!({"operation":req.operation,"payload":req.payload}));
        if tx
            .query_row(
                "SELECT 1 FROM renewals WHERE resource=? AND epoch=? AND operation_id=?",
                params![resource, epoch, req.request_id],
                |r| r.get::<_, i64>(0),
            )
            .optional()
            .map_err(db)?
            .is_some()
        {
            return Err("OPERATION_ID_CONFLICT".into());
        }
        if let Some((hash,receipt)) = tx.query_row("SELECT request_hash,receipt FROM commits WHERE resource=? AND epoch=? AND command_id=?",params![resource,epoch,req.request_id],|r|Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?))).optional().map_err(db)? {
            if request_hash != hash { return Err("OPERATION_ID_CONFLICT".into()); }
            return Ok(json!({"verdict":"DUPLICATE","receipt":parse(receipt)?}));
        }
        let receipt = commit_core(
            &tx,
            &self.identity,
            resource,
            epoch,
            &req,
            &p["command"],
            &mut fault,
        )?;
        fault("BEFORE_SQL_COMMIT")?;
        tx.commit().map_err(db)?;
        fault("AFTER_SQL_COMMIT")?;
        Ok(json!({"verdict":"COMMITTED","receipt":receipt}))
    }

    /// Historical receipt readback grants no lease and executes no mutation.
    pub fn read_commit(&self, p: &Value) -> Result<Value> {
        self.check_identity()?;
        if !core::keys(p, "resource epoch capability operation_id") {
            return Err("INVALID_READ".into());
        }
        let resource = text(p, "resource")?;
        let epoch = p["epoch"].as_i64().ok_or("INVALID_EPOCH")?;
        let token: String = self
            .conn
            .query_row(
                "SELECT capability FROM leases WHERE resource=? AND epoch=?",
                params![resource, epoch],
                |r| r.get(0),
            )
            .optional()
            .map_err(db)?
            .ok_or("FENCED")?;
        if token != text(p, "capability")? {
            return Err("FENCED".into());
        }
        let receipt: Option<String> = self.conn.query_row("SELECT receipt FROM commits WHERE resource=?1 AND epoch=?2 AND command_id=?3 UNION ALL SELECT receipt FROM renewals WHERE resource=?1 AND epoch=?2 AND operation_id=?3",params![resource,epoch,text(p,"operation_id")?],|r|r.get(0)).optional().map_err(db)?;
        match receipt {
            Some(r) => Ok(json!({"verdict":"COMMITTED","receipt":parse(r)?})),
            None => Ok(json!({"verdict":"NOT_FOUND"})),
        }
    }

    pub fn renew(&mut self, p: &Value) -> Result<Value> {
        self.check_identity()?;
        if !core::keys(p, "resource epoch capability operation_id lease_ms") {
            return Err("INVALID_RENEWAL".into());
        }
        let r = text(p, "resource")?;
        let e = p["epoch"].as_i64().ok_or("INVALID_EPOCH")?;
        let op = text(p, "operation_id")?;
        let duration = p["lease_ms"]
            .as_i64()
            .filter(|n| *n > 0 && *n <= MAX_LEASE_MS)
            .ok_or("INVALID_LEASE")?;
        let tx = self
            .conn
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(db)?;
        let (cap,boot,expiry,state,epoch): (String,String,i64,String,i64) = tx.query_row(
            "SELECT l.capability,l.boot,l.expires_ms,l.state,r.epoch FROM leases l JOIN resources r USING(resource) WHERE l.resource=? AND l.epoch=?",params![r,e],|row|Ok((row.get(0)?,row.get(1)?,row.get(2)?,row.get(3)?,row.get(4)?))).optional().map_err(db)?.ok_or("FENCED")?;
        let now = clock();
        if cap != text(p, "capability")?
            || boot != self.boot
            || now >= expiry
            || state != "ACTIVE"
            || epoch != e
        {
            return Err("FENCED".into());
        }
        let request_hash =
            core::digest(&json!({"resource":r,"epoch":e,"operation_id":op,"lease_ms":duration}));
        if tx
            .query_row(
                "SELECT 1 FROM commits WHERE resource=? AND epoch=? AND command_id=?",
                params![r, e, op],
                |row| row.get::<_, i64>(0),
            )
            .optional()
            .map_err(db)?
            .is_some()
        {
            return Err("OPERATION_ID_CONFLICT".into());
        }
        if let Some((hash,receipt)) = tx.query_row("SELECT request_hash,receipt FROM renewals WHERE resource=? AND epoch=? AND operation_id=?",params![r,e,op],|row|Ok((row.get::<_,String>(0)?,row.get::<_,String>(1)?))).optional().map_err(db)? {
            if hash != request_hash { return Err("OPERATION_ID_CONFLICT".into()); }
            return Ok(json!({"verdict":"DUPLICATE","receipt":parse(receipt)?}));
        }
        let expires = expiry.max(now + duration);
        let receipt = json!({"authority_id":self.identity,"resource":r,"epoch":e,"operation_id":op,"operation":"renew","request_hash":request_hash,"trusted_ms":now,"boot":boot,"expires_ms":expires});
        let hash = core::digest(&receipt);
        tx.execute(
            "UPDATE leases SET expires_ms=? WHERE resource=? AND epoch=?",
            params![expires, r, e],
        )
        .map_err(db)?;
        tx.execute(
            "INSERT INTO renewals VALUES(?,?,?,?,?)",
            params![r, e, op, request_hash, core::canonical(&receipt)],
        )
        .map_err(db)?;
        tx.execute(
            "INSERT INTO outbox VALUES(?,?,?,NULL,?)",
            params![hash, r, e, core::canonical(&receipt)],
        )
        .map_err(db)?;
        tx.commit().map_err(db)?;
        Ok(json!({"verdict":"COMMITTED","receipt":receipt}))
    }
}

fn released(snapshot: &Value) -> bool {
    snapshot["projection"].as_array().is_some_and(|rows| {
        rows.iter()
            .any(|r| r["object"] == "ResourceAllocation" && r["lifecycle"] == "RELEASED")
    })
}

fn validate_scope(req: &ProtocolRequest, owner: &Value) -> Result<()> {
    let check = |r: &Value| -> Result<()> {
        if r["object"] == "Execution" && r["id"] != owner["execution_id"] {
            return Err("EXECUTION_IDENTITY_CONFLICT".into());
        }
        if r["object"] == "Attempt" && r["id"] != owner["attempt_id"] {
            return Err("ATTEMPT_IDENTITY_CONFLICT".into());
        }
        let f = &r["facts"];
        for k in ["stored_owner", "presented_owner"] {
            if f.get(k).is_some_and(|v| v != owner) {
                return Err("OWNER_IDENTITY_CONFLICT".into());
            }
        }
        for k in ["binding", "result"] {
            if let Some(v) = f.get(k)
                && (v["execution_id"] != owner["execution_id"]
                    || v["attempt_id"] != owner["attempt_id"])
            {
                return Err("EXECUTION_IDENTITY_CONFLICT".into());
            }
        }
        if f.get("lease_execution_id")
            .is_some_and(|v| *v != owner["execution_id"])
            || f.get("lease_attempt_id")
                .is_some_and(|v| *v != owner["attempt_id"])
        {
            return Err("LEASE_IDENTITY_CONFLICT".into());
        }
        Ok(())
    };
    match req.operation.as_str() {
        "append" => {
            for e in req.payload["events"].as_array().ok_or("INVALID_EVENTS")? {
                check(&e["payload"])?;
            }
        }
        "finalize_before_send" => {
            if req.payload["execution_id"] != owner["execution_id"]
                || req.payload["attempt_id"] != owner["attempt_id"]
            {
                return Err("EXECUTION_IDENTITY_CONFLICT".into());
            }
        }
        "checkpoint" => check(&req.payload["record"])?,
        "record_delivery" | "late_ack" => {
            let row = &req.payload[if req.operation == "late_ack" {
                "ack"
            } else {
                "row"
            }];
            if row["execution_id"] != owner["execution_id"]
                || row["attempt_id"] != owner["attempt_id"]
            {
                return Err("DELIVERY_IDENTITY_CONFLICT".into());
            }
        }
        _ => return Err("INVALID_COMMAND".into()),
    }
    Ok(())
}

pub(crate) fn replay(
    conn: &Connection,
    resource: &str,
    epoch: i64,
) -> Result<(State, i64, String)> {
    let mut state = State::default();
    let mut position = 0;
    let mut previous = String::new();
    let authority: String = conn
        .query_row("SELECT id FROM identity", [], |r| r.get(0))
        .map_err(db)?;
    let owner = parse(
        conn.query_row(
            "SELECT owner FROM leases WHERE resource=? AND epoch=?",
            params![resource, epoch],
            |r| r.get(0),
        )
        .map_err(db)?,
    )?;
    let mut stmt = conn.prepare("SELECT position,command_id,request_hash,request,previous,hash,receipt FROM commits WHERE resource=? AND epoch=? ORDER BY position").map_err(db)?;
    let mut rows = stmt.query(params![resource, epoch]).map_err(db)?;
    while let Some(row) = rows.next().map_err(db)? {
        position += 1;
        let command = parse(row.get(3).map_err(db)?)?;
        let req: ProtocolRequest =
            serde_json::from_value(command.clone()).map_err(|_| "CORRUPT_COMMAND")?;
        validate_scope(&req, &owner)?;
        let request_hash = core::digest(&json!({"operation":req.operation,"payload":req.payload}));
        if row.get::<_, i64>(0).map_err(db)? != position
            || row.get::<_, String>(1).map_err(db)? != req.request_id
            || row.get::<_, String>(2).map_err(db)? != request_hash
            || req.protocol_version != PROTOCOL
            || row.get::<_, String>(4).map_err(db)? != previous
        {
            return Err("CORRUPT_COMMIT_CHAIN".into());
        }
        let (result, persist) = state.apply(&req)?;
        if !persist {
            return Err("CORRUPT_DUPLICATE_COMMIT".into());
        }
        let hash = core::digest(
            &json!({"resource":resource,"epoch":epoch,"position":position,"previous":previous,"request":command,"result":result}),
        );
        let receipt = json!({"authority_id":authority,"resource":resource,"epoch":epoch,"operation_id":req.request_id,"position":position,"hash":hash,"result":result});
        let outbox: String = conn
            .query_row(
                "SELECT receipt FROM outbox WHERE id=? AND resource=? AND epoch=? AND position=?",
                params![hash, resource, epoch, position],
                |r| r.get(0),
            )
            .map_err(db)?;
        if row.get::<_, String>(5).map_err(db)? != hash
            || parse(row.get(6).map_err(db)?)? != receipt
            || parse(outbox)? != receipt
        {
            return Err("CORRUPT_COMMIT_BUNDLE".into());
        }
        previous = hash;
    }
    Ok((state, position, previous))
}

fn verify_renewals(conn: &Connection) -> Result<()> {
    let mut stmt = conn
        .prepare("SELECT resource,epoch,operation_id,request_hash,receipt FROM renewals")
        .map_err(db)?;
    let mut rows = stmt.query([]).map_err(db)?;
    while let Some(row) = rows.next().map_err(db)? {
        let resource: String = row.get(0).map_err(db)?;
        let epoch: i64 = row.get(1).map_err(db)?;
        let op: String = row.get(2).map_err(db)?;
        let hash: String = row.get(3).map_err(db)?;
        let receipt = parse(row.get(4).map_err(db)?)?;
        let stored: String = conn.query_row("SELECT receipt FROM outbox WHERE id=? AND resource=? AND epoch=? AND position IS NULL",params![core::digest(&receipt),resource,epoch],|r|r.get(0)).map_err(db)?;
        let boot: String = conn
            .query_row(
                "SELECT boot FROM leases WHERE resource=? AND epoch=?",
                params![resource, epoch],
                |r| r.get(0),
            )
            .map_err(db)?;
        if receipt["resource"] != resource
            || receipt["epoch"] != epoch
            || receipt["operation_id"] != op
            || receipt["operation"] != "renew"
            || receipt["request_hash"] != hash
            || receipt["boot"] != boot
            || parse(stored)? != receipt
        {
            return Err("CORRUPT_RENEWAL".into());
        }
    }
    let bad: i64 = conn.query_row("SELECT count(*) FROM leases l WHERE expires_ms != max(claim_expires_ms, coalesce((SELECT max(json_extract(receipt,'$.expires_ms')) FROM renewals n WHERE n.resource=l.resource AND n.epoch=l.epoch),claim_expires_ms))",[],|r|r.get(0)).map_err(db)?;
    let counts: (i64,i64) = conn.query_row("SELECT (SELECT count(*) FROM outbox), (SELECT count(*) FROM commits)+(SELECT count(*) FROM renewals)",[],|r|Ok((r.get(0)?,r.get(1)?))).map_err(db)?;
    let foreign_errors: i64 = conn
        .query_row("SELECT count(*) FROM pragma_foreign_key_check", [], |r| {
            r.get(0)
        })
        .map_err(db)?;
    if bad != 0 || counts.0 != counts.1 || foreign_errors != 0 {
        return Err("CORRUPT_AUTHORITY_BUNDLE".into());
    }
    Ok(())
}

/// Commit a core command inside the caller's transaction. Only crate-owned domain
/// code may bypass worker admission; this function never commits the transaction.
pub(crate) fn commit_core(
    tx: &Connection,
    authority: &str,
    resource: &str,
    epoch: i64,
    req: &ProtocolRequest,
    command: &Value,
    fault: &mut impl FnMut(&str) -> Result<()>,
) -> Result<Value> {
    let request_hash = core::digest(&json!({"operation":req.operation,"payload":req.payload}));
    let (mut state, position, previous) = replay(tx, resource, epoch)?;
    let (result, persist) = state.apply(req)?;
    if !persist {
        return Err("UNEXPECTED_NONCOMMIT".into());
    }
    let position = position + 1;
    let body = json!({"resource":resource,"epoch":epoch,"position":position,"previous":previous,"request":command,"result":result});
    let hash = core::digest(&body);
    let receipt = json!({"authority_id":authority,"resource":resource,"epoch":epoch,"operation_id":req.request_id,"position":position,"hash":hash,"result":result});
    if core::canonical(&receipt).len() > crate::MAX_FRAME_BYTES - 1024 {
        return Err("RECEIPT_CAPACITY_EXCEEDED".into());
    }
    fault("BEFORE_COMMIT_ROWS")?;
    tx.execute(
        "INSERT INTO commits VALUES(?,?,?,?,?,?,?,?,?)",
        params![
            resource,
            epoch,
            position,
            req.request_id,
            request_hash,
            core::canonical(command),
            previous,
            hash,
            core::canonical(&receipt)
        ],
    )
    .map_err(db)?;
    fault("AFTER_COMMIT_ROWS")?;
    tx.execute("INSERT INTO projections VALUES(?,?,?) ON CONFLICT(resource,epoch) DO UPDATE SET snapshot=excluded.snapshot",params![resource,epoch,core::canonical(&state.snapshot())]).map_err(db)?;
    tx.execute(
        "INSERT INTO outbox VALUES(?,?,?,?,?)",
        params![hash, resource, epoch, position, core::canonical(&receipt)],
    )
    .map_err(db)?;
    if released(&state.snapshot()) {
        tx.execute(
            "UPDATE leases SET state='RELEASED' WHERE resource=? AND epoch=? AND state='ACTIVE'",
            params![resource, epoch],
        )
        .map_err(db)?;
    }
    Ok(receipt)
}

#[cfg(test)]
mod writer_lock_regression {
    use super::*;
    use std::os::unix::fs::PermissionsExt;
    use std::process::{Command, Stdio};

    #[test]
    fn closed_store_releases_writer_lock_while_unrelated_child_keeps_inherited_fd() {
        // Match the production state contract regardless of the runner's umask
        // or a platform temp-directory symlink. Keep the writer-lock assertions.
        let directory = tempfile::Builder::new()
            .permissions(fs::Permissions::from_mode(0o700))
            .tempdir()
            .unwrap();
        let path = directory.path().canonicalize().unwrap();
        let store = Store::open(&path, true).unwrap();
        assert_eq!(
            Store::open(&path, false).err().unwrap(),
            "WRITER_ALREADY_ACTIVE"
        );
        // Deterministically hold the same open-file description across a child,
        // as a concurrent fork before exec can do even with CLOEXEC on the fd.
        let mut child = Command::new("/bin/sh")
            .args(["-c", "read value"])
            .stdin(Stdio::piped())
            .stdout(Stdio::from(store._lock.0.try_clone().unwrap()))
            .spawn()
            .unwrap();
        drop(store);
        let reopened = Store::open(&path, false);
        let child_was_alive = child.try_wait().unwrap().is_none();
        child.kill().unwrap();
        child.wait().unwrap();
        assert!(child_was_alive);
        assert!(reopened.is_ok(), "{:?}", reopened.err());
    }
}
