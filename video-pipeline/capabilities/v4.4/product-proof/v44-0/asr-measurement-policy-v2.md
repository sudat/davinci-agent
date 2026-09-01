# ASR measurement policy v2 — predeclaration (2026-09-01)

Sanitized, committed predeclaration for the measurement-definition
correction approved by the operator on 2026-09-01. Counts and hashes only:
no transcript text, no private paths. Binding product spec record: PRD v4.4
§6.7.4 (appended the same day). Implementation:
`services/metrics/v44_asr_measurement.py` (commit `789e227`), gate binding
(commit `7fe6686`), frozen-evidence guard (commit `7cf93d9`).

## Operator decisions (2026-09-01)

1. Replace the over-strict omitted/duplicated/timestamp measurement
   definitions with meaningful predeclared ones (this document).
2. Run a corrected-transcript finishing diagnostic, recorded as non-product
   evidence (separate record; not part of this predeclaration).

Role assignment: Sol implements; Opus reviews read-only.

## Measured basis (read-only analysis, 2026-09-01)

The analysis reproduced the r3 system-ASR metrics byte-exactly
(cer 0.10372340425531915 / p95 5740.0 / omitted 31 / duplicated 37) from the
saved arm-A-r3 asr-cache artifact and then measured the pairing structure:

- corrected 93 / hypothesis 99 / paired 88 (v1 pairing) / unpaired 5;
- signed start-diff (expected - hypothesis): median -30 ms, mean +577 ms,
  stdev 6661 ms; absolute: median 320 ms, p75 1320 ms, p90 3300 ms,
  p95 5740 ms, max 54560 ms; 53/88 within 500 ms, 28/88 within 100 ms;
- best-match CER distribution: byte-exact 62/93, <=0.10: 3, <=0.25: 14,
  <=0.50: 9, unpairable (>0.50): 5 — the materially wrong 14 are mostly
  short back-channel utterances;
- 5 tail pairs with exact text (cer 0.00) drift -3.3 s .. -8.6 s: genuine
  ASR timing drift, not pairing error;
- drop_dup trims 8 frames of 8467 (max ~280 ms): constant-offset and
  scale-error hypotheses are refuted (regression slope -2.17 ms/1000 ms,
  wrong sign and magnitude).

## v1 defects (measurement instrument, not pipeline)

1. `omitted_utterance_count` / `duplicated_utterance_count` are strip-exact
   text multiset comparisons: a one-character difference counts as omitted+1
   AND duplicated+1. Measured: 31+37=68 flags reduce to 14 materially wrong
   utterances.
2. `_pair_timestamps` (arm gate path) is an untimed greedy best-CER pairing
   accepting CER <= 0.5: a 5-character utterance legally paired with an
   occurrence 54 s away; 44/88 pairs matched across |index delta| > 3.
3. Two different pairing functions fed the same gate family (arm path vs
   subtitle-proof path), so lanes were not measured identically.

## v2 definitions (frozen here before any remeasurement)

- One canonical module: `services.metrics.v44_asr_measurement`
  (`POLICY_VERSION = "v44-asr-measurement-v2"`).
- Normalizer: the `_v44_jp_metrics._STRIP_RE` character set (punctuation,
  brackets, whitespace, ellipsis, middle dot).
- Alignment: monotonic (order-preserving) one-to-one pairing over
  normalized-CER <= 0.5 candidate edges; maximum cardinality, then minimum
  exact total cost (Fraction arithmetic), then canonical earliest-index
  tie-break (fixed DP evaluation order: skip reference, skip hypothesis,
  pair).
- omitted = unpaired references.
- duplicated = unaligned hypothesis utterances near-matching (CER <= 0.5)
  an already-paired reference (true repeats).
- segmentation_split_count (report-only) = unaligned hypothesis utterances
  recoverable by concatenating >=2 adjacent hypothesis utterances into one
  reference with temporal overlap (resolve-auto-caption
  `quality_logic._recoverable` discipline).
- timestamp p95 = nearest-rank p95 over ALL paired start diffs; genuine
  drift pairs are never excluded. Signed/absolute nearest-rank medians and
  an over-1000 ms outlier count are reported alongside.
- CER (concatenated, unchanged), proper-noun recall (unchanged), and the
  four THRESHOLDS are carried over unchanged from v1
  (`services.cli._v44_arm_transcript`: CER_MAX 0.10,
  TIMESTAMP_P95_MS_MAX 500.0, OMITTED_MAX 5, DUPLICATED_MAX 5).

## Threshold rule

The four numeric thresholds are unchanged by this policy. Any future change
requires a fresh dated predeclaration committed BEFORE the next
measurement; post-observation adjustment is forbidden
(`thresholds_relaxed_after_observation` stays false everywhere).

## Historical-evidence rules

- Every report produced before 2026-09-01 keeps its v1 interpretation; the
  v1 functions in `services.metrics.v44_product_proof` and
  `services.cli._v44_jp_metrics.utterance_diffs_ms` are frozen in place
  (docstring-marked).
- The closed resolve-auto-caption spike stays fully reproducible: its
  wiring remains v1 and its committed outputs must never be regenerated
  onto the frozen filenames.
- Frozen evidence hashes are pinned by
  `tests/capabilities/test_v44_frozen_evidence.py`.

## Predeclared remeasurement prediction (probe-system-asr-r2)

Run AFTER this note is committed, with identical bindings to r1, zero paid
calls, refusal expected before any provider call:

- transcript CER: identical 0.10372340425531915 > 0.10 → refuse;
- timestamp p95: expected inside the genuine tail-drift band (~3.3-8.6 s)
  > 500 ms → refuse;
- omitted: expected small (the 5 unpairable references; band, not a point);
- duplicated: expected small (true repeats only).

The refusal is the expected CORRECT outcome and is recorded as evidence.
Gate V44-2 remains unsatisfied (`gate_v44_2_passed=false`); no finishing,
publication, or learning work is started by this policy.
