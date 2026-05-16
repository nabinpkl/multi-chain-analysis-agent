# Plan: LinkedIn article + X long-form Article on the agent-security catalog

Two long-form articles, one underlying piece of work (the 95-entry vulnerability catalog at `docs/securing-agents/catalog.md`), two genuinely different cuts. Both are articles, not threads.

## Research summary (sources verified May 2026)

### LinkedIn article (2026 platform state)

- Hard limit 125,000 chars (~20k words). Engineering articles work at 1,500-2,000 words. The 1,300-2,500 char band has the highest median engagement per LinkedIn's own 372k-post dataset (Authoredup, Feb 2026).
- Feed truncates at ~210 chars desktop, ~140 chars mobile. The headline conclusion belongs around line 5 so "see more" appears, but the first 210 chars must already be substantive.
- Dwell time is the primary 2026 algorithm signal (61s+ dwell = 15.6% engagement vs 1.2% at 0-3s). Comments weigh 15x likes. External links get penalized ~60% in feed reach (meet-lea, teract.ai, Feb 2026).
- AI-content backlash is strong. Originality.AI's 2025 study found 50%+ of LinkedIn long posts likely AI-generated; AI-flagged content sees ~30% less reach and ~55% less engagement.
- Code blocks render styled but without syntax highlight (2023 editor update, still current). For anything past ~20 lines, link to a gist.
- Audience skew: eng managers, staff+ ICs, technical hiring managers, recruiters.
- Editing: live, post-publish.

### X long-form Article (May 2026 platform state)

X Articles is alive and being actively pushed.

- Opened to **all Premium tiers** on January 7, 2026, ending Premium+ exclusivity (ppc.land, Jan 7 2026). Premium starts at $8/month.
- Distinct from "long posts" (which top out at 25,000 chars in a single post). Articles is a **separate authoring surface** with a richer editor, dedicated Article URL distinct from post URL, desktop-only authoring, unpublish-to-edit (no live edits) (ppc.land, Jan 7 2026).
- Formatting supported: headings, subheadings, bold, italics, strikethrough, indentation, bulleted lists, embedded images, videos, GIFs, posts, links.
- **No verified code-block support** with syntax highlighting. Real gap for engineering writing. Inline code via images or excerpt-and-link is the workaround.
- In-feed: appears with a "Listen" button (Grok TTS, March 8 2026) and inline preview that clicks through. The first two sentences are effectively the hook because they're what the preview shows.
- Distribution: native long-form gets algorithmic push because of dwell-time signals. Posts containing external links are explicitly down-ranked (AutoTweet, Feb 2026, updated Apr 2026). The dev.to claim that X started boosting external article links in 2026 is unverified and directly contradicts the AutoTweet algorithm read; treat as not corroborated.
- Growth signal: 18x in three months per Nikita Bier, head of product (Social Media Today, March 2026), boosted by a $1M prize in January 2026 for the top Article (≥1,000 words floor, Verified Home Timeline impressions, US-only).
- Norm length: 1,000-2,500 words. The $1M prize floor anchors expectations.
- Norm structure: curiosity-driven headline, optional header image, 2-4 line paragraphs, subheader every 3-5 paragraphs, one idea per paragraph, bold for key insights, recognizable voice (wordandvalue.com, Mar 2026).
- Audience skew: working engineers, OSS folks, founders. Tech Twitter has thinned since 2023 with engineers shifting to Mastodon, Bluesky, and own-blog; X is now used more for short links back than for primary publishing among the eng-Twitter cluster. I could not find a 2026 X Article by Simon Willison, Will Larson, Gergely Orosz, Phil Eaton, or Vicki Boykis.
- Editing: desktop-only, unpublish-to-edit. Draft fully before publishing.

### What both platforms reject in 2026

- Em-dashes (one of the clearest AI tells right now).
- Tricolons (groups of three forced for cadence).
- "It's not X, it's Y" unless earned.
- "Moreover," "furthermore," "additionally," "delve," "leverage," "robust," "comprehensive."
- Uniform paragraph rhythm.
- Vague attributions ("experts say," "many engineers find").
- CTAs at the end ("follow for more").

