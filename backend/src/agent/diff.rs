//! Ship 4 deterministic diff walker. Operates over typed primitive
//! outputs (serialized to `serde_json::Value`) using a per-primitive
//! `diff_spec` that declares each field's comparison strategy. Produces
//! a typed `Delta` the loop hands to the narrative-on-delta call (or
//! short-circuits on empty).
//!
//! Determinism is the floor: this module decides "is there ANY signal
//! to discuss" via math. The model decides how to talk about that signal
//! via prose. Same architectural pattern as the rest of this codebase:
//! primitives produce data, narrative interprets.
//!
//! The numeric tolerance compare reuses `policy_crosscheck::within_tolerance`
//! so ship 2.5's audit logic doubles as ship 4's signal filter. Same
//! "is this number meaningfully different" question, just inverted.

use serde_json::Value;

use super::policy_crosscheck::within_tolerance;
use super::types::{Delta, FieldChange, FieldDelta};

/// Default tolerance for numeric fields when the per-class spec
/// doesn't override. Matches `CrosscheckConfig::declarative_tolerance`.
/// Hedging never applies on the diff path (we're comparing primitive
/// outputs, not narrative prose), so there's a single number not a pair.
pub const DEFAULT_NUMBER_TOLERANCE: f64 = 0.10;

/// Comparison strategy for a single primitive output field. Per-
/// primitive `diff_spec()` returns a list of `(field_path, FieldKind)`
/// entries; the walker dispatches on the kind.
///
/// Backend-only: the wire-side `FieldChange` enum is what the frontend
/// renders. `FieldKind` is the internal contract for "how do we
/// compare this field," not "what does the change look like."
#[derive(Debug, Clone)]
pub enum FieldKind {
    /// Numeric field. Tolerance compare via
    /// `policy_crosscheck::within_tolerance`. `tolerance=0.0` reduces
    /// to exact compare; default tolerance is
    /// `DEFAULT_NUMBER_TOLERANCE` (matches the cross-check default).
    /// Field path must point to a JSON number in both prior and
    /// current; missing/non-numeric fields are reported as changed.
    Number { tolerance: f64 },
    /// Set-membership compare for arrays of objects. `key` is the
    /// field name within each element to dedupe by (e.g. `"addr"`
    /// for `top_counterparties`). Produces `SetChanged { added,
    /// removed }` when the membership shifted. Order changes alone
    /// are NOT changes.
    EntitySet { key: String },
    /// Integer-valued field where any delta is meaningful (e.g.
    /// `edge_count`, `community_id`). Produces `CountChanged`
    /// instead of `NumberMoved` to read more naturally in the
    /// timeline chip ("31 → 33" vs "31 → 33 (+6.5%)").
    Count,
    /// Skip this field entirely. Use for fields that are always
    /// expected to differ (e.g. timestamps, addresses that
    /// re-encode). The walker increments `unchanged_field_count`
    /// for ignored fields the same as for stable ones; from the
    /// model's view they look identical.
    Ignore,
}

impl FieldKind {
    /// Convenience constructor for the common case.
    pub fn number_default() -> Self {
        Self::Number {
            tolerance: DEFAULT_NUMBER_TOLERANCE,
        }
    }
}

