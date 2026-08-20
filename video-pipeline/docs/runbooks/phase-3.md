# Runbook — Phase 3: Presentation Layer

Operator commands for the phase-3 presentation layer. Run from
`video-pipeline/`.

## Verify the frozen gate policy and the A/B brand fixtures

Offline, fixture-backed:

```bash
uv run python - <<'PY'
from pathlib import Path
from services.gates import GatePolicy
policy = GatePolicy.model_validate_json(Path("config/gates/phase-3-v1.json").read_bytes())
print(f"policy {policy.gate_id} v{policy.gate_version}: canonical load OK")
manifests = Path("tests/fixtures/manifests/phase-3")
for brand in ("p3-brand-a.json", "p3-brand-b.json"):
    assert (manifests / brand).is_file(), brand
    assets = manifests / "assets" / brand.removesuffix(".json")
    assert assets.is_dir(), assets
print("A/B brand fixtures: present")
PY
```

## Scope guard before any presentation change

The registered scope operation (never trusts product-side checks alone):

```bash
uv run python -m services.cli check-scope --help
```

## Run the phase-3 gate (live Resolve required)

```text
"$UV_BIN" run python -m services.job_runner.run_gate phase-3 \
  --policy config/gates/phase-3-v1.json \
  --evidence "$EVIDENCE_DIR" \
  --toolchain-lock config/toolchains/phase-3-v1.json
```