### Cross-posting and canonical

- LinkedIn does not support `rel=canonical`. No current source verifiable on whether X Articles supports a canonical tag, but the safe assumption is no.
- Pasting the same content on both reads as lazy to readers who follow on both, and confuses Google indexing.
- Right pattern: write the canonical version once (in this case the catalog file in the repo, which is where the artifact actually lives), then derive two genuinely different cuts. Each platform's article links back to the canonical.

## Topic and angle

**Topic.** The 95-entry vulnerability catalog I just built for an LLM agent reading public on-chain data. The specific insight: 7 system invariants are doing more defense work than any individual mitigation, and "broken-by-construction" is a more useful framing than "defended."

**Why this topic.** Transferable insight with a concrete artifact behind it. The catalog itself is 2,093 lines of evidence. The argument compresses cleanly: most agent-security writing focuses on what defenses you added; the more honest framing is what surfaces your system shape excludes. A read-only agent has no Tier 5. A single-tenant agent has no T5.6. Naming these as invariants you have to keep proving is more useful than enumerating mitigations.

**Why this works for both platforms.** It is a builder's insight (which X rewards) packaged around a learning-moment story (which LinkedIn rewards). The catalog is real, the numbers are verifiable, and the artifact link is the receipt.

**Alternative topics I considered.**

- Chapter 06 resource bounds with the graceful-return-not-exception design pivot. Concrete, narrower. Better as a second piece once the catalog post has set context.
- The whole 7-chapter system as a meta-story. Too sprawling for either format. Better as a series.
- Runtime parity testing (chapter 05). Strong but technical; better for a future post once readers have context.

If the user wants a different topic, the skeleton below stays; only section content swaps.

## LinkedIn article: structure

**Target length.** 1,500-1,800 words.

**Filename.** `docs/engineering-blogs/articles/linkedin-agent-vulnerability-catalog.md`

**First 210 characters (above the fold).**

> I built a 95-entry vulnerability catalog for an LLM agent that reads attacker-controllable on-chain data. Most of the entries are "does not apply."

That is 144 chars. Leaves room for one more substantive sentence about WHY most are "does not apply" before truncation, which is exactly what should hook the dwell time.

**Section skeleton.**

