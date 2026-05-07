use serde::{Deserialize, Serialize};

use crate::domain::{Edge, Memo};

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

/// Memo wire envelope  borrowed for publish. Parallel to `EnvelopeRef`
/// so the two topics share the `{ v, <body> }` shape.
#[derive(Debug, Serialize)]
pub struct MemoEnvelopeRef<'a> {
    pub v: u8,
    pub memo: &'a Memo,
}

impl<'a> MemoEnvelopeRef<'a> {
    pub fn wrap(memo: &'a Memo) -> Self {
        Self {
            v: ENVELOPE_V,
            memo,
        }
    }
}

/// Memo wire envelope  read by consumers.
#[derive(Debug, Deserialize)]
pub struct MemoEnvelope {
    pub v: u8,
    pub memo: Memo,
}
