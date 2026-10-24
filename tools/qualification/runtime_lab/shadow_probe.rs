//! Stage-4 timing adapter only. Links the unchanged Stage-3 library.
use clinx_decision_kernel::execution_core;
use serde_json::{Value, json};
use std::{fs, time::Instant};

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let records: Vec<Value> = serde_json::from_slice(&fs::read(&args[1]).unwrap()).unwrap();
    // Initialize lazy contract and warm all input cases before sampling.
    for r in records.iter().cycle().take(350) {
        std::hint::black_box(execution_core::transition(r));
    }
    let mut samples = Vec::new();
    let mut verdicts = Vec::new();
    for r in records.iter().cycle().take(20000) {
        let start = Instant::now();
        let verdict = std::hint::black_box(execution_core::transition(std::hint::black_box(r)));
        samples.push(start.elapsed().as_nanos() as u64);
        verdicts.push(verdict);
    }
    println!("{}", json!({"ns": samples, "verdicts": verdicts}));
}
