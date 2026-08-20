# Runbook — Phase 1: Talking-head Editorial Vertical Slice

Operator commands for the phase-1 technical vertical slice. Run from
`video-pipeline/`.

## Verify the frozen gate policy and the five reference fixtures

Offline, fixture-backed:

```bash
uv run python - <<'PY'
from pathlib import Path
from services.gates import GatePolicy
policy = GatePolicy.model_validate_json(
    Path("config/gates/phase-1-technical-v1.json").read_bytes()
)
print(f"policy {policy.gate_id} v{policy.gate_version}: canonical load OK")
expected = {
    "p1-ref-01-clean-ja.json",
    "p1-ref-02-pauses-fillers.json",
    "p1-ref-03-multi-take-must-include.json",
    "p1-ref-04-linked-av-offset.json",
    "p1-ref-05-review-mix.json",
}
present = {
    path.name for path in Path("tests/fixtures/manifests/phase-1-technical").glob("*.json")
}
assert expected == present, expected.symmetric_difference(present)
print("five reference fixtures: present")
PY
```

## Ingest / normalize / run the editorial chain / review

The registered phase-1 operations:

```bash
uv run python -m services.cli ingest register-fixture --help
uv run python -m services.cli normalize run --help
uv run python -m services.cli phase1 --help
uv run python -m services.cli review --help
```

## H1 operator checkpoint (real episode, local TTY)

Show the displayed target hashes, review the preview in a normal player,
then record `EDITORIAL_APPROVED` from the TTY that displayed them:

```bash
uv run python -m services.cli checkpoint show --help
uv run python -m services.cli checkpoint record --help
```

## Run the phase-1 technical gate

```text
"$UV_BIN" run python -m services.job_runner.run_gate phase-1-technical \
  --policy config/gates/phase-1-technical-v1.json \
  --evidence "$EVIDENCE_DIR" \
  --toolchain-lock config/toolchains/phase-1-technical-v1.json
```
