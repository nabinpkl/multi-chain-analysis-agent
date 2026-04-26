## Session summary

### What we delivered

**Pass 1: Filter removal + uniform edge alpha (frontend).**
Removed two true data-hiding filters and replaced one stale label.

- `MIN_LONELY_VOLUME` deleted: every dust transfer enters the graph regardless of amount.
- `MIN_EDGE_TX_COUNT` deleted: every edge visible from first transaction. Cascaded into removing `visibleDegree`, the edge `hidden` attribute, `refreshNodeHidden`, and the `commitVisibility`-vs-`commitEdge` distinction.
- `EDGE_LONG_CUTOFF` and the stale length-fade label removed: every edge gets uniform `rgba(200,210,235,0.25)`. Density emerges from compositing where edges overlap.
- Cleaned up the now-dead `filteredRef` counter and the "noise filtered" UI block.

**Pass 2: Megacore legibility (frontend).**
Decomposed the 8-Jito-tip mass via two complementary moves.

- Added a tip ring spread force in `per-component-layout.ts`. Tips are pulled to angular positions on a ring around their component centroid, deterministic by id sort. Searchers fan around their tips via existing edge attraction. Bumped `MEGAHUB_EDGE_REST_LENGTH` (250→420) and added `LARGE_COMPONENT_EDGE_REST_LENGTH = 90` for searcher-to-searcher edges in components ≥100 nodes, so the petals get breathing room and don't mash together.
- Bumped `CROSS_COMMUNITY_REPULSION_FACTOR` (6→20) so MPC pockets visibly peel out to the megacore periphery instead of being trapped inside.
- Added fixed role colors (orange/cyan/green/gold/violet/beige) and a sidebar legend with role-by-role counts. Replaced the per-Louvain-community hue with one fixed hue per MPC member.
- Removed `colorForMpcCommunity` and `hslToRgbString` as dead code after the role-based coloring took over.

**Pass 3: SPL token capture via balance diffs (backend + frontend).**
Replaced the SystemProgram instruction parser with a universal balance-diff parser.

- Backend `rpc/types.rs`: extended `TxMeta` with `fee`, `pre_balances`, `post_balances`, `pre_token_balances`, `post_token_balances`. Added `TokenBalance` and `UiTokenAmount` types. Dropped the now-dead instruction-related types entirely.
- Backend `ingest/parser.rs`: rewritten to compute per-account, per-mint deltas from the metadata. Pairs sources to destinations greedily within each mint. SOL fee is added back to fee-payer's lamport delta so validator-bound flow doesn't show up as edges. Deterministic sorting by `(mint, kind, source, dest, amount)` for stable `instruction_idx` assignment across re-ingestion.
- Backend `domain.rs`: `Edge` gained `mint: String` (empty for SOL).
- Backend `store/schema.rs`: edges table dropped and recreated with `mint` column (per AGENTS.md no-backward-compat).
- Backend `store/clickhouse_store.rs`: SOL-aggregating queries (`top_edges`, `top_wallets`, `window_stats`) filter by `mint = ''` so SPL base units don't pollute lamport sums.
- Backend `state_machine/mod.rs`: skips non-SOL edges in `increment` so the overview view stays SOL-coherent.
- Backend `api/raw.rs`: `EdgeWire` carries `mint` (skipped if empty).
- Frontend `api.ts`: `RawEdge` gains optional `mint`.
- Frontend `use-raw-stream.ts`: SPL edges naturally fall through with `volume_sol = 0`, so all SOL-denominated signals (tip detection, whale labeling, MPC bidir-volume, flow-hub) stay accurate. Only `degree` and `txCount` get incremented for SPL.

**Pass 4: Mint and burn capture, token-mint role (backend + frontend).**
Closed the mint/burn data gap.

- Backend `ingest/parser.rs`: unmatched residuals from balance diffs are emitted as `kind="mint"` (destination-only residual, mint pubkey as synthetic source) or `kind="burn"` (source-only residual, mint pubkey as synthetic destination). SOL has no synthetic mint address so SOL residuals are still dropped.
- Backend `Edge`: gained `kind: String`.
- Backend ClickHouse schema: added `kind LowCardinality(String)` column.
- Backend `EdgeWire`: carries `kind` (skipped if empty).
- Frontend `RawEdge`: gained optional `kind`.
- Frontend `role-detect.ts`: added `token-mint` as the 7th role, checked first in the resolution order so mint pubkeys can't be mislabeled as tip-accounts or flow-hubs.
- Frontend `role-colors.ts`: pink-magenta for token-mint, lime-green for mint edges, red-orange for burn edges. All edges still at 0.25 alpha.
- Frontend `use-raw-stream.ts`: tracks the set of mint pubkeys observed (any address that appeared as the synthetic peer on a mint or burn edge), passes the set to `classifyNodes`, picks edge color from `kind`.
- Frontend sidebar: legend split into "Nodes" and "Edges" sections.

### Edge cases and missing data we left on the table

