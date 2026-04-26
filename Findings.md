## Findings from this session

**On the SOL slice (before SPL expansion):**

1. **The megacore is one thing, not many.** The 8 Jito tip accounts plus the H1uT mega-router co-anchor a single connected component of ~250 nodes. No competing MEV ecosystem exists as a second cluster.

2. **The megacore has no dominant hub.** Top:second degree ratio sits around 1.02. It's a multi-modal mesh of roughly equivalent attractors, not a star with one center. This explains why standard force layouts produce blob-shaped megacores.

3. **84% of MEV searchers touch only one Jito tip per stream window.** The long tail of one-off MEV bundles dominates by count. Only ~38 heavy bots touch all 7-8 tips.

4. **Jito tips don't always sit in one component.** Within short stream windows, 2 of 8 tips can be structurally isolated because no searcher in that window happened to bridge them. Union-Find correctly reports them as separate components rather than forcing a merger.

5. **Whale-class actors exist but are isolated.** The 24,000 SOL OTC pair we observed sits as a singleton — one transfer between two wallets, no bridges to anything else. Whales bypass the megacore entirely.

6. **Real economic hubs vs tip-style hubs are distinguishable by avg-volume-per-edge.** A tip account has 200+ degree at <0.001 SOL/edge. A real DEX vault hub has 30-50 degree at 1-100 SOL/edge. Same topology family, totally different economic role, separated cleanly by the avg/edge ratio.

7. **Caterpillar/peel-chain patterns are visible.** The `JAN1…SCUj` chain — five arms of degree-2 nodes radiating from a central hub — is a forensic signature for sequential-hop transfers (peel chains, distribution chains, sweeper bots). Filter removal made these visible because they're low-volume, low-degree subgraphs that the old thresholds suppressed.

8. **MPC closed-loop economies cluster but don't dominate.** Louvain detection consistently flags 13-22 communities at any moment, with the top one (c=24 in one snapshot, c=11 in another) moving 200-350 SOL among 30-60 wallets at near-perfect intra-community concentration (50% intra-volume share, 30%+ looper share).

**On Solana economics generally:**

9. **Native SOL transfers via SystemProgram capture only one slice of activity.** Most user-facing Solana activity (memecoin trading, USDC payments, NFT trades) happens in SPL tokens and is invisible to a SystemProgram-only parser.

10. **Transaction fees are not visible via SystemProgram parsing.** Solana fees are runtime-mechanic lamport deductions, not SystemProgram instructions. A wallet paying gas to do anything is invisible to us by design.

11. **Some SOL movements bypass SystemProgram entirely.** Programs that own accounts can mutate lamport balances directly. Marinade, certain stake pools, niche DeFi vaults do this. The new balance-diff parser closes this gap; the old instruction-level parser had it open.

**On the SPL expansion (after Pass 3):**

12. **SPL captures expand the data by ~3-5x edges per slot, not 10-50x.** Real-world ratio came in lower than feared. ~600 edges/slot post-expansion vs ~150 pre-expansion. Layout still keeps up.

13. **Wrapped SOL is the highest-volume mint by far.** Every Jupiter, Raydium, or Orca swap involving native SOL produces a wSOL mint (wrap) and a wSOL burn (unwrap). 6,982 mints + 3,427 burns of wSOL in one stream window — basically a count of "DEX swaps that touched SOL."

14. **USDC is second-most active.** Distinguishable in the data (`EPjFWdd5...`) and a natural baseline of "real money moving" on Solana.

15. **Pump.fun memecoins show distinctive lifecycle patterns.** Each pump token's edge kind distribution is a lifecycle signal:
    - All transfers, no burns: active buying phase on bonding curve
    - All burns, no transfers: active dumping phase, holders exiting via bonding curve sells
    - Mixed: healthy two-sided trading
    - Only transfers post-launch: token has graduated to Raydium AMM

16. **Pump.fun bonding curves use Burn for sells, not Transfer.** When a user sells back to the bonding curve, the curve calls Burn on their token account directly. The seller's wallet → mint pubkey edge with kind=burn is the structural signature of "user dumping their position."

17. **Pre-mint at token creation means we miss most mint events.** pump.fun pre-mints the entire supply once at token creation into the bonding curve contract. We see this only if our stream was running at that moment. For older tokens, we see only transfers (buys) and burns (sells), no mint events.

**On position-NFT protocols (the three-mints surprise):**

18. **Three specific mints (`GLVu…W5Jr`, `fN8AC…f1Uo`, `CSAB…9SaA`) burn together in lockstep.** 690 burns each, all in shared transactions, exactly 1 unit per burn, all from the same burner wallet. That's the position-NFT redemption signature — supply 1, decimals 0, burn-on-close.

19. **One bot operator dominates a redemption flow.** The burner wallet `Cf3tW…1tbi` runs the same three-burn pattern across ~700 transactions during the stream window. Different wallets pay 0.002 SOL to a destination on each transaction; the bot burns the trio. This is a single protocol's operational footprint surfaced cleanly because the new mint/burn distinction isolates it from regular trading.