1. **The artifact.** Open with what was built (95 entries, 10 tiers) and the surprise (~30 defended, ~30 broken-by-construction, ~25 genuine residual). One short paragraph. ~80 words.
2. **Why I built it.** Two short paragraphs naming the prior writing (7 chapters on specific defenses) and the gap I felt (no flat index of attack classes that wasn't either too theoretical or too tactical). ~120 words.
3. **The format.** The five fields per entry (What, Applies when, Does not apply when, Defense pattern, Example), with one worked entry shown inline. T5.1 lethal trifecta is the most quotable. ~180 words.
4. **The realization.** "Does not apply when" is doing more work than "Defense pattern." The 7 invariants list, with brief reasoning for each. ~250 words.
5. **Why this matters in 2026.** The frontier reframe: Microsoft Semantic Kernel CVE-2026-26030, OX Security MCP advisory (Apr 2026, ~200k vulnerable instances), OWASP Agentic Top 10 finalized Dec 2025. Prompt injection is now an RCE class; defenses live at the tool dispatcher. The cheapest defense is the surface you don't have. ~180 words.
6. **The thing I almost got wrong.** First draft was 580 lines of project-internal references ("Defended in `policy/binding_store.py`"). Useless to anyone outside the codebase. Rewriting it as a generalized study with the wallet analyst as an example, not as documentation, made it transferable and tripled the length. ~150 words.
7. **The residual that worries me.** Adaptive attackers (arXiv 2603.15714, March 2026). Single-layer defenses fall to >85% under adaptive pressure. Static eval cases are a misleadingly optimistic signal. The catalog calls this out as T8.2 but does not close it. ~130 words.
8. **Closing.** One link to the catalog in the repo, one sentence on what's next. No CTA. ~50 words.

**Total.** ~1,140 words in section content + intro/outro = ~1,300-1,500 words.

**Tone.** Slightly reflective. "I almost got this wrong" works on LinkedIn because the audience values learning-moment framing.

## X long-form Article: structure

**Target length.** 1,200-1,800 words. Above the 1,000-word floor that anchors X Article expectations, below the upper end of the norm.

**Filename.** `docs/engineering-blogs/articles/x-article-agent-vulnerability-catalog.md`

**Headline (curiosity-driven, not clickbait).**

> The cheapest defense in an LLM agent is the surface you don't have

**First two sentences (these are the in-feed preview, so they ARE the hook).**

> I built a 95-entry vulnerability catalog for an LLM agent that reads attacker-controllable on-chain data. Most entries are "does not apply because we have no write tool," "does not apply because data is public," and so on, which turned out to be more useful than I expected.

The preview-snippet rule on X means the conclusion has to compress into roughly the first 280 visible characters before the clickthrough decision is made.

**Section skeleton (with 2-4 line paragraphs and subheaders every 3-5 paragraphs per the X norm).**

1. **The setup.** What the agent does (read-only wallet analyst over public on-chain data, three primitive tools, two runtimes). Two short paragraphs. ~150 words.
2. **The five-field format.** Show one entry inline (T5.1 lethal trifecta). The format itself is the news. ~200 words.

   *Subheader.*

3. **Seven invariants are doing most of the work.** List them with one-line reasoning each. This is the load-bearing section. ~300 words.
4. **What this changes about agent-security writing.** Most writing in the space focuses on the defense list. The catalog flips it: the surface you don't have is doing more work than any defense you added. ~150 words.

   *Subheader.*

5. **2026 reframed prompt injection as RCE.** Microsoft Semantic Kernel CVE-2026-26030, OX Security MCP advisory. Brief, with linked references. ~180 words.
6. **What I'd build differently.** Tag entries against OWASP Agentic Top 10 codes from the start. Add an adaptive-attacker loop earlier (arXiv 2603.15714). Open the schema-drift test to cover tool description text, not just parameter shapes. ~200 words.

   *Subheader.*

7. **The artifact.** Link to the catalog in the repo. One image of the rendered tier index. Closing line about the residual still ahead. ~100 words.

**Total.** ~1,280 words.

**Tone.** Builder voice. Less reflective than the LinkedIn version, more "here's the thing I made and why the shape matters." More technical specificity in section 5 because the X engineering audience can absorb it.

**Image.** One image, rendered from the catalog's tier-index table or one full entry. Real markdown screenshot, not stock illustration. Inserted between sections 2 and 3 (where the reader has just seen one entry's shape and now wants to see the whole structure).

**Code.** No code blocks. X Articles have no verified code-block support, and the post does not need any; the catalog content is conceptual. If a future post does need code (e.g., the chapter 06 resource bounds piece), the X version excerpts to a screenshot and links to a Gist.

**No external links in the announce post.** Links inside the Article body are fine; the announce post that points to the Article should not have additional outbound URLs because of the algorithmic penalty.

## What differs between the two cuts

Both are now articles. The cuts differ in voice, in where the punchline lands, and in what they cut.

| Element | LinkedIn cut | X Article cut |
|---|---|---|
| Headline | Conclusion-shaped: "I built a 95-entry vulnerability catalog. Most of it is 'does not apply.'" | Curiosity-shaped: "The cheapest defense in an LLM agent is the surface you don't have" |
| Punchline location | Line 5 (after the fold) | First two sentences (preview snippet) |
| Voice register | Slightly reflective; "I almost got this wrong" earns its slot | Builder voice; the thing I made and why the shape matters |
| Length | 1,300-1,500 words | 1,200-1,800 words |
| Subheaders | 2-3, lighter touch | Every 3-5 paragraphs, more visible structure |
| Paragraph rhythm | Mixed, slightly longer on average | 2-4 lines per paragraph, tighter |
| Section 6 (the mistake) | Present (LinkedIn rewards learning-moment framing) | Cut (X rewards the result more than the process) |
| Section 6 (rebuild plan) | Cut (too tactical for LinkedIn audience) | Present (X audience wants the next-iteration list) |
| Image | None or one optional inline (LinkedIn is paragraph-first) | One, between sections 2 and 3 (X norm includes an image) |
| Code | No code in either (catalog is conceptual) | No code in either |
| External links | One closing link to the catalog | One in-body link plus one to the OWASP source and one to arXiv (in-body, not in announce post) |
| What gets cut | The detailed CVE references | The "what I almost got wrong" reflection |

