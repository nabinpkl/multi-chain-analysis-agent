//! Stub visibility registry. Per the ship-1 plan: silent stubs are
//! silent bugs. Every stubbed guardrail registers itself here, emits
//! a `tracing::warn!` once at registration, increments a hit counter
//! every time it short-circuits, and surfaces via
//! `GET /agent/diagnostics`.
//!
//! The agent UI shows a persistent banner naming each stub, why it
//! exists, and which ship promotes it. Removing a stub is a 3-line PR
//! (delete the register call, delete the StubMarker insert, swap the
//! impl).

use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::SystemTime;

use parking_lot::RwLock;
use serde::Serialize;
use tracing::warn;
use ts_rs::TS;

use super::types::StubMarker;

/// Static descriptor for a stubbed guardrail. Registered once at
/// startup; the registry tracks hit counts at runtime.
#[derive(Clone, Debug)]
pub struct StubInfo {
    pub name: &'static str,
    pub component: &'static str,
    pub reason: &'static str,
    pub promoted_in_ship: u8,
}

struct StubEntry {
    info: StubInfo,
    registered_at: SystemTime,
    hits: AtomicU64,
}

/// Process-wide registry of active stubs. Held in `Arc<StubRegistry>`
/// so all the stubbed components can `register` and `hit` against
/// the same instance.
#[derive(Default)]
pub struct StubRegistry {
    entries: RwLock<Vec<Arc<StubEntry>>>,
}

impl StubRegistry {
    pub fn new() -> Arc<Self> {
        Arc::new(Self::default())
    }

    /// Register a stub. Idempotent on `name`; subsequent calls with the
    /// same name are no-ops (the stubbed component constructs once at
    /// startup, but the call site is permitted to register defensively).
    pub fn register(&self, info: StubInfo) {
        {
            let entries = self.entries.read();
            if entries.iter().any(|e| e.info.name == info.name) {
                return;
            }
        }
        let mut entries = self.entries.write();
        if entries.iter().any(|e| e.info.name == info.name) {
            return;
        }
        warn!(
            name = info.name,
            component = info.component,
            reason = info.reason,
            promoted_in_ship = info.promoted_in_ship,
            "STUB ACTIVE: {} ({})",
            info.name,
            info.reason
        );
        entries.push(Arc::new(StubEntry {
            info,
            registered_at: SystemTime::now(),
            hits: AtomicU64::new(0),
        }));
    }

    /// Increment the hit counter for `name`. Silent if `name` is not
    /// registered; that condition shouldn't happen but we don't want
    /// stub bookkeeping to ever panic on the request path.
    pub fn hit(&self, name: &'static str) {
        let entries = self.entries.read();
        if let Some(e) = entries.iter().find(|e| e.info.name == name) {
            e.hits.fetch_add(1, Ordering::Relaxed);
        }
    }

    /// Snapshot for the diagnostics endpoint.
    pub fn snapshot(&self) -> Vec<StubInfoWire> {
        self.entries
            .read()
            .iter()
            .map(|e| StubInfoWire {
                name: e.info.name.to_string(),
                component: e.info.component.to_string(),
                reason: e.info.reason.to_string(),
                promoted_in_ship: e.info.promoted_in_ship,
                // u32 dodges bigint in TS. Hit counter caps at ~4B
                // (about 40/sec for 3 years), saturating add prevents
                // overflow if anyone ever hits that.
                hits: e
                    .hits
                    .load(Ordering::Relaxed)
                    .min(u32::MAX as u64) as u32,
                // Wallclock seconds since the Unix epoch. u32 holds
                // until 2106; sufficient for an aliveness signal.
                registered_at_s: e
                    .registered_at
                    .duration_since(SystemTime::UNIX_EPOCH)
                    .map(|d| d.as_secs().min(u32::MAX as u64) as u32)
                    .unwrap_or(0),
            })
            .collect()
    }

    /// Build a per-claim stub marker list naming the stubs that
    /// touched a claim's emission. v0 always includes policy +
    /// budget; ship 2 deletes the policy entry, ship 4 the budget
    /// entry. The frontend renders this as "via stubs: ..." on each
    /// claim card.
    pub fn markers_for_claim(&self) -> Vec<StubMarker> {
        self.entries
            .read()
            .iter()
            .filter(|e| {
                // Only stubs that affect every claim emission go on
                // the per-claim badge. Range-arm stub is per-call,
                // not per-claim, so it's excluded here.
                matches!(
                    e.info.name,
                    "policy.always_approve" | "budget.always_allow"
                )
            })
            .map(|e| StubMarker {
                name: e.info.name.to_string(),
                reason: e.info.reason.to_string(),
                promoted_in_ship: e.info.promoted_in_ship,
            })
            .collect()
    }
}

/// Wire shape for the diagnostics endpoint.
#[derive(Serialize, TS, Debug, Clone)]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
pub struct StubInfoWire {
    pub name: String,
    pub component: String,
    pub reason: String,
    pub promoted_in_ship: u8,
    pub hits: u32,
    pub registered_at_s: u32,
}
