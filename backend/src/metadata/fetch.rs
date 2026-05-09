//! Lazy on-demand metadata fetch. See `metadata::mod` for context.

use std::sync::OnceLock;

use base64::Engine;
use borsh::BorshDeserialize;
use solana_pubkey::Pubkey;
use tracing::debug;

use crate::rpc::client::RpcClient;
use crate::rpc::error::RpcError;
use crate::rpc::types::{AccountData, AccountInfoResponse};
use crate::util::singleflight::Singleflight;

/// Metaplex Token Metadata Program. Canonical singleton; never
/// redeployed at a different address.
const METAPLEX_PROGRAM_ID_B58: &str = "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s";

/// SPL Token-2022 program. Used to recognize Token-2022 mints owning
/// the inline metadata extension.
const TOKEN_2022_PROGRAM_ID_B58: &str = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb";

/// Provenance label surfaced through the proto response's
/// `source_program` field.
const PROGRAM_METAPLEX: &str = "metaplex";
const PROGRAM_TOKEN_2022: &str = "token2022";

/// On-chain metadata for a mint. Slim shape carrying only the fields
/// surfaced by `/primitive/get_token_info`. Nothing here is persisted;
/// callers re-fetch on every request.
#[derive(Debug, Clone)]
pub struct OnChainMetadata {
    pub name: String,
    pub symbol: String,
    pub uri: String,
    pub update_authority: String,
    /// `"metaplex"` (Metaplex Token Metadata PDA) or `"token2022"`
    /// (SPL Token-2022 metadata extension on the mint account itself).
    pub program: &'static str,
}

fn metaplex_program_id() -> &'static Pubkey {
    static PID: OnceLock<Pubkey> = OnceLock::new();
    PID.get_or_init(|| pubkey_from_b58(METAPLEX_PROGRAM_ID_B58))
}

fn pubkey_from_b58(s: &str) -> Pubkey {
    let mut bytes = [0u8; 32];
    bs58::decode(s)
        .onto(&mut bytes[..])
        .expect("compile-time-valid base58");
    Pubkey::new_from_array(bytes)
}

/// Derive the Metaplex Token Metadata PDA for a given mint.
/// Per the Metaplex convention the seeds are
/// `["metadata", program_id, mint]`.
pub fn derive_metaplex_metadata_pda(mint: &Pubkey) -> Pubkey {
    let pid = metaplex_program_id();
    let (pda, _bump) =
        Pubkey::find_program_address(&[b"metadata", pid.as_ref(), mint.as_ref()], pid);
    pda
}

/// Per-mint single-flight wrapper. Two concurrent calls for the same
/// mint share one in-flight execution; the second caller awaits the
/// first's result instead of firing its own `getAccountInfo` pair.
/// Cleared as soon as the leader finishes, so the next non-overlapping
/// call starts a fresh flight (no caching).
type FetchResult = Result<Option<OnChainMetadata>, RpcError>;
fn singleflight() -> &'static Singleflight<String, FetchResult> {
    static SF: OnceLock<Singleflight<String, FetchResult>> = OnceLock::new();
    SF.get_or_init(Singleflight::new)
}

/// Fetch on-chain metadata for a mint by trying the two known paths
/// in order:
///
/// 1. Derive the Metaplex PDA from the mint, `getAccountInfo` on it.
///    If the account exists and is owned by the Metaplex program,
///    borsh-decode the prefix.
/// 2. Otherwise `getAccountInfo` on the mint pubkey itself. If owned
///    by SPL Token-2022 and a `tokenMetadata` extension is present,
///    pluck `name / symbol / uri / updateAuthority` from the parsed
///    extension state.
///
/// Returns `Ok(None)` when the mint exists but has no resolvable
/// metadata via either path. `Err` is reserved for actual RPC failure.
///
/// Concurrent calls for the same mint coalesce via per-mint
/// single-flight: the first caller fires the `getAccountInfo` pair
/// against the primitive rate-limit lane; subsequent in-flight callers
/// for the same mint await the leader's result and consume zero
/// additional limiter tickets.
pub async fn fetch_token_metadata(
    rpc: &RpcClient,
    mint: &Pubkey,
) -> Result<Option<OnChainMetadata>, RpcError> {
    let mint_b58 = bs58::encode(mint.as_ref()).into_string();
    singleflight()
        .run(mint_b58.clone(), || fetch_inner(rpc, mint, mint_b58.clone()))
        .await
}

async fn fetch_inner(
    rpc: &RpcClient,
    mint: &Pubkey,
    mint_b58: String,
) -> FetchResult {
    let pda = derive_metaplex_metadata_pda(mint);
    let pda_b58 = bs58::encode(pda.as_ref()).into_string();

    debug!(mint = %mint_b58, pda = %pda_b58, "fetch_token_metadata: trying metaplex pda");
    let pda_resp = rpc.get_account_info(&pda_b58).await?;

    if let Some(parsed) = try_metaplex_path(&pda_resp) {
        return Ok(Some(make_metadata(parsed, PROGRAM_METAPLEX)));
    }

    // PDA missing or owner mismatched. Try the mint account itself
    // for the Token-2022 metadata extension.
    debug!(
        mint = %mint_b58,
        "fetch_token_metadata: trying token-2022 mint extension"
    );
    let mint_resp = rpc.get_account_info(&mint_b58).await?;

    if let Some(parsed) = try_token2022_path(&mint_resp) {
        return Ok(Some(make_metadata(parsed, PROGRAM_TOKEN_2022)));
    }

    Ok(None)
}

