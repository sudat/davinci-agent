# Runbook — Control Plane Baseline (Phase-1 prerequisite)

Operator commands for the control-plane baseline gate. Run from
`video-pipeline/`.

## Verify the frozen gate policy and the six control-plane scenarios

Offline, fixture-backed:

```bash
uv run python - <<'PY'
from pathlib import Path
from services.gates import GatePolicy
policy = GatePolicy.model_validate_json(
    Path("config/gates/phase-1-control-plane-v1.json").read_bytes()
)
print(f"policy {policy.gate_id} v{policy.gate_version}: canonical load OK")
expected = {
    "cp-atomic-publish.json",
    "cp-crash-before-rename.json",
    "cp-lease-expiry.json",
    "cp-orphan-reconcile.json",
    "cp-path-symlink-denial.json",
    "cp-stale-cas.json",
}
present = {path.name for path in Path("tests/fixtures/manifests/control-plane").glob("*.json")}
assert expected == present, expected.symmetric_difference(present)
print("six control-plane fixtures: present")
PY
```

## Run the control-plane baseline gate

```text
"$UV_BIN" run python -m services.job_runner.run_gate control-plane-baseline \
  --policy config/gates/phase-1-control-plane-v1.json \
  --evidence "$EVIDENCE_DIR" \
  --toolchain-lock config/toolchains/phase-1-technical-v1.json
```

Check the registered gate surface:

```bash
uv run python -m services.cli run-gate --help
```
