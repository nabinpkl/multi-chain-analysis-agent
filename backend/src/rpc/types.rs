use serde::Deserialize;
use serde::de::IgnoredAny;
use serde_json::Value;

#[derive(Debug, Deserialize)]
pub struct JsonRpcResponse<T> {
    pub result: Option<T>,
    pub error: Option<JsonRpcError>,
}

/// `getAccountInfo` response. Wraps the per-account state in a context
/// object that also carries the slot the read was performed at.
#[derive(Debug, Deserialize)]
pub struct AccountInfoResponse {
    /// `null` when the account does not exist at the queried pubkey.
    pub value: Option<AccountInfoValue>,
}

/// One account's state as returned by `getAccountInfo`. Fields beyond
/// what we need (`executable`, `rentEpoch`) are dropped.
#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct AccountInfoValue {
    /// base58 pubkey of the program that owns this account. The owner
    /// determines how to interpret `data`.
    pub owner: String,
    /// Native SOL balance held by the account, in lamports.
    #[serde(default)]
    pub lamports: u64,
    /// Account data. With `encoding=jsonParsed` this is either a
    /// structured object (when the owner is in the RPC's parser
    /// allowlist, e.g. SPL Token-2022) or a `[base64_string, "base64"]`
    /// fallthrough for everything else (e.g. Metaplex Token Metadata).
    pub data: AccountData,
}

/// Polymorphic `data` field. The RPC returns one of two shapes per the
/// jsonParsed encoding:
///
/// - For programs in its allowlist (System, SPL Token, SPL Token-2022,
///   Stake, Vote, Address Lookup Table, ComputeBudget, BPF Loader, etc.)
///   it walks the bytes into a structured object: `{ parsed, program,
///   space }`.
/// - For everything else it falls through to a base64 string in a
///   two-element tuple `[<base64>, "base64"]`.
///
/// `serde(untagged)` picks the right variant automatically based on the
/// JSON shape. The base64 variant comes second because the parsed
/// variant has stricter shape (object) so serde tries it first.
#[derive(Debug, Deserialize)]
#[serde(untagged)]
pub enum AccountData {
    Parsed(ParsedAccountData),
    Base64(Vec<String>),
}

/// `data` shape when the RPC parses the account natively.
#[derive(Debug, Deserialize)]
pub struct ParsedAccountData {
    /// The parsed object. Shape varies by program; opaque `Value` here
    /// because each owner program has its own layout. Consumers that
    /// know the owner navigate this themselves.
    pub parsed: Value,
    /// Owner-program label, e.g. `"spl-token-2022"`.
    #[serde(default)]
    pub program: String,
    /// Allocated account size in bytes.
    #[serde(default)]
    pub space: u64,
}

