//! Primitive-binding ledger (ship 3). Captures every successful
//! primitive output during a thread's lifetime so the policy gate can
//! verify that numbers and entities cited by the model trace back to
//! real data we returned, not values invented out of whole cloth.
//!
//! # Why this exists
//!
//! Ship 2.7's adversarial dogfood exposed that the narrative-vs-claim
//! cross-check is *internal consistency*, not authenticity. A model
//! that fabricates a Claim with invented numbers and then cites the
//! same numbers in narrative passes the cross-check (because the two
//! sides agree) but lies to the user. Ship 3 closes that gap by
//! recording, per primitive call, the structured numbers + entities
//! the runtime actually produced, then checking each emitted Claim
//! against that store.
//!
//! # What gets recorded
//!
//! For each primitive output:
//! - Every numeric leaf in the output JSON, classified by field-name
//!   path into the same `UnitClass` taxonomy the regex + LLM
//!   extractors use. Sol-bearing fields (`*volume*`, `*sol*`,
//!   `*lamports*`) become `UnitClass::Sol`; counts (`*degree*`,
//!   `*count*`, `*size*`) become `UnitClass::Count`; community-id
//!   fields become `UnitClass::CommunityId`; everything else
//!   `UnitClass::Raw` (skipped by the compare).
//! - Every entity declared in the primitive's provenance: wallets by
//!   base58 addr, communities by id. Used to validate Claim
//!   provenance refs, so a model emitting `ProvenanceRef::Wallet {
//!   addr: <invented> }` retracts.
//!
//! # Lifecycle
//!
//! Per-thread, ring-buffered to `MAX_THREAD_BINDINGS`. Survives across
//! turns within a thread so a follow-up turn can interpret prior
//! data without re-fetching. In-process only (named by the
//! `thread.in_memory_only` stub); restart resets the store.

use std::collections::{HashSet, VecDeque};

use serde::Serialize;

use crate::agent::policy_crosscheck::{ExtractedNumber, UnitClass};
use crate::agent::types::ProvenanceRef;

/// FIFO cap on per-thread bindings. 64 covers tens of turns of typical
/// dogfood without unbounded growth. Tunable; revisit if real load
/// surfaces eviction trouble.
pub const MAX_THREAD_BINDINGS: usize = 64;

/// One captured primitive output. Built by the loop tool adapter
/// immediately after a successful dispatch and pushed into the
/// per-session buffer; merged into the thread's persistent store at
/// session end.
#[derive(Debug, Clone, Serialize)]
pub struct PrimitiveBinding {
    /// Stable id for ledger / debug correlation. Format:
    /// `<primitive_name>:<ulid>`.
    pub call_id: String,
    pub primitive: String,
    pub captured_at_ms: u64,
    pub provenance: Vec<ProvenanceRef>,
    /// Flat list of every numeric value the primitive output JSON
    /// contained, classified by field path. Skipped fields (UnitClass
    /// Raw on noise) included as Raw so the breakdown is auditable.
    #[serde(serialize_with = "serialize_numbers")]
    pub numbers: Vec<ExtractedNumber>,
    pub entities: BindingEntities,
}

/// Wallets, communities, time ranges declared in this binding's
/// provenance. Flattened for fast `.contains()` checks during the
/// claim provenance-ref validation step.
#[derive(Debug, Clone, Default, Serialize)]
pub struct BindingEntities {
    pub wallets: HashSet<String>,
    pub communities: HashSet<u32>,
    /// `true` if any provenance ref carried a `TimeRange`. Used as a
    /// permissive guard: claim TimeRange refs are accepted whenever
    /// the binding store has at least one TimeRange-bearing primitive.
    pub has_time_range: bool,
}

/// Per-thread ring buffer of primitive bindings. Cheap to clone (the
/// only consumer outside the loop is the policy gate, which holds a
/// reference, not a clone). Eviction is FIFO at `MAX_THREAD_BINDINGS`.
#[derive(Debug, Clone, Default)]
pub struct PrimitiveBindingStore {
    bindings: VecDeque<PrimitiveBinding>,
}

