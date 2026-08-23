import { execSync } from "node:child_process";

/**
 * Port scrub for the acceptance run (task 51): the restart-resume test
 * SIGTERMs the webServer-spawned backend and starts its own detached
 * replacement on the same port — the replacement outlives Playwright's
 * own webServer teardown (which only knows the dead original), so
 * without this scrub the next run fails to bind (reuseExistingServer:
 * false).
 */
export default async function globalTeardown(): Promise<void> {
  try {
    execSync("lsof -nP -ti tcp:8765 -sTCP:LISTEN | xargs kill 2>/dev/null || true", {
      shell: "/bin/sh",
      stdio: "ignore",
    });
  } catch {
    // already free
  }
}
