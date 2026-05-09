use serde::{Deserialize, Serialize};

use crate::domain::Edge;

pub const ENVELOPE_V: u8 = 1;

/// Edge wire envelope written by the producer, borrowed to avoid a clone on publish.
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

/// Edge wire envelope read by consumers.
#[derive(Debug, Deserialize)]
pub struct Envelope {
    pub v: u8,
    pub edge: Edge,
}
