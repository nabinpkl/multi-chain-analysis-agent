
#


Runtime + HTTP + SSE:

tokio — async runtime, everything runs on this
axum — HTTP server, has axum::response built in
tower / tower-http — middleware layer for your connection hygiene (per-IP limits, timeouts) (later)