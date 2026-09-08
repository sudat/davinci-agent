import { defineConfig } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

// 2P isolation: keep 8765 default, allow a dedicated port when 8765 is held
// by an unrelated process (e2e teardown kills listeners on this port).
const backendPort = Number(process.env.COCKPIT_E2E_PORT ?? 8765);
const frontendPort = 3100;
// Frontend must be addressed via localhost (not 127.0.0.1): Next 16's dev
// allowed-origins guard 403s _next/static + HMR for 127.0.0.1, which kills
// hydration. The backend is loopback-bound 127.0.0.1 (task 44) and unaffected.
const backendBase = `http://127.0.0.1:${backendPort}`;
const frontendBase = `http://localhost:${frontendPort}`;
const stateRoot = path.resolve(__dirname, ".e2e", "state");

// Live (non-seeded) real-chain lane (task 10): only when LIVE_V44_E2E=1,
// the backend additionally boots with EDITORIAL_RUNTIME_CONFIG pinned to an
// explicit heuristic_diagnostic runtime json, so the detached runner
// subprocesses (which inherit the backend env) run the chain in diagnostic
// editorial mode with zero credentials. Default runs get NO env change.
const liveLane = process.env.LIVE_V44_E2E === "1";
const editorialRuntimePath = path.join(stateRoot, "editorial-runtime-heuristic.json");
if (liveLane) {
  fs.mkdirSync(stateRoot, { recursive: true });
  fs.writeFileSync(
    editorialRuntimePath,
    `${JSON.stringify({ mode: "heuristic_diagnostic" })}\n`,
  );
}

export default defineConfig({
  testDir: "tests/e2e",
  timeout: 30_000,
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"]],
  globalTeardown: path.resolve(__dirname, "tests", "e2e", "teardown-backend-port.ts"),
  use: { baseURL: frontendBase },
  webServer: [
    {
      // Real cockpit backend (task 44) on a throwaway state root, loopback only.
      // Health probe: GET /episodes answers 200 with {episodes: []} once up.
      command:
        `uv run python -m services.cli cockpit --port ${backendPort}` +
        ` --episodes-root "${path.join(stateRoot, "episodes")}"` +
        ` --state-store "${path.join(stateRoot, "state.db")}"`,
      cwd: path.resolve(__dirname, "..", "video-pipeline"),
      url: `${backendBase}/episodes`,
      timeout: 120_000,
      reuseExistingServer: false,
      stdout: "ignore",
      ...(liveLane
        ? { env: { ...process.env, EDITORIAL_RUNTIME_CONFIG: editorialRuntimePath } }
        : {}),
    },
    {
      command: `bun dev --port ${frontendPort}`,
      cwd: __dirname,
      url: frontendBase,
      timeout: 120_000,
      reuseExistingServer: false,
      stdout: "ignore",
      // COCKPIT_API feeds the next.config rewrite target (server-side).
      // Do NOT set NEXT_PUBLIC_COCKPIT_API here: a direct browser base would
      // be cross-origin (localhost:3100 → 127.0.0.1:8765) and CORS-blocked.
      env: {
        ...process.env,
        COCKPIT_API: backendBase,
      },
    },
  ],
});
