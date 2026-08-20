# Runbook — Ops: Retention and Garbage Collection

Operator commands for retention GC. Run from `video-pipeline/`. The GC is
plan-first and defaults to dry-run; `--execute` re-hashes every target
immediately before unlink. Never point it outside a jobs root you own.

## Dry-run the default policy over a scratch jobs root

Offline, fixture-backed (the scratch tree is created inline under a temp
directory; nothing real is touched):

```bash
RET_SCRATCH="$(mktemp -d)"
uv run python - <<PY
from pathlib import Path
from services.retention.models import JobRetentionState, RegisteredPath, RetentionRegistry
root = Path("$RET_SCRATCH")
job = root / "ep-demo"
(job / "proxies").mkdir(parents=True)
(job / "proxies/p1.mp4").write_bytes(b"proxy-bytes")
(job / "job-state.json").write_bytes(
    JobRetentionState.model_validate({
        "schema_version": "retention-job-state-v1",
        "job_id": "ep-demo",
        "status": "FROZEN",
        "frozen_at_epoch_s": 0,
    }).model_dump_json().encode()
)
(job / "retention-registry.json").write_bytes(
    RetentionRegistry.mint({
        "proxies/p1.mp4": RegisteredPath(sha256="a" * 64),
    }).model_dump_json().encode()
)
print("scratch job tree ready")
PY
uv run python -m services.retention.gc --jobs-root "$RET_SCRATCH" --policy config/retention/default-v1.json --dry-run
```

## Apply a plan for real (destructive; scratch root shown)

```text
"$UV_BIN" run python -m services.retention.gc \
  --jobs-root "$JOBS_ROOT" --policy config/retention/default-v1.json --execute
```

Registered surface and audit trail:

```bash
uv run python -m services.cli retention-gc --help
```