/// Walk both serialized outputs against the spec, building a `Delta`.
/// Each spec entry produces at most one `FieldDelta` (when the field
/// changed) or contributes to `unchanged_field_count` (when it didn't,
/// or when its kind is `Ignore`).
///
/// `primitive_name` is propagated into each `FieldDelta` so the
/// frontend can group deltas per primitive when a turn fired multiple.
///
/// Robust to missing fields: a field path that's absent in either
/// JSON is reported as changed (best-effort signal that something
/// shifted shape-wise) UNLESS its kind is `Ignore`. Schema drift is
/// rare in practice but we'd rather show "field appeared/disappeared"
/// than silently swallow it.
pub fn diff_outputs(
    primitive_name: &str,
    spec: &[(&str, FieldKind)],
    prior: &Value,
    current: &Value,
) -> Delta {
    let mut changed: Vec<FieldDelta> = Vec::new();
    let mut unchanged: u32 = 0;

    for (field_path, kind) in spec {
        let prior_val = pointer_lookup(prior, field_path);
        let current_val = pointer_lookup(current, field_path);

        match kind {
            FieldKind::Ignore => {
                // Treated as "always unchanged" from the model's
                // perspective. Counted so the totals line up.
                unchanged = unchanged.saturating_add(1);
            }
            FieldKind::Number { tolerance } => {
                match (prior_val.and_then(as_f64), current_val.and_then(as_f64)) {
                    (Some(p), Some(c)) => {
                        if within_tolerance(c, p, *tolerance) {
                            unchanged = unchanged.saturating_add(1);
                        } else {
                            let pct = if p == 0.0 { 0.0 } else { (c - p) / p };
                            changed.push(FieldDelta {
                                field_path: field_path.to_string(),
                                primitive: primitive_name.to_string(),
                                change: FieldChange::NumberMoved {
                                    prior: p,
                                    current: c,
                                    pct,
                                },
                            });
                        }
                    }
                    // Missing on either side or non-numeric: flag as
                    // changed so we don't silently swallow shape drift.
                    _ => {
                        changed.push(FieldDelta {
                            field_path: field_path.to_string(),
                            primitive: primitive_name.to_string(),
                            change: FieldChange::NumberMoved {
                                prior: prior_val.and_then(as_f64).unwrap_or(0.0),
                                current: current_val.and_then(as_f64).unwrap_or(0.0),
                                pct: 0.0,
                            },
                        });
                    }
                }
            }
            FieldKind::Count => {
                let p = prior_val.and_then(as_f64).unwrap_or(f64::NAN);
                let c = current_val.and_then(as_f64).unwrap_or(f64::NAN);
                let same = (p == c) || (p.is_nan() && c.is_nan());
                if same && !p.is_nan() {
                    unchanged = unchanged.saturating_add(1);
                } else {
                    changed.push(FieldDelta {
                        field_path: field_path.to_string(),
                        primitive: primitive_name.to_string(),
                        change: FieldChange::CountChanged {
                            prior: if p.is_nan() { 0.0 } else { p },
                            current: if c.is_nan() { 0.0 } else { c },
                        },
                    });
                }
            }
            FieldKind::EntitySet { key } => {
                let prior_set = collect_keys(prior_val, key);
                let current_set = collect_keys(current_val, key);
                let added: Vec<String> = current_set
                    .iter()
                    .filter(|k| !prior_set.contains(*k))
                    .cloned()
                    .collect();
                let removed: Vec<String> = prior_set
                    .iter()
                    .filter(|k| !current_set.contains(*k))
                    .cloned()
                    .collect();
                if added.is_empty() && removed.is_empty() {
                    unchanged = unchanged.saturating_add(1);
                } else {
                    changed.push(FieldDelta {
                        field_path: field_path.to_string(),
                        primitive: primitive_name.to_string(),
                        change: FieldChange::SetChanged { added, removed },
                    });
                }
            }
        }
    }

    Delta {
        changed,
        unchanged_field_count: unchanged,
    }
}

/// Look up a dotted field path against a JSON value. Supports nested
/// objects (`stats.in_volume_lamports` walks
/// `obj.stats.in_volume_lamports`). Arrays are not
/// indexable via dotted paths; spec entries that need array values
/// (entity sets) point at the array itself.
fn pointer_lookup<'a>(v: &'a Value, dotted: &str) -> Option<&'a Value> {
    let mut cur = v;
    for seg in dotted.split('.') {
        cur = cur.get(seg)?;
    }
    Some(cur)
}

fn as_f64(v: &Value) -> Option<f64> {
    match v {
        Value::Number(n) => n.as_f64(),
        // Booleans / nulls / strings aren't numbers; the caller treats
        // missing-numeric as a shape change.
        _ => None,
    }
}

