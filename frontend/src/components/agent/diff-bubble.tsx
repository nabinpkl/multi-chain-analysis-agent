"use client";

import type { ChangedSince } from "@/lib/generated/ChangedSince";
import type { FieldDelta } from "@/lib/generated/FieldDelta";
import type { NoMovement } from "@/lib/generated/NoMovement";

/**
 * Ship 4 diff bubble (`dont_repeat_yourself` switch). Renders when
 * the agent recognized a repeat question, re-fetched the prior
 * turn's primitives, and either produced no change or a typed
 * delta. Mutually exclusive with the regular claim card + narrative
 * bubble for that turn.
 *
 * Two variants:
 *  - no-movement: "we covered this in turn N, no movement since"
 *  - changed-since: small narrative + chip list of changed fields
 *
 * Both render a "scroll to turn N" anchor that jumps to the prior
 * turn's DOM node (id=`turn-anchor-N`). Helps the user verify what
 * was originally answered without scrolling manually.
 */
export function DiffBubble({
  diffReply,
  onScrollToTurn,
}: {
  diffReply:
    | { kind: "no-movement"; payload: NoMovement }
    | { kind: "changed-since"; payload: ChangedSince };
  onScrollToTurn: (turn: number) => void;
}) {
  if (diffReply.kind === "no-movement") {
    return (
      <NoMovementBubble payload={diffReply.payload} onScrollToTurn={onScrollToTurn} />
    );
  }
  return (
    <ChangedSinceBubble payload={diffReply.payload} onScrollToTurn={onScrollToTurn} />
  );
}

function NoMovementBubble({
  payload,
  onScrollToTurn,
}: {
  payload: NoMovement;
  onScrollToTurn: (turn: number) => void;
}) {
  const list = payload.primitives_replayed.length
    ? payload.primitives_replayed.join(", ")
    : "no primitives replayed";
  return (
    <div className="border border-mca-border rounded bg-mca-surface px-3 py-2 space-y-1.5">
      <div className="flex items-center justify-between gap-2">
        <span className="text-[0.55rem] uppercase tracking-[1.5px] text-mca-muted">
          no movement
        </span>
        <ScrollToTurnChip turn={payload.prior_turn} onClick={onScrollToTurn} />
      </div>
      <p className="text-[0.75rem] text-mca-text leading-snug">
        Re-checked {list}. Nothing has shifted since turn{" "}
        <span className="tabular-nums">{payload.prior_turn + 1}</span>.
      </p>
    </div>
  );
}

function ChangedSinceBubble({
  payload,
  onScrollToTurn,
}: {
  payload: ChangedSince;
  onScrollToTurn: (turn: number) => void;
}) {
  return (
    <div className="border border-mca-border rounded bg-mca-surface px-3 py-2 space-y-2">
      <div className="flex items-center justify-between gap-2">
        <span className="text-[0.55rem] uppercase tracking-[1.5px] text-mca-muted">
          changed since turn{" "}
          <span className="tabular-nums">{payload.prior_turn + 1}</span>
        </span>
        <ScrollToTurnChip turn={payload.prior_turn} onClick={onScrollToTurn} />
      </div>
      <p className="text-[0.75rem] text-mca-text leading-snug whitespace-pre-line">
        {payload.prose}
      </p>
      {payload.delta.changed.length > 0 ? (
        <DeltaChipList changed={payload.delta.changed} />
      ) : null}
      <p className="text-[0.55rem] uppercase tracking-[1px] text-mca-dim">
        {payload.delta.changed.length} changed ·{" "}
        {payload.delta.unchanged_field_count} unchanged
      </p>
    </div>
  );
}

function DeltaChipList({ changed }: { changed: FieldDelta[] }) {
  return (
    <ul className="flex flex-wrap gap-1.5">
      {changed.map((d, i) => (
        <li
          key={`${d.primitive}.${d.field_path}.${i}`}
          className="text-[0.6rem] tabular-nums border border-mca-border rounded px-1.5 py-0.5 text-mca-text bg-mca-bg"
          title={`${d.primitive}.${d.field_path}`}
        >
          <span className="text-mca-dim">{d.field_path}</span>{" "}
          <DeltaChipValue change={d.change} />
        </li>
      ))}
    </ul>
  );
}

function DeltaChipValue({ change }: { change: FieldDelta["change"] }) {
  switch (change.kind) {
    case "number_moved": {
      const pctSign = change.pct >= 0 ? "+" : "";
      return (
        <span className="text-mca-accent">
          {fmtNum(change.prior)} → {fmtNum(change.current)}{" "}
          <span className="text-mca-dim">
            ({pctSign}
            {(change.pct * 100).toFixed(1)}%)
          </span>
        </span>
      );
    }
    case "count_changed":
      return (
        <span className="text-mca-accent">
          {fmtNum(change.prior)} → {fmtNum(change.current)}
        </span>
      );
    case "set_changed":
      return (
        <span className="text-mca-accent">
          +{change.added.length} / −{change.removed.length}
        </span>
      );
  }
}

function ScrollToTurnChip({
  turn,
  onClick,
}: {
  turn: number;
  onClick: (turn: number) => void;
}) {
  return (
    <button
      onClick={() => onClick(turn)}
      className="text-[0.55rem] uppercase tracking-[1px] text-mca-accent border border-mca-border rounded px-1.5 py-0.5 hover:bg-mca-bg transition-colors"
      title={`scroll to turn ${turn + 1}`}
    >
      ↑ turn {turn + 1}
    </button>
  );
}

function fmtNum(v: number): string {
  if (Number.isInteger(v)) return v.toString();
  if (Math.abs(v) >= 1000) return v.toFixed(0);
  if (Math.abs(v) >= 1) return v.toFixed(2);
  return v.toFixed(4);
}
