# Runbook — Ops: Metrics Report

Operator commands for the Active Human Time / quality / trace metrics
report. Run from `video-pipeline/`. Reports are derived from event bundles
only; missing data is reported as `not_evaluated`, never as zero.

## Derive a report from a synthetic technical bundle

Offline, fixture-backed (the bundle is assembled from the repo's synthetic
bundle builder under a temp directory):

```bash
MET_SCRATCH="$(mktemp -d)"
uv run python - <<PY
from pathlib import Path
from tests.metrics.support import BundleBuilder
builder = BundleBuilder(Path("$MET_SCRATCH/bundle"))
builder.episode("ep-tech-1", kind="technical")
builder.episode("ep-syn-1", kind="synthetic")
builder.review("ep-tech-1", decision_id="dec-1", base="v1", result="v2")
builder.stage("ep-tech-1", "build", "attempt-failed", attempt=1)
builder.stage("ep-tech-1", "build", "attempt-succeeded", attempt=2)
builder.qc_outcome("ep-tech-1", verdict="passed")
builder.qc_outcome("ep-syn-1", verdict="blocked", blockers=1)
builder.artifact("ep-tech-1", "art-1", "authoritative", 1000)
builder.artifact("ep-syn-1", "art-2", "rebuildable", 500)
builder.trace_source("ep-tech-1", "src-1")
builder.trace_decision("ep-tech-1", "dec-1", ("src-1",))
builder.trace_item("ep-tech-1", "item-1", "dec-1")
print(builder.write())
PY
uv run python -m services.metrics.report --events "$MET_SCRATCH/bundle" --out "$MET_SCRATCH/report.json"
uv run python -c "import json,sys; r=json.load(open(sys.argv[1])); assert r['validation']['valid'] is True; print('metrics report valid:', r['schema_version'])" "$MET_SCRATCH/report.json"
```

## Reports over real episodes

```text
"$UV_BIN" run python -m services.metrics.report \
  --events "$EVENTS_BUNDLE" --out "$REPORT_OUT"
```

Registered surface:

```bash
uv run python -m services.cli metrics-report --help
```
