#![forbid(unsafe_code)]

use serde::de::{self, MapAccess, SeqAccess, Visitor};
use serde::{Deserialize, Deserializer, Serialize};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;
use std::fmt;

pub const PROTOCOL_VERSION: &str = "CLINX_KERNEL_V1";
pub const AUTHORITY: &str = "NON_AUTHORITATIVE";
pub const MAX_FRAME_BYTES: usize = 1_048_576;
pub const MAX_EVENTS: usize = 1_024;
pub const MAX_JSON_DEPTH: usize = 32;
pub const MAX_REQUESTS_PER_PROCESS: usize = 4_096;
pub const MAX_SAFE_INTEGER: u64 = 9_007_199_254_740_991;

const EVENT_FAMILY: &str = "RUNTIME_WORKER_V1";

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ProtocolRequest {
    pub request_id: String,
    pub protocol_version: String,
    pub operation: String,
    pub payload: Value,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ProtocolError {
    pub code: String,
    pub message: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ProtocolResponse {
    pub request_id: String,
    pub protocol_version: String,
    pub ok: bool,
    pub authority: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub result: Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<ProtocolError>,
}

impl ProtocolResponse {
    pub fn success(request_id: impl Into<String>, result: Value) -> Self {
        Self {
            request_id: request_id.into(),
            protocol_version: PROTOCOL_VERSION.to_owned(),
            ok: true,
            authority: AUTHORITY.to_owned(),
            result: Some(result),
            error: None,
        }
    }

    pub fn failure(
        request_id: impl Into<String>,
        code: impl Into<String>,
        message: impl Into<String>,
    ) -> Self {
        Self {
            request_id: request_id.into(),
            protocol_version: PROTOCOL_VERSION.to_owned(),
            ok: false,
            authority: AUTHORITY.to_owned(),
            result: None,
            error: Some(ProtocolError {
                code: code.into(),
                message: message.into(),
            }),
        }
    }
}

struct UniqueJson(Value);

impl<'de> Deserialize<'de> for UniqueJson {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        deserializer.deserialize_any(UniqueJsonVisitor)
    }
}

struct UniqueJsonVisitor;

impl<'de> Visitor<'de> for UniqueJsonVisitor {
    type Value = UniqueJson;

    fn expecting(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("a JSON value without duplicate object fields")
    }

    fn visit_bool<E>(self, value: bool) -> Result<Self::Value, E> {
        Ok(UniqueJson(Value::Bool(value)))
    }

    fn visit_i64<E>(self, value: i64) -> Result<Self::Value, E> {
        Ok(UniqueJson(Value::Number(value.into())))
    }

    fn visit_u64<E>(self, value: u64) -> Result<Self::Value, E> {
        Ok(UniqueJson(Value::Number(value.into())))
    }

    fn visit_f64<E>(self, value: f64) -> Result<Self::Value, E>
    where
        E: de::Error,
    {
        serde_json::Number::from_f64(value)
            .map(Value::Number)
            .map(UniqueJson)
            .ok_or_else(|| E::custom("JSON number must be finite"))
    }

    fn visit_str<E>(self, value: &str) -> Result<Self::Value, E>
    where
        E: de::Error,
    {
        Ok(UniqueJson(Value::String(value.to_owned())))
    }

    fn visit_string<E>(self, value: String) -> Result<Self::Value, E> {
        Ok(UniqueJson(Value::String(value)))
    }

    fn visit_none<E>(self) -> Result<Self::Value, E> {
        Ok(UniqueJson(Value::Null))
    }

    fn visit_unit<E>(self) -> Result<Self::Value, E> {
        Ok(UniqueJson(Value::Null))
    }

    fn visit_some<D>(self, deserializer: D) -> Result<Self::Value, D::Error>
    where
        D: Deserializer<'de>,
    {
        UniqueJson::deserialize(deserializer)
    }

    fn visit_seq<A>(self, mut sequence: A) -> Result<Self::Value, A::Error>
    where
        A: SeqAccess<'de>,
    {
        let mut values = Vec::new();
        while let Some(value) = sequence.next_element::<UniqueJson>()? {
            values.push(value.0);
        }
        Ok(UniqueJson(Value::Array(values)))
    }

    fn visit_map<A>(self, mut mapping: A) -> Result<Self::Value, A::Error>
    where
        A: MapAccess<'de>,
    {
        let mut values = Map::new();
        while let Some((key, value)) = mapping.next_entry::<String, UniqueJson>()? {
            if values.insert(key.clone(), value.0).is_some() {
                return Err(de::Error::custom(format!("duplicate JSON field: {key}")));
            }
        }
        Ok(UniqueJson(Value::Object(values)))
    }
}

pub fn parse_protocol_request(frame: &[u8]) -> Result<ProtocolRequest, serde_json::Error> {
    let mut deserializer = serde_json::Deserializer::from_slice(frame);
    let parsed = UniqueJson::deserialize(&mut deserializer)?;
    deserializer.end()?;
    serde_json::from_value(parsed.0)
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct KernelError {
    pub code: &'static str,
    pub message: String,
}

impl KernelError {
    fn new(code: &'static str, message: impl Into<String>) -> Self {
        Self {
            code,
            message: message.into(),
        }
    }
}

impl fmt::Display for KernelError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "{}: {}", self.code, self.message)
    }
}

impl std::error::Error for KernelError {}

pub fn execute_request(request: ProtocolRequest) -> ProtocolResponse {
    let request_id = request.request_id.clone();
    if request_id.is_empty() || request_id.len() > 128 {
        return ProtocolResponse::failure(
            request_id,
            "INVALID_REQUEST_ID",
            "request_id must contain 1..128 bytes",
        );
    }
    if request.protocol_version != PROTOCOL_VERSION {
        return ProtocolResponse::failure(
            request_id,
            "UNSUPPORTED_PROTOCOL",
            format!("expected protocol_version {PROTOCOL_VERSION}"),
        );
    }
    if json_depth(&request.payload) > MAX_JSON_DEPTH {
        return ProtocolResponse::failure(
            request_id,
            "INPUT_LIMIT_EXCEEDED",
            format!("payload nesting exceeds {MAX_JSON_DEPTH}"),
        );
    }
    let result = match request.operation.as_str() {
        "evaluate_ownership" => evaluate_ownership(&request.payload),
        "replay_assignment" => replay_assignment(&request.payload),
        _ => Err(KernelError::new(
            "UNKNOWN_OPERATION",
            "operation is not supported by this prototype",
        )),
    };
    match result {
        Ok(value) => ProtocolResponse::success(request_id, value),
        Err(error) => ProtocolResponse::failure(request_id, error.code, error.message),
    }
}

fn json_depth(value: &Value) -> usize {
    match value {
        Value::Array(values) => 1 + values.iter().map(json_depth).max().unwrap_or(0),
        Value::Object(values) => 1 + values.values().map(json_depth).max().unwrap_or(0),
        _ => 1,
    }
}

