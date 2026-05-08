use serde::{Deserialize, Serialize};

use crate::domain::{Edge, TokenMetadataEvent};

pub const ENVELOPE_V: u8 = 1;

/// Edge wire envelope  written by the producer, borrowed to avoid a clone on publish.
#[derive(Debug, Serialize)]
pub struct EnvelopeRef<'a> {
    pub v: u8,
    pub edge: &'a Edge,
}

impl<'a> EnvelopeRef<'a> {
    pub fn wrap(edge: &'a Edge) -> Self {
        Self {
            v: ENVELOPE_V,
            edge,
        }
    }
}

/// Edge wire envelope  read by consumers.
#[derive(Debug, Deserialize)]
pub struct Envelope {
    pub v: u8,
    pub edge: Edge,
}

/// Token metadata wire envelope  borrowed for publish. Mirror of
/// `EnvelopeRef` so the two topics share the `{ v, <body> }` shape.
#[derive(Debug, Serialize)]
pub struct TokenMetadataEnvelopeRef<'a> {
    pub v: u8,
    #[serde(rename = "tokenMetadata")]
    pub token_metadata: &'a TokenMetadataEvent,
}

impl<'a> TokenMetadataEnvelopeRef<'a> {
    pub fn wrap(row: &'a TokenMetadataEvent) -> Self {
        Self {
            v: ENVELOPE_V,
            token_metadata: row,
        }
    }
}

/// Token metadata wire envelope  read by consumers.
#[derive(Debug, Deserialize)]
pub struct TokenMetadataEnvelope {
    pub v: u8,
    #[serde(rename = "tokenMetadata")]
    pub token_metadata: TokenMetadataEvent,
}
