# v44-real-01 arm-B-r3 failure classification (2026-08-31)

Sanitized, committed evidence for the round-3 measured diagnosis (work plan
`v44-input-integrity-cut-policy`, Todo 8). Private transcripts, media, notes,
and workspace paths are intentionally absent: every error is identified by
ground-truth anchor ID and its integer half-open span only, in the ground-truth
anchor frame space (original 30000/1001 timeline; spans are `[start, end)`).

## Input evidence (immutable, cited by hash)

| Artifact | sha256 |
|---|---|
| Ground truth v1 (75 must_keep / 2 must_remove) | `c87a4f6298d0dacbc5762f5be440586084305110c60ba8fd2edbd00674aab7b6` |
| Ground truth v2 (77 must_keep / 0 must_remove, operator decision 2026-08-30) | `9af2802d0d1393f431fba38ab093ab22fedf707e740cb17dbfa5563aa6cec4b5` |
| arm-A-r3 report | `a35ccc17174aad6a8d91dfd1a79b06da3a2a25ebbfbe34e103b1cc1971391692` |
| arm-B-r3 report | `af1a8df8d9e8277bca85344310e0c405f7a7b29c1c0630b7bb8a21cdb1bfc859` |
| arm-A-r3 committed selection proposal (v2) | `94b077ad3886cf50482d78c8b3d3bb38d216b414713ac5499b8675b10678c366` |
| arm-B-r3 committed selection proposal (v2) | `ea63508fe8ae3534cc9426c1609268a085a2a27c74a99a6e915b0f16846ca98e` |

All counts below recompute from those bytes with the production metric seam
(`services/metrics/v44_product_proof.py`), using the r3-era kept-span
derivation (`intent == "keep"` only) and the mezzanine-to-anchor conversion
`round(frame * 1000 / 1001)` (`services/cli/v44_arm_evidence.py`).

## B-r3 measured failure surface

- must_keep recall 68/75 (0.90666...), catastrophic removals 7, escalations 0
- must_remove retention 2/2 (1.0) against GT v1
- evidence quality: transcript CER 0.10372340425531915, timestamp error p95
  5740.0 ms, 31 omitted utterances, 37 duplicated utterances, proper-noun
  recall 1.0
- both r3 arms discovered 95 of 99 speech candidates (missing segment IDs
  s96, s97, s98, s99 — the episode tail)

## The nine errors (each listed exactly once)

### Class 1 — perception / input-path (5)

Anchors the r3 system could not keep because the editorial decision layer
never received a faithful, complete candidate map. Both r3 arms exhibit
exactly these five must_keep misses (arm-A-r3 catastrophic count is the same
5), which is the shared-input signature that separates this class from
arm-B-only reasoning errors.

| # | Anchor | Half-open span (anchor frames) | Mechanism (measured) | Correction seam, proven by tests |
|---|---|---|---|---|
| 1 | a041 | [3854, 3927) | Covering candidate delivered with intent `optional`; r3 kept-span derivation counted only `keep`, silently dropping optional spans | kept-span derivation now `intent in {keep, optional}` (`services/cli/_v44_arm_results.py`); `tests/cli/test_v44_arm_pipeline.py::test_arm_a_preserves_optional_candidates_in_kept_spans` |
| 2 | a049 | [5108, 5168) | Same optional-drop mechanism on its covering candidate | same seam and test |
| 3 | a075 | [8165, 8285) | Tail utterances s96–s99 were never discovered: shot discovery queried `[0, covered_frames)` where `covered_frames` summed segment lengths (8005) instead of the authoritative extent (8467), so no candidate existed for the anchor | authoritative `source_total_frames` extent plus exact expected/indexed/proposed ID-set integrity refusal `candidate-integrity-failed` (`services/cli/_v44_arm_integrity.py`, `services/editorial_v2/director_v2.py`); `tests/cli/test_v44_arm_pipeline.py::test_sparse_tail_candidates_reach_director_and_commit`, `::test_equal_count_id_substitution_refuses_before_paid_calls` |
| 4 | a076 | [8285, 8375) | Same undiscovered-tail mechanism | same seams and tests |
| 5 | a077 | [8375, 8459) | Same undiscovered-tail mechanism | same seams and tests |

