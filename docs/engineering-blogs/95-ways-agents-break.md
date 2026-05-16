# 95 ways agents break, from fundamentals to frontier

## Short post

I spent the last few weeks working and writing down every way I could think of to break an LLM agent priming for production. I have managed to research 95 attack classes ranking from fundamentals(raw models) to frontier(Harnesses with tooluse). The list below in the article provides a checklist for you and your agent to check against. A single line on what an attacker gets if you ship without a defense for it.

The shape of the list:

- Raw model alone (1 to 10). Direct prompt injection, persona-swap jailbreaks, confident fabrication, training-data regurgitation.
- Retrieved data (11 to 22). Indirect prompt injection, source-provenance loss, multi-modal payload smuggling, vector-store poisoning.
- Read tools (23 to 37). Tool-argument injection, server-side request forgery, resource exhaustion loops, tool-ordering bias.
- Output checks (38 to 45). Fabricated entity emission, LLM-judge manipulation, eval-suite gaming, structural-token forgery.
- Identity (46 to 50). Confused-deputy escalation, brand impersonation, canonical-entity spoofing, internal-architecture disclosure.
- Writes (51 to 58). The lethal trifecta lives here: an agent with access to private data, exposure to untrusted input, and an outbound channel can be steered into leaking the data through the channel.
- Multi-agent (59 to 64). Inter-agent message forgery, rogue-agent enrollment, cascading blast radius, agent-card spoofing.
- Supply chain and runtime (65 to 76). Silent model swap, cross-MCP token confusion, log-channel poisoning, configuration drift.
- Eval and incident response (77 to 81). Non-ablatable defenses, static-eval blind spots, runbook gaps, compliance vocabulary drift.
- Frontier (82 to 95). Adaptive attackers, computer-use hijack, long-horizon goal drift, denial-of-inference via refusal.

When you use no tools, no retrieval, no writes, no memory: most of the list goes away. The learning is to derive your threat model from what your system actually is, not from what every attacker has ever published.

## Full article

I spent the last few weeks writing down every way I could think of to break an LLM agent. Ninety-five classes by the time I stopped. The list below numbers them straight through. For each one, a single line on what an attacker gets if you ship without a defense for it.

### The model alone

1. **Direct prompt injection:** User input overrides operator instructions, model executes attacker goals as if you wrote them.
2. **Jailbreak via persona swap:** Safety policy dissolves once the model agrees to roleplay an unrestricted character.
3. **System-prompt extraction:** Operator's hidden instructions, allowlists, and reasoning leak verbatim to anyone who asks the right way.
4. **Underlying-model identity reveal:** Branded agent confesses it is GPT or Claude or Gemini, breaking product fiction and inviting model-specific attacks.
5. **Fabrication of facts:** Confident wrong answers about entities, balances, addresses that downstream readers treat as ground truth.
6. **Off-domain drift:** Agent ranges outside its declared job and answers medical, legal, financial questions with the same authority as its real domain.
7. **Training-data regurgitation:** Verbatim copyrighted text, leaked PII, or memorized secrets surface in completions.
8. **Refusal-channel exfiltration:** The refusal message itself carries the secret, encoded as which words appear or how it is phrased.
9. **Unsafe-content generation:** CSAM, weapon synthesis, self-harm encouragement emitted in production traffic and attached to the operator's brand.
10. **Bias amplification:** Outputs systematically favor or harm a demographic, embedded in routine decisions at scale.

### Retrieved data

11. **Indirect prompt injection from retrieved data:** Anyone who can write into a source the agent reads becomes a co-author of its instructions.
12. **Envelope close-tag forgery:** Untrusted data closes the system envelope and reopens as system, escalating attacker text to operator privilege.
13. **Chat-template control-token forgery:** Raw template tokens in user input rewrite the conversation structure the model sees.
14. **Markup or markdown injection:** Hidden links, images, and styled blocks render in the client and exfiltrate or phish the human reader.
15. **Source-provenance loss:** Quotes lose their origin tag, so attacker-controlled text is cited with the same weight as canonical references.
16. **Unicode obfuscation:** Homoglyphs, zero-width characters, and bidi overrides smuggle instructions past human review and substring filters.
17. **Steganographic instructions in non-text bytes:** Images, audio, and PDFs carry instructions the model reads but the human cannot see.
18. **Embedding or vector-store poisoning:** Crafted documents win retrieval for queries they should not, redirecting the agent to attacker content.
19. **Memory or cache poisoning:** One bad turn pollutes a long-term store and replays into every future session.
20. **Long-context attention attacks:** Instructions buried in the middle of a long document evade both attention and human review.
21. **Multi-modal input injection:** Pixel-level or audio-band instructions invisible to humans steer the model.
22. **Self-poisoning via prior output:** Agent's own earlier text re-enters context as authoritative source and reinforces fabricated claims.

