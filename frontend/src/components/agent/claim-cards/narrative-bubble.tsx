"use client";

/**
 * Free-form interpretation prose from the agent. Ship 1.6 introduced
 * this channel to let the model talk in natural language instead of
 * cramming everything into a Claim card. Visually distinct from
 * `ProfileCard` (lighter chrome, dashed left edge, italic-leaning
 * type) so the user can tell at a glance what was measured (Claim)
 * versus what is the model's interpretation (Narrative).
 *
 * Ship 2 added the constitution gate. When the gate retracts a
 * narrative, the same payload arrives with `retractedReason` set: we
 * still show the user the text (visible retraction beats silent
 * removal) but in a struck-through amber treatment with the gate's
 * one-sentence reason below it. Mirrors the `RetractedCard` styling
 * on the Claim channel so the two retraction surfaces are visually
 * coherent.
 *
 * Ship 2.6.1: `retractedDebug` is the raw constitution reason,
 * present only when the backend was started with
 * `AGENT_DEBUG_PUBLIC=1`. Rendered as a small monospace block under
 * the friendly `retractedReason` so the dev sees what the gate
 * actually flagged without leaking it to prod users.
 *
 * No markdown rendering yet: prints text verbatim with whitespace
 * preserved. Markdown / link safety would land alongside an
 * equivalent claim-body rendering treatment.
 */
export function NarrativeBubble({
  text,
  retractedReason,
  retractedDebug,
}: {
  text: string;
  retractedReason: string | null;
  retractedDebug: string | null;
}) {
  if (retractedReason) {
    return (
      <div className="border border-amber-500/40 rounded-md p-3 bg-amber-500/5 space-y-2 opacity-80">
        <div className="flex items-baseline justify-between gap-2">
          <span className="text-[0.55rem] uppercase tracking-[2px] text-amber-500">
            interpretation retracted
          </span>
        </div>
        <p className="text-sm text-mca-muted leading-relaxed whitespace-pre-wrap line-through italic">
          {text}
        </p>
        <p className="text-[0.6rem] uppercase tracking-[1.5px] text-amber-500/80 pt-1 border-t border-mca-border">
          <span className="text-mca-text normal-case tracking-normal">{retractedReason}</span>
        </p>
        {retractedDebug ? (
          <pre className="text-[0.6rem] font-mono text-mca-dim leading-snug whitespace-pre-wrap break-all bg-amber-500/5 rounded px-2 py-1 border border-amber-500/20">
            <span className="text-amber-500/60">debug</span> {retractedDebug}
          </pre>
        ) : null}
      </div>
    );
  }

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
