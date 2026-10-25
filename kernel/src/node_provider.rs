//! Scope-bound Provider exchange for the existing paired Node channel.
//! The channel authenticates the principal and project before invoking this
//! code. Rust checks the exact method/input identity before any Provider I/O.
use crate::{
    business::native_client_id,
    provider::{Client, Provider, Result},
    workspace_probe,
};
use serde_json::{Value, json};
use std::{path::Path, time::Duration};

pub fn validate(context: &Value, body: &Value) -> Result<()> {
    let method = body["method"].as_str().ok_or("PROVIDER_METHOD_DENIED")?;
    let p = &body["params"];
    if !p.is_object() {
        return Err("INVALID_PROVIDER_PARAMS".into());
    }
    let tid = context["thread_id"].as_str().filter(|s| !s.is_empty());
    let reads = ["thread/read", "thread/turns/list", "thread/items/list"];
    if (reads.contains(&method)
        || [
            "thread/resume",
            "turn/start",
            "turn/interrupt",
            "thread/name/set",
        ]
        .contains(&method))
        && (tid.is_none() || p["threadId"].as_str() != tid)
    {
        return Err("PROVIDER_THREAD_IDENTITY_CONFLICT".into());
    }
    if method == "workspace/inspect" {
        if context["mode"] != "workspace" || p["cwd"] != context["cwd"] {
            return Err("WORKSPACE_CONTRACT_INVALID".into());
        }
        return Ok(());
    }
    if reads.contains(&method) {
        if !context["turn_id"].is_null()
            && !p["turnId"].is_null()
            && p["turnId"] != context["turn_id"]
        {
            return Err("PROVIDER_TURN_IDENTITY_CONFLICT".into());
        }
        return Ok(());
    }
    match method {
        "thread/start" => {
            if context["mode"] != "dispatch" || tid.is_some() || p["cwd"] != context["cwd"] {
                return Err("PROVIDER_CREATE_DENIED".into());
            }
            // These were also enforced by the previous shared-writer client,
            // in addition to the relay's route check.
            if p["ephemeral"] != false
                || p["threadSource"] != "user"
                || p.get("dynamicTools")
                    .is_some_and(|v| !v.is_null() && v.as_array().is_none_or(|a| !a.is_empty()))
                || p.get("developerInstructions")
                    .is_some_and(|v| !v.is_null() && v != "")
            {
                return Err("PROVIDER_CREATE_DENIED".into());
            }
        }
        "thread/resume" => {
            if context["mode"] != "dispatch" || *p != json!({"threadId":tid,"excludeTurns":true}) {
                return Err("PROVIDER_RESUME_DENIED".into());
            }
        }
        "turn/start" => {
            let execution = context["execution_ref"].as_str().filter(|s| !s.is_empty());
            let mut expected = json!({"threadId":tid,"input":[{"type":"text","text":context["prompt"]}],
                "cwd":context["cwd"],"clientUserMessageId":native_client_id(tid.unwrap_or(""),execution.unwrap_or("")),
                "sandboxPolicy":{"type":"dangerFullAccess"},"approvalPolicy":"never"});
            for key in ["model", "effort"] {
                if !context[key].is_null() {
                    expected[key] = context[key].clone();
                }
            }
            if context["mode"] != "dispatch" || execution.is_none() || *p != expected {
                return Err("PROVIDER_INPUT_IDENTITY_CONFLICT".into());
            }
        }
        "turn/interrupt" => {
            if context["mode"] != "cancel"
                || *p != json!({"threadId":tid,"turnId":context["turn_id"]})
            {
                return Err("PROVIDER_CANCEL_DENIED".into());
            }
        }
        "thread/name/set" => {
            if context["mode"] != "metadata"
                || *p != json!({"threadId":tid,"name":context["title"]})
            {
                return Err("PROVIDER_METADATA_DENIED".into());
            }
        }
        _ => return Err("PROVIDER_METHOD_DENIED".into()),
    }
    Ok(())
}

