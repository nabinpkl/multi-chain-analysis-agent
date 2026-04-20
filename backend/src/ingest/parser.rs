use tracing::debug;

use crate::domain::Edge;
use crate::rpc::types::{Block, Instruction, MaybeTransaction, ParsedField};

const SYSTEM_PROGRAM: &str = "system";
const TRANSFER: &str = "transfer";
const TRANSFER_WITH_SEED: &str = "transferWithSeed";

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
        if let Some(meta) = &tx.meta
            && meta.err.is_some()
        {
            continue;
        }

        let signature = match tx.transaction.signatures.first() {
            Some(s) => s.clone(),
            None => continue,
        };

        let mut idx: u16 = 0;

        for instr in &tx.transaction.message.instructions {
            if let Some(edge) = try_extract(instr, &signature, slot, block_time, idx, version) {
                edges.push(edge);
            }
            idx = idx.saturating_add(1);
        }

        if let Some(meta) = &tx.meta {
            for inner in &meta.inner_instructions {
                for instr in &inner.instructions {
                    if let Some(edge) =
                        try_extract(instr, &signature, slot, block_time, idx, version)
                    {
                        edges.push(edge);
                    }
                    idx = idx.saturating_add(1);
                }
            }
        }
    }

    if skipped_bad > 0 {
        debug!(slot, skipped_bad, "skipped malformed txs in block");
    }

    edges
}

fn try_extract(
    instr: &Instruction,
    signature: &str,
    slot: u64,
    block_time: u32,
    idx: u16,
    version: u64,
) -> Option<Edge> {
    let parsed = match instr {
        Instruction::Parsed(p) => p,
        Instruction::Other(_) => return None,
    };
    if parsed.program != SYSTEM_PROGRAM {
        return None;
    }
    let (kind, info) = match &parsed.parsed {
        ParsedField::Object { kind, info } => (kind.as_str(), info),
        ParsedField::Other(_) => return None,
    };
    if kind != TRANSFER && kind != TRANSFER_WITH_SEED {
        return None;
    }

    let from = info.get("source")?.as_str()?.to_string();
    let to = info.get("destination")?.as_str()?.to_string();
    let lamports = info.get("lamports").and_then(|v| v.as_u64())?;

    Some(Edge {
        signature: signature.to_string(),
        instruction_idx: idx,
        slot,
        block_time,
        from_wallet: from,
        to_wallet: to,
        amount: lamports,
        version,
    })
}