impl PrimitiveBindingStore {
    pub fn new() -> Self {
        Self::default()
    }

    /// Append a binding. Evicts the oldest when the store overflows.
    pub fn record(&mut self, binding: PrimitiveBinding) {
        self.bindings.push_back(binding);
        while self.bindings.len() > MAX_THREAD_BINDINGS {
            self.bindings.pop_front();
        }
    }

    pub fn len(&self) -> usize {
        self.bindings.len()
    }

    pub fn is_empty(&self) -> bool {
        self.bindings.is_empty()
    }

    pub fn iter(&self) -> impl Iterator<Item = &PrimitiveBinding> {
        self.bindings.iter()
    }

    /// Flat list of every cross-check-able number across all
    /// bindings. Cloned because the compare consumes
    /// `&[ExtractedNumber]`. Cheap; bindings are bounded.
    pub fn all_numbers(&self) -> Vec<ExtractedNumber> {
        self.bindings
            .iter()
            .flat_map(|b| b.numbers.iter().cloned())
            .collect()
    }

    pub fn all_wallets(&self) -> HashSet<String> {
        let mut out = HashSet::new();
        for b in &self.bindings {
            for w in &b.entities.wallets {
                out.insert(w.clone());
            }
        }
        out
    }

    pub fn all_communities(&self) -> HashSet<u32> {
        let mut out = HashSet::new();
        for b in &self.bindings {
            for c in &b.entities.communities {
                out.insert(*c);
            }
        }
        out
    }

    pub fn has_any_time_range(&self) -> bool {
        self.bindings.iter().any(|b| b.entities.has_time_range)
    }

    /// Concatenate every binding's call_id in chronological order.
    /// Used by the ledger PolicyVerdict event so ship 6's eval suite
    /// can replay "what primitives did this turn rely on".
    pub fn call_ids(&self) -> Vec<String> {
        self.bindings.iter().map(|b| b.call_id.clone()).collect()
    }
}

/// Build a `PrimitiveBinding` from a primitive's dispatch output.
/// `value_json` is walked for numbers; `provenance` is walked for
/// entities. Both walks are deterministic and synchronous.
pub fn build_binding(
    primitive: &str,
    call_id: String,
    captured_at_ms: u64,
    value_json: &serde_json::Value,
    provenance: &[ProvenanceRef],
) -> PrimitiveBinding {
    let mut numbers: Vec<ExtractedNumber> = Vec::new();
    walk_numbers("", value_json, &mut numbers);

    // Provenance also carries explicit `Number` refs with a metric
    // string. Use the existing `classify_metric`-style logic to fold
    // them into the same number set.
    for r in provenance {
        if let ProvenanceRef::Number { metric, value, .. } = r {
            numbers.push(ExtractedNumber {
                value: *value,
                unit_class: classify_field_name(metric),
                hedged: false,
            });
        }
    }

    PrimitiveBinding {
        call_id,
        primitive: primitive.to_string(),
        captured_at_ms,
        provenance: provenance.to_vec(),
        numbers,
        entities: collect_entities(provenance),
    }
}

/// Recursively walk a JSON value, classifying each numeric leaf by
/// its field-name path. Arrays inherit their parent field name (so
/// `top_wallets[0].volume` classifies on `volume`). Objects descend
/// keyed by field name.
fn walk_numbers(field_path: &str, v: &serde_json::Value, out: &mut Vec<ExtractedNumber>) {
    match v {
        serde_json::Value::Number(n) => {
            if let Some(value) = n.as_f64() {
                let unit_class = classify_field_name(field_path);
                out.push(ExtractedNumber {
                    value,
                    unit_class,
                    hedged: false,
                });
            }
        }
        serde_json::Value::Array(arr) => {
            for item in arr {
                walk_numbers(field_path, item, out);
            }
        }
        serde_json::Value::Object(obj) => {
            for (k, child) in obj {
                walk_numbers(k, child, out);
            }
        }
        _ => {}
    }
}

