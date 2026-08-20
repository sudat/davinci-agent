# Runbook — Phase 0B: Coordinate and Conform Spike

Operator commands for the phase-0B coordinate/conform spike. Run from
`video-pipeline/`.

## Verify the frozen gate policy and all six fixture manifests

Offline, fixture-backed:

```bash
uv run python - <<'PY'
from pathlib import Path
from services.gates import GatePolicy
policy = GatePolicy.model_validate_json(Path("config/gates/phase-0b-v1.json").read_bytes())
print(f"policy {policy.gate_id} v{policy.gate_version}: canonical load OK")
for path in sorted(Path("tests/fixtures/manifests/phase-0b").glob("*.json")):
    assert path.stat().st_size > 0
    print(f"fixture {path.name}: present")
PY
```

## Conform one source and emit the conform map

The registered conform operations (typed subcommands of the operator CLI):

```bash
uv run python -m services.cli conform --help
uv run python -m services.cli conform-map --help
```

## Run the phase-0B gate (live Resolve required)

```text
"$UV_BIN" run python -m services.job_runner.run_gate phase-0b \
  --policy config/gates/phase-0b-v1.json \
  --evidence "$EVIDENCE_DIR" \
  --toolchain-lock config/toolchains/phase-0b-v1.json
```
