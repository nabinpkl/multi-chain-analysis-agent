// Hex equivalents of the oklch cluster palette defined in docs/design.md.
// Sigma's color parser (WebGL) only understands hex and rgb(a); oklch strings
// silently fall back to black. CSS tokens in globals.css stay canonical for
// HTML surfaces; these mirror them for canvas rendering.

export const CLUSTER_COLORS = [
  "#f28573", // 0  hue 20   warm red
  "#d3a14d", // 1  hue 65   amber
  "#a4be59", // 2  hue 110  chartreuse
  "#4fc09b", // 3  hue 160  emerald
  "#4eafcc", // 4  hue 200  cyan
  "#7a95e0", // 5  hue 245  azure
  "#a07cdb", // 6  hue 290  violet
  "#db7fae", // 7  hue 330  magenta
] as const;

export const LONELY_COLOR = "#5b6070";

export function colorForComponent(component: number | null): string {
  if (component === null) return LONELY_COLOR;
  return CLUSTER_COLORS[component % CLUSTER_COLORS.length];
}