fn object<'a>(value: &'a Value, context: &str) -> Result<&'a Map<String, Value>, KernelError> {
    value.as_object().ok_or_else(|| {
        KernelError::new("INVALID_INPUT", format!("{context} must be a JSON object"))
    })
}

fn path_value<'a>(root: &'a Value, path: &str) -> Option<&'a Value> {
    let mut current = root;
    for component in path.split('.') {
        current = current.as_object()?.get(component)?;
    }
    Some(current)
}

fn incomplete(path: &str, missing: &mut Vec<String>) -> Option<()> {
    missing.push(path.to_owned());
    None
}

fn required_text<'a>(
    root: &'a Value,
    path: &str,
    missing: &mut Vec<String>,
) -> Result<Option<&'a str>, KernelError> {
    let Some(value) = path_value(root, path) else {
        return Ok(incomplete(path, missing).map(|_| ""));
    };
    if value.is_null() || value.as_str() == Some("UNKNOWN") {
        return Ok(incomplete(path, missing).map(|_| ""));
    }
    let text = value
        .as_str()
        .ok_or_else(|| KernelError::new("INVALID_INPUT", format!("{path} must be text")))?;
    if text.is_empty() || text.len() > 512 {
        return Err(KernelError::new(
            "INVALID_INPUT",
            format!("{path} must contain 1..512 bytes"),
        ));
    }
    Ok(Some(text))
}

fn required_integer(
    root: &Value,
    path: &str,
    missing: &mut Vec<String>,
) -> Result<Option<u64>, KernelError> {
    let Some(value) = path_value(root, path) else {
        return Ok(incomplete(path, missing).map(|_| 0));
    };
    if value.is_null() || value.as_str() == Some("UNKNOWN") {
        return Ok(incomplete(path, missing).map(|_| 0));
    }
    let number = value.as_u64().ok_or_else(|| {
        KernelError::new(
            "INVALID_INPUT",
            format!("{path} must be a non-negative integer"),
        )
    })?;
    if number > MAX_SAFE_INTEGER {
        return Err(KernelError::new(
            "INTEGER_OUT_OF_RANGE",
            format!("{path} exceeds {MAX_SAFE_INTEGER}"),
        ));
    }
    Ok(Some(number))
}

fn required_timestamp<'a>(
    root: &'a Value,
    path: &str,
    missing: &mut Vec<String>,
) -> Result<Option<&'a str>, KernelError> {
    let value = required_text(root, path, missing)?;
    if let Some(timestamp) = value {
        validate_timestamp(timestamp).map_err(|_| {
            KernelError::new(
                "INVALID_INPUT",
                format!("{path} must be canonical UTC with microsecond precision"),
            )
        })?;
    }
    Ok(value)
}

fn validate_timestamp(value: &str) -> Result<(), ()> {
    let bytes = value.as_bytes();
    if bytes.len() != 32
        || bytes[4] != b'-'
        || bytes[7] != b'-'
        || bytes[10] != b'T'
        || bytes[13] != b':'
        || bytes[16] != b':'
        || bytes[19] != b'.'
        || &bytes[26..] != b"+00:00"
    {
        return Err(());
    }
    for (index, byte) in bytes.iter().enumerate() {
        if matches!(index, 4 | 7 | 10 | 13 | 16 | 19 | 26 | 29) {
            continue;
        }
        if !byte.is_ascii_digit() {
            return Err(());
        }
    }
    let number = |range: std::ops::Range<usize>| -> Result<u32, ()> {
        std::str::from_utf8(&bytes[range])
            .map_err(|_| ())?
            .parse::<u32>()
            .map_err(|_| ())
    };
    let year = number(0..4)?;
    let month = number(5..7)?;
    let day = number(8..10)?;
    let hour = number(11..13)?;
    let minute = number(14..16)?;
    let second = number(17..19)?;
    let leap_year =
        year.is_multiple_of(4) && (!year.is_multiple_of(100) || year.is_multiple_of(400));
    let days_in_month = match month {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 if leap_year => 29,
        2 => 28,
        _ => 0,
    };
    if year == 0 || day == 0 || day > days_in_month || hour > 23 || minute > 59 || second > 59 {
        return Err(());
    }
    Ok(())
}

fn ownership_decision(decision: &str, reason_code: &str, details: Value) -> Value {
    json!({
        "authority": AUTHORITY,
        "decision": decision,
        "reason_code": reason_code,
        "details": details,
    })
}

