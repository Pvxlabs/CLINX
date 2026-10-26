//! Bounded private child protocol. Parent only relays through its existing identity.
//! A missing or mismatched callback is UNKNOWN; no operation is replayed.
use crate::{
    mcp_api::{self, Api, ApiResult, Error, NativeReads, Reads},
    native_transport::Authority,
    node_api::{self, Backend, Node},
};
use serde_json::{Value, json};
use std::{
    cell::RefCell,
    io::{BufRead, Write},
    rc::Rc,
};
pub const LIMIT: usize = 1_048_576;
pub struct Pipe<I: BufRead, O: Write> {
    pub input: I,
    pub output: O,
    sequence: u64,
}
impl<I: BufRead, O: Write> Pipe<I, O> {
    pub fn new(input: I, output: O) -> Self {
        Self {
            input,
            output,
            sequence: 0,
        }
    }
    pub fn read(&mut self) -> Result<Value, String> {
        let mut b = Vec::new();
        std::io::Read::take(&mut self.input, (LIMIT + 1) as u64)
            .read_until(b'\n', &mut b)
            .map_err(|_| "REQUEST_PIPE_UNKNOWN")?;
        if b.len() > LIMIT || b.last() != Some(&b'\n') {
            return Err("REQUEST_PIPE_UNKNOWN".into());
        }
        serde_json::from_slice(&b).map_err(|_| "REQUEST_PIPE_UNKNOWN".into())
    }
    pub fn write(&mut self, value: &Value) -> Result<(), String> {
        let mut b = serde_json::to_vec(value).map_err(|_| "REQUEST_PIPE_UNKNOWN")?;
        b.push(b'\n');
        if b.len() > LIMIT {
            return Err("REQUEST_TOO_LARGE".into());
        }
        self.output
            .write_all(&b)
            .and_then(|_| self.output.flush())
            .map_err(|_| "REQUEST_PIPE_UNKNOWN".into())
    }
    fn callback(
        &mut self,
        kind: &str,
        operation: &str,
        arguments: Value,
        operation_id: Option<&str>,
    ) -> ApiResult {
        self.sequence += 1;
        let id = self.sequence;
        self.write(&json!({"kind":kind,"id":id,"operation":operation,"arguments":arguments,"operation_id":operation_id})).map_err(|_| Error::from("DELIVERY_UNKNOWN"))?;
        let response = self.read().map_err(|_| Error::from("DELIVERY_UNKNOWN"))?;
        if response["id"] != id
            || response.get("result").is_some() == response.get("error").is_some()
        {
            return Err("DELIVERY_UNKNOWN".into());
        }
        if let Some(e) = response.get("error") {
            return Err(Error {
                code: e["code"].as_str().unwrap_or("DELIVERY_UNKNOWN").to_owned(),
                operation_id: e["operation_id"].as_str().map(str::to_owned),
                provider: e["provider"].as_bool().unwrap_or(false),
            });
        }
        Ok(response["result"].clone())
    }
}
pub struct PipeAuthority<I: BufRead, O: Write>(Rc<RefCell<Pipe<I, O>>>);
impl<I: BufRead, O: Write> Authority for PipeAuthority<I, O> {
    fn call(&mut self, op: &str, p: Value, id: &str) -> Result<Value, String> {
        self.0
            .borrow_mut()
            .callback("authority", op, p, Some(id))
            .map_err(|e| e.code)
    }
}
struct Delegated<I: BufRead, O: Write>(Rc<RefCell<Pipe<I, O>>>);
impl<I: BufRead, O: Write> Backend for Delegated<I, O> {
    fn invoke(&mut self, op: &str, a: Value) -> ApiResult {
        self.0.borrow_mut().callback("integration", op, a, None)
    }
    fn task(&mut self, r: Value) -> ApiResult {
        self.0
            .borrow_mut()
            .callback("integration", "task", json!({"reference":r}), None)
    }
    fn prepared(&mut self, r: Value) -> ApiResult {
        self.0
            .borrow_mut()
            .callback("integration", "prepared", json!({"reference":r}), None)
    }
}
fn api_call<A: Authority, R: Reads>(api: &mut Api<A, R>, op: &str, a: &Value) -> ApiResult {
    match op {
        "internal.task" => api.task(&a["reference"]),
        "internal.tasks" => api.tasks().map(|t| json!(t)),
        "internal.host_readback" => Ok(mcp_api::host_readback(a["receipt"].clone(), &a["turn"])),
        _ => api.invoke(op, a),
    }
}
pub fn serve<I: BufRead, O: Write>(input: I, output: O) -> Result<(), String> {
    let pipe = Rc::new(RefCell::new(Pipe::new(input, output)));
    let request = pipe.borrow_mut().read()?;
    let result = execute(pipe.clone(), request);
    let value = match result {
        Ok(result) => json!({"kind":"result","result":result}),
        Err(e) => {
            json!({"kind":"result","error":{"code":e.code,"operation_id":e.operation_id,"provider":e.provider}})
        }
    };
    let written = pipe.borrow_mut().write(&value);
    written
}
fn execute<I: BufRead, O: Write>(pipe: Rc<RefCell<Pipe<I, O>>>, r: Value) -> ApiResult {
    if r["protocol"] != "CLINX_REQUEST_PIPE_V1" || !r["arguments"].is_object() {
        return Err("REQUEST_PIPE_INVALID".into());
    }
    let op = r["operation"].as_str().ok_or("REQUEST_PIPE_INVALID")?;
    if r["mode"] == "config" {
        return crate::runtime_config::invoke(op, &r["arguments"], |op, args| {
            pipe.borrow_mut()
                .callback("config", op, args, None)
                .map_err(|e| e.code)
        })
        .map_err(Error::from);
    }
    if r["mode"] == "node_owner" {
        return crate::node_owner::handle(&r["arguments"], |op, args| {
            pipe.borrow_mut()
                .callback("owner", op, args, None)
                .map_err(|e| e.code)
        })
        .map_err(Error::from);
    }
    if r["mode"] == "centre" {
        return crate::node_centre::handle(&r["arguments"], |op, args| {
            pipe.borrow_mut()
                .callback("network", op, args, None)
                .map_err(|e| e.code)
        })
        .map_err(Error::from);
    }
    if r["mode"] == "network" {
        return crate::node_network::command(op, &r["arguments"]).map_err(Error::from);
    }
    if r["mode"] == "delegated_node" {
        let config = serde_json::from_value(r["node"].clone())
            .map_err(|_| Error::from("NODE_CONFIG_INVALID"))?;
        return Node {
            backend: Delegated(pipe),
            config,
        }
        .invoke(op, &r["arguments"]);
    }
    if r["mode"] != "node" && r["mode"] != "api" {
        return Err("REQUEST_PIPE_INVALID".into());
    }
    let config: mcp_api::Config = serde_json::from_value(r["config"].clone())
        .map_err(|_| Error::from("API_CONFIG_INVALID"))?;
    let mut api = Api {
        authority: PipeAuthority(pipe),
        reads: NativeReads(config.clone()),
        config,
    };
    if r["mode"] == "node" {
        let config: node_api::Config = serde_json::from_value(r["node"].clone())
            .map_err(|_| Error::from("NODE_CONFIG_INVALID"))?;
        if config.node_id != api.config.runtime_host {
            return Err("NODE_TARGET_MISMATCH".into());
        }
        return Node {
            backend: api,
            config,
        }
        .invoke(op, &r["arguments"]);
    }
    api_call(&mut api, op, &r["arguments"])
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;
    #[test]
    fn retired_python_read_backend_is_rejected_without_callbacks() {
        let request = json!({"protocol":"CLINX_REQUEST_PIPE_V1","mode":"api_compat", "operation":"clinx_prepare_execution","arguments":{"approved":true,"prompt":"must not execute"}});
        let mut input = serde_json::to_vec(&request).unwrap();
        input.push(b'\n');
        let mut output = Vec::new();
        serve(Cursor::new(input), &mut output).unwrap();
        let frames: Vec<Value> = String::from_utf8(output)
            .unwrap()
            .lines()
            .map(|line| serde_json::from_str(line).unwrap())
            .collect();
        assert_eq!(frames.len(), 1);
        assert_eq!(frames[0]["kind"], "result");
        assert_eq!(frames[0]["error"]["code"], "REQUEST_PIPE_INVALID");
    }
    fn run(reply: &str) -> Vec<Value> {
        let request = json!({"protocol":"CLINX_REQUEST_PIPE_V1","mode":"api","operation":"clinx_cancel_execution","arguments":{"execution_ref":"exec_exact","approved":true},"config":{"runtime_host":"air","provider_endpoint":"/unused","provider_timeout_seconds":1,"host_aliases":{},"codex_host_ids":{}}});
        let mut input = serde_json::to_vec(&request).unwrap();
        input.push(b'\n');
        input.extend_from_slice(reply.as_bytes());
        let mut out = Vec::new();
        serve(Cursor::new(input), &mut out).unwrap();
        String::from_utf8(out)
            .unwrap()
            .lines()
            .map(|l| serde_json::from_str(l).unwrap())
            .collect()
    }
    #[test]
    fn callback_preserves_exact_operation_identity_and_does_not_retry_unknown() {
        for reply in [
            "",
            "{}\n",
            "{\"id\":2,\"result\":{}}\n",
            "{\"id\":1,\"error\":{\"code\":\"DELIVERY_UNKNOWN\"}}\n",
            "{\"id\":1,\"result\":{},\"error\":{}}\n",
        ] {
            let frames = run(reply);
            assert_eq!(frames.len(), 2); // one write only, then UNKNOWN
            assert_eq!(frames[0]["kind"], "authority");
            assert_eq!(frames[0]["operation"], "cancel");
            assert_eq!(
                frames[0]["arguments"],
                json!({"execution_ref":"exec_exact","approved":true})
            );
            assert_eq!(frames[1]["error"]["code"], "DELIVERY_UNKNOWN");
            assert_eq!(
                frames[1]["error"]["operation_id"],
                frames[0]["operation_id"]
            );
        }
    }
    #[test]
    fn bounded_frames_reject_partial_eof_and_excess_but_accept_boundary() {
        let mut p = Pipe::new(Cursor::new(b"{}"), Vec::new());
        assert!(p.read().is_err());
        let mut p = Pipe::new(
            Cursor::new(format!("{{}}{}\n", " ".repeat(LIMIT - 3))),
            Vec::new(),
        );
        assert_eq!(p.read().unwrap(), json!({}));
        let mut p = Pipe::new(Cursor::new(vec![b' '; LIMIT + 1]), Vec::new());
        assert!(p.read().is_err());
    }
}
