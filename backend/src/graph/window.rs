/// Rolling-window definitions. The graph maintains six overlapping views
/// over the same global state, sized 10s / 60s / 300s / 900s / 1800s / 3600s.
///
/// Indexing convention: `0 = 10s`, `5 = 3600s`. `MAX = 5` is the global
/// retention window; everything older than `latest_block_time - WINDOWS[MAX]`
/// is dropped from `GraphState` slabs entirely.
pub const WINDOWS: [u64; 6] = [10, 60, 300, 900, 1800, 3600];
pub const NUM_WINDOWS: usize = 6;
pub const MAX_WINDOW_IDX: usize = 5;
pub const DEFAULT_WINDOW_IDX: usize = MAX_WINDOW_IDX;

/// Map a window-seconds value to its slot index. Unknown values return
/// `None` so the API layer can reject with 400.
pub fn window_index(secs: u64) -> Option<usize> {
    WINDOWS.iter().position(|&w| w == secs)
}

/// Parse the `?window=` query param. Empty / missing -> default (3600).
pub fn parse_window_param(raw: Option<&str>) -> Result<usize, String> {
    let Some(raw) = raw else {
        return Ok(DEFAULT_WINDOW_IDX);
    };
    let secs: u64 = raw
        .parse()
        .map_err(|_| format!("invalid window value '{raw}'; expected one of {:?}", WINDOWS))?;
    window_index(secs)
        .ok_or_else(|| format!("unsupported window {secs}; expected one of {:?}", WINDOWS))
}

