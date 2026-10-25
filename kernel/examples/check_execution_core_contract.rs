//! Offline qualification of the frozen execution contract, without runtime IO.
use clinx_decision_kernel::execution_core as core;
use serde_json::{Map, Value};
use std::collections::BTreeSet;

fn strings(value: &Value) -> Result<BTreeSet<&str>, String> {
    value
        .as_array()
        .ok_or_else(|| "expected string list".to_owned())?
        .iter()
        .map(|v| v.as_str().ok_or_else(|| "expected string".to_owned()))
        .collect()
}

pub fn validate_contract(contract: &Value) -> Result<(), String> {
    if contract["schema_version"].as_i64() != Some(1)
        || contract["family"] != core::FAMILY
        || contract["contract_version"] != core::VERSION
    {
        return Err("unsupported contract".into());
    }
    let objects = contract["objects"].as_object().ok_or("object coverage")?;
    let expected: BTreeSet<_> = [
        "Task",
        "Execution",
        "Attempt",
        "ProviderSession",
        "RuntimeWorker",
        "ResourceAllocation",
        "Event",
    ]
    .into_iter()
    .collect();
    if objects.keys().map(String::as_str).collect::<BTreeSet<_>>() != expected {
        return Err("object coverage".into());
    }
    let guards: BTreeSet<_> = [
        "children_resolved",
        "admission",
        "fence",
        "binding",
        "provider_live",
        "provider_terminal",
        "not_dispatched",
        "deliveries",
        "retry",
        "finalizer",
        "release",
        "expired",
        "stopped",
        "incarnation",
        "event_bundle",
        "grant",
        "new_incarnation",
    ]
    .into_iter()
    .collect();
    for (name, object) in objects {
        let states = strings(&object["states"])?;
        let terminal = strings(&object["terminal"])?;
        if states.len() != object["states"].as_array().unwrap().len()
            || !terminal.is_subset(&states)
        {
            return Err("invalid states".into());
        }
        let mut keys = BTreeSet::new();
        for edge in object["transitions"]
            .as_array()
            .ok_or("invalid transitions")?
        {
            let from = edge["from"].as_str().ok_or("invalid transition endpoint")?;
            let to = edge["to"].as_str().ok_or("invalid transition endpoint")?;
            let action = edge["action"].as_str().ok_or("invalid transition action")?;
            if !keys.insert((from, action, to)) || !strings(&edge["guards"])?.is_subset(&guards) {
                return Err("ambiguous transition or unknown guard".into());
            }
            if !states.contains(to)
                || terminal.contains(from)
                || !(states.contains(from) || name == "Event" && from == "ABSENT")
            {
                return Err("invalid transition endpoint".into());
            }
        }
    }
    Ok(())
}

pub fn materialize(contract: &Value, record: &Value) -> Result<Value, String> {
    let mut result = record.as_object().ok_or("invalid golden record")?.clone();
    let mut facts = Map::new();
    if let Some(refs) = result.remove("facts_refs") {
        for name in refs.as_array().ok_or("invalid fact references")? {
            let context = &contract["contexts"][name.as_str().ok_or("invalid fact reference")?];
            facts.extend(context.as_object().ok_or("unknown fact reference")?.clone());
        }
    }
    if let Some(own) = result.remove("facts") {
        facts.extend(own.as_object().ok_or("invalid facts")?.clone());
    }
    result.insert("facts".into(), Value::Object(facts));
    Ok(Value::Object(result))
}

pub fn check() -> Result<(usize, usize), String> {
    let contract = core::contract();
    validate_contract(contract)?;
    let cases = contract["cases"].as_array().ok_or("missing golden cases")?;
    for case in cases {
        let actual = core::transition(&materialize(contract, &case["record"])?);
        if case["expected"] != actual {
            return Err(format!(
                "{}: expected {}, got {actual}",
                case["name"], case["expected"]
            ));
        }
    }
    Ok((contract["objects"].as_object().unwrap().len(), cases.len()))
}

#[cfg(not(test))]
fn main() {
    match check() {
        Ok((objects, cases)) => println!(
            "PASS {} {}: {objects} objects, {cases} golden cases",
            core::FAMILY,
            core::VERSION
        ),
        Err(error) => {
            eprintln!("{error}");
            std::process::exit(1);
        }
    }
}
