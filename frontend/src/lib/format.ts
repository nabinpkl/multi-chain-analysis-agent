const COMPACT = new Intl.NumberFormat("en-US", {
  notation: "compact",
  maximumFractionDigits: 2,
});

const FULL = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });

export function formatSol(value: number): string {
  if (value < 1) return `${value.toFixed(3)}`;
  return COMPACT.format(value);
}

export function formatInt(value: number): string {
  return FULL.format(value);
}

export function truncateAddress(addr: string, head = 4, tail = 4): string {
  if (addr.length <= head + tail + 1) return addr;
  return `${addr.slice(0, head)}…${addr.slice(-tail)}`;
}

export function relativeTime(epochSecs: number, now: number = Date.now() / 1000): string {
  const diff = Math.max(0, Math.round(now - epochSecs));
  if (diff < 5) return "just now";
  if (diff < 60) return `${diff}s ago`;
  const mins = Math.floor(diff / 60);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  return `${hrs}h ago`;
}
