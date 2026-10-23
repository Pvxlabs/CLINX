#![forbid(unsafe_code)]

use clinx_decision_kernel::evaluate_ownership;
use serde_json::{Value, json};
use std::env;
use std::hint::black_box;
use std::io::{self, Read};
use std::time::Instant;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let arguments: Vec<String> = env::args().collect();
    if arguments.len() != 3
        || !matches!(
            arguments[1].as_str(),
            "core" | "parse_evaluate_serialize" | "serde"
        )
    {
        return Err(
            "usage: clinx-kernel-bench <core|parse_evaluate_serialize|serde> <iterations>".into(),
        );
    }
    let mode = &arguments[1];
    let iterations: u64 = arguments[2].parse()?;
    if iterations == 0 || iterations > 10_000_000 {
        return Err("iterations must be between 1 and 10000000".into());
    }
    let mut encoded = String::new();
    io::stdin().read_to_string(&mut encoded)?;
    let parsed: Value = serde_json::from_str(&encoded)?;
    evaluate_ownership(&parsed)?;

    let started = Instant::now();
    for _ in 0..iterations {
        let result = if mode == "core" {
            evaluate_ownership(black_box(&parsed))?
        } else if mode == "parse_evaluate_serialize" {
            let roundtrip: Value = serde_json::from_str(black_box(&encoded))?;
            let result = evaluate_ownership(&roundtrip)?;
            black_box(serde_json::to_vec(&result)?);
            result
        } else {
            let roundtrip: Value = serde_json::from_str(black_box(&encoded))?;
            black_box(serde_json::to_vec(&roundtrip)?);
            roundtrip
        };
        black_box(result);
    }
    let elapsed = started.elapsed();
    println!(
        "{}",
        serde_json::to_string(&json!({
            "mode": mode,
            "iterations": iterations,
            "elapsed_ns": elapsed.as_nanos(),
            "authority": "NON_AUTHORITATIVE"
        }))?
    );
    Ok(())
}
