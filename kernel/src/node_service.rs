//! Node wire execution mapping and request uncertainty. Rust Authority remains
//! the sole task writer; the Node ledger only records protocol request receipts.
use crate::{
    node_scope::{PROTOCOL, PROVIDER, Scope},
    node_tls::Result,
};
use serde_json::{Value, json};
fn text(r: &Value, key: &str) -> Result<String> {
    r[key]
        .as_str()
        .filter(|s| {
            !s.trim().is_empty()
                && s.chars().count() <= 256
                && !s.chars().any(|c| c < ' ' || c == '\u{7f}')
        })
        .map(|s| s.trim().into())
        .ok_or("INVALID_NODE_REQUEST".into())
}
fn annotate(mut v: Value, fields: Value) -> Result<Value> {
    let obj = v.as_object_mut().ok_or("NODE_RESPONSE_INVALID")?;
    for (k, v) in fields.as_object().unwrap() {
        obj.insert(k.clone(), v.clone());
    }
    Ok(v)
}
pub fn authorize(record: &Value, scope: &Scope, op: &str, r: &Value) -> Result<()> {
    if !r["node_id"].is_null() && r["node_id"] != record["node_id"] {
        return Err("NODE_TARGET_MISMATCH".into());
    }
    if !r["user_scope"].is_null() && r["user_scope"] != scope.user_scope {
        return Err("USER_SCOPE_DENIED".into());
    }
    let provider = r.get("provider").cloned().unwrap_or(json!(PROVIDER));
    let permitted = if let Some(p) = provider.as_str() {
        let mut s = scope.clone();
        s.providers = if s.providers.iter().any(|v| v == p) {
            vec![PROVIDER.into()]
        } else {
            vec![]
        };
        s.allows(op)
    } else {
        false
    };
    if record["trusted"] != true || record["authorized"] != true || !permitted {
        return Err("SHARING_SCOPE_DENIED".into());
    }
    if record["protocol_version"] != PROTOCOL || record["version_compatible"] != true {
        return Err("NODE_VERSION_INCOMPATIBLE".into());
    }
    Ok(())
}
fn canonical(mut r: Value, record: &Value, scope: &Scope) -> Result<Value> {
    r.as_object_mut()
        .ok_or("INVALID_NODE_REQUEST")?
        .remove("_canonical_owner");
    if !r["canonical_identity"].is_null() {
        let i = &r["canonical_identity"];
        if !i.is_object()
            || i["node_id"] != record["node_id"]
            || i["user_scope"] != scope.user_scope
            || !scope.project(&i["cwd"])
        {
            return Err("CANONICAL_HANDOFF_SCOPE_DENIED".into());
        }
        r["_canonical_owner"] = json!({"owner":record["node_id"],"user":scope.user_scope,"fingerprint":record["public_key_fingerprint"]});
    }
    Ok(r)
}
fn unknown(id: &str) -> Value {
    json!({"request_id":id,"operation_state":"UNKNOWN","side_effect":"UNKNOWN","retry":"RECONCILIATION_REQUIRED","idempotent":true})
}
fn release_cancel(
    op: &str,
    r: &Value,
    result: &Value,
    id: &Value,
    scope: &Scope,
    call: &mut impl FnMut(&str, Value) -> Result<Value>,
) -> Result<()> {
    if op == "execution.cancel"
        && matches!(
            result["operation_state"].as_str(),
            Some("CANCELLED" | "COMPLETED" | "CONFIRMED")
        )
        && r["thread_id"].as_str().is_some_and(|s| !s.is_empty())
    {
        call(
            "registry.release_fence",
            json!({"identity":{"node_id":id,"user_scope":scope.user_scope,"provider":r.get("provider").cloned().unwrap_or(json!(PROVIDER)),"native_thread_id":r["thread_id"],"source_version":r.get("source_version").cloned().unwrap_or(json!("1"))},"execution_ref":r.get("execution_ref").cloned().unwrap_or(json!(""))}),
        )?;
    }
    Ok(())
}
pub fn handle(a: &Value, call: impl FnMut(&str, Value) -> Result<Value>) -> Result<Value> {
    match handle_inner(a, call) {
        Err(e)
            if matches!(
                e.as_str(),
                "NODE_REGISTRY_UNAVAILABLE"
                    | "NODE_WORKER_UNAVAILABLE"
                    | "PROVIDER_DELIVERY_UNKNOWN"
                    | "DELIVERY_UNKNOWN"
                    | "CALLBACK_UNAVAILABLE"
            ) =>
        {
            let read = matches!(
                a["operation"].as_str(),
                Some(
                    "session.read"
                        | "session.status"
                        | "execution.context"
                        | "execution.status"
                        | "node.status"
                )
            );
            Ok(
                json!({"error_code":e,"request_id":a["request"]["request_id"],"read_only":read,"operation_state":if read{"READ_FAILED"}else{"UNKNOWN"},"side_effect":if read{"NONE"}else{"UNKNOWN"},"retry":if read{"READ_ONLY_RETRY"}else{"RECONCILIATION_REQUIRED"}}),
            )
        }
        result => result,
    }
}
fn handle_inner(a: &Value, mut call: impl FnMut(&str, Value) -> Result<Value>) -> Result<Value> {
    let record = &a["record"];
    let scope = Scope::parse(&a["scope"])?;
    let mut r = a["request"].clone();
    let op = a["operation"].as_str().ok_or("UNKNOWN_NODE_OPERATION")?;
    if op == "authorize" {
        authorize(
            record,
            &scope,
            a["permission"].as_str().ok_or("UNKNOWN_NODE_OPERATION")?,
            &r,
        )?;
        return Ok(Value::Null);
    }
    if r.get("protocol_version").is_some_and(|v| v != PROTOCOL) {
        return Err("NODE_VERSION_INCOMPATIBLE".into());
    }
    if !matches!(
        op,
        "node.status"
            | "session.read"
            | "session.status"
            | "execution.adopt"
            | "execution.context"
            | "execution.prepare"
            | "execution.start"
            | "execution.status"
            | "execution.cancel"
            | "execution.reconcile"
    ) {
        return Err("UNKNOWN_NODE_OPERATION".into());
    }
    let permission = match op {
        "execution.context" => "session.read",
        "execution.status" if r["native_turn_id"].is_null() => "session.status",
        _ => op,
    };
    authorize(record, &scope, permission, &r)?;
    if op == "node.status" {
        return Ok(
            json!({"node":record,"scope":scope.value(),"node_reachable":true,"provider_reachable":true,"history_readable":scope.read_sessions,"execution_active":false,"observed_at":crate::node_tls::now(),"read_only":true}),
        );
    }
    if !matches!(
        op,
        "session.read"
            | "session.status"
            | "execution.adopt"
            | "execution.context"
            | "execution.prepare"
            | "execution.start"
            | "execution.status"
            | "execution.cancel"
            | "execution.reconcile"
    ) {
        return Err("UNKNOWN_NODE_OPERATION".into());
    }
    if op != "execution.start" && a["available"][op] != true {
        return Err("OPERATION_NOT_IMPLEMENTED".into());
    }
    if matches!(
        op,
        "execution.adopt" | "execution.prepare" | "execution.cancel"
    ) {
        r = canonical(r, record, &scope)?;
    }
    let id = record["node_id"].clone();
    if matches!(
        op,
        "session.read"
            | "session.status"
            | "execution.context"
            | "execution.status"
            | "execution.adopt"
    ) {
        let result = call("execute", json!({"operation":op,"request":r}))?;
        if let Some(e) = result.get("callback_error") {
            if op == "execution.adopt" {
                return Ok(
                    json!({"adoption_status":"BLOCKED","error_code":e["code"],"unavailable_reason":e["reason"],"execution_started":false,"control_transferred":false,"node_id":id}),
                );
            }
            return Err(e["code"].as_str().unwrap_or("NODE_REQUEST_FAILED").into());
        }
        return if matches!(op, "session.read" | "session.status") {
            Ok(result)
        } else {
            annotate(
                result,
                if op == "execution.status" {
                    json!({"node_id":id,"read_only":true})
                } else {
                    json!({"node_id":id})
                },
            )
        };
    }
    let request_id = text(&r, "request_id")?;
    let execution = if op == "execution.start" {
        Some(text(&r, "execution_ref")?)
    } else {
        None
    };
    let claim = call(
        "registry.claim_request",
        json!({"node_id":id,"request_id":request_id,"operation":op,"payload":r}),
    )?;
    if claim[0] != "NEW" {
        let prior = &claim[1];
        if !prior.is_null() && (op != "execution.start" || prior["side_effect"] != "UNKNOWN") {
            let result = annotate(prior.clone(), json!({"idempotent":true}))?;
            release_cancel(op, &r, &result, &id, &scope, &mut call)?;
            return Ok(result);
        }
        if op == "execution.start"
            && a["available"]["execution.status"] == true
            && let Ok(result) = call(
                "execute",
                json!({"operation":"execution.status","request":r}),
            )
            && result["execution_ref"].as_str() == execution.as_deref()
        {
            let result = annotate(
                result,
                json!({"execution_started":false,"idempotent":true,"operation_state":"RECONCILED","request_id":request_id}),
            )?;
            call(
                "registry.finish_request",
                json!({"node_id":id,"request_id":request_id,"state":"COMPLETED","response":result}),
            )?;
            return Ok(result);
        }
        return Ok(unknown(&request_id));
    }
    if op == "execution.start" {
        let mut fence = Value::Null;
        if !r["thread_id"].is_null() && a["authority_owned"] != true {
            let identity = json!({"node_id":id,"user_scope":scope.user_scope,"provider":r.get("provider").cloned().unwrap_or(json!(PROVIDER)),"native_thread_id":r["thread_id"],"source_version":r.get("source_version").cloned().unwrap_or(json!("1"))});
            let response = call(
                "registry.acquire_fence",
                json!({"identity":identity,"execution_ref":execution}),
            )?;
            if !response["callback_error"].is_null() {
                let e = &response["callback_error"];
                let mut result = e["details"].clone();
                if !result.is_object() {
                    result = json!({});
                }
                result = annotate(
                    result,
                    json!({"error_code":e["code"],"unavailable_reason":e["reason"],"request_id":request_id,"operation_state":"BLOCKED","side_effect":"NONE"}),
                )?;
                call(
                    "registry.finish_request",
                    json!({"node_id":id,"request_id":request_id,"state":"FAILED","response":result}),
                )?;
                return Ok(result);
            }
            fence = response;
        }
        r["fence"] = fence;
        r["target_node_id"] = id.clone();
    }
    if a["available"][op] != true {
        return Err("OPERATION_NOT_IMPLEMENTED".into());
    }
    let result = call("execute", json!({"operation":op,"request":r}))?;
    let (result, failed) = if let Some(e) = result.get("callback_error") {
        let side = e
            .get("side_effect")
            .cloned()
            .unwrap_or(json!(if op == "execution.prepare" {
                "NONE"
            } else {
                "UNKNOWN"
            }));
        let mut result = json!({"request_id":request_id,"operation_state":if op!="execution.prepare"&&side=="NONE"{"BLOCKED"}else{"FAILED"},"error_code":e["code"],"unavailable_reason":e["reason"],"side_effect":side});
        if op == "execution.start" {
            result = annotate(
                result,
                json!({"execution_ref":r["execution_ref"],"prepared_execution_ref":r["prepared_execution_ref"],"execution_started":false}),
            )?;
        }
        (result, true)
    } else {
        (result, false)
    };
    call(
        "registry.finish_request",
        json!({"node_id":id,"request_id":request_id,"state":if failed{"FAILED"}else{"COMPLETED"},"response":result}),
    )?;
    let result = if failed {
        result
    } else {
        annotate(result, json!({"idempotent":false}))?
    };
    release_cancel(op, &r, &result, &id, &scope, &mut call)?;
    Ok(result)
}
