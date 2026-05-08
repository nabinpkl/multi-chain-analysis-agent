//! Metaplex Token Metadata Program decoder.
//!
//! Walks every successful tx in a `Block` and emits one
//! `TokenMetadataEvent` per Metaplex Create instruction. The bytes
//! flow through `getBlock` already; we filter by program ID at the
//! universal entry point (`message.instructions` plus
//! `meta.inner_instructions`), base58-decode the `data` field, then
//! borsh-deserialize the slice we care about.
//!
//! Update support (discriminator 15, `UpdateMetadataAccountV2`, and
//! the v1-namespace `UpdateV1` discriminator 47) is deliberately
//! deferred. Updates carry only the metadata PDA, not the mint, so
//! linking an update back to its mint requires a side mapping table
//! populated from prior Creates. That work lands in a follow-up. See
//! `docs/architecture/token-metadata-ingestion.md`.
//!
//! The borsh schema below is hand-rolled from the upstream
//! `mpl-token-metadata` source. We only deserialize the prefix we
//! read (`name / symbol / uri`); the trailing fields (creators,
//! collection, uses, collection_details, is_mutable) are present in
//! the structs so borsh consumes them but we then drop them. Schema
//! has been stable since the program shipped in 2021.

use borsh::BorshDeserialize;
use tracing::{debug, warn};

use crate::domain::TokenMetadataEvent;
use crate::rpc::types::{Block, MaybeTransaction, RawInstruction};

/// Metaplex Token Metadata Program. Canonical singleton; never
/// redeployed at a different address.
pub const METAPLEX_TOKEN_METADATA: &str = "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s";

/// Instruction discriminators we decode. Single-byte tags at the
/// front of the borsh-encoded args. Other discriminators (Update,
/// Verify, Print, etc.) are skipped.
const DISC_CREATE_METADATA_ACCOUNT_V2: u8 = 16;
const DISC_CREATE_METADATA_ACCOUNT_V3: u8 = 33;

/// Program label written to the ClickHouse row's `program` column.
/// Future Token-2022 stream-decode work writes `"token2022"` from
/// its own decoder; the column distinguishes source for read-side
/// queries that care.
const PROGRAM_METAPLEX: &str = "metaplex";

/// Op labels written to the ClickHouse row's `op` column.
/// Program-scoped so values won't collide once Token-2022 decode
/// lands (which will emit e.g. `"t22_initialize"`).
const OP_CREATE_V2: &str = "create_v2";
const OP_CREATE_V3: &str = "create_v3";

/// Account-list positions inside a Metaplex Create instruction.
/// Both V2 and V3 share this layout per the Metaplex IDL:
/// `[metadata_pda, mint, mint_authority, payer, update_authority, ...]`.
const ACC_METADATA_PDA: usize = 0;
const ACC_MINT: usize = 1;
const ACC_UPDATE_AUTHORITY: usize = 4;

pub fn parse_token_metadata(
    block: &Block,
    slot: u64,
    version: u64,
) -> Vec<TokenMetadataEvent> {
    let block_time = block.block_time.unwrap_or(0).max(0) as u32;
    let mut events = Vec::new();

    for maybe in &block.transactions {
        let tx = match maybe {
            MaybeTransaction::Ok(t) => t,
            MaybeTransaction::Bad(_) => continue,
        };
        let Some(meta) = &tx.meta else { continue };
        if meta.err.is_some() {
            continue;
        }
        let Some(signature) = tx.transaction.signatures.first() else {
            continue;
        };

        let mut idx: u16 = 0;
        // Top-level instructions first.
        for inst in &tx.transaction.message.instructions {
            try_emit(
                inst,
                false,
                signature,
                slot,
                block_time,
                version,
                &mut idx,
                &mut events,
            );
        }
        // Then CPI'd metadata writes from inner instructions.
        for grp in &meta.inner_instructions {
            for inst in &grp.instructions {
                try_emit(
                    inst,
                    true,
                    signature,
                    slot,
                    block_time,
                    version,
                    &mut idx,
                    &mut events,
                );
            }
        }
    }

    if !events.is_empty() {
        debug!(slot, count = events.len(), "parsed token metadata events");
    }

    events
}

