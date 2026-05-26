<!-- BEGIN:nextjs-agent-rules -->
# This is NOT the Next.js you know

This version has breaking changes — APIs, conventions, and file structure may all differ from your training data. Read the relevant guide in `node_modules/next/dist/docs/` before writing any code. Heed deprecation notices.
<!-- END:nextjs-agent-rules -->

# `frontend/` stack and conventions

These are the picks. Root [../AGENTS.md](../AGENTS.md) carries the cross-service rules; this file is what an agent working in `frontend/` needs in front of them.

- **Framework:** Next.js 16+, App Router, TypeScript, `src/` directory, `@/*` alias.
- **Package manager:** pnpm. No `npm` or `yarn` mixed in.
- **Styling:** Tailwind CSS v4, CSS-first via `@tailwindcss/postcss`.
- **UI components:** shadcn/ui. All installed components live under `src/components/ui/`.
- **State:** Zustand v5 (client), TanStack Query v5 (server).
- **Animation:** motion v12, imported from `motion/react`.
- **Color:** All colors `oklch`, no `#` hex. Convert if you need to.
- **Wire types:** import from `src/lib/wire/` (generated from `proto/` by `just regen-wire-types`). Never hand-type something that crosses the agent-service boundary.

## What goes elsewhere

- Cross-service stack + versions: [../README.md  Stack](../README.md#stack).
- System topology, ports, hop-by-hop wire format: [../SPEC.md](../SPEC.md).
- Visual design tokens + reasoning: [../docs/design.md](../docs/design.md).
