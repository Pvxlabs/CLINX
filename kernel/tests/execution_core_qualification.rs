#[path = "../examples/check_execution_core_contract.rs"]
mod checker;
use clinx_decision_kernel::execution_core as core;
use serde_json::json;

#[test]
fn original_offline_entrypoint_checks_all_seven_objects_and_35_frozen_cases() {
    assert_eq!(checker::check().unwrap(), (7, 35));
}

#[test]
fn original_table_integrity_rejections_are_preserved() {
    let original = core::contract();
    for mutation in 0..12 {
        let mut c = original.clone();
        match mutation {
            0 => c["schema_version"] = json!(true),
            1 => c["family"] = json!("OTHER_FAMILY"),
            2 => c["contract_version"] = json!("2.0.0"),
            3 => {
                c["objects"].as_object_mut().unwrap().remove("Task");
            }
            4 => c["objects"]["Other"] = c["objects"]["Task"].clone(),
            5 => {
                let state = c["objects"]["Task"]["states"][0].clone();
                c["objects"]["Task"]["states"]
                    .as_array_mut()
                    .unwrap()
                    .push(state);
            }
            6 => c["objects"]["Task"]["terminal"] = json!(["UNKNOWN_STATE"]),
            7 => {
                let edge = c["objects"]["Task"]["transitions"][0].clone();
                c["objects"]["Task"]["transitions"]
                    .as_array_mut()
                    .unwrap()
                    .push(edge);
            }
            8 => c["objects"]["Task"]["transitions"][0]["guards"] = json!(["unknown_guard"]),
            9 => c["objects"]["Task"]["transitions"][0]["from"] = json!("UNKNOWN_STATE"),
            10 => c["objects"]["Task"]["transitions"][0]["to"] = json!("UNKNOWN_STATE"),
            11 => {
                let terminal = c["objects"]["Task"]["terminal"][0].clone();
                c["objects"]["Task"]["transitions"][0]["from"] = terminal;
            }
            _ => unreachable!(),
        }
        assert!(
            checker::validate_contract(&c).is_err(),
            "mutation {mutation}"
        );
    }
}

#[test]
fn fact_references_merge_in_order_without_mutating_the_frozen_record() {
    let contract = json!({"contexts":{"first":{"a":1,"b":2},"second":{"a":3,"c":4}}});
    let record = json!({"id":"original","facts_refs":["first","second"],"facts":{"c":5}});
    let original = record.clone();
    assert_eq!(
        checker::materialize(&contract, &record).unwrap(),
        json!({"id":"original","facts":{"a":3,"b":2,"c":5}})
    );
    assert_eq!(record, original);
}
