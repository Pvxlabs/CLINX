//! Offline, additive route maintenance through the existing exclusive writer.
//! No public IPC operation exposes this path. Configuration and durable receipt
//! commit together; service activation and root-controlled policy files remain
//! the operator's responsibility.
use super::*;

impl Store {
    pub fn extend_business_policy(
        &mut self,
        next: &Policy,
        expected_authority: &str,
        expected_policy_hash: &str,
        operation_id: &str,
    ) -> Result<Value> {
        self.extend_business_policy_at(
            next,
            expected_authority,
            expected_policy_hash,
            operation_id,
            |_| Ok(()),
        )
    }
    pub fn extend_business_policy_at(
        &mut self,
        next: &Policy,
        expected_authority: &str,
        expected_policy_hash: &str,
        operation_id: &str,
        mut fault: impl FnMut(&str) -> Result<()>,
    ) -> Result<Value> {
        self.check_identity()?;
        self.verify_business()?;
        if expected_authority != self.identity {
            return Err("POLICY_AUTHORITY_CONFLICT".into());
        }
        if operation_id.is_empty() || operation_id.len() > 128 {
            return Err("INVALID_OPERATION_ID".into());
        }
        next.validate()?;
        let next_value = serde_json::to_value(next).map_err(|_| "INVALID_POLICY")?;
        let request = json!({"operation":"extend_business_policy","authority_id":expected_authority,
            "expected_policy_hash":expected_policy_hash,"policy":next_value});
        let request_hash = core::digest(&request);
        let tx = self
            .conn
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(db)?;
        let stored: String = tx
            .query_row("SELECT policy FROM business_config WHERE id=1", [], |r| {
                r.get(0)
            })
            .map_err(db)?;
        if let Some(body) = tx
            .query_row(
                "SELECT body FROM business_commands WHERE id=?",
                [operation_id],
                |r| r.get::<_, String>(0),
            )
            .optional()
            .map_err(db)?
        {
            let body = parse(body)?;
            if body["operation"] != "extend_business_policy" || body["request_hash"] != request_hash
            {
                return Err("POLICY_OPERATION_CONFLICT".into());
            }
            // A later extension is never rolled back by an old duplicate.
            return Ok(body["result"].clone());
        }
        let old: Policy = serde_json::from_str(&stored).map_err(|_| "INVALID_STORED_POLICY")?;
        let old_value = serde_json::to_value(&old).map_err(|_| "INVALID_STORED_POLICY")?;
        if core::digest(&old_value) != expected_policy_hash {
            return Err("POLICY_CAS_CONFLICT".into());
        }
        // This operation only appends exact projects/endpoints/routes. It cannot
        // widen host capabilities, relocate existing projects or replace a route.
        if old_value["workspaces"] != next_value["workspaces"]
            || old_value["host_operations"] != next_value["host_operations"]
            || old_value["derived_git_roots"] != next_value["derived_git_roots"]
            || old.lease_ms != next.lease_ms
            || old.projects.iter().any(|p| {
                !next
                    .projects
                    .iter()
                    .any(|n| n.alias == p.alias && n.host == p.host && n.cwd == p.cwd)
            })
            || old.endpoints.iter().any(|p| !next.endpoints.contains(p))
        {
            return Err("POLICY_ADDITIVE_ROUTES_ONLY".into());
        }
        let old_hosts: std::collections::BTreeSet<_> = old
            .projects
            .iter()
            .map(|p| p.host.as_str())
            .chain(old.workspaces.iter().map(|p| p.host.as_str()))
            .collect();
        for host in old_hosts {
            if next.endpoints_for(host)? != old.endpoints_for(host)? {
                return Err("POLICY_EXISTING_ROUTE_CHANGED".into());
            }
        }
        if old
            .provider_routes
            .iter()
            .any(|(h, r)| next.provider_routes.get(h) != Some(r))
        {
            return Err("POLICY_EXISTING_ROUTE_CHANGED".into());
        }
        if next.provider_routes.is_empty() || next_value == old_value {
            return Err("POLICY_EXPLICIT_NEW_ROUTE_REQUIRED".into());
        }
        let active: i64 = tx.query_row("SELECT count(*) FROM business_rows WHERE kind='execution' AND (json_extract(value,'$.states.ResourceAllocation') IS NOT 'RELEASED' OR json_extract(value,'$.states.Execution') NOT IN ('SUCCEEDED','FAILED','CANCELLED'))", [], |r| r.get(0)).map_err(db)?;
        let leases: i64 = tx
            .query_row(
                "SELECT count(*) FROM leases WHERE state='ACTIVE'",
                [],
                |r| r.get(0),
            )
            .map_err(db)?;
        if active != 0 || leases != 0 {
            return Err("POLICY_ACTIVE_EXECUTIONS_OR_LEASES".into());
        }
        let result = json!({"contract":"CLINX_ADDITIVE_POLICY_V1","status":"POLICY_EXTENDED_NOT_ACTIVATED",
            "authority_id":self.identity,"operation_id":operation_id,"before":old_value,"after":next_value,
            "before_hash":expected_policy_hash,"after_hash":core::digest(&next_value)});
        let (position, previous) = tx
            .query_row(
                "SELECT position,hash FROM business_commands ORDER BY position DESC LIMIT 1",
                [],
                |r| Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?)),
            )
            .optional()
            .map_err(db)?
            .unwrap_or((0, String::new()));
        let body = json!({"authority_id":self.identity,"position":position+1,"previous":previous,
            "operation_id":operation_id,"operation":"extend_business_policy","role":"OFFLINE_MAINTENANCE",
            "request_hash":request_hash,"changes":[],"result":result});
        if core::canonical(&body).len() > MAX_BUSINESS_RECEIPT_BYTES {
            return Err("BUSINESS_RECEIPT_CAPACITY_EXCEEDED".into());
        }
        tx.execute(
            "UPDATE business_config SET policy=? WHERE id=1",
            [core::canonical(&next_value)],
        )
        .map_err(db)?;
        tx.execute(
            "INSERT INTO business_commands VALUES(?,?,?,?,?,?,?)",
            params![
                position + 1,
                operation_id,
                "OFFLINE_MAINTENANCE",
                request_hash,
                previous,
                core::digest(&body),
                core::canonical(&body)
            ],
        )
        .map_err(db)?;
        fault("BEFORE_POLICY_COMMIT")?;
        tx.commit().map_err(db)?;
        fault("AFTER_POLICY_COMMIT")?;
        Ok(result)
    }
}