pub fn evaluate_ownership(payload: &Value) -> Result<Value, KernelError> {
    let root = object(payload, "payload")?;
    if root.keys().any(|key| key != "snapshot" && key != "request") {
        return Err(KernelError::new(
            "INVALID_INPUT",
            "evaluate_ownership payload contains unknown fields",
        ));
    }
    let snapshot = root
        .get("snapshot")
        .ok_or_else(|| KernelError::new("INVALID_INPUT", "snapshot is required"))?;
    let request = root
        .get("request")
        .ok_or_else(|| KernelError::new("INVALID_INPUT", "request is required"))?;
    object(snapshot, "snapshot")?;
    object(request, "request")?;

    let required_text_paths = [
        "execution.execution_id",
        "execution.task_id",
        "execution.lifecycle",
        "attempt.attempt_id",
        "attempt.execution_id",
        "attempt.lifecycle",
        "worker.worker_id",
        "worker.lifecycle",
        "worker.current_incarnation_id",
        "incarnation.incarnation_id",
        "incarnation.worker_id",
        "incarnation.lifecycle",
        "assignment.assignment_id",
        "assignment.attempt_id",
        "assignment.worker_id",
        "assignment.incarnation_id",
        "assignment.resource_key",
        "assignment.lifecycle",
        "allocation.allocation_id",
        "allocation.assignment_id",
        "allocation.attempt_id",
        "allocation.worker_id",
        "allocation.incarnation_id",
        "allocation.resource_key",
        "allocation.lifecycle",
        "resource.resource_key",
        "clock.source",
        "safety_handoff.state",
        "safety_handoff.assignment_id",
        "safety_handoff.worker_id",
        "safety_handoff.incarnation_id",
        "safety_handoff.attempt_id",
        "safety_handoff.resource_key",
    ];
    let required_integer_paths = [
        "assignment.resource_epoch",
        "assignment.version",
        "allocation.resource_epoch",
        "allocation.version",
        "resource.fencing_epoch",
        "resource.version",
        "safety_handoff.resource_epoch",
    ];
    let required_timestamp_paths = [
        "assignment.lease_expires_at",
        "allocation.expires_at",
        "clock.now",
        "clock.watermark",
    ];
    let request_text_paths = [
        "execution_id",
        "attempt_id",
        "worker_id",
        "incarnation_id",
        "assignment_id",
        "allocation_id",
        "resource_key",
    ];
    let request_integer_paths = [
        "resource_epoch",
        "expected_assignment_version",
        "expected_allocation_version",
        "expected_resource_version",
    ];

    let mut missing = Vec::new();
    for path in required_text_paths {
        required_text(snapshot, path, &mut missing)?;
    }
    for path in required_integer_paths {
        required_integer(snapshot, path, &mut missing)?;
    }
    for path in required_timestamp_paths {
        required_timestamp(snapshot, path, &mut missing)?;
    }
    for path in request_text_paths {
        required_text(request, path, &mut missing)?;
    }
    for path in request_integer_paths {
        required_integer(request, path, &mut missing)?;
    }
    if !missing.is_empty() {
        missing.sort();
        missing.dedup();
        return Ok(ownership_decision(
            "INSUFFICIENT_EVIDENCE",
            "EVIDENCE_INCOMPLETE",
            json!({"missing_or_unknown": missing}),
        ));
    }

    let s = |path: &str| path_value(snapshot, path).and_then(Value::as_str).unwrap();
    let r = |path: &str| path_value(request, path).and_then(Value::as_str).unwrap();
    let si = |path: &str| path_value(snapshot, path).and_then(Value::as_u64).unwrap();
    let ri = |path: &str| path_value(request, path).and_then(Value::as_u64).unwrap();

    let reject = |reason: &str, path: &str| {
        ownership_decision("REJECT_CANDIDATE", reason, json!({"failed_field": path}))
    };

    if s("execution.execution_id") != r("execution_id") {
        return Ok(reject("EXECUTION_ID_MISMATCH", "execution.execution_id"));
    }
    if s("execution.lifecycle") == "TERMINAL" {
        return Ok(reject("EXECUTION_NOT_LIVE", "execution.lifecycle"));
    }
    if s("attempt.execution_id") != s("execution.execution_id")
        || s("attempt.attempt_id") != r("attempt_id")
    {
        return Ok(reject("ATTEMPT_EXECUTION_MISMATCH", "attempt.execution_id"));
    }
    if s("attempt.lifecycle") != "ASSIGNED" {
        return Ok(reject("ATTEMPT_NOT_ASSIGNED", "attempt.lifecycle"));
    }
    if s("worker.worker_id") != r("worker_id") {
        return Ok(reject("WORKER_ID_MISMATCH", "worker.worker_id"));
    }
    if s("worker.lifecycle") != "REGISTERED" {
        return Ok(reject("WORKER_NOT_ACTIVE", "worker.lifecycle"));
    }
    if s("worker.current_incarnation_id") != r("incarnation_id") {
        return Ok(reject(
            "WORKER_INCARNATION_NOT_CURRENT",
            "worker.current_incarnation_id",
        ));
    }
    if s("incarnation.incarnation_id") != r("incarnation_id")
        || s("incarnation.worker_id") != r("worker_id")
    {
        return Ok(reject(
            "INCARNATION_IDENTITY_MISMATCH",
            "incarnation.incarnation_id",
        ));
    }
    if s("incarnation.lifecycle") != "ACTIVE" {
        return Ok(reject("INCARNATION_NOT_ACTIVE", "incarnation.lifecycle"));
    }
    let assignment_identity = [
        ("assignment.assignment_id", "assignment_id"),
        ("assignment.attempt_id", "attempt_id"),
        ("assignment.worker_id", "worker_id"),
        ("assignment.incarnation_id", "incarnation_id"),
        ("assignment.resource_key", "resource_key"),
    ];
    for (snapshot_path, request_path) in assignment_identity {
        if s(snapshot_path) != r(request_path) {
            return Ok(reject("ASSIGNMENT_IDENTITY_MISMATCH", snapshot_path));
        }
    }
    if s("assignment.lifecycle") != "ACTIVE" {
        return Ok(reject("ASSIGNMENT_NOT_ACTIVE", "assignment.lifecycle"));
    }
    if si("assignment.resource_epoch") != ri("resource_epoch") {
        return Ok(reject(
            "ASSIGNMENT_EPOCH_MISMATCH",
            "assignment.resource_epoch",
        ));
    }
    let allocation_identity = [
        ("allocation.allocation_id", "allocation_id"),
        ("allocation.assignment_id", "assignment_id"),
        ("allocation.attempt_id", "attempt_id"),
        ("allocation.worker_id", "worker_id"),
        ("allocation.incarnation_id", "incarnation_id"),
        ("allocation.resource_key", "resource_key"),
    ];
    for (snapshot_path, request_path) in allocation_identity {
        if s(snapshot_path) != r(request_path) {
            return Ok(reject("ALLOCATION_IDENTITY_MISMATCH", snapshot_path));
        }
    }
    if s("allocation.lifecycle") != "ACTIVE" {
        return Ok(reject("ALLOCATION_NOT_ACTIVE", "allocation.lifecycle"));
    }
    if si("allocation.resource_epoch") != ri("resource_epoch") {
        return Ok(reject(
            "ALLOCATION_EPOCH_MISMATCH",
            "allocation.resource_epoch",
        ));
    }
    if s("resource.resource_key") != r("resource_key") {
        return Ok(reject(
            "RESOURCE_IDENTITY_MISMATCH",
            "resource.resource_key",
        ));
    }
    if si("resource.fencing_epoch") != ri("resource_epoch") {
        return Ok(reject("RESOURCE_EPOCH_MISMATCH", "resource.fencing_epoch"));
    }
    if si("assignment.version") != ri("expected_assignment_version") {
        return Ok(reject("ASSIGNMENT_VERSION_MISMATCH", "assignment.version"));
    }
    if si("allocation.version") != ri("expected_allocation_version") {
        return Ok(reject("ALLOCATION_VERSION_MISMATCH", "allocation.version"));
    }
    if si("resource.version") != ri("expected_resource_version") {
        return Ok(reject("RESOURCE_VERSION_MISMATCH", "resource.version"));
    }
    if s("assignment.lease_expires_at") != s("allocation.expires_at") {
        return Ok(reject("LEASE_SNAPSHOT_MISMATCH", "allocation.expires_at"));
    }
    if s("clock.source") != "COORDINATOR_TRUSTED" {
        return Ok(reject("CLOCK_SOURCE_UNTRUSTED", "clock.source"));
    }
    if s("clock.now") < s("clock.watermark") {
        return Ok(reject("CLOCK_WATERMARK_REGRESSION", "clock.now"));
    }
    if s("clock.now") >= s("assignment.lease_expires_at") {
        return Ok(reject("LEASE_EXPIRED", "assignment.lease_expires_at"));
    }
    if s("safety_handoff.state") != "VERIFIED" {
        return Ok(reject(
            "SAFETY_HANDOFF_NOT_VERIFIED",
            "safety_handoff.state",
        ));
    }
    let handoff_identity = [
        ("safety_handoff.assignment_id", "assignment_id"),
        ("safety_handoff.worker_id", "worker_id"),
        ("safety_handoff.incarnation_id", "incarnation_id"),
        ("safety_handoff.attempt_id", "attempt_id"),
        ("safety_handoff.resource_key", "resource_key"),
    ];
    for (snapshot_path, request_path) in handoff_identity {
        if s(snapshot_path) != r(request_path) {
            return Ok(reject("SAFETY_HANDOFF_OWNER_MISMATCH", snapshot_path));
        }
    }
    if si("safety_handoff.resource_epoch") != ri("resource_epoch") {
        return Ok(reject(
            "SAFETY_HANDOFF_OWNER_MISMATCH",
            "safety_handoff.resource_epoch",
        ));
    }

    Ok(ownership_decision(
        "ALLOW_CANDIDATE",
        "OWNERSHIP_SNAPSHOT_MATCHES",
        json!({
            "assignment_version": si("assignment.version"),
            "allocation_version": si("allocation.version"),
            "resource_version": si("resource.version"),
            "resource_epoch": ri("resource_epoch"),
            "receipt_used_as_authority": false,
        }),
    ))
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct RuntimeEvent {
    event_id: String,
    stream_type: String,
    stream_id: String,
    sequence: u64,
    event_family: String,
    event_type: String,
    schema_version: u64,
    occurred_at: String,
    recorded_at: String,
    payload: Value,
    payload_hash: String,
    #[serde(default)]
    global_position: Option<u64>,
}

fn event_lifecycle(event_type: &str) -> Option<&'static str> {
    match event_type {
        "AssignmentGranted" | "AssignmentRenewed" => Some("ACTIVE"),
        "AssignmentReleased" => Some("RELEASED"),
        "AssignmentRevoked" => Some("REVOKED"),
        "AssignmentExpired" => Some("EXPIRED"),
        "AssignmentOrphaned" => Some("ORPHANED"),
        "AssignmentRecovered" => Some("RECOVERED"),
        _ => None,
    }
}