/// Extract per-element keys from a JSON array of objects. Returns a
/// sorted-deduped Vec so the diff is stable across runs. Non-array
/// or missing values produce an empty Vec (treated as "no entries"
/// by the diff caller).
fn collect_keys(v: Option<&Value>, key: &str) -> Vec<String> {
    let arr = match v {
        Some(Value::Array(a)) => a,
        _ => return Vec::new(),
    };
    let mut keys: Vec<String> = arr
        .iter()
        .filter_map(|elem| match elem.get(key) {
            Some(Value::String(s)) => Some(s.clone()),
            Some(Value::Number(n)) => Some(n.to_string()),
            _ => None,
        })
        .collect();
    keys.sort();
    keys.dedup();
    keys
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn n(v: f64) -> Value {
        Value::Number(serde_json::Number::from_f64(v).unwrap())
    }

    #[test]
    fn unchanged_within_tolerance_counts_as_unchanged() {
        let prior = json!({"vol": 100.0});
        let current = json!({"vol": 105.0}); // +5% < 10% tolerance
        let spec = &[("vol", FieldKind::number_default())];
        let d = diff_outputs("p", spec, &prior, &current);
        assert!(d.changed.is_empty());
        assert_eq!(d.unchanged_field_count, 1);
    }

    #[test]
    fn outside_tolerance_produces_number_moved() {
        let prior = json!({"vol": 100.0});
        let current = json!({"vol": 120.0}); // +20% > 10%
        let spec = &[("vol", FieldKind::number_default())];
        let d = diff_outputs("wallet_profile", spec, &prior, &current);
        assert_eq!(d.changed.len(), 1);
        match &d.changed[0].change {
            FieldChange::NumberMoved { prior, current, pct } => {
                assert_eq!(*prior, 100.0);
                assert_eq!(*current, 120.0);
                assert!((*pct - 0.20).abs() < 1e-9);
            }
            other => panic!("unexpected: {other:?}"),
        }
        assert_eq!(d.changed[0].primitive, "wallet_profile");
        assert_eq!(d.changed[0].field_path, "vol");
        assert_eq!(d.unchanged_field_count, 0);
    }

    #[test]
    fn nested_path_walks_correctly() {
        let prior = json!({"stats": {"in_volume_lamports": 10.0, "out_volume_lamports": 5.0}});
        let current = json!({"stats": {"in_volume_lamports": 10.5, "out_volume_lamports": 5.0}});
        let spec = &[
            ("stats.in_volume_lamports", FieldKind::number_default()),
            ("stats.out_volume_lamports", FieldKind::number_default()),
        ];
        let d = diff_outputs("p", spec, &prior, &current);
        // in_volume_lamports moved 5% which is within 10% tol;
        // out_volume_lamports exactly equal.
        assert!(d.changed.is_empty());
        assert_eq!(d.unchanged_field_count, 2);
    }

    #[test]
    fn count_kind_any_delta_changed() {
        let prior = json!({"edge_count": 31});
        let current = json!({"edge_count": 32});
        let spec = &[("edge_count", FieldKind::Count)];
        let d = diff_outputs("p", spec, &prior, &current);
        assert_eq!(d.changed.len(), 1);
        match &d.changed[0].change {
            FieldChange::CountChanged { prior, current } => {
                assert_eq!(*prior, 31.0);
                assert_eq!(*current, 32.0);
            }
            other => panic!("unexpected: {other:?}"),
        }
    }

    #[test]
    fn entity_set_added_removed() {
        let prior = json!({"top": [{"addr": "A"}, {"addr": "B"}, {"addr": "C"}]});
        let current = json!({"top": [{"addr": "A"}, {"addr": "B"}, {"addr": "D"}]});
        let spec = &[(
            "top",
            FieldKind::EntitySet {
                key: "addr".to_string(),
            },
        )];
        let d = diff_outputs("p", spec, &prior, &current);
        assert_eq!(d.changed.len(), 1);
        match &d.changed[0].change {
            FieldChange::SetChanged { added, removed } => {
                assert_eq!(added, &vec!["D".to_string()]);
                assert_eq!(removed, &vec!["C".to_string()]);
            }
            other => panic!("unexpected: {other:?}"),
        }
    }

    #[test]
    fn entity_set_unchanged_when_only_order_differs() {
        let prior = json!({"top": [{"addr": "A"}, {"addr": "B"}]});
        let current = json!({"top": [{"addr": "B"}, {"addr": "A"}]});
        let spec = &[(
            "top",
            FieldKind::EntitySet {
                key: "addr".to_string(),
            },
        )];
        let d = diff_outputs("p", spec, &prior, &current);
        assert!(d.changed.is_empty());
        assert_eq!(d.unchanged_field_count, 1);
    }

    #[test]
    fn ignore_kind_skipped() {
        let prior = json!({"timestamp": 100, "vol": 50.0});
        let current = json!({"timestamp": 200, "vol": 50.0});
        let spec = &[
            ("timestamp", FieldKind::Ignore),
            ("vol", FieldKind::number_default()),
        ];
        let d = diff_outputs("p", spec, &prior, &current);
        assert!(d.changed.is_empty());
        assert_eq!(d.unchanged_field_count, 2);
    }

    #[test]
    fn missing_field_treated_as_changed_for_number_kind() {
        let prior = json!({"vol": 100.0});
        let current = json!({}); // vol field disappeared
        let spec = &[("vol", FieldKind::number_default())];
        let d = diff_outputs("p", spec, &prior, &current);
        // Missing on current side: surface as changed.
        assert_eq!(d.changed.len(), 1);
    }

    #[test]
    fn zero_prior_with_nonzero_current_is_changed() {
        let prior = json!({"vol": 0.0});
        let current = json!({"vol": 5.0});
        let spec = &[("vol", FieldKind::number_default())];
        let d = diff_outputs("p", spec, &prior, &current);
        // within_tolerance returns prior==0 ? narr==0 ; so 5 vs 0 = changed.
        assert_eq!(d.changed.len(), 1);
        match &d.changed[0].change {
            FieldChange::NumberMoved { prior, current, pct } => {
                assert_eq!(*prior, 0.0);
                assert_eq!(*current, 5.0);
                // pct safely 0 when prior is 0 (avoid div-by-zero).
                assert_eq!(*pct, 0.0);
            }
            other => panic!("unexpected: {other:?}"),
        }
    }

    #[test]
    fn empty_set_to_populated_set_is_changed() {
        let prior = json!({"top": []});
        let current = json!({"top": [{"addr": "X"}]});
        let spec = &[(
            "top",
            FieldKind::EntitySet {
                key: "addr".to_string(),
            },
        )];
        let d = diff_outputs("p", spec, &prior, &current);
        assert_eq!(d.changed.len(), 1);
        match &d.changed[0].change {
            FieldChange::SetChanged { added, removed } => {
                assert_eq!(added, &vec!["X".to_string()]);
                assert!(removed.is_empty());
            }
            other => panic!("unexpected: {other:?}"),
        }
    }

    #[test]
    fn all_unchanged_returns_empty_changed() {
        let prior = json!({
            "size": 8,
            "vol": 100.0,
            "members": [{"addr": "A"}],
        });
        let current = json!({
            "size": 8,
            "vol": 102.0, // +2% within 10%
            "members": [{"addr": "A"}],
        });
        let spec = &[
            ("size", FieldKind::Count),
            ("vol", FieldKind::number_default()),
            (
                "members",
                FieldKind::EntitySet {
                    key: "addr".to_string(),
                },
            ),
        ];
        let d = diff_outputs("community_summary", spec, &prior, &current);
        assert!(d.changed.is_empty());
        assert_eq!(d.unchanged_field_count, 3);
    }
}
