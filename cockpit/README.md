# cockpit (Episode Cockpit UI)

Next.js (App Router / TypeScript / Bun) operator shell over the task-44
loopback FastAPI. See `DESIGN.md` for the design tokens used by all views.

## Run the pair locally

```bash
# 1) backend (loopback only, task 44) — from ../video-pipeline
uv run python -m services.cli cockpit --port 8765 \
  --episodes-root jobs --state-store jobs/state.db

# 2) frontend — from ./cockpit
COCKPIT_API=http://127.0.0.1:8765 bun dev --port 3100
```

The browser calls the backend through the same-origin proxy
`/cockpit-api/*` (see `next.config.ts` rewrites; target from `COCKPIT_API`,
default `http://127.0.0.1:8765`). Do NOT point `NEXT_PUBLIC_COCKPIT_API`
(a direct browser base) at the backend from a `localhost` page — that is
cross-origin and CORS-blocked (the backend has no CORS middleware).

## Quality gates

```bash
bun run build        # production build
bun run typecheck    # tsc --noEmit
bun run test         # vitest unit/component tests
bunx playwright test tests/e2e/intake.spec.ts   # e2e (boots BOTH servers)
```

E2E keeps throwaway state under `cockpit/.e2e/` and unique source folders
per run (episode ids are path-derived hashes, so reruns never 409).