**Solana data we still don't capture.**
- **Native SOL movements that bypass SystemProgram.** Some programs mutate lamports directly on accounts they own (Marinade, certain stake pool implementations, niche DeFi vaults). Per AGENTS.md known limitations. Could be closed by also reading `meta.preBalances`/`postBalances` SOL deltas for non-SystemProgram-only paths — actually, the new parser already does this. So this gap may already be closed for SOL specifically. Worth a follow-up to confirm.
- **Transaction fees.** Fee-payer to leader flows are intentionally excluded by adding `meta.fee` back to the fee-payer's lamport delta. Documented choice, not a gap.
- **Failed transactions.** Skipped per parser line. Documented.
- **Versioned transactions with address table lookups beyond v0.** RPC client sets `maxSupportedTransactionVersion: 0`. Future Solana protocol versions would need a bump.
- **Token decimals.** Not tracked. We store raw base units only, so 1 USDC (1,000,000 base units) and 1 BONK (100,000 base units) are not directly comparable. Per-mint volume aggregation would require decimals.
- **USD-equivalent normalization.** Not done. We don't know prices. Cross-mint volume comparison is impossible.
- **Token metadata (symbols, names).** Mints are opaque pubkeys. `EPjFWdd5...` is USDC to a human reading the docs but to us it's just a string.

**Visualization edge cases.**
- **Megacore bridges still visually buried.** Long edges that cut through the megacore from one tip to another are technically rendered but get drowned by local density. No alpha rule fixes this; the deferred Option C (tip-set primary attraction per searcher) was rejected as falsifying topology.
- **Component grid-packing for singletons.** The thousands of singleton pairs at the margins are scattered randomly. Could be grid-packed for cleaner visual real estate.
- **Tips spread across multiple components.** When fewer than 8 of the 8 Jito tips share searchers in the current stream window, Union-Find correctly leaves them in separate components. We chose not to force-merge (Option B was rejected as falsifying topology).
- **Pump.fun mint pubkey burn-only/transfer-only patterns.** Not a gap — it's a real structural finding. Some pump tokens show only burns (active dump phase), some only transfers (active buying phase). We surfaced this via the new mint/burn distinction. The bonding curve PDA itself isn't yet labeled as a flow-hub specifically.
- **Frontend perf at higher edge volumes.** The SPL pass increased edge throughput. Layout still keeps up at current rates but hasn't been stress-tested at 10x. Documented as out-of-scope for the data-layer pass.

**Backend operational edge cases.**
- **Re-ingestion non-idempotency in the in-memory state machine.** Known and documented. Mitigated by the external state-reset script on every restart. Fix when moving off the reset script: LRU dedupe by `(signature, instruction_idx)` in `apply()`.
- **Mints and burns of native SOL.** Not captured (no native SOL mint address to use as synthetic peer). Probably negligible for actual Solana behavior.
- **Token amounts above u64::MAX.** Clamped to u64::MAX with a warning rather than crashing. Should never happen for well-behaved mints.

### Possible next directions

**Closest in scope.**
- **Bonding curve PDA detection.** For each pump-style token, the bonding curve PDA is structurally the wallet that's the source of all transfers and recipient of all burns for that mint. Detecting it and tagging it as a `protocol-vault` role (or extending `flow-hub`) would make pump.fun lifecycle stages legible at a glance.
- **Burn/mint pairing for trade events.** A pump.fun sell is a `burn` edge from user → mint plus a SOL transfer from bonding-curve-PDA → user. Pairing those two edges into a single "trade" event would make the swap visible as a unit rather than two disconnected halves.
- **Token-2022 transfer fees.** Token-2022 has a built-in transfer fee extension. The fee receiver is part of the mint config. Could be detected and labeled distinctly.
- **Live MEV dashboard.** Documented in earlier session summaries. Aggregates per-tip volume, top searchers, real-time. Uses data we already have.
- **Wallet profile page.** Per-wallet view: role, neighbors, in/out volume, component context. All data already classified.
- **MPC explorer page.** Surfaces the closed-loop community detection that's already running but only shown via console logs.

**Bigger lifts.**
- **Token metadata enrichment.** Resolve mint pubkeys → symbols (USDC, BONK, etc.) via on-chain metadata or an off-chain registry. Would let the legend show real names instead of pubkey prefixes.
- **Per-mint volume tracking.** Add `total_volume` per mint as a sidecar metric. Lets you see "USDC moved 12M base units in this window" without USD normalization.
- **USD value normalization.** Resolve mint → USD price (separate price feed). Aggregate across mints. Gives a unified "economic activity" metric.
- **Frontend perf at scale.** Sample, paginate, or hide-on-zoom-out the long tail of singletons if edge counts grow another order of magnitude.
- **Multi-chain expansion.** Project name is multi-chain-analysis-engine. Currently Solana-only. Adding Ethereum (via Erigon/Reth) or Bitcoin would test how generalizable our balance-diff abstraction is.

**Fundamentally different surfaces.**
- **Caterpillar/peel-chain detection.** Earlier in the session you flagged the `JAN1…SCUj` chain pattern as structurally distinct. Could become its own role or its own visualization page (forensics-oriented).
- **Memecoin lifecycle dashboard.** Combine the mint/burn distinction with the bonding curve PDA detection into a "live look at every memecoin's pump.fun trajectory" surface. Distinctive — almost no public Solana tool surfaces this directly.

The architectural rule we settled on early holds across all of this: filters hide, labels surface. Hiding stays out; labeling stays in. Every direction above adds labels or expands data capture, never gates visibility.