### Tool-call surface for read tools

23. **Tool name confusion or shadowing:** Two tools with similar names cause the model to call the attacker's instead of yours.
24. **Tool description as instruction vector:** The tool catalog itself carries hidden instructions executed before any user input.
25. **Tool argument injection:** Attacker-chosen values flow into tool calls and abuse the downstream system.
26. **Prompt-to-RCE via shell-bound arguments:** String concatenation into a shell or eval turns chat input into code execution on your server.
27. **SQL, Cypher, or NoSQL injection:** Tool wraps a query language and untrusted text alters the query shape, leaking or mutating the database.
28. **Path traversal:** Tool argument escapes the intended directory and reads or writes anywhere on disk.
29. **SSRF:** Tool fetches a URL the attacker controls, including cloud metadata endpoints and internal services.
30. **Tool poisoning via post-install description mutation:** A tool's description changes after vetting, slipping new instructions past review.
31. **Excessive agency:** Tool surface grants more capability than the task needs, so any compromise gets the larger blast radius.
32. **Capability creep within a session:** Per-turn restrictions accumulate into a wider net than any single approved call.
33. **Resource exhaustion: tool-call loop:** Agent ping-pongs between tools until quotas or wall clock burn out.
34. **Resource exhaustion: token burn:** Crafted input forces maximum-length completions over and over, draining inference budget.
35. **Resource exhaustion: wall-clock:** Long synchronous tools tie up the request thread and block other users.
36. **Quota or cost exhaustion across turns:** Slow leak across many sessions runs the operator's bill into the ground without tripping rate limits.
37. **Tool-list ordering bias:** The model picks the first tool that seems to fit, so order in the catalog becomes a security boundary.

### Output verification

38. **Fabricated entity emission:** Output names a wallet, customer, or ticket that does not exist, then humans act on it.
39. **Number-paraphrase drift:** "About one million" used in place of an exact figure changes meaning across the downstream pipeline.
40. **Sourcing claims that do not trace:** Citations look real but point to nothing or to attacker-controlled URLs.
41. **Verifier-rule bypass via phrasing:** Output evades string-match rules by rewording the prohibited content.
42. **Judge manipulation:** The LLM judge is itself prompt-injectable through the candidate output it scores.
43. **Judge-model downgrade:** A weaker judge silently slipped into the verifier rubber-stamps things the strong judge would catch.
44. **Eval gaming, Goodhart on the suite:** Optimizing the eval score erodes the property the eval was meant to measure.
45. **Output structural-token forgery:** Model emits forged tool-call tokens that the orchestrator runs verbatim.

### Agent identity and domain

46. **Off-domain forced answer:** Agent gives a confident answer outside its scope when it should have refused.
47. **Canonical-entity impersonation:** "USDC" rendered by an attacker-issued token of the same name is treated as the real one.
48. **Confused deputy:** Agent acts with its own privileges on behalf of a user who does not have them.
49. **Brand impersonation:** Outputs render operator branding while serving attacker content.
50. **Internal architecture disclosure:** Model names internal services, env vars, and pipeline steps, mapping the attack surface for free.

### Write-capable side effects

51. **Lethal trifecta:** Private data plus untrusted input plus an outbound channel equals exfiltration on tap.
52. **Plan mutation:** Multi-step plan changes after approval and executes a different action than the human saw.
53. **Write amplification:** One approved write triggers a cascade of follow-on writes the human never reviewed.
54. **Action provenance loss:** Audit trail cannot tell which human instruction caused which action.
55. **Pre-execution policy bypass:** Policy check happens too early and lets the actual call slip past with different args.
56. **Cross-tenant data leakage:** Tenant A's data lands in tenant B's session through shared state.
57. **Authorization confusion:** Agent has rights the user does not and uses them on the user's behalf.
58. **Side-channel exfiltration via write outputs:** Filenames, commit messages, ticket titles carry secrets out of the system.

