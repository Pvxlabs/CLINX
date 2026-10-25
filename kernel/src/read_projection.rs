//! Shared bounded read-plane codecs and read-only SQLite snapshots.
//! No Authority write or Provider execution capability is exposed here.
use crate::node_tls::Result;
use rusqlite::{Connection, OpenFlags, Row, types::ValueRef};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    path::{Component, Path, PathBuf},
    time::{Duration, SystemTime, UNIX_EPOCH},
};
pub const SQL_STEPS: usize = 250_000;
pub fn now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}
pub fn sha(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}
pub fn resolve(p: &Path) -> PathBuf {
    // Python Path.resolve(strict=False): resolve existing symlinks as well as
    // lexical '..' in the non-existing suffix. No files/directories are created.
    let p = if p.is_absolute() {
        p.to_path_buf()
    } else {
        std::env::current_dir().unwrap_or_default().join(p)
    };
    let mut out = PathBuf::new();
    for component in p.components() {
        match component {
            Component::ParentDir => {
                out.pop();
            }
            Component::CurDir => {}
            _ => out.push(component.as_os_str()),
        }
        if let Ok(real) = out.canonicalize() {
            out = real;
        }
    }
    out
}
pub fn readonly(path: &Path, code: &str, budget: bool) -> Result<Connection> {
    let c = Connection::open_with_flags(
        path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .map_err(|_| code)?;
    c.busy_timeout(Duration::from_secs(2)).map_err(|_| code)?;
    c.execute_batch("PRAGMA query_only=ON; BEGIN;")
        .map_err(|_| code)?;
    if budget {
        let mut steps = 0;
        c.progress_handler(
            1000,
            Some(move || {
                steps += 1000;
                steps > SQL_STEPS
            }),
        );
    }
    Ok(c)
}
pub fn row(r: &Row<'_>) -> rusqlite::Result<Value> {
    let mut m = serde_json::Map::new();
    for i in 0..r.as_ref().column_count() {
        let v = match r.get_ref(i)? {
            ValueRef::Null => Value::Null,
            ValueRef::Integer(n) => json!(n),
            ValueRef::Real(n) => json!(n),
            ValueRef::Text(s) => {
                json!(std::str::from_utf8(s).map_err(|_| rusqlite::Error::InvalidQuery)?)
            }
            _ => return Err(rusqlite::Error::InvalidQuery),
        };
        m.insert(r.as_ref().column_name(i)?.into(), v);
    }
    Ok(Value::Object(m))
}
pub fn rows(
    c: &Connection,
    sql: &str,
    args: &[rusqlite::types::Value],
    code: &str,
) -> Result<Vec<Value>> {
    let mut stmt = c.prepare(sql).map_err(|e| sql_error(&e, code))?;
    stmt.query_map(rusqlite::params_from_iter(args), row)
        .map_err(|e| sql_error(&e, code))?
        .collect::<rusqlite::Result<Vec<_>>>()
        .map_err(|e| sql_error(&e, code))
}
pub fn sql_error(e: &rusqlite::Error, code: &str) -> String {
    if matches!(e,rusqlite::Error::SqliteFailure(s,_) if s.code==rusqlite::ErrorCode::OperationInterrupted)
    {
        "NATIVE_HISTORY_BUDGET_EXHAUSTED".into()
    } else {
        code.into()
    }
}
pub fn sql(v: &Value) -> rusqlite::types::Value {
    use rusqlite::types::Value as S;
    match v {
        Value::Null => S::Null,
        Value::Bool(b) => S::Integer(i64::from(*b)),
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                S::Integer(i)
            } else {
                S::Real(n.as_f64().unwrap_or(0.0))
            }
        }
        Value::String(s) => S::Text(s.clone()),
        _ => S::Text(compact(v)),
    }
}
pub fn columns(c: &Connection, table: &str) -> Result<Vec<String>> {
    Ok(rows(
        c,
        &format!("PRAGMA table_info({table})"),
        &[],
        "NATIVE_SCHEMA_UNSUPPORTED",
    )?
    .iter()
    .filter_map(|r| r["name"].as_str().map(str::to_owned))
    .collect())
}
fn float_repr(n: &serde_json::Number) -> String {
    let s = n.to_string();
    let sign = if s.starts_with('-') { "-" } else { "" };
    let unsigned = s.trim_start_matches('-');
    let (mantissa, exp) = unsigned
        .split_once(['e', 'E'])
        .map(|(m, e)| (m, e.parse::<i32>().unwrap()))
        .unwrap_or((unsigned, 0));
    let point = mantissa.find('.').unwrap_or(mantissa.len()) as i32;
    let raw = mantissa.replace('.', "");
    let leading = raw.bytes().take_while(|b| *b == b'0').count();
    let digits = raw[leading..].trim_end_matches('0');
    if digits.is_empty() {
        return format!("{sign}0.0");
    }
    let exponent = point - 1 + exp - leading as i32;
    if (-4..16).contains(&exponent) {
        let point = 1 + exponent;
        if point <= 0 {
            format!("{sign}0.{}{digits}", "0".repeat((-point) as usize))
        } else if point as usize >= digits.len() {
            format!(
                "{sign}{digits}{}.0",
                "0".repeat(point as usize - digits.len())
            )
        } else {
            format!(
                "{sign}{}.{}",
                &digits[..point as usize],
                &digits[point as usize..]
            )
        }
    } else {
        format!(
            "{sign}{}{}e{}{:02}",
            &digits[..1],
            if digits.len() > 1 {
                format!(".{}", &digits[1..])
            } else {
                String::new()
            },
            if exponent >= 0 { "+" } else { "-" },
            exponent.abs()
        )
    }
}
pub fn compact(v: &Value) -> String {
    // Match deployed json.dumps(ensure_ascii=False,sort_keys=True,separators=(',',':')).
    // Existing digests and cursor scopes remain valid across the migration.
    match v {
        Value::Number(n) if n.is_f64() => float_repr(n),
        Value::Array(a) => format!("[{}]", a.iter().map(compact).collect::<Vec<_>>().join(",")),
        Value::Object(m) => {
            let mut keys = m.keys().collect::<Vec<_>>();
            keys.sort();
            format!(
                "{{{}}}",
                keys.into_iter()
                    .map(|k| format!("{}:{}", serde_json::to_string(k).unwrap(), compact(&m[k])))
                    .collect::<Vec<_>>()
                    .join(",")
            )
        }
        _ => v.to_string(),
    }
}
pub fn truth(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(m) => !m.is_empty(),
        Value::Number(n) => n.as_f64() != Some(0.0),
    }
}
pub fn text(v: &Value) -> &str {
    v.as_str().unwrap_or("")
}
pub fn truncate(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}
pub fn bytes(s: &str, n: usize) -> String {
    let mut end = n.min(s.len());
    while !s.is_char_boundary(end) {
        end -= 1;
    }
    s[..end].into()
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn deployed_python_digest_bytes_survive_float_unicode_and_control_fields() {
        let v = json!({"unicode":"字\n\u{0000}","proof":[1e-5,1e-4,1e16,1e15,-0.0,1e-7]});
        assert_eq!(
            compact(&v),
            "{\"proof\":[1e-05,0.0001,1e+16,1000000000000000.0,-0.0,1e-07],\"unicode\":\"字\\n\\u0000\"}"
        );
    }
}
