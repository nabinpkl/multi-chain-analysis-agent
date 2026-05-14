# Vec-as-Slab vs HashMap

 **Vec with EdgeIdx as key is already behaving as a HashMap with integer keys — just better.**

## Why Vec wins for integer keys

For dense u32 keys (which EdgeIdx is, after slab allocator reuses freed slots):

| Op | `Vec<Option<T>>` | `FxHashMap<u32, T>` |
|---|---|---|
| Insert | O(1), single deref + write | O(1), but: hash + probe + maybe realloc |
| Delete | O(1), set None + push idx to free-list | O(1), but: hash + probe + tombstone bucket |
| Lookup by idx | O(1), `vec[idx as usize]` — single L1-cache hit | O(1), but: hash + probe + chase |
| Iterate | linear, cache-friendly | linear, scattered |
| Memory | dense, ~sizeof(T) per slot | hash table overhead (1.5–2x) |
| Insertion idx control | YES, allocator chooses | NO, hash decides |

Vec wins on every axis when keys are dense integers we control.

## Why HashMap exists at all in our design

For sparse keys or non-integer keys. We use it for:
- `interner.forward: FxHashMap<String, NodeIdx>` — string key, can't index
- `node_to_component` could be a HashMap if NodeIdx were sparse, but it isn't (interner gives 0,1,2,…) so Vec wins

## Mental model

> "HashMap with integer keys" = "Vec with sentinel for missing entries"

When the key space is dense and we control allocation, the latter is strictly better. Cache-friendly, no hashing, no probing. Slab allocator (free-list) preserves the "dense u32 key" invariant under deletion.

This is exactly the leetcode "design data structure with O(1) insert/delete/get" pattern — backing array + free-list. Same trick.

## When HashMap would be the right call

If `EdgeIdx` were sparse — e.g. external IDs we don't control, like `signature + instruction_idx` — Vec would mostly be empty and HashMap saves space. We dodge that by minting EdgeIdx ourselves at allocation time.

So yes: same big-O, Vec strictly better in constants, memory, cache. Slab allocator is just "manual hashmap with the hash function = identity."