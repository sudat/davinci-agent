# Runbook — Phase 0A: Resolve Capability Spike

Operator commands for the phase-0A capability spike. Run every command from
`video-pipeline/` (the pipeline root). The typed operator CLI never executes
arbitrary shell, path, network, or UI commands — every operation is a
registered name (`python -m services.cli --help`).

## Verify the frozen gate policy and the canonical fixture manifest

Offline, fixture-backed, no hidden setup:

```bash
uv run python - <<'PY'
import json
from pathlib import Path
from services.fixtures.manifest import Phase0AFixtureManifest
from services.gates import GatePolicy
policy = GatePolicy.model_validate_json(Path("config/gates/phase-0a-v1.json").read_bytes())
manifest = Phase0AFixtureManifest.model_validate_json(
    Path("tests/fixtures/manifests/phase-0a/p0a-cfr30-fixed.json").read_bytes()
)
print(f"policy {policy.gate_id} v{policy.gate_version}: canonical load OK")
print(f"fixture {manifest.fixture_id}: canonical load OK")
PY
```

## Run the phase-0A gate (live Resolve required)

The gate command itself needs the frozen toolchain lock and the live Resolve
bridge; it is executed by the job runner with the locked absolute binaries:

```text
"$UV_BIN" run python -m services.job_runner.run_gate phase-0a \
  --policy config/gates/phase-0a-v1.json \
  --evidence "$EVIDENCE_DIR" \
  --toolchain-lock config/toolchains/phase-0a-v1.json
```

Check the registered surface and gate flags before running:

```bash
uv run python -m services.cli run-gate --help
uv run python -m services.cli toolchain-verify --help
```
