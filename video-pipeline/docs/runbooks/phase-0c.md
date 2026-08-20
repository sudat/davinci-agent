# Runbook — Phase 0C: Playable Preview and Natural-language Review Spike

Operator commands for the phase-0C preview/review spike. Run from
`video-pipeline/`.

## Verify the frozen gate policy and the five review fixtures

Offline, fixture-backed:

```bash
uv run python - <<'PY'
from pathlib import Path
from services.gates import GatePolicy
policy = GatePolicy.model_validate_json(Path("config/gates/phase-0c-v1.json").read_bytes())
print(f"policy {policy.gate_id} v{policy.gate_version}: canonical load OK")
expected = {
    "p0c-remove-clear.json",
    "p0c-span-clear.json",
    "p0c-subtitle-clear.json",
    "p0c-ambiguous-two-targets.json",
    "p0c-locked-conflict.json",
}
present = {path.name for path in Path("tests/fixtures/manifests/phase-0c").glob("*.json")}
assert expected == present, expected.symmetric_difference(present)
print("five phase-0c fixtures: present")
PY
```

## Render the low-resolution editorial preview

The registered preview operation:

```bash
uv run python -m services.cli preview --help
```

## Run the phase-0C gate (live Resolve required)

```text
"$UV_BIN" run python -m services.job_runner.run_gate phase-0c \
  --policy config/gates/phase-0c-v1.json \
  --evidence "$EVIDENCE_DIR" \
  --toolchain-lock config/toolchains/phase-0c-v1.json
```
