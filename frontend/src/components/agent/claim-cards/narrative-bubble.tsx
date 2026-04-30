"use client";

/**
 * Free-form interpretation prose from the agent. Ship 1.6 introduced
 * this channel to let the model talk in natural language instead of
 * cramming everything into a Claim card. Visually distinct from
 * `ProfileCard` (lighter chrome, dashed left edge, italic-leaning
 * type) so the user can tell at a glance what was measured (Claim)
 * versus what is the model's interpretation (Narrative).
 *
 * No markdown rendering yet: ship 1.6 prints the text verbatim with
 * whitespace preserved. Markdown / link safety lands when the
 * factuality gate ships in 2 alongside `narrative.no_factuality_gate`
 * stub retirement.
 */
export function NarrativeBubble({ text }: { text: string }) {
  return (
    <div className="border-l-2 border-dashed border-mca-border/70 pl-3 py-2 space-y-1">
      <div className="text-[0.55rem] uppercase tracking-[2px] text-mca-dim">
        interpretation
      </div>
      <p className="text-sm text-mca-text/90 leading-relaxed whitespace-pre-wrap italic">
        {text}
      </p>
      <p className="text-[0.55rem] uppercase tracking-[1.5px] text-mca-dim pt-1">
        model interpretation. verify via cited data.
      </p>
    </div>
  );
}