/// Map a field name to a unit class. Conservative: unrecognized names
/// go to `Raw` so they don't accidentally satisfy a typed claim
/// number. Mirrors `policy_crosscheck::classify_metric` so the
/// taxonomy is consistent across the regex extractor, LLM extractor,
/// and binding walker.
fn classify_field_name(name: &str) -> UnitClass {
    let lower = name.to_ascii_lowercase();
    // CommunityId first: a field literally called `community_id` is a
    // community id, not a count, even though "id" might suggest "raw".
    if lower == "community_id" || lower.contains("community_id") {
        return UnitClass::CommunityId;
    }
    if lower.contains("sol")
        || lower.contains("lamport")
        || lower.contains("volume")
        || lower.contains("inflow")
        || lower.contains("outflow")
        || lower.contains("inbound")
        || lower.contains("outbound")
    {
        return UnitClass::Sol;
    }
    if lower.contains("count")
        || lower.contains("degree")
        || lower.contains("size")
        || lower.contains("connection")
        || lower == "tx"
        || lower.contains("edges")
        || lower == "edge_count"
        || lower.contains("counterparty")
        || lower.contains("counterparties")
        || lower.contains("nodes")
    {
        return UnitClass::Count;
    }
    UnitClass::Raw
}

fn collect_entities(provenance: &[ProvenanceRef]) -> BindingEntities {
    let mut e = BindingEntities::default();
    for r in provenance {
        match r {
            ProvenanceRef::Wallet { addr, .. } => {
                e.wallets.insert(addr.clone());
            }
            ProvenanceRef::Community { id } => {
                e.communities.insert(*id);
            }
            ProvenanceRef::TimeRange { .. } => {
                e.has_time_range = true;
            }
            // Edge / Number refs don't add entities; numbers are
            // already collected via the dedicated number walk.
            _ => {}
        }
    }
    e
}