### Multi-agent

59. **Inter-agent message forgery:** Agent A receives a message claiming to be from agent B that B never sent.
60. **Agent-to-agent injection:** Output of one agent is input to another, propagating the same injection chain across hops.
61. **Sub-agent context-budget exhaustion:** Adversary forces a sub-agent to spend its budget producing noise so the parent times out.
62. **Rogue agent enrollment:** Unknown agent joins the mesh and starts receiving traffic with no vetting.
63. **Agent-card spoofing:** Capability advertisement claims tools or permissions the agent does not actually have.
64. **Cascading failure or blast radius:** One agent's error fans out across the mesh until the whole system degrades.

### Supply chain, runtime, and infrastructure

65. **MCP server supply-chain compromise:** Upstream MCP server ships malicious tools that your agent now offers users.
66. **Tool schema drift between client and server:** Client and server disagree on argument shape, validations get bypassed at the gap.
67. **Model swap or downgrade:** Production model silently replaced with a cheaper or unaligned one, no eval delta visible.
68. **Runtime drift between environments:** Staging passes evals, prod runs different model or system prompt, gap goes unmonitored.
69. **Observability gap or silent failure:** Tool fails or returns wrong data, agent narrates around it, no alert fires.
70. **Backdoored model weights:** Weights from an untrusted source contain a trigger that activates on a specific input pattern.
71. **Fine-tune training-data poisoning:** Crafted examples in the fine-tune set introduce backdoors or bias.
72. **Hook or plugin supply chain:** Pre and post hooks loaded from a registry can execute arbitrary code in the agent process.
73. **Secret exfiltration via logs or tool arguments:** API keys land in log lines or tool args, then leak through whatever consumes those logs.
74. **OAuth refresh-token races or cross-MCP token confusion:** Tokens for service A get sent to service B because the agent does not track audience.
75. **Telemetry or log-channel poisoning:** Attacker writes into the logs the operators read, framing or hiding incidents.
76. **Configuration mutation mid-flight:** Config changes between turns, the second turn runs with surprising policy.

### Evaluation and incident response

77. **Defense not individually ablatable:** Stacked defenses you cannot turn off one at a time, so you do not know which one is actually catching the attacks.
78. **Static eval understates adaptive-attacker exposure:** A fixed test set tells you nothing about what a creative adversary will find.
79. **Trust-boundary mis-claim on meta-defenses:** Saying "we have a judge" hides that the judge sees attacker input too.
80. **Incident response and runbook gap:** When something fires, nobody knows what to do or who is on call.
81. **Compliance vocabulary drift:** Reports say "deterministic" or "sandboxed" when the underlying behavior is neither.

### Frontier

82. **Adaptive attacker:** Defenders ship a fix, attackers ship a counter, and your bench was a snapshot.
83. **Computer-use or GUI hijack:** Agent that drives a browser or desktop accepts instructions from on-screen content.
84. **MCP elicitation or sampling abuse:** Server-side prompts piggyback on MCP elicitation flows and reach the model directly.
85. **Workload identity federation confusion:** Federated identity from one cloud accepted by another with broader trust than intended.
86. **A2A protocol exploitation:** Holes in agent-to-agent protocols replay the same lessons HTTP learned in 1998.
87. **Agent-authored output as untrusted re-input:** Agent's own output, persisted and re-read, counts as adversarial once memory was poisoned upstream.
88. **Markdown-rendered exfiltration via tool result:** Tool returns markdown the client renders, including image URLs that beacon out.
89. **Cross-conversation memory injection:** User A poisons shared memory and steers user B's session.
90. **RAG retrieval manipulation via query-string injection:** Untrusted text in the agent's query string changes which corpus shards get hit.
91. **Long-running goal drift:** Agent operates over days and the goal subtly shifts each turn, no single step alarming.
92. **Token-distribution or probabilistic-defense bypass:** Defense relies on logits that the attacker shapes by choosing inputs.
93. **Time-of-check, time-of-use on snapshot data:** Policy check runs on a snapshot, action runs on live data that has since changed.
94. **Denial-of-inference via weaponized refusal:** Attacker crafts inputs that trip safety filters at scale, the system stops serving real users.
95. **Race conditions on shared per-turn state:** Concurrent turns mutate the same buffer and produce inconsistent reads.