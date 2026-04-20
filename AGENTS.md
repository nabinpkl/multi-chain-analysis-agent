
# Must Do's
- Every backend feature change do docker compose up -d --build at last.

# Don'ts
- No God component if it makes sense to extract to a component do it
- No dead code — if something is removed, delete it entirely (files, imports, types, everything referencing it)
- No backward compatibility layers — this is iteration-based development; just change the code directly

# Writing rules (docs/LinkedInEngineeringPosts/ only)

These rules apply when drafting or editing post content inside `docs/EngineeringPosts/`.

- No em-dashes. They read as AI-written on sight. Use periods, commas, colons, or parens instead.
- No "X is not Y, it's Z" cadence unless it really earns it.
- Keep the numbers. They do the heavy lifting.
- First person, plain words, short paragraphs.
- Audience is peer engineers and technical hiring managers, not recruiters. Technical terms (O-notation, mmap, asymptotic) stay when they advance the story. Flex-for-flex's-sake (naming libraries just to sound senior) gets cut.
- The post is a log, not content marketing. Skip hook-bait openers. The reader arrived from a resume link, not a scroll.

# MultiChain Analysis Engine

# JustGetDomain.com — Build Context

## What It Is

Listen txs from multiple chain normalize it link each tx to the data then build a graph serve the graph.

## Infrastructure

**Oracle Free Tier VM:** 24GB RAM, 4 Ampere cores. Runs Rust binary + cloudflared. That's it.
- Next.JS Vercel deploy
**Security:** Zero open inbound ports. Cloudflare absorbs DDoS. Connection hygiene in Rust service.

## `frontend/` stack

- **Framework:** Next.js 16.2.3, App Router, TypeScript, `src/` directory, `@/*` alias
- **Package manager:** pnpm
- **Styling:** Tailwind CSS v4 (CSS-first via `@tailwindcss/postcss`)
- **UI components:** shadcn/ui — all components installed in `src/components/ui/`
- **State:** Zustand v5 (client), TanStack Query v5 (server)
- **Animation:** motion v12 (`motion/react`)
- Color: Every color should be oklch no # based colors (if needed convert first)

## Backend Rust Service Architecture

### Decision: Axum
Axum 0.8.x is the 2026 default. Built on Tokio, via Tower middleware for connection hygiene. Same runtime for batch (tokio::io file streaming) and serving. Single binary, single process.

### Core Crate Stack
#### All latest
tokio
axum
tower
serde
serde_json
rustc-hash         # FxHashSet — faster than std HashMap