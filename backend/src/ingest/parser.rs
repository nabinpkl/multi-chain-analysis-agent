use std::collections::BTreeMap;
use tracing::{debug, warn};

use crate::domain::Edge;
use crate::rpc::types::{Block, MaybeTransaction, TokenBalance};

/// Empty `mint` value means "native SOL" in the wire format and the
/// ClickHouse row. Avoids `Nullable(String)` on a column that's part
/// of common queries.
pub const SOL_MINT: &str = "";

/// Edge `kind` values. `""` means a regular wallet-to-wallet transfer;
/// `"mint"` means tokens were issued to the destination by the mint
/// (source is the synthetic mint pubkey); `"burn"` means tokens were
/// destroyed by the source (destination is the synthetic mint pubkey).
const KIND_TRANSFER: &str = "";
const KIND_MINT: &str = "mint";
const KIND_BURN: &str = "burn";

/// Parses every wallet-to-wallet fungible transfer in a block by
/// diffing pre/post balances from each transaction's metadata.
/// Captures native SOL (via `pre_balances`/`post_balances`) plus every
/// SPL/Token-2022 token (via `pre_token_balances`/`post_token_balances`)
/// without per-protocol decoding. The Solana runtime tells us what
/// moved; we record it.
///
/// Failed transactions are skipped (preserves prior behavior). Fees
/// are excluded by adding `meta.fee` back to the fee payer's lamport
/// delta before pairing  the burn-half asymmetry means leftover fee
/// movements don't pair cleanly, so they fall out naturally too.
pub fn parse_edges(block: &Block, slot: u64, version: u64) -> Vec<Edge> {
    let block_time = block.block_time.unwrap_or(0).max(0) as u32;
    let mut edges = Vec::new();

    let mut skipped_bad = 0u32;
    for maybe in &block.transactions {
        let tx = match maybe {
            MaybeTransaction::Ok(t) => t,
            MaybeTransaction::Bad(_) => {
                skipped_bad += 1;
                continue;
            }
        };
        let Some(meta) = &tx.meta else { continue };
        if meta.err.is_some() {
            continue;
        }
        let signature = match tx.transaction.signatures.first() {
            Some(s) => s.clone(),
            None => continue,
        };
        let account_keys = &tx.transaction.message.account_keys;
        if account_keys.is_empty()
            || meta.pre_balances.len() != account_keys.len()
            || meta.post_balances.len() != account_keys.len()
        {
            continue;
        }

        // Pair-up phase: build one delta map per mint, then pair
        // sources to destinations greedily within each mint.
        let mut groups: BTreeMap<String, Vec<(String, i128)>> = BTreeMap::new();

        // SOL group. Subtract the fee from the payer's apparent loss
        // so the validator-bound flow doesn't show up as an edge.
        let mut sol: Vec<(String, i128)> = Vec::with_capacity(account_keys.len());
        for i in 0..account_keys.len() {
            let pre = meta.pre_balances[i] as i128;
            let post = meta.post_balances[i] as i128;
            let mut delta = post - pre;
            if i == 0 {
                // The fee payer is the first signer, which jsonParsed
                // places at index 0. Adding the fee back removes the
                // fee leg from this account's net change.
                delta += meta.fee as i128;
            }
            if delta != 0 {
                sol.push((account_keys[i].pubkey.clone(), delta));
            }
        }
        if !sol.is_empty() {
            groups.insert(SOL_MINT.to_string(), sol);
        }

        // SPL groups. preTokenBalances / postTokenBalances each carry
        // the (account_index, mint, owner, amount). We index by
        // (account_index, mint) so the pre and post entries pair up
        // correctly even when a token account is created or closed
        // mid-tx.
        let mut spl: BTreeMap<(u16, String), (Option<String>, i128, i128)> = BTreeMap::new();
        for tb in &meta.pre_token_balances {
            insert_token_balance(&mut spl, tb, true);
        }
        for tb in &meta.post_token_balances {
            insert_token_balance(&mut spl, tb, false);
        }
        for ((_, mint), (owner, pre, post)) in spl {
            let Some(owner) = owner else { continue };
            let delta = post - pre;
            if delta == 0 {
                continue;
            }
            groups.entry(mint).or_default().push((owner, delta));
        }

        // Pair within each mint, then emit any unmatched residuals as
        // mint/burn edges using the mint pubkey as the synthetic peer.
        // Native SOL has no equivalent "mint pubkey" address, so SOL
        // residuals are dropped (they'd be rare leftovers from rent or
        // stake operations anyway).
        let mut tx_edges: Vec<(String, String, u64, String, &'static str)> = Vec::new();
        for (mint, deltas) in groups {
            let (paired, unmatched_sources, unmatched_dests) = pair_within_mint(&mint, deltas);
            for (from, to, amount) in paired {
                tx_edges.push((from, to, amount, mint.clone(), KIND_TRANSFER));
            }
            if !mint.is_empty() {
                for (wallet, amount) in unmatched_sources {
                    tx_edges.push((wallet, mint.clone(), amount, mint.clone(), KIND_BURN));
                }
                for (wallet, amount) in unmatched_dests {
                    tx_edges.push((mint.clone(), wallet, amount, mint.clone(), KIND_MINT));
                }
            }
        }

        // Deterministic ordering across re-ingestion: sort by
        // (mint, kind, source, dest, amount) so the same tx always
        // produces the same instruction_idx assignment.
        tx_edges.sort_by(|a, b| {
            a.3.cmp(&b.3)
                .then(a.4.cmp(b.4))
                .then(a.0.cmp(&b.0))
                .then(a.1.cmp(&b.1))
                .then(a.2.cmp(&b.2))
        });

        for (idx, (from, to, amount, mint, kind)) in tx_edges.into_iter().enumerate() {
            if idx > u16::MAX as usize {
                warn!(
                    signature = %signature,
                    "transaction emitted more transfers than u16 can index; truncating"
                );
                break;
            }
            edges.push(Edge {
                signature: signature.clone(),
                instruction_idx: idx as u16,
                slot,
                block_time,
                from_wallet: from,
                to_wallet: to,
                amount,
                mint,
                kind: kind.to_string(),
                version,
            });
        }
    }

    if skipped_bad > 0 {
        debug!(slot, skipped_bad, "skipped malformed txs in block");
    }

    edges
}

fn insert_token_balance(
    map: &mut BTreeMap<(u16, String), (Option<String>, i128, i128)>,
    tb: &TokenBalance,
    is_pre: bool,
) {
    let key = (tb.account_index, tb.mint.clone());
    let amount = tb.ui_token_amount.amount.parse::<u128>().unwrap_or(0) as i128;
    let entry = map.entry(key).or_insert((None, 0, 0));
    if entry.0.is_none() {
        entry.0 = tb.owner.clone();
    }
    if is_pre {
        entry.1 = amount;
    } else {
        entry.2 = amount;
    }
}

/// Greedy largest-first pairing. Within one mint, the sum of
/// negative deltas is roughly the sum of positive deltas (modulo
/// mints, burns, and fees). We pop the largest source and largest
/// destination, emit `min(abs)` worth of edge, subtract from both,
/// repeat. Returns three lists:
///   * paired: (from, to, amount) for cleanly matched transfers
///   * unmatched_sources: (wallet, amount) for source residuals
///     (interpreted as burns by the caller for SPL mints)
///   * unmatched_dests: (wallet, amount) for destination residuals
///     (interpreted as mint events by the caller for SPL mints)
#[allow(clippy::type_complexity)]
fn pair_within_mint(
    _mint: &str,
    deltas: Vec<(String, i128)>,
) -> (
    Vec<(String, String, u64)>,
    Vec<(String, u64)>,
    Vec<(String, u64)>,
) {
    let mut sources: Vec<(String, i128)> =
        deltas.iter().filter(|(_, d)| *d < 0).cloned().collect();
    let mut dests: Vec<(String, i128)> =
        deltas.into_iter().filter(|(_, d)| *d > 0).collect();
    // Sort by absolute delta descending, with wallet address as a
    // stable tiebreaker so re-ingestion produces identical pairings.
    sources.sort_by(|a, b| b.1.abs().cmp(&a.1.abs()).then(a.0.cmp(&b.0)));
    dests.sort_by(|a, b| b.1.cmp(&a.1).then(a.0.cmp(&b.0)));

    let mut paired = Vec::new();
    let (mut s, mut d) = (0usize, 0usize);
    while s < sources.len() && d < dests.len() {
        let src_abs = (-sources[s].1) as u128;
        let dst_abs = dests[d].1 as u128;
        let amount = src_abs.min(dst_abs);
        if amount == 0 {
            break;
        }
        let amount_u64 = if amount <= u64::MAX as u128 {
            amount as u64
        } else {
            u64::MAX
        };
        paired.push((sources[s].0.clone(), dests[d].0.clone(), amount_u64));
        sources[s].1 += amount as i128;
        dests[d].1 -= amount as i128;
        if sources[s].1 == 0 {
            s += 1;
        }
        if dests[d].1 == 0 {
            d += 1;
        }
    }

    // Whichever side still has remaining magnitude is the residual.
    // For SPL these become mint/burn edges via the caller.
    let unmatched_sources: Vec<(String, u64)> = sources[s..]
        .iter()
        .map(|(w, d)| (w.clone(), clamp_u64(-d)))
        .filter(|(_, a)| *a > 0)
        .collect();
    let unmatched_dests: Vec<(String, u64)> = dests[d..]
        .iter()
        .map(|(w, d)| (w.clone(), clamp_u64(*d)))
        .filter(|(_, a)| *a > 0)
        .collect();
    (paired, unmatched_sources, unmatched_dests)
}

fn clamp_u64(x: i128) -> u64 {
    if x <= 0 {
        0
    } else if x > u64::MAX as i128 {
        u64::MAX
    } else {
        x as u64
    }
}