fn try_metaplex_path(resp: &AccountInfoResponse) -> Option<ParsedMetadata> {
    let value = resp.value.as_ref()?;
    if value.owner != METAPLEX_PROGRAM_ID_B58 {
        debug!(
            owner = %value.owner,
            "try_metaplex_path: PDA exists but owner is not Metaplex"
        );
        return None;
    }
    decode_metaplex_account(&value.data)
}

fn try_token2022_path(resp: &AccountInfoResponse) -> Option<ParsedMetadata> {
    let value = resp.value.as_ref()?;
    if value.owner != TOKEN_2022_PROGRAM_ID_B58 {
        return None;
    }
    decode_token2022_metadata(&value.data)
}

fn make_metadata(parsed: ParsedMetadata, program: &'static str) -> OnChainMetadata {
    OnChainMetadata {
        name: parsed.name,
        symbol: parsed.symbol,
        uri: parsed.uri,
        update_authority: parsed.update_authority,
        program,
    }
}

struct ParsedMetadata {
    name: String,
    symbol: String,
    uri: String,
    update_authority: String,
}

/// Borsh-decode the Metaplex metadata account prefix. The full 679-
/// byte allocation has more trailing fields (creators, collection,
/// uses, edition_nonce, etc.) we don't need; `deserialize_reader` on
/// a Cursor leaves them unread without erroring.
fn decode_metaplex_account(data: &AccountData) -> Option<ParsedMetadata> {
    let bytes = match data {
        // Metaplex isn't in the RPC's `jsonParsed` allowlist, so the
        // RPC falls through to base64. Encoding tuple shape is
        // `[<base64_string>, "base64"]`. If the bytes fail to decode
        // we treat as no metadata rather than erroring upstream.
        AccountData::Base64(parts) if !parts.is_empty() => {
            match base64::engine::general_purpose::STANDARD.decode(&parts[0]) {
                Ok(b) => b,
                Err(e) => {
                    debug!(error = %e, "decode_metaplex_account: base64 decode failed");
                    return None;
                }
            }
        }
        // Parsed shape is unexpected for Metaplex (RPC has no parser
        // for the program). Skip silently.
        _ => return None,
    };

    #[derive(BorshDeserialize)]
    struct MetaplexAccountHead {
        key: u8,
        update_authority: [u8; 32],
        #[allow(dead_code)]
        mint: [u8; 32],
        name: String,
        symbol: String,
        uri: String,
    }

    let mut cursor = std::io::Cursor::new(&bytes[..]);
    let head = match MetaplexAccountHead::deserialize_reader(&mut cursor) {
        Ok(h) => h,
        Err(e) => {
            debug!(error = %e, "decode_metaplex_account: borsh deserialize failed");
            return None;
        }
    };

    // `Key` enum: 0=Uninitialized, 1=EditionV1, 2=MasterEditionV1,
    // 3=ReservationListV1, 4=MetadataV1, ... (mpl-token-metadata
    // historical enum). Only MetadataV1 carries name/symbol/uri.
    if head.key != 4 {
        debug!(key = head.key, "decode_metaplex_account: key != MetadataV1");
        return None;
    }

    // Metaplex stores strings padded with NUL chars to their max
    // allocation (32 / 10 / 200 bytes). Trim before returning so
    // consumers don't see embedded nulls.
    Some(ParsedMetadata {
        name: trim_nul(&head.name),
        symbol: trim_nul(&head.symbol),
        uri: trim_nul(&head.uri),
        update_authority: bs58::encode(head.update_authority).into_string(),
    })
}

/// Walk the Token-2022 `parsed.info.extensions[]` array for a
/// `tokenMetadata` entry and pluck the metadata fields. The RPC's
/// jsonParsed allowlist includes Token-2022, so the metadata extension
/// state arrives already structured; no borsh.
fn decode_token2022_metadata(data: &AccountData) -> Option<ParsedMetadata> {
    let parsed = match data {
        AccountData::Parsed(p) => &p.parsed,
        _ => return None,
    };

    let info = parsed.get("info")?;
    let exts = info.get("extensions")?.as_array()?;
    for ext in exts {
        let kind = ext.get("extension")?.as_str()?;
        if kind != "tokenMetadata" {
            continue;
        }
        let state = ext.get("state")?;
        return Some(ParsedMetadata {
            name: state.get("name")?.as_str()?.to_string(),
            symbol: state.get("symbol")?.as_str()?.to_string(),
            uri: state.get("uri")?.as_str()?.to_string(),
            update_authority: state
                .get("updateAuthority")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string(),
        });
    }
    None
}

fn trim_nul(s: &str) -> String {
    s.trim_end_matches('\0').to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    /// USDC's mint and its known Metaplex metadata PDA. The PDA value
    /// is checked against Solana Explorer / mpl-token-metadata client
    /// tooling; treat as a fixed external reference. If this test
    /// fails, the PDA derivation function is wrong (or upstream
    /// changed seeds, which would be a major break).
    #[test]
    fn pda_matches_known_usdc() {
        let mint = pubkey_from_b58("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v");
        let pda = derive_metaplex_metadata_pda(&mint);
        assert_eq!(
            bs58::encode(pda.as_ref()).into_string(),
            "5x38Kp4hvdomTCnCrAny4UtMUt5rQBdB6px2K1Ui45Wq",
        );
    }
}
