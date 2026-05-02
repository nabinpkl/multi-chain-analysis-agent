//! Ship 5a structural value compare. Walks a provenance array and
//! verifies every entry traces back to the binding store.
//!
//! This is the second deterministic gate stage after
//! `policy_placeholder`. Where placeholder validation answers "does
//! `${ref:N}` resolve to an entry in the model's own provenance
//! array?" (self-consistency), structural compare answers "does the
//! value the model put in that entry actually trace to a primitive
//! call we made?" (factual integrity).
//!
//! Source of truth: `PrimitiveBindingStore` (the typed numbers +
//! entities `build_binding` captured during primitive dispatch).
//!
//! No regex, no prose parsing. Just iteration over the typed
//! provenance vec, struct field access, and the existing tolerance /
//! classification helpers from `policy_crosscheck` (kept across the
//! ship 5a deletion as the shared taxonomy).

use super::policy_crosscheck::{classify_metric, within_tolerance, CrosscheckConfig, UnitClass};
use super::primitives::PrimitiveBindingStore;
use super::types::ProvenanceRef;

/// Reason a structural compare failed. The retract message surfaces
/// this on the SSE wire and into the ledger so retries can name the
/// specific entry that didn't trace.
#[derive(Debug, Clone, PartialEq)]
pub enum MismatchError {
    /// `ProvenanceRef::Number { metric, value }` did not match any
    /// entry in `binding_store.all_numbers()` within tolerance for
    /// the metric's classified unit. Carries the metric label for
    /// the retry message.
    NumberNotInBinding {
        metric: String,
        value: f64,
        unit_class: UnitClass,
    },
    /// `ProvenanceRef::Wallet { addr, .. }` references a wallet that
    /// was never returned by any primitive this thread.
    WalletNotInBinding { addr: String },
    /// `ProvenanceRef::Community { id }` references a community the
    /// binding store has never recorded.
    CommunityNotInBinding { id: u32 },
}

impl MismatchError {
    pub fn to_human_string(&self) -> String {
        match self {
            MismatchError::NumberNotInBinding {
                metric,
                value,
                unit_class,
            } => {
                let unit = match unit_class {
                    UnitClass::Sol => " SOL",
                    UnitClass::Count => "",
                    UnitClass::CommunityId => " (community id)",
                    UnitClass::Raw => "",
                };
                format!(
                    "cited number {metric}={}{} does not trace to any primitive output",
                    fmt_value(*value),
                    unit,
                )
            }
            MismatchError::WalletNotInBinding { addr } => format!(
                "cited wallet {addr} was not returned by any primitive call this thread"
            ),
            MismatchError::CommunityNotInBinding { id } => format!(
                "cited community {id} was not returned by any primitive call this thread"
            ),
        }
    }
}

/// Walk the provenance array and verify every entry traces. Returns
/// `Ok(())` on full match, the first `MismatchError` otherwise.
///
/// Empty provenance is `Ok(())`; the caller (claim leg / narrative
/// leg) is expected to enforce its own "must have provenance" rule
/// separately. Same for empty binding store: an empty store with
/// non-empty provenance always errors on the first Number/Wallet/
/// Community ref since nothing matches; that's the desired semantic.
///
/// `Edge` and `TimeRange` provenance variants are not validated here:
/// today's binding store doesn't carry edge ids, and `TimeRange`
/// arrives in ship 5b's warehouse primitives. The caller can layer
/// additional checks for those entity classes if needed.
pub fn verify_chip_values(
    provenance: &[ProvenanceRef],
    binding: &PrimitiveBindingStore,
) -> Result<(), MismatchError> {
    let cfg = CrosscheckConfig::default();
    let store_numbers = binding.all_numbers();
    let store_wallets = binding.all_wallets();
    let store_communities = binding.all_communities();

    for prov in provenance {
        match prov {
            ProvenanceRef::Number { metric, value, .. } => {
                let unit_class = classify_metric(metric);
                // Raw class always approves: the metric name didn't
                // map to a known unit, so we can't typed-compare.
                // Conservative: don't retract on something we don't
                // recognize, the constitution gate carries the
                // judgment.
                if matches!(unit_class, UnitClass::Raw) {
                    continue;
                }
                let matched = store_numbers.iter().any(|src| {
                    src.unit_class == unit_class
                        && within_tolerance(
                            *value,
                            src.value,
                            cfg.declarative_tolerance,
                        )
                });
                if !matched {
                    return Err(MismatchError::NumberNotInBinding {
                        metric: metric.clone(),
                        value: *value,
                        unit_class,
                    });
                }
            }
            ProvenanceRef::Wallet { addr, .. } => {
                if !store_wallets.contains(addr) {
                    return Err(MismatchError::WalletNotInBinding {
                        addr: addr.clone(),
                    });
                }
            }
            ProvenanceRef::Community { id } => {
                if !store_communities.contains(id) {
                    return Err(MismatchError::CommunityNotInBinding { id: *id });
                }
            }
            // Skip Edge + TimeRange; out of scope today (see fn doc).
            ProvenanceRef::Edge { .. } | ProvenanceRef::TimeRange { .. } => {}
        }
    }

    Ok(())
}