#[derive(Debug, Deserialize)]
pub struct JsonRpcError {
    pub code: i64,
    pub message: String,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Block {
    #[serde(default)]
    pub block_time: Option<i64>,
    #[serde(default)]
    pub transactions: Vec<MaybeTransaction>,
}

/// Lossy per-tx decode: one malformed transaction shouldn't poison the whole block.
#[derive(Debug, Deserialize)]
#[serde(untagged)]
pub enum MaybeTransaction {
    Ok(EncodedTransaction),
    Bad(IgnoredAny),
}

#[derive(Debug, Deserialize)]
pub struct EncodedTransaction {
    pub transaction: TransactionPayload,
    pub meta: Option<TxMeta>,
}

#[derive(Debug, Deserialize)]
pub struct TransactionPayload {
    pub signatures: Vec<String>,
    pub message: TxMessage,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct TxMessage {
    /// Resolved account keys for the transaction. With `jsonParsed`
    /// encoding the RPC returns objects with `pubkey` plus role flags;
    /// we keep `pubkey` and `signer` (the latter so program-specific
    /// extractors can attribute signers without re-parsing the message
    /// header). Address-table-lookup entries are resolved before we
    /// see them, so every entry is a base58 wallet or program id.
    #[serde(default)]
    pub account_keys: Vec<AccountKey>,
    /// Top-level instructions. Walked by program-specific extractors
    /// (e.g. `ingest::metadata::parse_token_metadata` filtering on the
    /// Metaplex program ID). The edge parser ignores this field.
    /// Decoded from the `jsonParsed` shape only.
    #[serde(default)]
    pub instructions: Vec<RawInstruction>,
}

#[derive(Debug, Deserialize)]
pub struct AccountKey {
    pub pubkey: String,
    /// jsonParsed marks each account with its role; `signer=true` for
    /// any signer (fee payer + additional signers). Defaults to false
    /// so older response shapes that omit the flag still deserialize.
    #[serde(default)]
    pub signer: bool,
}

/// One instruction inside a tx, jsonParsed shape. The RPC decodes
/// `parsed` only for programs in its hardcoded allowlist (System,
/// Stake, Vote, SPL Token, SPL Token-2022, BPF Loader, etc.); for
/// everything else the raw instruction args land in `data` as a
/// base58 string. For the Metaplex Token Metadata Program the `data`
/// field is the base58-encoded borsh args, decoded by
/// `ingest::metadata`.
#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct RawInstruction {
    pub program_id: String,
    /// Account pubkeys referenced by this instruction. For Metaplex
    /// Create instructions account[0] is the metadata PDA and
    /// account[1] is the mint.
    #[serde(default)]
    pub accounts: Vec<String>,
    /// Program-specific parsed payload, populated only for programs
    /// in the RPC's jsonParsed allowlist. Kept as `Value` because
    /// each allowlisted program has a different shape.
    #[serde(default)]
    pub parsed: Option<Value>,
    /// Base58-encoded raw instruction data. Populated by the RPC for
    /// any program whose instruction shape jsonParsed doesn't natively
    /// decode (Metaplex, Token-2022 extensions, Anchor programs in
    /// general). The metadata extractor borsh-decodes this slice.
    #[serde(default)]
    pub data: Option<String>,
}

/// One inner-instructions group. `index` is the position of the parent
/// top-level instruction this CPI batch ran under. The contained
/// `instructions` carry the same shape as top-level ones.
#[derive(Debug, Deserialize)]
pub struct RawInnerInstructions {
    pub index: u16,
    #[serde(default)]
    pub instructions: Vec<RawInstruction>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct TxMeta {
    pub err: Option<Value>,
    #[serde(default)]
    pub fee: u64,
    /// Native lamport balances per account, indexed by position in
    /// `TxMessage::account_keys`. Diffing pre vs post gives every SOL
    /// movement caused by the transaction, including program-internal
    /// PDA mutations that bypass `SystemProgram::transfer`.
    #[serde(default)]
    pub pre_balances: Vec<u64>,
    #[serde(default)]
    pub post_balances: Vec<u64>,
    /// SPL token balances. One entry per (token account, mint) that the
    /// transaction touched. The `owner` field gives the wallet that
    /// owns the token account, which is the graph node we want.
    #[serde(default)]
    pub pre_token_balances: Vec<TokenBalance>,
    #[serde(default)]
    pub post_token_balances: Vec<TokenBalance>,
    /// CPI'd instructions per top-level instruction. Program-specific
    /// extractors (e.g. `ingest::metadata`) walk both this and
    /// `TxMessage::instructions` because metadata writes can happen
    /// either way. The edge parser ignores it. Note: we deliberately
    /// do NOT capture `logMessages`; at ~831 KB per block average it's
    /// the same cost wall as storing whole blocks and adds nothing.
    #[serde(default)]
    pub inner_instructions: Vec<RawInnerInstructions>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct TokenBalance {
    pub account_index: u16,
    pub mint: String,
    /// Wallet that owns the token account. Some closing or oddly-shaped
    /// accounts omit it; we skip those rather than emit edges keyed on
    /// the token-account address.
    #[serde(default)]
    pub owner: Option<String>,
    pub ui_token_amount: UiTokenAmount,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct UiTokenAmount {
    /// Raw u64 base units serialized as a string per Solana RPC spec.
    /// Decimals are not tracked here  different mints have different
    /// precision, and we deliberately don't normalize across mints.
    pub amount: String,
}
