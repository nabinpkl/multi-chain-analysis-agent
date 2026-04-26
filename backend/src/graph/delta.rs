use serde::Serialize;
use ts_rs::TS;

#[derive(Serialize, TS, Clone, Debug)]
#[serde(tag = "type")]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
pub enum GraphDelta {
    NodeAdded { idx: u32, pubkey: String },
    EdgeAdded { idx: u32, src: u32, dst: u32 },
    ComponentMerged { absorbed_root: u32, surviving_root: u32, new_size: u32 },
}
