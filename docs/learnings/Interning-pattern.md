**Interning** (or "Dictionary Encoding") and **Referential Integrity** in high-performance data systems.

To put it simply: the `NodeAdded` event isn't about the *existence* of the node in a physical sense; it is a **definition** event that assigns a short nickname (the `NodeIdx`) to a long, bulky name (the `pubkey`).

Here is a breakdown of why this architecture is superior to "collapsing" the events.

---

### 1. The "Interning" Efficiency
In high-frequency data (like wallet transfers), the same address (pubkey) will likely appear in thousands of different edges. 
* **Without `NodeAdded`:** Every transfer event would have to carry two ~44-character strings. 
* **With `NodeAdded`:** You send the 44-character string **exactly once**. Every subsequent transfer just sends a 4-byte integer (`u32`).



**The math (from your example):**
* **Redundant:** $360,000 \text{ edges} \times 88 \text{ bytes (2 pubkeys)} \approx 31.6 \text{ MB}$
* **Interned:** $(10,000 \text{ nodes} \times 44 \text{ bytes}) + (360,000 \text{ edges} \times 8 \text{ bytes}) \approx 3.3 \text{ MB}$
* **Result:** You are saving roughly **90%** of your data volume by separating the "Identity" from the "Activity."

---

### 2. The Reducer's "Guarantee"
By emitting `NodeAdded` before `EdgeAdded`, you create a **topological dependency**. The consumer (the Reducer) can be "dumb" and fast because it never has to handle an "Unknown Node" state.

| Event Order | Reducer State | Action |
| :--- | :--- | :--- |
| **1. `NodeAdded(idx:5, key:"ABC")`** | `{5: "ABC"}` | Register mapping in memory. |
| **2. `EdgeAdded(src:5, dst:6)`** | `Error!` | Wait... I don't know what `6` is yet. |
| **3. `NodeAdded(idx:6, key:"XYZ")`** | `{5: "ABC", 6: "XYZ"}` | Now I'm ready. |
| **4. `EdgeAdded(src:5, dst:6)`** | `Success` | Draw line between "ABC" and "XYZ". |

This avoids "Out-of-Order" processing logic, which is one of the most common sources of bugs in distributed systems.

---

### 3. Decoupling Entity from Relationship
Your "Future-proofing" point is the most critical for long-term scale. If you collapse nodes into edges, you create a **Latent Entity** problem:
* What if a wallet is assigned a "Whale" role but hasn't made a transaction yet?
* If your system *requires* an edge to create a node, that wallet literally cannot exist in your database until it moves money.

By keeping `NodeAdded` distinct, the node becomes a first-class citizen. It can be created, tagged, categorized, and moved spatially (Slice 8) regardless of whether it has ever participated in an "Edge."

### Summary: Schema vs. Fact
Think of `NodeAdded` as **DDL** (Data Definition Language) and `EdgeAdded` as **DML** (Data Manipulation Language). You define the "columns" (nodes) before you insert the "rows" (edges). 

**The Verdict:** Stick with two events. The minor complexity of a second event type pays for itself a thousand times over in memory savings and system stability.