fn previous_allowed(event_type: &str, previous: Option<&str>) -> bool {
    matches!(
        (event_type, previous),
        ("AssignmentGranted", None)
            | ("AssignmentRenewed", Some("ACTIVE"))
            | ("AssignmentReleased", Some("ACTIVE"))
            | ("AssignmentRevoked", Some("ACTIVE"))
            | ("AssignmentExpired", Some("ACTIVE"))
            | ("AssignmentOrphaned", Some("ACTIVE"))
            | ("AssignmentRecovered", Some("EXPIRED" | "ORPHANED"))
    )
}

fn value_text<'a>(
    mapping: &'a Map<String, Value>,
    field: &str,
    context: &str,
) -> Result<&'a str, KernelError> {
    let value = mapping.get(field).ok_or_else(|| {
        KernelError::new(
            "ASSIGNMENT_STATE_INVALID",
            format!("{context} is missing required field {field}"),
        )
    })?;
    let text = value.as_str().ok_or_else(|| {
        KernelError::new(
            "ASSIGNMENT_STATE_INVALID",
            format!("{context}.{field} must be non-empty text"),
        )
    })?;
    if text.is_empty() {
        return Err(KernelError::new(
            "ASSIGNMENT_STATE_INVALID",
            format!("{context}.{field} must be non-empty text"),
        ));
    }
    Ok(text)
}

fn value_integer(
    mapping: &Map<String, Value>,
    field: &str,
    context: &str,
) -> Result<u64, KernelError> {
    let value = mapping.get(field).ok_or_else(|| {
        KernelError::new(
            "ASSIGNMENT_STATE_INVALID",
            format!("{context} is missing required field {field}"),
        )
    })?;
    let number = value.as_u64().ok_or_else(|| {
        KernelError::new(
            "ASSIGNMENT_STATE_INVALID",
            format!("{context}.{field} must be a non-negative integer"),
        )
    })?;
    if number > MAX_SAFE_INTEGER {
        return Err(KernelError::new(
            "INTEGER_OUT_OF_RANGE",
            format!("{context}.{field} exceeds {MAX_SAFE_INTEGER}"),
        ));
    }
    Ok(number)
}

fn payload_hash(payload: &Value) -> Result<String, KernelError> {
    let canonical = serde_json::to_string(payload).map_err(|error| {
        KernelError::new(
            "INVALID_INPUT",
            format!("cannot canonicalize payload: {error}"),
        )
    })?;
    Ok(format!("{:x}", Sha256::digest(canonical.as_bytes())))
}

fn clone_state(value: &Value) -> Result<Map<String, Value>, KernelError> {
    value
        .as_object()
        .cloned()
        .ok_or_else(|| KernelError::new("ASSIGNMENT_STATE_INVALID", "state must be an object"))
}