fn fmt_value(v: f64) -> String {
    if v.fract() == 0.0 && v.abs() < 1e15 {
        format!("{:.0}", v)
    } else if v.abs() >= 1.0 {
        format!("{:.2}", v)
    } else {
        format!("{:.4}", v)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::agent::primitives::{build_binding, PrimitiveBindingStore};
    use serde_json::json;

    fn store_with_wallet_profile_output() -> PrimitiveBindingStore {
        // Realistic wallet_profile output. build_binding walks the
        // JSON + provenance refs to populate the typed store. Keys
        // mirror `NodeStatsWire` field names, which use the
        // `*_volume_lamports` form so they classify as Sol via
        // `classify_metric` (substring "volume" + "lamport").
        let value_json = json!({
            "addr": "9XYZ",
            "stats": {
                "total_volume_lamports": 12.4,
                "degree": 33,
                "in_volume_lamports": 8.2,
                "out_volume_lamports": 4.2,
            },
            "top_counterparties": [
                {"addr": "ABC", "volume": 3.1},
                {"addr": "DEF", "volume": 2.5},
            ],
            "community_id": 7,
        });
        let provenance = vec![
            ProvenanceRef::Wallet {
                addr: "9XYZ".into(),
                idx: Some(1),
            },
            ProvenanceRef::Wallet {
                addr: "ABC".into(),
                idx: None,
            },
            ProvenanceRef::Community { id: 7 },
            ProvenanceRef::Number {
                metric: "volume".into(),
                value: 12.4,
                support: vec![],
            },
            ProvenanceRef::Number {
                metric: "degree".into(),
                value: 33.0,
                support: vec![],
            },
        ];
        let binding = build_binding(
            "wallet_profile",
            "wallet_profile:01H".into(),
            0,
            &value_json,
            &provenance,
        );
        let mut store = PrimitiveBindingStore::new();
        store.record(binding);
        store
    }

    #[test]
    fn approves_when_all_refs_trace() {
        let store = store_with_wallet_profile_output();
        let provenance = vec![
            ProvenanceRef::Wallet {
                addr: "9XYZ".into(),
                idx: Some(1),
            },
            ProvenanceRef::Number {
                metric: "volume".into(),
                value: 12.4,
                support: vec![],
            },
            ProvenanceRef::Number {
                metric: "degree".into(),
                value: 33.0,
                support: vec![],
            },
        ];
        assert!(verify_chip_values(&provenance, &store).is_ok());
    }

    #[test]
    fn approves_within_tolerance() {
        let store = store_with_wallet_profile_output();
        // 10% default tolerance; 12.4 → 12.5 is well within.
        let provenance = vec![ProvenanceRef::Number {
            metric: "volume".into(),
            value: 12.5,
            support: vec![],
        }];
        assert!(verify_chip_values(&provenance, &store).is_ok());
    }

    #[test]
    fn retracts_outside_tolerance() {
        let store = store_with_wallet_profile_output();
        // 12.4 → 50.0 is way outside 10% tolerance.
        let provenance = vec![ProvenanceRef::Number {
            metric: "volume".into(),
            value: 50.0,
            support: vec![],
        }];
        let err = verify_chip_values(&provenance, &store).unwrap_err();
        match err {
            MismatchError::NumberNotInBinding { metric, value, .. } => {
                assert_eq!(metric, "volume");
                assert_eq!(value, 50.0);
            }
            other => panic!("unexpected: {other:?}"),
        }
    }

    #[test]
    fn retracts_unsourced_wallet() {
        let store = store_with_wallet_profile_output();
        let provenance = vec![ProvenanceRef::Wallet {
            addr: "FAKE_WALLET_NEVER_SEEN".into(),
            idx: None,
        }];
        let err = verify_chip_values(&provenance, &store).unwrap_err();
        match err {
            MismatchError::WalletNotInBinding { addr } => {
                assert_eq!(addr, "FAKE_WALLET_NEVER_SEEN");
            }
            other => panic!("unexpected: {other:?}"),
        }
    }

    #[test]
    fn retracts_unsourced_community() {
        let store = store_with_wallet_profile_output();
        let provenance = vec![ProvenanceRef::Community { id: 9999 }];
        let err = verify_chip_values(&provenance, &store).unwrap_err();
        match err {
            MismatchError::CommunityNotInBinding { id } => {
                assert_eq!(id, 9999);
            }
            other => panic!("unexpected: {other:?}"),
        }
    }

    #[test]
    fn empty_provenance_approves() {
        let store = store_with_wallet_profile_output();
        assert!(verify_chip_values(&[], &store).is_ok());
    }

    #[test]
    fn empty_store_with_any_ref_retracts() {
        let store = PrimitiveBindingStore::new();
        let provenance = vec![ProvenanceRef::Number {
            metric: "volume".into(),
            value: 12.4,
            support: vec![],
        }];
        // The Number lookup fails because store_numbers is empty.
        let err = verify_chip_values(&provenance, &store).unwrap_err();
        assert!(matches!(err, MismatchError::NumberNotInBinding { .. }));
    }

    #[test]
    fn raw_unit_class_skips_value_check() {
        // Metric name like "score" doesn't classify into any known
        // unit; we intentionally don't retract on Raw class. The
        // constitution gate (LLM judge) handles judgment on
        // unrecognized metrics.
        let store = store_with_wallet_profile_output();
        let provenance = vec![ProvenanceRef::Number {
            metric: "score".into(),
            value: 999_999.0,
            support: vec![],
        }];
        // Even though 999_999 isn't anywhere near anything in the
        // store, "score" classifies as Raw, so we skip.
        assert!(verify_chip_values(&provenance, &store).is_ok());
    }

    #[test]
    fn edge_and_timerange_refs_skipped() {
        // Out-of-scope variants pass through silently. The narrative
        // leg's structural verify shouldn't fail just because the
        // model cited an edge or time range; those land in ship 5b.
        let store = store_with_wallet_profile_output();
        let provenance = vec![
            ProvenanceRef::Edge {
                id: "1234:1".into(),
                src: 1,
                dst: 2,
            },
            ProvenanceRef::TimeRange {
                from_s: 100,
                to_s: 200,
            },
        ];
        assert!(verify_chip_values(&provenance, &store).is_ok());
    }

    #[test]
    fn first_mismatch_short_circuits() {
        // If the provenance array contains multiple bad refs, we
        // surface the first and stop. Same semantics as the gate
        // legs already use for retract reasons (one reason per turn).
        let store = store_with_wallet_profile_output();
        let provenance = vec![
            ProvenanceRef::Number {
                metric: "volume".into(),
                value: 50.0, // outside tolerance
                support: vec![],
            },
            ProvenanceRef::Wallet {
                addr: "FAKE".into(), // also bad
                idx: None,
            },
        ];
        let err = verify_chip_values(&provenance, &store).unwrap_err();
        // First bad entry is the Number (volume), not the Wallet.
        match err {
            MismatchError::NumberNotInBinding { metric, .. } => {
                assert_eq!(metric, "volume");
            }
            other => panic!("expected Number mismatch first; got {other:?}"),
        }
    }
}
