use serde::Deserialize;
use serde::de::IgnoredAny;
use serde_json::Value;

#[derive(Debug, Deserialize)]
pub struct JsonRpcResponse<T> {
    pub result: Option<T>,
    pub error: Option<JsonRpcError>,
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
    /// we only need the pubkey. Address-table-lookup entries are
    /// resolved before we see them, so every entry is a base58 wallet
    /// or program id.
    #[serde(default)]
    pub account_keys: Vec<AccountKey>,
}

#[derive(Debug, Deserialize)]
pub struct AccountKey {
    pub pubkey: String,
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
