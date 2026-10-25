//! Bilateral Node sharing contract. This is routing scope, never task authority.
use crate::node_tls::{Result, now};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
pub const PROTOCOL: &str = "clinx-node-v1";
pub const PROVIDER: &str = "codex_app_server";
fn providers() -> Vec<String> {
    vec![PROVIDER.into()]
}
fn projects() -> Vec<String> {
    vec!["*".into()]
}
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct Scope {
    pub user_scope: String,
    #[serde(default)]
    pub read_sessions: bool,
    #[serde(default)]
    pub execute_tasks: bool,
    #[serde(default = "providers")]
    pub providers: Vec<String>,
    #[serde(default = "projects")]
    pub projects: Vec<String>,
    #[serde(default)]
    pub revoked: bool,
    #[serde(default = "now")]
    pub updated_at: String,
}
fn text(s: &str, limit: usize) -> bool {
    !s.trim().is_empty()
        && s.chars().count() <= limit
        && !s.chars().any(|c| c < ' ' || c == '\u{7f}')
}
impl Scope {
    pub fn parse(value: &Value) -> Result<Self> {
        let mut v: Self =
            serde_json::from_value(value.clone()).map_err(|_| "INVALID_SHARING_SCOPE")?;
        if !text(&v.user_scope, 256) || v.providers.len() > 64 || v.projects.len() > 64 {
            return Err("INVALID_SHARING_SCOPE".into());
        }
        v.user_scope = v.user_scope.trim().into();
        for (list, limit) in [(&mut v.providers, 128), (&mut v.projects, 1024)] {
            let mut normalized = Vec::new();
            for s in list.iter() {
                if !text(s, limit) {
                    return Err("INVALID_SHARING_SCOPE".into());
                }
                let s = s.trim().to_owned();
                if !normalized.contains(&s) {
                    normalized.push(s);
                }
            }
            *list = normalized;
        }
        Ok(v)
    }
    pub fn value(&self) -> Value {
        serde_json::to_value(self).unwrap()
    }
    pub fn allows(&self, operation: &str) -> bool {
        !self.revoked
            && self.providers.iter().any(|p| p == PROVIDER)
            && if operation.starts_with("execution.") {
                self.execute_tasks
            } else {
                matches!(
                    operation,
                    "session.read"
                        | "session.status"
                        | "session.inventory"
                        | "observation.upload"
                        | "node.status"
                ) && self.read_sessions
            }
    }
    pub fn project(&self, cwd: &Value) -> bool {
        self.projects.iter().any(|p| p == "*" || cwd == p)
    }
}
pub fn intersect(local: Option<Scope>, centre: Option<Scope>) -> Result<Scope> {
    let (Some(l), Some(c)) = (local.clone(), centre.clone()) else {
        return Scope::parse(
            &json!({"user_scope":centre.or(local).map(|s|s.user_scope).unwrap_or("default".into()),"revoked":true}),
        );
    };
    if l.revoked || c.revoked {
        return Scope::parse(&json!({"user_scope":c.user_scope,"revoked":true}));
    }
    if l.user_scope != c.user_scope {
        return Err("USER_SCOPE_DENIED".into());
    }
    Ok(Scope {
        user_scope: c.user_scope,
        read_sessions: l.read_sessions && c.read_sessions,
        execute_tasks: l.execute_tasks && c.execute_tasks,
        providers: l
            .providers
            .iter()
            .filter(|p| c.providers.contains(p))
            .cloned()
            .collect(),
        projects: if l.projects.iter().any(|p| p == "*") {
            c.projects
        } else if c.projects.iter().any(|p| p == "*") {
            l.projects
        } else {
            l.projects
                .into_iter()
                .filter(|p| c.projects.contains(p))
                .collect()
        },
        revoked: false,
        updated_at: l.updated_at.max(c.updated_at),
    })
}
pub fn optional(v: &Value) -> Result<Option<Scope>> {
    if v.is_null() {
        Ok(None)
    } else {
        Scope::parse(v).map(Some)
    }
}
pub fn stale(record: &Value) -> bool {
    let Some(s) = record["last_seen"].as_str() else {
        return true;
    };
    let Ok(t) = chrono::DateTime::parse_from_rfc3339(s) else {
        return true;
    };
    let age = (std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
        - t.timestamp_micros() as f64 / 1e6)
        .max(0.0);
    age > record["stale_after_seconds"].as_f64().unwrap_or(120.0)
}