fn legacy_state(
    event: &RuntimeEvent,
    payload: &Map<String, Value>,
    previous: Option<&Value>,
) -> Result<Value, KernelError> {
    if event.event_type == "AssignmentGranted" {
        let assignment_id = value_text(payload, "assignment_id", "AssignmentGranted")?;
        let attempt_id = value_text(payload, "attempt_id", "AssignmentGranted")?;
        let worker_id = value_text(payload, "worker_id", "AssignmentGranted")?;
        let incarnation_id = value_text(payload, "incarnation_id", "AssignmentGranted")?;
        let resource_key = value_text(payload, "resource_key", "AssignmentGranted")?;
        let allocation_id = value_text(payload, "allocation_id", "AssignmentGranted")?;
        let epoch = value_integer(payload, "resource_epoch", "AssignmentGranted")?;
        if epoch == 0 {
            return Err(KernelError::new(
                "ASSIGNMENT_STATE_INVALID",
                "AssignmentGranted.resource_epoch must be positive",
            ));
        }
        let lease = value_text(payload, "lease_expires_at", "AssignmentGranted")?;
        validate_timestamp(lease).map_err(|_| {
            KernelError::new(
                "EVENT_TIMESTAMP_INVALID",
                "legacy lease timestamp is invalid",
            )
        })?;
        return Ok(json!({
            "state_version": 1,
            "assignment": {
                "assignment_id": assignment_id,
                "attempt_id": attempt_id,
                "worker_id": worker_id,
                "incarnation_id": incarnation_id,
                "resource_key": resource_key,
                "resource_epoch": epoch,
                "lifecycle": "ACTIVE",
                "version": 0,
                "lease_expires_at": lease,
                "created_at": event.occurred_at,
                "released_at": null,
            },
            "allocation": {
                "allocation_id": allocation_id,
                "assignment_id": assignment_id,
                "attempt_id": attempt_id,
                "worker_id": worker_id,
                "incarnation_id": incarnation_id,
                "resource_key": resource_key,
                "resource_epoch": epoch,
                "lifecycle": "ACTIVE",
                "version": 0,
                "expires_at": lease,
                "release_reason": null,
            },
            "attempt": {"attempt_id": attempt_id, "lifecycle": "ASSIGNED", "version": 1},
            "recovery": null,
        }));
    }
    let previous = previous.ok_or_else(|| {
        KernelError::new(
            "ASSIGNMENT_TRANSITION_INVALID",
            format!("{} has no preceding AssignmentGranted", event.event_type),
        )
    })?;
    let mut state = clone_state(previous)?;
    let assignment_id = value_text(payload, "assignment_id", &event.event_type)?;
    let previous_assignment_id = path_value(previous, "assignment.assignment_id")
        .and_then(Value::as_str)
        .unwrap_or_default();
    if assignment_id != previous_assignment_id {
        return Err(KernelError::new(
            "ASSIGNMENT_IDENTITY_INVALID",
            "legacy assignment event identity changed",
        ));
    }
    let mut assignment = state
        .remove("assignment")
        .and_then(|value| value.as_object().cloned())
        .ok_or_else(|| {
            KernelError::new(
                "ASSIGNMENT_STATE_INVALID",
                "previous assignment is malformed",
            )
        })?;
    increment(&mut assignment, "version", "state.assignment")?;
    assignment.insert(
        "lifecycle".to_owned(),
        json!(event_lifecycle(&event.event_type).unwrap()),
    );

    let mut allocation = state
        .remove("allocation")
        .and_then(|value| value.as_object().cloned())
        .ok_or_else(|| {
            KernelError::new(
                "ASSIGNMENT_STATE_INVALID",
                "previous allocation is malformed",
            )
        })?;
    let mut attempt = state
        .remove("attempt")
        .and_then(|value| value.as_object().cloned())
        .ok_or_else(|| {
            KernelError::new("ASSIGNMENT_STATE_INVALID", "previous attempt is malformed")
        })?;

    match event.event_type.as_str() {
        "AssignmentRenewed" => {
            let lease = value_text(payload, "lease_expires_at", "AssignmentRenewed")?;
            validate_timestamp(lease).map_err(|_| {
                KernelError::new(
                    "EVENT_TIMESTAMP_INVALID",
                    "renewal lease timestamp is invalid",
                )
            })?;
            assignment.insert("lease_expires_at".to_owned(), json!(lease));
            allocation.insert("expires_at".to_owned(), json!(lease));
            increment(&mut allocation, "version", "state.allocation")?;
        }
        "AssignmentOrphaned" => {
            allocation.insert("lifecycle".to_owned(), json!("QUARANTINED"));
            allocation.insert(
                "release_reason".to_owned(),
                json!("worker_incarnation_superseded"),
            );
            state.insert(
                "recovery".to_owned(),
                json!({
                    "assignment_id": assignment_id,
                    "state": "PENDING",
                    "reason": "worker incarnation superseded",
                    "attempts": 0,
                }),
            );
        }
        "AssignmentExpired" => {
            allocation.insert("lifecycle".to_owned(), json!("QUARANTINED"));
            allocation.insert("release_reason".to_owned(), json!("lease_expired"));
            increment(&mut allocation, "version", "state.allocation")?;
            state.insert(
                "recovery".to_owned(),
                json!({
                    "assignment_id": assignment_id,
                    "state": "PENDING",
                    "reason": "lease expired; process death not proven",
                    "attempts": 0,
                }),
            );
        }
        "AssignmentReleased" | "AssignmentRevoked" => {
            let reason = value_text(payload, "reason", &event.event_type)?;
            allocation.insert("lifecycle".to_owned(), json!("RELEASED"));
            allocation.insert("release_reason".to_owned(), json!(reason));
            increment(&mut allocation, "version", "state.allocation")?;
            assignment.insert("released_at".to_owned(), json!(event.occurred_at));
            attempt.insert(
                "lifecycle".to_owned(),
                json!(if event.event_type == "AssignmentReleased" {
                    "RELEASED"
                } else {
                    "PENDING"
                }),
            );
            increment(&mut attempt, "version", "state.attempt")?;
        }
        "AssignmentRecovered" => {
            allocation.insert("lifecycle".to_owned(), json!("RELEASED"));
            allocation.insert("release_reason".to_owned(), json!("controlled_recovery"));
            increment(&mut allocation, "version", "state.allocation")?;
            assignment.insert("released_at".to_owned(), json!(event.occurred_at));
            attempt.insert("lifecycle".to_owned(), json!("PENDING"));
            increment(&mut attempt, "version", "state.attempt")?;
            let recovery = state
                .get_mut("recovery")
                .and_then(Value::as_object_mut)
                .ok_or_else(|| {
                    KernelError::new(
                        "ASSIGNMENT_STATE_INVALID",
                        "AssignmentRecovered requires pending recovery state",
                    )
                })?;
            recovery.insert("state".to_owned(), json!("DONE"));
            increment(recovery, "attempts", "state.recovery")?;
        }
        _ => unreachable!(),
    }
    state.insert("assignment".to_owned(), Value::Object(assignment));
    state.insert("allocation".to_owned(), Value::Object(allocation));
    state.insert("attempt".to_owned(), Value::Object(attempt));
    Ok(Value::Object(state))
}

fn increment(
    mapping: &mut Map<String, Value>,
    field: &str,
    context: &str,
) -> Result<(), KernelError> {
    let old = value_integer(mapping, field, context)?;
    if old == MAX_SAFE_INTEGER {
        return Err(KernelError::new(
            "INTEGER_OUT_OF_RANGE",
            format!("{context}.{field} cannot advance"),
        ));
    }
    mapping.insert(field.to_owned(), json!(old + 1));
    Ok(())
}