#[allow(clippy::too_many_arguments)]
fn try_emit(
    inst: &RawInstruction,
    is_inner: bool,
    signature: &str,
    slot: u64,
    block_time: u32,
    version: u64,
    idx: &mut u16,
    out: &mut Vec<TokenMetadataEvent>,
) {
    if inst.program_id != METAPLEX_TOKEN_METADATA {
        return;
    }
    let Some(data_b58) = inst.data.as_deref() else {
        return;
    };
    let bytes = match bs58::decode(data_b58).into_vec() {
        Ok(b) => b,
        Err(e) => {
            warn!(signature = %signature, error = %e, "metaplex inst data not base58");
            return;
        }
    };
    if bytes.is_empty() {
        return;
    }
    let disc = bytes[0];
    let payload = &bytes[1..];

    // Decode args by discriminator. Each branch returns `Option<DataV2>`
    // for the metadata fields, or skips the instruction entirely.
    let (op, data) = match disc {
        DISC_CREATE_METADATA_ACCOUNT_V3 => {
            let args = match CreateMetadataAccountV3Args::try_from_slice(payload) {
                Ok(a) => a,
                Err(e) => {
                    warn!(signature = %signature, error = %e, "decode CreateMetadataAccountV3 args");
                    return;
                }
            };
            (OP_CREATE_V3, args.data)
        }
        DISC_CREATE_METADATA_ACCOUNT_V2 => {
            let args = match CreateMetadataAccountV2Args::try_from_slice(payload) {
                Ok(a) => a,
                Err(e) => {
                    warn!(signature = %signature, error = %e, "decode CreateMetadataAccountV2 args");
                    return;
                }
            };
            (OP_CREATE_V2, args.data)
        }
        _ => return,
    };

    let metadata_pda = match inst.accounts.get(ACC_METADATA_PDA) {
        Some(s) => s.clone(),
        None => return,
    };
    let mint = match inst.accounts.get(ACC_MINT) {
        Some(s) => s.clone(),
        None => return,
    };
    let update_authority = inst
        .accounts
        .get(ACC_UPDATE_AUTHORITY)
        .cloned()
        .unwrap_or_default();

    if *idx == u16::MAX {
        warn!(
            signature = %signature,
            "tx emitted more metadata instructions than u16 can index; truncating"
        );
        return;
    }

    out.push(TokenMetadataEvent {
        mint,
        metadata_pda,
        signature: signature.to_string(),
        slot,
        block_time,
        instruction_idx: *idx,
        is_inner,
        program: PROGRAM_METAPLEX.to_string(),
        op: op.to_string(),
        // Metaplex pads name/symbol with NUL bytes to the fixed
        // schema sizes (32, 10, 200). Trim them so consumers don't
        // see embedded nulls; the strings remain UTF-8.
        name: trim_nul(&data.name),
        symbol: trim_nul(&data.symbol),
        uri: trim_nul(&data.uri),
        update_authority,
        version,
    });
    *idx += 1;
}

fn trim_nul(s: &str) -> String {
    s.trim_end_matches('\0').to_string()
}

// =====================================================================
// Hand-rolled borsh schema slice. Mirrors mpl-token-metadata source
// (clients/rust/src/generated/types/*.rs and instructions/*.rs). We
// only consume the fields we need; trailing fields are present so
// borsh can deserialize the full payload without leaving trailing
// bytes that try_from_slice would reject.
// =====================================================================

type Pubkey = [u8; 32];

#[derive(BorshDeserialize)]
#[allow(dead_code)]
struct DataV2 {
    name: String,
    symbol: String,
    uri: String,
    seller_fee_basis_points: u16,
    creators: Option<Vec<Creator>>,
    collection: Option<Collection>,
    uses: Option<Uses>,
}

#[derive(BorshDeserialize)]
#[allow(dead_code)]
struct Creator {
    address: Pubkey,
    verified: bool,
    share: u8,
}

#[derive(BorshDeserialize)]
#[allow(dead_code)]
struct Collection {
    verified: bool,
    key: Pubkey,
}

#[derive(BorshDeserialize)]
#[allow(dead_code)]
struct Uses {
    /// Borsh tag-encodes UseMethod as a u8 enum (Burn=0, Multiple=1, Single=2).
    use_method: u8,
    remaining: u64,
    total: u64,
}

#[derive(BorshDeserialize)]
#[allow(dead_code)]
enum CollectionDetails {
    V1 { size: u64 },
    V2 { padding: [u8; 8] },
}

#[derive(BorshDeserialize)]
struct CreateMetadataAccountV3Args {
    data: DataV2,
    #[allow(dead_code)]
    is_mutable: bool,
    #[allow(dead_code)]
    collection_details: Option<CollectionDetails>,
}

#[derive(BorshDeserialize)]
struct CreateMetadataAccountV2Args {
    data: DataV2,
    #[allow(dead_code)]
    is_mutable: bool,
}
