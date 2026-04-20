# MultiChain Analysis Engine — Design System

This is the source of truth for visual decisions. All tokens live in `frontend/src/app/globals.css`. This document explains the reasoning so future changes stay coherent.

## Principles

1. **Data is the subject.** Every visual element must either show data, scaffold data, or explain data. No decoration.
2. **Dark theme first.** Graph visualization at scale reads better on dark. Bubblemaps, Arkham, XRay, Reactor all default dark for the same reason: bright nodes on a dark canvas give the most contrast per pixel.
3. **Color encodes structure, not mood.** A hue means "this node belongs to cluster N", not "this is exciting". If a color doesn't carry information, it doesn't belong.
4. **Typography does the hierarchy.** Three sizes. One sans for prose, one mono for addresses and numbers that want exactness. No more.
5. **Motion signals life.** Fade-in when nodes appear. Subtle pulse on live counters. No decorative animation.
6. **Every color is oklch.** Perceptual uniformity means cluster colors look equally spaced. No hex, no hsl.

## Theme

Dark by default. No light mode at launch. The viz page is always dark; a light mode for the marketing home page can come later if wanted.

Rationale: the graph is the product. Every pixel around it must be neutral enough that nothing competes. A dark canvas with bright nodes gives WebGL the contrast it needs to feel luminous.

## Core palette

Cool-tilted neutrals (slight blue bias at hue 260) for the shell. Pure grey reads sterile against the saturated cluster colors.

| Token | oklch | Role |
|---|---|---|
| `--mca-bg` | `0.12 0.01 260` | Page canvas, deepest surface |
| `--mca-surface` | `0.16 0.01 260` | Cards, sidebars, popovers |
| `--mca-surface-raised` | `0.20 0.01 260` | Hover states on surfaces |
| `--mca-border` | `0.24 0.01 260` | Dividers, card outlines |
| `--mca-text` | `0.95 0.01 260` | Primary text |
| `--mca-dim` | `0.70 0.01 260` | Secondary text, labels |
| `--mca-muted` | `0.50 0.01 260` | Captions, disabled |
| `--mca-accent` | `0.75 0.18 142` | Primary CTA, active state (green, preserved from previous work) |
| `--mca-accent-dim` | `0.75 0.18 142 / 0.15` | Accent background tint |

## Cluster palette

Eight hues spaced at ~45° intervals around the color wheel. Chroma held at 0.18 and lightness at 0.72 so no cluster pops louder than another. Perceptually balanced.

| Token | Hue | Approx name |
|---|---|---|
| `--mca-cluster-0` | 20 | warm red |
| `--mca-cluster-1` | 65 | amber |
| `--mca-cluster-2` | 110 | chartreuse |
| `--mca-cluster-3` | 160 | emerald |
| `--mca-cluster-4` | 200 | cyan |
| `--mca-cluster-5` | 245 | azure |
| `--mca-cluster-6` | 290 | violet |
| `--mca-cluster-7` | 330 | magenta |
| `--mca-lonely` | neutral, `0.42 0.01 260` | lonely whales (no cluster) |

**Not Bubblemaps.** Their palette sits at higher chroma (~0.25) with more saturation, which reads as playful. Ours holds at 0.18 so the viz reads editorial: serious, research-leaning, closer to a Financial Times infographic in vibe. Same information density, different emotional register.

**Mapping strategy.** Connected-component ids from the backend are assigned colors in order: component 0 gets cluster-0, component 1 gets cluster-1, and so on. When more than 8 components exist, we cycle. Repetition is acceptable because the graph is read locally (user hovers to confirm), not by counting distinct colors globally.

## Edge color

Edges use a neutral low-contrast tone, `oklch(0.45 0.02 260 / 0.35)`, so nodes remain the visual subject. When a node is hovered, its incident edges lift to `oklch(0.75 0.18 142 / 0.90)` (accent green) for emphasis.

## Typography

Two families, three sizes, one weight scale.

| Role | Family | Size / Line | Weight |
|---|---|---|---|
| Headline | sans | 24 / 32 | 600 |
| Body | sans | 14 / 20 | 400 |
| Label | sans | 12 / 16 | 500 (uppercase, tracked) |
| Caption | sans | 11 / 14 | 400 |
| Mono | mono | 13 / 18 | 400 |

Sans: Inter (or system-ui fallback). Mono: Geist Mono (or ui-monospace fallback). Wallet addresses and numeric readouts are always mono so digits align in columns.

## Layout

Fixed header, flex main, fixed sidebar. No drawers, no collapsibles at launch.

```
┌─ header (56px) ─────────────────────────────────────┐
│ logo │ window selector │ ? help │ theme │ github    │
├─────────────────────────────────────────────────────┤
│                                       │ sidebar     │
│              graph canvas             │   320px     │
│              (flex-1)                 │             │
│                                       │   stats     │
│                                       │   top       │
│                                       │   legend    │
└─────────────────────────────────────────────────────┘
│ footer (32px) "updated 3s ago · 1018 nodes · 500 edges"
└─────────────────────────────────────────────────────┘
```

## Motion

| Event | Effect | Duration |
|---|---|---|
| Node appears | fade + scale from 0.8 | 600ms, ease-out |
| Node removed | fade + shrink | 400ms, ease-in |
| Counter tick | scale 1.02 → 1.00 | 200ms, ease-out |
| Hover on node | halo expand | 150ms, linear |
| Loading state | skeleton pulse | 2s infinite |

`prefers-reduced-motion` disables all of the above. Tokens live in the animation block of `globals.css`.

## Component inventory

Shadcn primitives already installed. For v0 we need:

- `Card`, `Separator` — for sidebar sections
- `Select` — for window selector
- `Tooltip` — for node hover details
- `Button` — for header actions
- `Skeleton` — for loading states

Custom components (new, in `components/flow/`):

- `<GraphCanvas>` — Cosmograph wrapper, reads query data
- `<StatsPanel>` — three numbers: volume, txs, wallets
- `<TopWalletCard>` — highlighted wallet + volume
- `<Legend>` — color scale, size scale
- `<WindowSelect>` — header dropdown
- `<NodeTooltip>` — hover card

## What this is not

Not a brand identity. Not icon guidelines. Not marketing typography. Those don't exist yet and shouldn't until there's actual content that needs them.