fn validate_assignment_state(
    event: &RuntimeEvent,
    payload: &Map<String, Value>,
    previous: Option<&Value>,
) -> Result<(Value, Vec<String>), KernelError> {
    let legacy = !payload.contains_key("state") || payload.get("state") == Some(&Value::Null);
    let raw = if legacy {
        legacy_state(event, payload, previous)?
    } else {
        payload.get("state").cloned().unwrap()
    };
    let raw_object = object(&raw, "state")?;
    if raw_object.get("state_version").and_then(Value::as_u64) != Some(1) {
        return Err(KernelError::new(
            "ASSIGNMENT_STATE_INVALID",
            "assignment event has unknown state payload version",
        ));
    }
    let assignment = raw_object
        .get("assignment")
        .and_then(Value::as_object)
        .ok_or_else(|| {
            KernelError::new("ASSIGNMENT_STATE_INVALID", "state.assignment is malformed")
        })?;
    let allocation = raw_object
        .get("allocation")
        .and_then(Value::as_object)
        .ok_or_else(|| {
            KernelError::new("ASSIGNMENT_STATE_INVALID", "state.allocation is malformed")
        })?;
    let attempt = raw_object
        .get("attempt")
        .and_then(Value::as_object)
        .ok_or_else(|| {
            KernelError::new("ASSIGNMENT_STATE_INVALID", "state.attempt is malformed")
        })?;
    let recovery = raw_object.get("recovery").unwrap_or(&Value::Null);

    let assignment_fields = [
        "assignment_id",
        "attempt_id",
        "worker_id",
        "incarnation_id",
        "resource_key",
        "resource_epoch",
        "lifecycle",
        "version",
        "lease_expires_at",
        "created_at",
        "released_at",
    ];
    let allocation_fields = [
        "allocation_id",
        "assignment_id",
        "attempt_id",
        "worker_id",
        "incarnation_id",
        "resource_key",
        "resource_epoch",
        "lifecycle",
        "version",
        "expires_at",
        "release_reason",
    ];
    let attempt_fields = ["attempt_id", "lifecycle", "version"];
    let recovery_fields = ["assignment_id", "state", "reason", "attempts"];
    for field in assignment_fields {
        if !assignment.contains_key(field) {
            return Err(KernelError::new(
                "ASSIGNMENT_STATE_INVALID",
                format!("state.assignment is missing required field {field}"),
            ));
        }
    }
    for field in allocation_fields {
        if !allocation.contains_key(field) {
            return Err(KernelError::new(
                "ASSIGNMENT_STATE_INVALID",
                format!("state.allocation is missing required field {field}"),
            ));
        }
    }
    for field in attempt_fields {
        if !attempt.contains_key(field) {
            return Err(KernelError::new(
                "ASSIGNMENT_STATE_INVALID",
                format!("state.attempt is missing required field {field}"),
            ));
        }
    }

    let identity_fields = [
        "assignment_id",
        "attempt_id",
        "worker_id",
        "incarnation_id",
        "resource_key",
        "resource_epoch",
    ];
    for field in identity_fields {
        if assignment.get(field) != allocation.get(field) {
            return Err(KernelError::new(
                "ASSIGNMENT_IDENTITY_INVALID",
                format!("assignment/allocation identity mismatch: {field}"),
            ));
        }
    }
    let assignment_id = value_text(assignment, "assignment_id", "state.assignment")?;
    if assignment_id != event.stream_id
        || payload.get("assignment_id").and_then(Value::as_str) != Some(assignment_id)
    {
        return Err(KernelError::new(
            "ASSIGNMENT_IDENTITY_INVALID",
            "assignment event stream identity mismatch",
        ));
    }
    if attempt.get("attempt_id") != assignment.get("attempt_id") {
        return Err(KernelError::new(
            "ASSIGNMENT_IDENTITY_INVALID",
            "assignment/attempt identity mismatch",
        ));
    }
    for field in [
        "assignment_id",
        "attempt_id",
        "worker_id",
        "incarnation_id",
        "resource_key",
    ] {
        value_text(assignment, field, "state.assignment")?;
    }
    value_text(allocation, "allocation_id", "state.allocation")?;
    value_text(attempt, "attempt_id", "state.attempt")?;
    let epoch = value_integer(assignment, "resource_epoch", "state.assignment")?;
    let assignment_version = value_integer(assignment, "version", "state.assignment")?;
    let allocation_version = value_integer(allocation, "version", "state.allocation")?;
    let attempt_version = value_integer(attempt, "version", "state.attempt")?;
    if epoch == 0 {
        return Err(KernelError::new(
            "ASSIGNMENT_STATE_INVALID",
            "assignment resource epoch must be positive",
        ));
    }
    for (mapping, field, context) in [
        (assignment, "lease_expires_at", "state.assignment"),
        (assignment, "created_at", "state.assignment"),
    ] {
        let timestamp = value_text(mapping, field, context)?;
        validate_timestamp(timestamp).map_err(|_| {
            KernelError::new(
                "EVENT_TIMESTAMP_INVALID",
                format!("{context}.{field} is invalid"),
            )
        })?;
    }
    if allocation.get("expires_at") != assignment.get("lease_expires_at") {
        return Err(KernelError::new(
            "ASSIGNMENT_STATE_INVALID",
            "assignment/allocation lease mismatch",
        ));
    }
    let expected_lifecycle = event_lifecycle(&event.event_type).unwrap();
    let payload_lifecycle = if legacy {
        payload
            .get("lifecycle")
            .and_then(Value::as_str)
            .unwrap_or(expected_lifecycle)
    } else {
        payload
            .get("lifecycle")
            .and_then(Value::as_str)
            .unwrap_or("")
    };
    if assignment.get("lifecycle").and_then(Value::as_str) != Some(expected_lifecycle)
        || payload_lifecycle != expected_lifecycle
    {
        return Err(KernelError::new(
            "ASSIGNMENT_TRANSITION_INVALID",
            format!("{} has illegal assignment lifecycle", event.event_type),
        ));
    }
    let expected_allocation = match event.event_type.as_str() {
        "AssignmentGranted" | "AssignmentRenewed" => "ACTIVE",
        "AssignmentOrphaned" | "AssignmentExpired" => "QUARANTINED",
        _ => "RELEASED",
    };
    let expected_attempt = match event.event_type.as_str() {
        "AssignmentGranted" | "AssignmentRenewed" | "AssignmentOrphaned" | "AssignmentExpired" => {
            "ASSIGNED"
        }
        "AssignmentReleased" => "RELEASED",
        _ => "PENDING",
    };
    if allocation.get("lifecycle").and_then(Value::as_str) != Some(expected_allocation)
        || attempt.get("lifecycle").and_then(Value::as_str) != Some(expected_attempt)
    {
        return Err(KernelError::new(
            "ASSIGNMENT_TRANSITION_INVALID",
            format!("{} has illegal dependent lifecycle", event.event_type),
        ));
    }
    match event.event_type.as_str() {
        "AssignmentOrphaned" | "AssignmentExpired" => {
            if recovery.get("state").and_then(Value::as_str) != Some("PENDING") {
                return Err(KernelError::new(
                    "ASSIGNMENT_STATE_INVALID",
                    format!("{} requires pending recovery state", event.event_type),
                ));
            }
        }
        "AssignmentRecovered" => {
            if recovery.get("state").and_then(Value::as_str) != Some("DONE") {
                return Err(KernelError::new(
                    "ASSIGNMENT_STATE_INVALID",
                    "AssignmentRecovered requires done recovery state",
                ));
            }
        }
        _ if !recovery.is_null() => {
            return Err(KernelError::new(
                "ASSIGNMENT_STATE_INVALID",
                format!("{} cannot introduce recovery state", event.event_type),
            ));
        }
        _ => {}
    }
    if let Some(recovery) = recovery.as_object() {
        for field in recovery_fields {
            if !recovery.contains_key(field) {
                return Err(KernelError::new(
                    "ASSIGNMENT_STATE_INVALID",
                    format!("state.recovery is missing required field {field}"),
                ));
            }
        }
        if recovery.get("assignment_id") != assignment.get("assignment_id") {
            return Err(KernelError::new(
                "ASSIGNMENT_IDENTITY_INVALID",
                "assignment/recovery identity mismatch",
            ));
        }
        value_text(recovery, "reason", "state.recovery")?;
        let attempts = value_integer(recovery, "attempts", "state.recovery")?;
        if matches!(
            event.event_type.as_str(),
            "AssignmentOrphaned" | "AssignmentExpired"
        ) && attempts != 0
        {
            return Err(KernelError::new(
                "ASSIGNMENT_VERSION_INVALID",
                "new recovery work must start with zero attempts",
            ));
        }
        if event.event_type == "AssignmentRecovered" && attempts < 1 {
            return Err(KernelError::new(
                "ASSIGNMENT_VERSION_INVALID",
                "completed recovery must record an attempt",
            ));
        }
    }
    if let Some(previous) = previous {
        for field in identity_fields {
            if assignment.get(field) != path_value(previous, &format!("assignment.{field}")) {
                return Err(KernelError::new(
                    "ASSIGNMENT_IDENTITY_INVALID",
                    format!("assignment identity changed during replay: {field}"),
                ));
            }
        }
        let old_assignment_version = path_value(previous, "assignment.version")
            .and_then(Value::as_u64)
            .unwrap();
        let old_allocation_version = path_value(previous, "allocation.version")
            .and_then(Value::as_u64)
            .unwrap();
        let old_attempt_version = path_value(previous, "attempt.version")
            .and_then(Value::as_u64)
            .unwrap();
        if assignment_version != old_assignment_version + 1 {
            return Err(KernelError::new(
                "ASSIGNMENT_VERSION_INVALID",
                "assignment version did not advance exactly once",
            ));
        }
        let allocation_delta = u64::from(!(legacy && event.event_type == "AssignmentOrphaned"));
        let attempt_delta = u64::from(matches!(
            event.event_type.as_str(),
            "AssignmentRecovered" | "AssignmentReleased" | "AssignmentRevoked"
        ));
        if allocation_version != old_allocation_version + allocation_delta {
            return Err(KernelError::new(
                "ASSIGNMENT_VERSION_INVALID",
                "allocation version did not advance exactly once",
            ));
        }
        if attempt_version != old_attempt_version + attempt_delta {
            return Err(KernelError::new(
                "ASSIGNMENT_VERSION_INVALID",
                "attempt version transition is invalid",
            ));
        }
    } else if event.event_type != "AssignmentGranted"
        || assignment_version != 0
        || allocation_version != 0
        || attempt_version < 1
    {
        return Err(KernelError::new(
            "ASSIGNMENT_VERSION_INVALID",
            "assignment stream has invalid initial versions",
        ));
    }

    let assignment_known: BTreeSet<&str> = assignment_fields.into_iter().collect();
    let allocation_known: BTreeSet<&str> = allocation_fields.into_iter().collect();
    let attempt_known: BTreeSet<&str> = attempt_fields.into_iter().collect();
    let recovery_known: BTreeSet<&str> = recovery_fields.into_iter().collect();
    let state_known: BTreeSet<&str> = [
        "state_version",
        "assignment",
        "allocation",
        "attempt",
        "recovery",
    ]
    .into_iter()
    .collect();
    let mut unknown = Vec::new();
    for key in raw_object
        .keys()
        .filter(|key| !state_known.contains(key.as_str()))
    {
        unknown.push(format!("state.{key}"));
    }
    for key in assignment
        .keys()
        .filter(|key| !assignment_known.contains(key.as_str()))
    {
        unknown.push(format!("state.assignment.{key}"));
    }
    for key in allocation
        .keys()
        .filter(|key| !allocation_known.contains(key.as_str()))
    {
        unknown.push(format!("state.allocation.{key}"));
    }
    for key in attempt
        .keys()
        .filter(|key| !attempt_known.contains(key.as_str()))
    {
        unknown.push(format!("state.attempt.{key}"));
    }
    if let Some(recovery) = recovery.as_object() {
        for key in recovery
            .keys()
            .filter(|key| !recovery_known.contains(key.as_str()))
        {
            unknown.push(format!("state.recovery.{key}"));
        }
    }
    if !legacy {
        let payload_known: BTreeSet<&str> = ["assignment_id", "lifecycle", "state", "reason"]
            .into_iter()
            .collect();
        for key in payload
            .keys()
            .filter(|key| !payload_known.contains(key.as_str()))
        {
            unknown.push(format!("payload.{key}"));
        }
    }
    unknown.sort();
    unknown.dedup();

    let clean = json!({
        "state_version": 1,
        "assignment": assignment_fields.into_iter().map(|field| (field.to_owned(), assignment[field].clone())).collect::<Map<String, Value>>(),
        "allocation": allocation_fields.into_iter().map(|field| (field.to_owned(), allocation[field].clone())).collect::<Map<String, Value>>(),
        "attempt": attempt_fields.into_iter().map(|field| (field.to_owned(), attempt[field].clone())).collect::<Map<String, Value>>(),
        "recovery": recovery.as_object().map(|mapping| {
            recovery_fields.into_iter().map(|field| (field.to_owned(), mapping[field].clone())).collect::<Map<String, Value>>()
        }),
    });
    Ok((clean, unknown))
}

