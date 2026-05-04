"use client";

import type {
  ChangedSince,
  FieldDelta,
  NoMovement,
} from "@/lib/wire/multichain/wire/agent/v1/diff_pb";

/**
 * Ship 4 diff bubble (`dontRepeatYourself` switch). Renders when
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
 * turn's DOM node (id=`turn-anchor-N`).
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
  // The `primitivesReplayed` array is the backend's list of internal
  // primitive function names (`wallet_profile`, `community_summary`).
  // The user has no model for those names, and the fact that we
  // re-fetched is what matters; the specific endpoint isn't. Render
  // a generic phrase instead of leaking impl. Builder-view audit
  // surface for the same data lives in the trace.
  return (
    <div className="border border-mca-border rounded bg-mca-surface px-3 py-2 space-y-1.5">
      <div className="flex items-center justify-between gap-2">
        <span className="text-[0.55rem] uppercase tracking-[1.5px] text-mca-muted">
          no movement
        </span>
        <ScrollToTurnChip turn={payload.priorTurn} onClick={onScrollToTurn} />
      </div>
      <p className="text-[0.75rem] text-mca-text leading-snug">
        Re-checked the data. Nothing has shifted since turn{" "}
        <span className="tabular-nums">{payload.priorTurn + 1}</span>.
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
  const changedCount = payload.delta?.changed.length ?? 0;
  const unchangedCount = payload.delta?.unchangedFieldCount ?? 0;
  return (
    <div className="border border-mca-border rounded bg-mca-surface px-3 py-2 space-y-2">
      <div className="flex items-center justify-between gap-2">
        <span className="text-[0.55rem] uppercase tracking-[1.5px] text-mca-muted">
          changed since turn{" "}
          <span className="tabular-nums">{payload.priorTurn + 1}</span>
        </span>
        <ScrollToTurnChip turn={payload.priorTurn} onClick={onScrollToTurn} />
      </div>
      <p className="text-[0.75rem] text-mca-text leading-snug whitespace-pre-line">
        {payload.prose}
      </p>
      {changedCount > 0 ? (
        <DeltaChipList changed={payload.delta?.changed ?? []} />
      ) : null}
      <p className="text-[0.55rem] uppercase tracking-[1px] text-mca-dim">
        {changedCount} changed · {unchangedCount} unchanged
      </p>
    </div>
  );
}

function DeltaChipList({ changed }: { changed: FieldDelta[] }) {
  return (
    <ul className="flex flex-wrap gap-1.5">
      {changed.map((d, i) => (
        <li
          key={`${d.primitive}.${d.fieldPath}.${i}`}
          className="text-[0.6rem] tabular-nums border border-mca-border rounded px-1.5 py-0.5 text-mca-text bg-mca-bg"
          title={`${d.primitive}.${d.fieldPath}`}
        >
          <span className="text-mca-dim">{d.fieldPath}</span>{" "}
          <DeltaChipValue change={d.change} />
        </li>
      ))}
    </ul>
  );
}

function DeltaChipValue({ change }: { change: FieldDelta["change"] }) {
  const inner = change?.change;
  switch (inner?.case) {
    case "numberMoved": {
      const v = inner.value;
      const pctSign = v.pct >= 0 ? "+" : "";
      return (
        <span className="text-mca-accent">
          {fmtNum(v.prior)} → {fmtNum(v.current)}{" "}
          <span className="text-mca-dim">
            ({pctSign}
            {(v.pct * 100).toFixed(1)}%)
          </span>
        </span>
      );
    }
    case "countChanged": {
      const v = inner.value;
      return (
        <span className="text-mca-accent">
          {fmtNum(v.prior)} → {fmtNum(v.current)}
        </span>
      );
    }
    case "setChanged": {
      const v = inner.value;
      return (
        <span className="text-mca-accent">
          +{v.added.length} / −{v.removed.length}
        </span>
      );
    }
    default:
      return null;
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