Related but distinct input-path defects fixed by the same correction wave and
exercised by the same tests (no separate GT anchor): expected/actual candidate
ID-set equality was not verified pre-model, and subtitle misalignment was
recorded only after the paid calls. The r3 evidence-quality row above
(CER/p95/omitted/duplicated) is the measured perception floor that the r4
system-ASR readiness probe re-measured and enforced fail-closed.

### Class 2 — reasoning / ineligible cut (2)

Must_keep anchors lost only in arm B: the model demoted their covering
candidates from keep on free-text rationale (dependency/redundancy prose).
Arm A kept the same candidates, so the input was adequate at these spans; the
loss is an editorial cut judgment without deterministic evidence. Under the
r3 kept-span derivation an `optional` demotion equals a cut.

| # | Anchor | Half-open span (anchor frames) | Mechanism (measured) | Correction seam, proven by tests |
|---|---|---|---|---|
| 6 | a016 | [1624, 1714) | Covering candidate demoted to optional in arm-B-r3 only (keep in arm-A-r3) on model free-text rationale; no deterministic false-start or exact-duplicate evidence existed | fail-closed cut policy: default KEEP, removal requires precomputed deterministic eligibility (`false_start` / `exact_duplicate`) with cited evidence (`services/editorial_v2/removal_policy.py`, `director_gates.py`, Pass B contract); `tests/editorial_v2/test_proposal_validate.py`, `tests/cli/test_v44_arm_pipeline.py::test_arm_eligible_removals_commit_and_optional_is_preserved`, `::test_compute_removal_eligibility_refuses_near_match_and_nonadjacent`, `::test_compute_removal_eligibility_ignores_injection_prose` |
| 7 | a043 | [4359, 4419) | Same arm-B-only demotion mechanism on its covering candidate | same seams and tests |

### Class 3 — ground-truth / specification decision change (2)

Anchors counted as r3 failures under GT v1 (must_remove retention 2/2: the
arms kept them) that the operator re-specified on 2026-08-30 as must_keep in
GT v2. These are measurement-basis changes, not system errors, and they are
the only v1→v2 label differences.

| # | Anchor | Half-open span (anchor frames) | Mechanism | Correction seam |
|---|---|---|---|---|
| 8 | a001 | [0, 83) | Labeled must_remove in GT v1; both arms kept the span, scoring a retention failure under v1; operator decision 2026-08-30 re-labels it must_keep (GT v2, 77/0) | versioned ground truth + report evaluation binding (`services/metrics/v44_product_proof.py` `EditorialGroundTruthV1`/`EvaluationBindingV1`); `truth-freeze.json` records both hashes; cross-version comparisons are typed refusals |
| 9 | a002 | [83, 212) | Same re-specification as a001 | same seam; `tests/metrics/test_v44_product_proof.py` binding/refusal tests |

## Reconciliation

- 5 (class 1) + 2 (class 2) + 2 (class 3) = 9 distinct anchor entries, no
  duplicate anchor or span.
- Class 1 + class 2 = the 7 B-r3 catastrophic must_keep removals
  (a016, a041, a043, a049, a075, a076, a077), i.e. 75 − 68 recalled.
- Class 3 = the 2 GT-v1 must_remove anchors kept (a001, a002), i.e. the
  v1 retention failure 2/2 that GT v2 dissolves by re-specification.
- Cross-check: arm-A-r3 catastrophic count is 5 and equals class 1 exactly;
  the arm-B-only delta (a016, a043) is class 2.

## Outcome boundary (not a gate record)

The corrected diagnostic re-runs (A-r4/B-r4) and the system-ASR probe outcome
are recorded separately in `r4-diagnostic-summary.json`. This file classifies
the r3 failure only; it is not a Gate V44-0/V44-2 record and no publication,
Resolve, or learning state may be derived from it.