Same material. Different surface. A reader who follows me on both should feel they got two genuinely different reads.

## Canonical location

The canonical version of the catalog itself is `docs/securing-agents/catalog.md` in the repo. Both articles link there as the artifact.

Neither article is the canonical record of the work; both are derivatives pointing to it. If the project ever gets a standalone blog (the `docs/engineering-blogs/` folder suggests this is the eventual plan), the canonical article-shaped writeup moves to the blog and both LinkedIn and X versions link back with whatever canonical signals each platform supports.

## Pre-publish AI-tell strip pass (apply to both before posting)

Before posting, search the draft for and remove:

- Em-dashes. Replace with periods, commas, or parens.
- "Moreover," "furthermore," "additionally," "delve," "leverage," "robust," "comprehensive," "navigate" (figurative), "landscape" (abstract), "testament," "stands as," "serves as."
- "It's not X, it's Y" unless the contrast is genuinely earned.
- Tricolons (three items in a row) where two or four would read more natural.
- Vague attributions ("experts say," "many engineers find"). Replace with named sources or cut.
- Generic positive closings. Either cut or replace with a specific number about what's next.
- Uniform paragraph length. Mix one-sentence paragraphs into longer ones deliberately.

X Articles have one additional check: no code blocks rendered, so if the draft has any inline code, decide between excerpt-as-image and link-to-Gist before publishing (since the post cannot be edited live without unpublishing).

## Numbers that must appear, verified against the file before publish

- 95 (catalog entries; verify against `docs/securing-agents/catalog.md`)
- 10 (tiers)
- 14 (Tier 9 entries)
- 7 (invariants)
- 2,093 (catalog file line count today; re-verify before publish)
- ~30 / ~30 / ~25 (defended / broken-by-construction / genuine residual split)
- CVE-2026-26030 (Semantic Kernel)
- ~200k (OX Security MCP advisory vulnerable instances, Apr 2026)
- arXiv 2603.15714 (adaptive attacker, March 2026)
- 85% (adaptive attacker success rate against single-layer defenses)
- OWASP Agentic Top 10 (finalized December 2025)

## Open decisions for the user

1. **Confirm the topic.** The catalog is the recommended pick. Alternatives: chapter 06 resource bounds, or the broader 7-chapter system as a meta-story. Each would change section content but not plan shape.
2. **Confirm the canonical location stays as `docs/securing-agents/catalog.md`** for now (versus spinning up a standalone blog post first).
3. **Pen-name vs real-name.** Affects voice register slightly. Default assumption is real name (portfolio-shaped writing).
4. **Image for the X Article.** Recommendation: yes, one image, rendered from the catalog's tier-index table or T5.1 entry. The user captures the screenshot from the rendered Markdown.
5. **Whether to announce the X Article with a separate short post that links to it.** Recommendation: yes, one announce post with no other outbound links (to avoid the algorithmic penalty). The Article URL itself is the only link.

## Next actions if the plan is approved

1. Draft `docs/engineering-blogs/articles/linkedin-agent-vulnerability-catalog.md` (~1,500 words, structure above).
2. Draft `docs/engineering-blogs/articles/x-article-agent-vulnerability-catalog.md` (~1,400 words, structure above, one image slot marked).
3. Run both through the AI-tell strip pass.
4. Verify every number in section "Numbers that must appear" against `docs/securing-agents/catalog.md` line by line.
5. Note where the user needs to capture the X Article image (tier-index table or T5.1 entry, rendered from `catalog.md`).
