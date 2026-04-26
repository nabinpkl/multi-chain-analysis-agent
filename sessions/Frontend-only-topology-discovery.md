# Architectural Decision Record: Topological Truth & Frontend-First Discovery

**Status:** Approved / Historical  
**Date:** April 25, 2026  
**Architect:** [User Name/Title]  
**Project:** Multi-Chain Analysis Engine (Solana Live Graph)

---

## 1. Executive Summary
This document codifies the architectural justification for implementing the full graph compute stack—including ForceAtlas2 physics, role-based classification heuristics, and real-time community detection—within the frontend application layer. While traditionally viewed as a "performance bottleneck," this decision was a strategic move to prioritize **Developer Velocity** and **Topological Discovery** over premature backend optimization.

## 2. The Problem Statement: "The Blind Ingestion Trap"
Early iterations of the Solana ingestion engine (Rust-based) utilized strict server-side filtering (e.g., volume-per-edge thresholds) to manage data flow. However, this created a "Blind Ingestion Trap":
* **Data Erasure:** By filtering on the backend, we were accidentally dropping critical structural anchors (like "Mega-routers") that moved low volume per edge but had massive topological significance.
* **Invisible Narratives:** We couldn't "see" the shape of MEV clusters or MPC closed-loop economies because the filters were designed before we understood the data's geometry.
* **The Build-Cycle Tax:** Every minor tweak to how we categorized a "Jito Tip" required a Rust recompilation and redeployment, slowing the feedback loop to minutes instead of seconds.

## 3. The Decision: Frontend-Driven Truth
We moved the entire "intelligence" layer of the graph into the frontend hooks (`use-raw-stream.ts`) and classification modules (`role-detect.ts`). This architecture treats the backend as a high-performance "stateless firehose" and the browser as a "Real-time Lab."

### 3.1 Key Components of the Frontend Stack
* **Physics Engine:** Custom ForceAtlas2 implementation with `snapTogether` logic to manage 100k+ nodes.
* **Heuristic Engine (`role-detect.ts`):** Real-time tagging of Jito Tips, MEV Searchers, and Flow Hubs.
* **Community Detection:** In-browser Louvain algorithm execution to identify MPC clusters.
* **Dynamic Styling:** Logarithmic node scaling based on emergent degree and volume.

---

## 4. Architectural Justification

The decision to keep this compute in the frontend was driven by three core pillars:

### A. The "Visual Feedback Loop" (Primary Driver)
In systems engineering, the speed of the feedback loop determines the quality of the product. 
* **Instant Calibration:** Developing a layout that doesn't "explode" requires constant tweaking of repulsion strengths and collision distances (`touchDistance`). 
* **The HMR Advantage:** By using TypeScript/React with Hot Module Replacement, we could modify the `log10` scaling for a "Flow Hub" and see the entire 100,000-node graph reorganize **instantly**. If this were backend-driven, we would have had to restart the ingestion stream and wait for the graph to "re-warm" for every single UI adjustment.

### B. Topological Truth over Human Narrative
We chose a "Radically Simpler Raw View." We didn't want to tell the graph what it was; we wanted the graph to tell us.
* **Emergent Identification:** By rendering everything as pure topology, the "narrative" emerged from physics:
    * **Dense Blobs:** Naturally identified MEV and high-frequency routing.
    * **Spidery Chains:** Naturally identified peer-to-peer retail flow.
* **Heuristic Validation:** The frontend allowed us to visually verify our roles. If a node was tagged as a "Whale" but sat in the middle of an "MEV Blob," we knew our heuristic was wrong. Seeing the "mistake" in the layout was the only way to correct the logic.

### C. Protocol Decoupling & Statelessness
A backend-driven layout would require the server to maintain a "Mirror State" of what the user is seeing. 
* **Avoidance of State Sync:** By computing on the frontend, the backend doesn't need to know about "X/Y coordinates" or "Node visibility." It simply streams raw data. 
* **Client-Side Agency:** This allows different clients to visualize the same stream differently (e.g., one user focusing on MEV, another on Retail) without reconfiguring the backend.

---

## 5. Case Study: The "Mega-Router" Discovery
During Session `bf7c713d`, we identified a specific node (`H1uT...fXnp`) with a degree of 329 but a low volume-per-edge. 
* **The Old Way:** This node was being filtered out by the Rust backend's "0.001 SOL gate."
* **The Frontend Way:** Because we were rendering the "Raw Mess," we saw a massive hub visually dominating the canvas. This discovery allowed us to immediately update `role-detect.ts` to prioritize "Degree" over "Volume," a change that took 5 seconds in the frontend but would have taken a full sprint to identify and fix in a "blind" backend.

## 6. The "Hardening" Roadmap (Transition Strategy)
This ADR acknowledges that frontend compute is for **Discovery**, while backend compute is for **Scale**.
1. **Phase 1 (Current):** Frontend compute to find structural invariants and prove heuristics.
2. **Phase 2:** Port proven heuristics (e.g., Jito Tip detection) into the Rust ingestion layer to reduce client-side CPU load.
3. **Phase 3:** Transition to a hybrid model where the backend tags roles at "point-of-entry," but the frontend retains control over the "physics" of the layout to preserve the interactive experience.

---

## 7. Conclusion
We traded CPU cycles for **Architectural Insight**. By doing "messy" compute on the frontend, we successfully moved from a screen full of noise to a categorized, high-fidelity map of the Solana economy. We didn't build a renderer; we built a **discovery engine.**