fn serialize_numbers<S>(
    nums: &Vec<ExtractedNumber>,
    s: S,
) -> Result<S::Ok, S::Error>
where
    S: serde::Serializer,
{
    use serde::ser::SerializeSeq;
    let mut seq = s.serialize_seq(Some(nums.len()))?;
    for n in nums {
        let unit = match n.unit_class {
            UnitClass::Sol => "sol",
            UnitClass::Count => "count",
            UnitClass::CommunityId => "community_id",
            UnitClass::Raw => "raw",
        };
        seq.serialize_element(&serde_json::json!({
            "value": n.value,
            "unit_class": unit,
            "hedged": n.hedged,
        }))?;
    }
    seq.end()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::agent::types::ProvenanceRef;
    use serde_json::json;

    #[test]
    fn classify_field_basics() {
        assert!(matches!(classify_field_name("volume"), UnitClass::Sol));
        assert!(matches!(classify_field_name("total_volume"), UnitClass::Sol));
        assert!(matches!(classify_field_name("internal_volume"), UnitClass::Sol));
        assert!(matches!(classify_field_name("sol_inflow"), UnitClass::Sol));
        assert!(matches!(classify_field_name("degree"), UnitClass::Count));
        assert!(matches!(classify_field_name("sol_degree"), UnitClass::Sol));
        assert!(matches!(classify_field_name("size"), UnitClass::Count));
        assert!(matches!(classify_field_name("edge_count"), UnitClass::Count));
        assert!(matches!(classify_field_name("community_id"), UnitClass::CommunityId));
        assert!(matches!(classify_field_name("age_in_window_secs"), UnitClass::Raw));
    }

    #[test]
    fn walk_numbers_flat_object() {
        let v = json!({
            "degree": 33,
            "volume": 12.4,
            "community_id": 42,
            "age_in_window_secs": 50
        });
        let mut out = Vec::new();
        walk_numbers("", &v, &mut out);
        assert_eq!(out.len(), 4);
        let counts = out.iter().filter(|n| matches!(n.unit_class, UnitClass::Count)).count();
        let sols = out.iter().filter(|n| matches!(n.unit_class, UnitClass::Sol)).count();
        let cids = out.iter().filter(|n| matches!(n.unit_class, UnitClass::CommunityId)).count();
        assert_eq!(counts, 1);
        assert_eq!(sols, 1);
        assert_eq!(cids, 1);
    }

    #[test]
    fn walk_numbers_nested_array_inherits_field_name() {
        // Numbers under `top_wallets[].volume` should classify as Sol
        // because the immediate parent key is `volume`. The walker
        // descends into objects keyed by field name, so the array
        // layer carries the parent name through to its items.
        let v = json!({
            "top_wallets": [
                { "addr": "AAA", "volume": 5.0, "degree": 7 },
                { "addr": "BBB", "volume": 3.0, "degree": 4 }
            ]
        });
        let mut out = Vec::new();
        walk_numbers("", &v, &mut out);
        let sols: Vec<_> = out.iter()
            .filter(|n| matches!(n.unit_class, UnitClass::Sol))
            .collect();
        let counts: Vec<_> = out.iter()
            .filter(|n| matches!(n.unit_class, UnitClass::Count))
            .collect();
        assert_eq!(sols.len(), 2, "expected two volume entries; got {:?}", out);
        assert_eq!(counts.len(), 2);
    }

    #[test]
    fn build_binding_collects_provenance_entities() {
        let provenance = vec![
            ProvenanceRef::Wallet { addr: "AAA".into(), idx: Some(0) },
            ProvenanceRef::Wallet { addr: "BBB".into(), idx: Some(1) },
            ProvenanceRef::Community { id: 42 },
            ProvenanceRef::Number {
                metric: "degree".into(),
                value: 33.0,
                support: vec![],
            },
        ];
        let binding = build_binding(
            "wallet_profile",
            "wallet_profile:01HXY".into(),
            123,
            &json!({ "degree": 33, "volume": 12.4 }),
            &provenance,
        );
        assert!(binding.entities.wallets.contains("AAA"));
        assert!(binding.entities.wallets.contains("BBB"));
        assert!(binding.entities.communities.contains(&42));
        // Numbers from JSON walk (degree=Count, volume=Sol) +
        // provenance Number entry (degree -> Count). Total = 3.
        assert_eq!(binding.numbers.len(), 3);
    }

    #[test]
    fn store_record_evicts_at_cap() {
        let mut store = PrimitiveBindingStore::new();
        for i in 0..(MAX_THREAD_BINDINGS + 5) {
            store.record(PrimitiveBinding {
                call_id: format!("p:{i}"),
                primitive: "wallet_profile".into(),
                captured_at_ms: i as u64,
                provenance: vec![],
                numbers: vec![],
                entities: BindingEntities::default(),
            });
        }
        assert_eq!(store.len(), MAX_THREAD_BINDINGS);
        // First 5 should have been evicted.
        let first = store.iter().next().unwrap();
        assert_eq!(first.call_id, format!("p:{}", 5));
    }

    #[test]
    fn store_aggregates_across_bindings() {
        let mut store = PrimitiveBindingStore::new();
        store.record(build_binding(
            "wallet_profile",
            "wp:1".into(),
            1,
            &json!({ "degree": 33, "volume": 12.4 }),
            &[ProvenanceRef::Wallet { addr: "AAA".into(), idx: Some(0) }],
        ));
        store.record(build_binding(
            "community_summary",
            "cs:1".into(),
            2,
            &json!({ "size": 7, "total_volume": 100.0 }),
            &[ProvenanceRef::Community { id: 42 }],
        ));
        let nums = store.all_numbers();
        // wp: degree(Count) + volume(Sol) = 2
        // cs: size(Count) + total_volume(Sol) = 2
        assert_eq!(nums.len(), 4);
        assert!(store.all_wallets().contains("AAA"));
        assert!(store.all_communities().contains(&42));
        assert_eq!(store.call_ids(), vec!["wp:1", "cs:1"]);
    }
}