pub fn exchange(
    endpoint: &Path,
    program: &Path,
    context: &Value,
    body: &Value,
    timeout: Duration,
) -> Result<Value> {
    validate(context, body)?;
    let uid = rustix::process::getuid().as_raw();
    let method = body["method"].as_str().unwrap();
    if method == "workspace/inspect" {
        return workspace_probe::local(endpoint, program, &body["params"], uid, timeout);
    }
    let mut client = Client::connect_checked(endpoint, timeout, uid)?;
    let result = client.request(method, body["params"].clone());
    if method == "thread/read"
        && client.last_error.as_ref().is_some_and(|e| {
            e["code"] == -32600
                && e["message"]
                    == format!(
                        "thread not loaded: {}",
                        context["thread_id"].as_str().unwrap_or("")
                    )
        })
    {
        return Err("NATIVE_THREAD_NOT_LOADED".into());
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn exact_provider_input_is_bound_to_execution_thread_workspace_and_mode() {
        let c = json!({"mode":"dispatch","thread_id":"native", "execution_ref":"exec_exact",
            "cwd":"/only-on-node/repo", "prompt":"verbatim input", "model":"model", "effort":"high"});
        let p = json!({"threadId":"native","input":[{"type":"text","text":"verbatim input"}],
            "cwd":"/only-on-node/repo", "clientUserMessageId":native_client_id("native","exec_exact"),
            "sandboxPolicy":{"type":"dangerFullAccess"},"approvalPolicy":"never", "model":"model","effort":"high"});
        let b = json!({"method":"turn/start","params":p});
        validate(&c, &b).unwrap();
        for key in [
            "thread_id",
            "execution_ref",
            "cwd",
            "prompt",
            "model",
            "effort",
            "mode",
        ] {
            let mut wrong = c.clone();
            wrong[key] = json!("conflict");
            assert!(validate(&wrong, &b).is_err(), "accepted conflicting {key}");
        }
        let mut wrong = b.clone();
        wrong["params"]["additional"] = json!("cannot smuggle extra inputs");
        assert_eq!(
            validate(&c, &wrong).unwrap_err(),
            "PROVIDER_INPUT_IDENTITY_CONFLICT"
        );
    }
    #[test]
    fn reads_cancel_metadata_resume_and_creation_keep_separate_contracts() {
        let c = json!({"mode":"read", "thread_id":"native", "turn_id":"exact"});
        validate(
            &c,
            &json!({"method":"thread/items/list","params":{"threadId":"native","turnId":"exact"}}),
        )
        .unwrap();
        assert_eq!(validate(&c, &json!({"method":"thread/items/list","params":{"threadId":"native","turnId":"another"}})).unwrap_err(),"PROVIDER_TURN_IDENTITY_CONFLICT");
        for (method, mode, p) in [
            (
                "turn/interrupt",
                "cancel",
                json!({"threadId":"native","turnId":"exact"}),
            ),
            (
                "thread/name/set",
                "metadata",
                json!({"threadId":"native","name":"title"}),
            ),
            (
                "thread/resume",
                "dispatch",
                json!({"threadId":"native","excludeTurns":true}),
            ),
        ] {
            let mut c = c.clone();
            c["title"] = json!("title");
            let b = json!({"method":method,"params":p});
            assert!(validate(&c, &b).is_err());
            c["mode"] = json!(mode);
            validate(&c, &b).unwrap();
        }
        let c = json!({"mode":"dispatch","cwd":"/node/repo"});
        let b = json!({"method":"thread/start","params":{"cwd":"/node/repo","ephemeral":false,"threadSource":"user"}});
        validate(&c, &b).unwrap();
        for (key, value) in [
            ("ephemeral", json!(true)),
            ("threadSource", json!("subagent")),
            ("dynamicTools", json!([{"name":"cannot smuggle tools"}])),
            (
                "developerInstructions",
                json!("cannot smuggle instructions"),
            ),
        ] {
            let mut wrong = b.clone();
            wrong["params"][key] = value;
            assert_eq!(validate(&c, &wrong).unwrap_err(), "PROVIDER_CREATE_DENIED");
        }
    }
}