pub fn replay_assignment(payload: &Value) -> Result<Value, KernelError> {
    let root = object(payload, "payload")?;
    if root.keys().any(|key| key != "events") {
        return Err(KernelError::new(
            "INVALID_INPUT",
            "replay_assignment payload contains unknown fields",
        ));
    }
    let raw_events = root
        .get("events")
        .and_then(Value::as_array)
        .ok_or_else(|| KernelError::new("INVALID_INPUT", "events must be an array"))?;
    if raw_events.is_empty() {
        return Ok(json!({
            "event_family": EVENT_FAMILY,
            "stream_version": 0,
            "event_types": [],
            "states": {},
            "state": null,
            "not_covered_fields": [],
        }));
    }
    if raw_events.len() > MAX_EVENTS {
        return Err(KernelError::new(
            "INPUT_LIMIT_EXCEEDED",
            format!("events exceeds {MAX_EVENTS}"),
        ));
    }
    let events: Vec<RuntimeEvent> = raw_events
        .iter()
        .cloned()
        .map(|value| {
            serde_json::from_value(value).map_err(|error| {
                KernelError::new(
                    "INVALID_INPUT",
                    format!("event envelope is invalid: {error}"),
                )
            })
        })
        .collect::<Result<_, _>>()?;
    let first = &events[0];
    let mut state: Option<Value> = None;
    let mut unknown = Vec::new();
    let mut event_types = Vec::new();

    for (index, event) in events.iter().enumerate() {
        if event.sequence > MAX_SAFE_INTEGER
            || event.schema_version > MAX_SAFE_INTEGER
            || event
                .global_position
                .is_some_and(|value| value > MAX_SAFE_INTEGER)
        {
            return Err(KernelError::new(
                "INTEGER_OUT_OF_RANGE",
                "event integer exceeds protocol range",
            ));
        }
        if event.event_family != EVENT_FAMILY || event.schema_version != 1 {
            return Err(KernelError::new(
                "EVENT_FAMILY_SCHEMA_UNSUPPORTED",
                "unknown runtime event family or schema",
            ));
        }
        if event.stream_type != "assignment"
            || event.stream_type != first.stream_type
            || event.stream_id != first.stream_id
        {
            return Err(KernelError::new(
                "EVENT_STREAM_MIXED",
                "runtime replay contains multiple or non-assignment streams",
            ));
        }
        if event.sequence != (index + 1) as u64 {
            return Err(KernelError::new(
                "EVENT_SEQUENCE_GAP",
                "runtime event stream has a version gap",
            ));
        }
        if event_lifecycle(&event.event_type).is_none() {
            return Err(KernelError::new(
                "EVENT_TYPE_INVALID",
                format!(
                    "event type {} is invalid for assignment stream",
                    event.event_type
                ),
            ));
        }
        validate_timestamp(&event.occurred_at).map_err(|_| {
            KernelError::new("EVENT_TIMESTAMP_INVALID", "event occurred_at is invalid")
        })?;
        validate_timestamp(&event.recorded_at).map_err(|_| {
            KernelError::new("EVENT_TIMESTAMP_INVALID", "event recorded_at is invalid")
        })?;
        if event.recorded_at < event.occurred_at {
            return Err(KernelError::new(
                "EVENT_RECORDED_BEFORE_OCCURRED",
                "runtime event was recorded before it occurred",
            ));
        }
        if event.payload_hash.len() != 64 || payload_hash(&event.payload)? != event.payload_hash {
            return Err(KernelError::new(
                "EVENT_PAYLOAD_HASH_MISMATCH",
                "runtime event payload hash mismatch",
            ));
        }
        let payload_object = object(&event.payload, "event.payload")?;
        let previous_lifecycle = state
            .as_ref()
            .and_then(|value| path_value(value, "assignment.lifecycle"))
            .and_then(Value::as_str);
        if !previous_allowed(&event.event_type, previous_lifecycle) {
            return Err(KernelError::new(
                "ASSIGNMENT_TRANSITION_INVALID",
                format!(
                    "illegal assignment transition {:?} -> {}",
                    previous_lifecycle, event.event_type
                ),
            ));
        }
        let (next, event_unknown) =
            validate_assignment_state(event, payload_object, state.as_ref())?;
        state = Some(next);
        unknown.extend(event_unknown);
        event_types.push(event.event_type.clone());
    }
    unknown.sort();
    unknown.dedup();
    let state = state.unwrap();
    let lifecycle = path_value(&state, "assignment.lifecycle")
        .and_then(Value::as_str)
        .unwrap();
    Ok(json!({
        "event_family": EVENT_FAMILY,
        "stream_type": "assignment",
        "stream_id": first.stream_id,
        "stream_version": events.len(),
        "event_types": event_types,
        "states": { first.stream_id.clone(): lifecycle },
        "state": state,
        "not_covered_fields": unknown,
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn valid_ownership() -> Value {
        json!({
            "snapshot": {
                "execution": {"execution_id": "exe-1", "task_id": "task-1", "lifecycle": "REQUESTED"},
                "attempt": {"attempt_id": "att-1", "execution_id": "exe-1", "lifecycle": "ASSIGNED"},
                "worker": {"worker_id": "worker-1", "lifecycle": "REGISTERED", "current_incarnation_id": "inc-1"},
                "incarnation": {"incarnation_id": "inc-1", "worker_id": "worker-1", "lifecycle": "ACTIVE"},
                "assignment": {"assignment_id": "asn-1", "attempt_id": "att-1", "worker_id": "worker-1", "incarnation_id": "inc-1", "resource_key": "res-1", "resource_epoch": 3, "lifecycle": "ACTIVE", "version": 2, "lease_expires_at": "2026-09-11T12:00:10.000000+00:00"},
                "allocation": {"allocation_id": "alloc-1", "assignment_id": "asn-1", "attempt_id": "att-1", "worker_id": "worker-1", "incarnation_id": "inc-1", "resource_key": "res-1", "resource_epoch": 3, "lifecycle": "ACTIVE", "version": 2, "expires_at": "2026-09-11T12:00:10.000000+00:00"},
                "resource": {"resource_key": "res-1", "fencing_epoch": 3, "version": 4},
                "clock": {"source": "COORDINATOR_TRUSTED", "now": "2026-09-11T12:00:09.000000+00:00", "watermark": "2026-09-11T12:00:09.000000+00:00"},
                "safety_handoff": {"state": "VERIFIED", "assignment_id": "asn-1", "worker_id": "worker-1", "incarnation_id": "inc-1", "attempt_id": "att-1", "resource_key": "res-1", "resource_epoch": 3}
            },
            "request": {"execution_id": "exe-1", "attempt_id": "att-1", "worker_id": "worker-1", "incarnation_id": "inc-1", "assignment_id": "asn-1", "allocation_id": "alloc-1", "resource_key": "res-1", "resource_epoch": 3, "expected_assignment_version": 2, "expected_allocation_version": 2, "expected_resource_version": 4}
        })
    }

    #[test]
    fn ownership_allows_only_as_candidate() {
        let result = evaluate_ownership(&valid_ownership()).unwrap();
        assert_eq!(result["decision"], "ALLOW_CANDIDATE");
        assert_eq!(result["authority"], AUTHORITY);
        assert_eq!(result["details"]["receipt_used_as_authority"], false);
    }

    #[test]
    fn exact_expiry_boundary_rejects() {
        let mut input = valid_ownership();
        input["snapshot"]["clock"]["now"] = json!("2026-09-11T12:00:10.000000+00:00");
        input["snapshot"]["clock"]["watermark"] = json!("2026-09-11T12:00:10.000000+00:00");
        let result = evaluate_ownership(&input).unwrap();
        assert_eq!(result["reason_code"], "LEASE_EXPIRED");
    }

    #[test]
    fn unknown_evidence_is_not_filled_from_receipt() {
        let mut input = valid_ownership();
        input["snapshot"]["assignment"]["resource_epoch"] = json!("UNKNOWN");
        input["snapshot"]["receipt"] = json!({"original_success": true});
        let result = evaluate_ownership(&input).unwrap();
        assert_eq!(result["decision"], "INSUFFICIENT_EVIDENCE");
    }
}
