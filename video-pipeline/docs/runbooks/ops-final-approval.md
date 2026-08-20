# Runbook — Ops: FINAL_APPROVED and the Global Review Report v1

The FINAL_APPROVED checkpoint binds one immutable release candidate to a
complete Global Review Report v1 set. Run from `video-pipeline/`. The set is
SEPARATE from F-lane reports: exactly six fixed files in `--report-dir`:

- `goal-constraints.json`
- `code-quality.json`
- `security.json`
- `hands-on-qa.json`
- `context-mining.json`
- `debugging.json`

Each file is `{schema_version:"global-review-v1", lane, full_sha, candidate_id,
verdict, raw_response_sha256, artifact_hashes, findings}`. All six verdicts
must be APPROVE, all must bind one identical `full_sha` (the declared work
state) and one identical `candidate_id`, the candidate must not be revoked,
and the canonical report-set sha256 is bound into the record together with
the operator's local TTY and uid. Missing/duplicate/extra reports, a REJECT,
mixed SHA/candidate, a stale SHA, a revoked candidate, or a non-TTY
invocation are all typed refusals.

## Full walkthrough on fixtures (scratch directories only)

```bash
FA_SCRATCH="$(mktemp -d)"
FA_FULL_SHA="$(git rev-parse HEAD)"
FA_CANDIDATE="cand-runbook-demo"
mkdir -p "$FA_SCRATCH/raw" "$FA_SCRATCH/reports"
for LANE in goal-constraints code-quality security hands-on-qa context-mining debugging; do
  printf 'lane: %s\nfull_sha: %s\ncandidate_id: %s\nverdict: PASS\nfinding: no blocking issues\nSTOP: audit closed; approval may proceed\n' \
    "$LANE" "$FA_FULL_SHA" "$FA_CANDIDATE" > "$FA_SCRATCH/raw/$LANE.md"
  uv run python -m services.cli convert-review \
    --raw "$FA_SCRATCH/raw/$LANE.md" --lane "$LANE" \
    --full-sha "$FA_FULL_SHA" --candidate-id "$FA_CANDIDATE" \
    --out "$FA_SCRATCH/reports/$LANE.json"
done
uv run python -m services.cli checkpoint record \
  --purpose FINAL_APPROVED \
  --work-id "work-runbook-demo" \
  --git-sha "$FA_FULL_SHA" \
  --candidate-id "$FA_CANDIDATE" \
  --report-dir "$FA_SCRATCH/reports" \
  --decision approve \
  --out "$FA_SCRATCH/op-record.jsonl" \
  --fixture
uv run python -c "import json,sys; line=json.loads(open(sys.argv[1]).read().splitlines()[0]); b=line['final_binding']; assert b['candidate_id']==sys.argv[2]; print('final binding:', b['report_set_sha256'])" \
  "$FA_SCRATCH/op-record.jsonl" "$FA_CANDIDATE"
```

The `--fixture` seam is for dry practice only: it produces a FIXTURE-MARKED
record that can never satisfy a real approval. A real FINAL_APPROVED record
is written from a local TTY displaying the hashes (automation is refused).

## A REJECT anywhere is a typed refusal

Self-contained: rebuild a set, flip one lane to FAIL, and prove the record
is refused (exit 1, no record written):

```bash
FA_T="$(mktemp -d)"
FA_T_SHA="$(git rev-parse HEAD)"
mkdir -p "$FA_T/raw" "$FA_T/reports"
for LANE in goal-constraints code-quality security hands-on-qa context-mining debugging; do
  printf 'lane: %s\nfull_sha: %s\ncandidate_id: cand-runbook-tamper\nverdict: PASS\nfinding: none\nSTOP: audit closed\n' \
    "$LANE" "$FA_T_SHA" > "$FA_T/raw/$LANE.md"
  uv run python -m services.cli convert-review \
    --raw "$FA_T/raw/$LANE.md" --lane "$LANE" \
    --full-sha "$FA_T_SHA" --candidate-id "cand-runbook-tamper" \
    --out "$FA_T/reports/$LANE.json"
done
printf 'lane: security\nfull_sha: %s\ncandidate_id: cand-runbook-tamper\nverdict: FAIL\nfinding: blocker found\nSTOP: fix before approval\n' \
  "$FA_T_SHA" > "$FA_T/raw/security-fail.md"
uv run python -m services.cli convert-review \
  --raw "$FA_T/raw/security-fail.md" --lane security \
  --full-sha "$FA_T_SHA" --candidate-id "cand-runbook-tamper" \
  --out "$FA_T/reports/security.json"
! uv run python -m services.cli checkpoint record \
  --purpose FINAL_APPROVED \
  --work-id "work-runbook-tamper" \
  --git-sha "$FA_T_SHA" \
  --candidate-id "cand-runbook-tamper" \
  --report-dir "$FA_T/reports" \
  --decision approve \
  --out "$FA_T/op-record.jsonl" \
  --fixture 2>&1 | grep -q "verdict-not-approved"
test ! -f "$FA_T/op-record.jsonl" && echo "refused: no record written"
```

## Real operator invocation (local TTY)

```text
"$UV_BIN" run python -m services.cli checkpoint record \
  --purpose FINAL_APPROVED \
  --work-id "$WORK_ID" \
  --git-sha "$FULL_SHA" \
  --candidate-id "$CANDIDATE_ID" \
  --report-dir "$REPORT_DIR" \
  --decision approve \
  --out "$OP_RECORD_OUT"
```
