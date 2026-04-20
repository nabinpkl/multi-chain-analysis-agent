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
pub struct TxMessage {
    pub instructions: Vec<Instruction>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct TxMeta {
    pub err: Option<Value>,
    #[serde(default)]
    pub inner_instructions: Vec<InnerInstructions>,
}

#[derive(Debug, Deserialize)]
pub struct InnerInstructions {
    #[allow(dead_code)]
    pub index: u32,
    pub instructions: Vec<Instruction>,
}

#[derive(Debug, Deserialize)]
#[serde(untagged)]
pub enum Instruction {
    Parsed(ParsedInstruction),
    /// Anything we don't care about (raw, partial, unknown program).
    Other(IgnoredAny),
}

#[derive(Debug, Deserialize)]
pub struct ParsedInstruction {
    pub program: String,
    pub parsed: ParsedField,
}

#[derive(Debug, Deserialize)]
#[serde(untagged)]
pub enum ParsedField {
    Object {
        #[serde(rename = "type")]
        kind: String,
        info: Value,
    },
    Other(IgnoredAny),
}