**On Solana market structure:**

20. **Jito has a near-monopoly on observable MEV.** All 8 wallets matching the tip-account behavioral signature are Jito's. The classifier didn't know about Jito specifically — it just looked for "high degree + dust per edge" — and 8 of 8 happen to be Jito accounts. No second ecosystem exists at scale.

21. **Stealth MEV is invisible to us.** Funds running their own validator + searcher pipelines without paying observable tips don't show up. Plausibly 10-30% of Solana MEV is invisible.

22. **Jito's monopoly persists for structural reasons.** Validator-side integration depth (forked client), block engine infrastructure cost, two-sided network effects. Not regulatory or accidental — it's the natural equilibrium of a market where coordination has strong returns.

23. **Jito is genuinely mixed in impact.** Pro-validator, pro-arbitrageur, pro-network-efficiency. Mixed for DEX users (less congestion, more efficient sandwiches). Anti-decentralization (single point of policy control). Not a villain, not a neutral utility, just a private company running critical public-good infrastructure with real tradeoffs.

**On the project's own design:**

24. **"Filters hide, labels surface" is the right architectural rule.** Removing filters cost almost nothing and exposed real findings (the JAN1 caterpillar, the lone-tip components). Adding labels surfaced the existing data without occluding it.

25. **The graph topology itself is the chain identity.** Solana looks like one MEV mass + DEX hubs + scattered whales + memecoin lifecycle patterns. A different chain (Bitcoin, Ethereum) would look topologically different out of the same generic balance-diff parser. The shape is the chain's economic fingerprint.

26. **Mint pubkeys are not wallets.** The classifier mislabels them as tip-accounts or flow-hubs unless explicitly excluded. They're token contracts, structurally different, and deserve their own role label.

27. **Universal balance-diff parsing handles every program type without per-protocol decoding.** One parser handles SystemProgram, SPL Token, Token-2022, all DEX programs, all lending protocols, all NFT marketplaces. We don't need a registry of program IDs; we read the runtime's balance bookkeeping. This is an architectural win that scales to whatever new Solana programs ship.


Both framings are honest. The reality is genuinely mixed and the "good vs bad" answer depends on which stakeholder you're framing for.

**The case that Jito is positive infrastructure**

MEV exists on any blockchain with public mempools and DEX activity. It's not something Jito invented; it's emergent from the structure. Given that MEV will happen, the question is how it gets allocated:

- **Without Jito-style coordination**: searchers spam transactions racing to land first. Failed bundles waste compute, validator income is unpredictable, network congestion spikes during arbitrage opportunities. Whoever has the best validator-side relationships wins through opaque side channels.
- **With Jito**: a transparent auction. Searchers bid via tips, top bids land, the rest don't get included. Failed bundles cost only the tip (not gas for the whole transaction). Validator income smooths out. The market for blockspace becomes legible.

In this framing Jito is the same kind of coordination mechanism as any well-functioning auction: it doesn't create the competition, it organizes it. Spreads tighten because arbitrage is more efficient. Liquidations land reliably so lending protocols stay solvent. Validator and staker revenue is higher and more uniformly distributed across the stake set, which actually *helps* small validators relative to a regime where only well-connected operators capture MEV.

**The case that Jito is extractive**

The flip side: Jito enables more efficient sandwich attacks. A sandwich is structurally negative-sum for the victim — the user pays more slippage than they would on a fair-priced trade, that excess flows to the searcher. Bundles make sandwiches more reliable, which means more sandwiches happen. So Jito's better infrastructure for searchers is, in part, better infrastructure for extracting from regular DEX users.

It also concentrates power. Jito Foundation runs the Block Engine. Validators running `jito-solana` are economically dependent on Jito's continued operation. If Jito decided to censor certain transactions, blacklist wallets, or change tip mechanics adversely, there's no realistic alternative for searchers in the short term. That's a single point of policy control over Solana's MEV layer.

**Where the honest answer lands**

Jito is good for: validators, stakers, arbitrageurs, the protocol's overall health.
Jito is mixed for: DEX users (less congestion = good, more efficient sandwiches = bad).
Jito is bad for: decentralization purists, anyone worried about single points of policy control.

Most informed people in the space view Jito as net-positive infrastructure that comes with a real concentration cost. It's not a villain, but it's not a neutral utility either. It's a private company running critical public-good infrastructure with strong network effects, and that has tradeoffs.

The cleanest way to frame it: **Jito formalized something that was happening informally**. Pre-Jito, MEV on Solana was a mess of spam transactions and validator-searcher private deals. Jito turned it into a structured market. That's a real efficiency gain. But formalizing extraction can also mean making it more efficient, which means more of it happens.

Both things can be true at once.

The graph view we built actually surfaces a piece of this: every Jito tip account is a structurally identical signature (high degree, dust per edge), and they all sit in one component because they share searchers. There's no "competing MEV ecosystem" cluster anywhere in the graph. That's not a value judgment, just a topology fact. A second ecosystem would show up as a second 8-tip pattern. There isn't one.


