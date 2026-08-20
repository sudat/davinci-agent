# Runbook — Phase 2: Resolve Finalization Vertical Slice

Operator commands for the phase-2 finalization slice. Run from
`video-pipeline/`.

## Verify the frozen gate policy and the five fault fixtures

Offline, fixture-backed:

```bash
uv run python - <<'PY'
from pathlib import Path
from services.gates import GatePolicy
policy = GatePolicy.model_validate_json(Path("config/gates/phase-2-v1.json").read_bytes())
print(f"policy {policy.gate_id} v{policy.gate_version}: canonical load OK")
expected = {
    "p2-stale-capability.json",
    "p2-partial-build-restart.json",
    "p2-same-duration-wrong-media.json",
    "p2-false-render-complete.json",
    "p2-blocking-qc-privacy.json",
}
present = {path.name for path in Path("tests/fixtures/manifests/phase-2").glob("*.json")}
assert expected == present, expected.symmetric_difference(present)
print("five phase-2 fault fixtures: present")
PY
```

## QC the final render

The registered QC operations:

```bash
uv run python -m services.cli qc build-policy --help
uv run python -m services.cli qc run --help
```

## Run the phase-2 gate (live Resolve required)

```text
"$UV_BIN" run python -m services.job_runner.run_gate phase-2 \
  --policy config/gates/phase-2-v1.json \
  --evidence "$EVIDENCE_DIR" \
  --toolchain-lock config/toolchains/phase-2-v1.json
```
