# AI YouTube Video Production Orchestrator PRD v4.4

Status: Binding product-correction rebaseline after v4.3 implementation

Repository: `sudat/davinci-agent`

Implementation baseline: `c37247e9c10fa7c1c2ca50b84f0d1296b50c9b81`

Date: 2026-08-23

Supersedes for product direction and acceptance: `docs/prd/PRD_v4.3.md`

Historical v4.3 artifacts, gates, capability evidence, and code remain valid historical evidence unless this document explicitly changes their interpretation.

---

## 0. v4.4 exists because v4.3 is already implemented

v4.4 is not a second architecture rebuild.

v4.3 successfully built most of the intended platform surface:

- pinned DaVinci Resolve MCP client and live capability probes,
- MCP and legacy execution paths,
- Media Intelligence v2 schemas and query surface,
- progressive analysis scheduling,
- Moment Deep Review artifacts,
- recall audit machinery,
- Editorial Director v2 and three-pass planning contracts,
- reference-learning schemas, ingestion, feature extraction, pairwise preference, and derived taste profiles,
- Channel Production Kit registry and recipe selection,
- Timeline IR v2,
- subtitle/audio/color/presentation plans,
- MCP execution plans and readback,
- seven-domain quality reporting,
- editorial QC and publishability artifacts,
- Episode Cockpit,
- natural-language review command surface,
- approval bundling and recovery UX,
- publish package and YouTube upload path,
- performance observation and channel-learning machinery.

The v4.4 problem is different:

> The repository contains a broad implementation, but the most important product hypothesis has not yet been proven on a representative real episode: can the system make editing decisions that the operator actually considers good enough to publish?

Therefore v4.4 changes the center of gravity from capability construction to product proof.

The first question is no longer:

> Is the architecture complete?

The first question is:

> If the operator puts real footage into the system today, does the system produce a cut worth continuing, can the operator correct it comfortably, and can the resulting video become something the operator genuinely wants to publish?

---

## 0.1 Binding implementation discipline

Every requirement in this PRD has one of the following execution classes.

| Tag | Meaning | Implementation rule |
|---|---|---|
| `[FIRST-PUBLISH]` | Required to obtain the first real publishable episode | May be implemented now |
| `[STEADY-STATE]` | Required after the first publishable episode to reach repeatable low-human-time operation | Do not expand before First Publish unless a measured blocker requires it |
| `[POST-PUBLISH]` | Learning, generalization, or optimization useful only after the product path works | Frozen from the critical path until First Publish passes |
| `[KEEP]` | Existing v4.3 capability that should be reused rather than rebuilt | Modify only when real-episode evidence identifies a blocker |

Hard rule:

> Before Gate V44-2 First Publishable Real Episode passes, the coding agent must not add a new generic framework, new reusable abstraction layer, new artifact family, new genre-generalization mechanism, or new learning subsystem unless a measured failure in the current critical path requires it.

The presence of a future requirement in this PRD is not authorization to implement it early.

---

## 0.2 Artifact-growth limit before First Publish

v4.3 already contains sufficient artifact infrastructure.

Before Gate V44-2, new authoritative artifact types are limited to the minimum needed to make the product hypothesis auditable:

1. `EditorialGroundTruthV1`
2. `ProductProofReportV1`

Any proposal for a third new authoritative artifact type before Gate V44-2 must identify:

- the measured failure it fixes,
- why an existing v4.3 artifact cannot carry the information,
- why an ephemeral runtime view is insufficient.

This rule exists to prevent a return to infrastructure-first development.

---

# 1. Product mission

## 1.1 Primary operator

The primary operator is sudaさん.

This is first and foremost a personal production system for the operator's actual YouTube workflow. Generality is useful only when it follows real usage.

The product must optimize for the operator's judgment of a good video, not an abstract generic creator persona.

## 1.2 Desired end state

The normal steady-state experience is:

```text
Choose source folder
+
Write a short episode brief
+
Optionally attach reference videos/comments

        |
        v

AI understands all relevant footage
AI finds valuable and boring moments
AI builds story and edit
AI adds appropriate B-roll/subtitles/audio/color/graphics
DaVinci Resolve produces the finished timeline/render

        |
        v

Operator watches a preview
Operator gives natural-language corrections
System partially rebuilds only what changed

        |
        v

Operator approves final video
System builds publish package
Operator approves publication
System uploads/schedules to YouTube
```

The operator should not need to become the low-level DaVinci operator for normal episodes.

## 1.3 Product promise

The product promise has two regimes.

### Bootstrap regime

The first few episodes may require additional operator time to:

- establish what the operator likes,
- identify failure patterns in editorial judgment,
- choose initial Production Kit recipes,
- correct proper nouns and subtitle conventions,
- validate real Resolve capability paths.

There is no 30-minute Active Human Time promise during bootstrap.

### Steady-state regime

After the bootstrap kit and taste context stabilize, the target is:

- median Active Human Time <= 30 minutes per supported episode,
- no routine CLI use,
- no routine JSON inspection,
- no routine direct Resolve editing,
- normally <= 2 blocking human sessions,
- natural-language correction as the main editing interaction.

---

# 2. Product principles

## 2.1 Editorial value before infrastructure elegance

A technically perfect pipeline that produces a boring edit is a failed product.

Priority order:

1. editorial usefulness,
2. operator reviewability,
3. finished audiovisual quality,
4. technical correctness and safety,
5. deterministic replay of accepted decisions,
6. breadth and generality.

Determinism applies strongly after a decision is accepted. It does not require the creative decision itself to be deterministic.

## 2.2 Test the dangerous assumption first

The most dangerous assumption is:

> Sparse/coarse evidence plus targeted dense review is sufficient for an AI model to identify moments and construct an edit that the operator considers worth continuing.

v4.4 must explicitly try to falsify this assumption before more product breadth is added.

## 2.3 Real footage beats synthetic fixture success

Synthetic fixtures remain useful for contracts, safety, and regression.

They are not evidence that the product edits well.

Product gates require representative real footage.

## 2.4 MCP is a capability provider, not an editorial authority

`davinci-resolve-mcp` provides generic media-analysis and Resolve execution capabilities when those capabilities pass the relevant fit checks.

`davinci-agent` remains responsible for:

- episode intent,
- channel semantics,
- taste context,
- editorial judgment orchestration,
- review and approval,
- learning governance,
- publication governance.

## 2.5 Reuse implemented v4.3 code before adding more code

v4.4 does not discard the implemented v4.3 subsystems.

Existing code may be:

- used in the critical path,
- bypassed from the critical path,
- feature-flagged,
- frozen until later,
- corrected when real evidence demonstrates a problem.

Already-built complexity is not itself a reason to force that complexity into the first-publish path.

## 2.6 The operator is allowed to communicate taste naturally

The operator should be able to say:

- "この動画の色使いは好き"
- "0:20から1:10の喋り構成が上手"
- "テンポは好きだけど字幕は嫌い"
- "このB-rollの入り方は参考にしたい"

The system must not require numeric scoring as the primary calibration method.

## 2.7 Unspecified taste must stay unspecified

If the operator says only that the color is good, the system must not infer that the operator also likes:

- story structure,
- subtitle styling,
- B-roll density,
- pacing,
- audio treatment.

Domain scoping is mandatory.

## 2.8 The system should ask the operator only when the answer matters

Safe technical fallback should continue automatically.

The operator is interrupted when the decision materially affects:

- meaning,
- rights,
- privacy,
- irreversible publication,
- an unresolved editorial ambiguity that could change the intended video.

---

# 3. Current v4.3 implementation truth

v4.4 begins from the code at baseline commit `c37247e9c10fa7c1c2ca50b84f0d1296b50c9b81`.

The following distinctions are binding.

## 3.1 Mechanically implemented and valuable

The repository already has strong foundations that v4.4 should preserve:

- artifact registry/store and lineage,
- job state and single-writer discipline,
- proposal/validation/commit separation,
- conform/time-coordinate handling,
- MCP pinning and live probing,
- MCP execution plan/readback/fallback,
- preview infrastructure,
- Cockpit basic intake/status/review surfaces,
- quality-domain reporting,
- release/audit/security controls,
- publication package/idempotency.

## 3.2 Implemented but not yet product-proven

The following are implemented in code but are not accepted as product-quality evidence until v4.4 real-episode gates pass:

- progressive Media Intelligence routing,
- Moment Deep Review quality,
- Editorial Director v2 quality,
- reference-derived taste influence,
- Production Kit tastefulness,
- Japanese subtitle quality,
- natural-language review flexibility,
- end-to-end Cockpit workflow,
- first-preview usefulness,
- final publishability.

## 3.3 Known v4.3 product-proof gaps

The current implementation contains several deliberate or accidental test doubles that are acceptable for mechanism verification but insufficient for product acceptance.

### Editorial Director default path

`services/editorial_v2/director_v2.py` supports an injectable LLM call, but its default path uses the deterministic heuristic planner.

v4.4 requires a real production editorial model path.

### Moment Deep Review assessment

`services/media_intelligence/moment_review.py` explicitly describes its assessment as a deterministic placeholder and currently includes synthetic frame/transcript/audio providers.

v4.4 requires a real evidence provider for product runs.

### Episode 0 suitability

The recorded v4.3 real Episode 0 probe is a DJI clip without the transcript required to exercise the full editorial chain.

It cannot serve as the representative product-validation episode for v4.4.

### Recall audit signal

The recorded Gate V43-1 summary reports `miss_rate = 1.0` and `signal = degraded` for all three synthetic fixture families.

This is not automatically proof that the real editorial system fails, because the benchmark path contains synthetic placeholder behavior. It is, however, an explicit unresolved red flag. v4.4 must not interpret V43-1 as evidence that valuable-moment recall is already good.

### Cockpit acceptance path

The Cockpit acceptance harness verifies important UX mechanics, but its fast path seeds internal state to `PREVIEW_READY` rather than proving the full raw-footage-to-preview journey.

### Review chat interpretation

The current review chat parser is deterministic and pattern-based. It recognizes a bounded command vocabulary but is not yet equivalent to general natural-language editing conversation.

---

# 4. v4.4 success definition

v4.4 is successful when the operator can produce at least one genuinely publishable real episode through the product path and then reproduce the workflow on subsequent actual episodes with improving human effort.

## 4.1 First Publish success

The first publishable episode must satisfy all of the following:

- real source media chosen from the operator's actual production workflow,
- actual media analysis, not synthetic providers,
- actual production editorial model, not default heuristic planner,
- at least one human-evaluated editorial selection comparison,
- real Editorial Preview,
- natural-language correction path exercised,
- relevant partial rebuild exercised,
- real Resolve finishing path exercised,
- applicable quality domains assessed,
- operator explicitly answers that the final video is publishable,
- no hidden requirement for routine manual Resolve editing.

## 4.2 Steady-state success

After bootstrap, on the operator's next actual episodes:

- median AHT <= 30 minutes,
- first preview is useful enough that corrections are incremental rather than a complete re-edit,
- Must-Keep moments are not silently lost,
- corrections are accepted through normal-language interaction,
- no more than two normal blocking human sessions,
- publication remains explicitly operator-approved.

---

# 5. Representative real episode contract

## 5.1 `[FIRST-PUBLISH]` v4.4 Episode 0 must be representative

v4.4 must create a new representative validation episode, referred to in this document as `v44-real-01`.

It should reflect the next real video the operator actually wants to make.

It should not be selected merely because it is easy to process.

If the operator's expected workflow contains speech plus visual material, the validation episode must contain speech plus visual material.

## 5.2 The validation episode should exercise real uncertainty

Useful properties include some combination of:

- multiple takes or alternate moments,
- speech with useful and unnecessary portions,
- B-roll or visual inserts,
- quiet/visual moments that should not be removed solely because they lack speech,
- Japanese dialogue,
- proper nouns relevant to the episode,
- differing visual quality or camera conditions,
- a reason for at least one real review correction.

## 5.3 Future format support follows actual episodes

v4.4 removes the requirement that three predetermined generic format classes must be proven before the product is useful.

The architecture remains format-capable, but validation proceeds in this order:

```text
actual next episode
        |
        v
measured failure/success
        |
        v
required capability extension
        |
        v
next actual episode
```

Generality is earned from real use.

---

# 6. Editorial Feasibility Spike

## 6.1 `[FIRST-PUBLISH]` Purpose

Before additional feature expansion, v4.4 must test whether the current evidence strategy and an actual editorial model can identify worthwhile moments.

This is a product experiment, not a schema-development phase.

## 6.2 Operator ground truth

The operator supplies a lightweight set of anchor labels over `v44-real-01`.

The operator does not need to manually edit the entire episode.

Required label classes:

- `must_keep`: losing this moment would materially harm the episode,
- `good_optional`: useful but not mandatory,
- `must_remove`: clearly unwanted, redundant, broken, or boring for this episode,
- `uncertain`: intentionally excluded from strict scoring.

The operator may also add short natural-language rationales.

Examples:

```text
02:14-02:29 must_keep
"ここが一番面白い話"

04:02-04:30 must_remove
"同じ話の言い直し"

07:11-07:18 must_keep
"無言だけど表情がいいので残したい"
```

## 6.3 Experiment A: coarse evidence

Experiment A uses the cheapest viable evidence path:

- shot boundaries,
- sampled frames/shot descriptions,
- transcript,
- basic audio facts,
- Episode Brief,
- no Moment Deep Review.

An actual editorial model returns candidate keep/remove decisions and a minimal story ordering.

## 6.4 Experiment B: progressive deep review

Experiment B uses the same episode and editorial model, but adds targeted Moment Deep Review for high-value, ambiguous, or risk-prone regions.

The experiment measures whether Progressive Attention actually improves editorial quality enough to justify its extra cost.

## 6.5 Experiment C: human-rich evidence diagnostic arm

Experiment C is executed only if Experiment B fails the predeclared criteria.

For `v44-real-01`, Experiment C is additionally conditional on the corrected Arm B of §6.7.1: it runs only if that corrected Arm B still fails.

For a small set of failed regions, a human provides richer evidence or corrected evidence context.

Purpose:

- if the same editorial model succeeds with human-rich evidence, the primary problem is evidence generation,
- if the model still fails with rich evidence, the primary problem is editorial reasoning/model selection/prompting.

This prevents blind tuning of the wrong layer.

## 6.6 Predeclared pass criteria

Before the first run, the exact evaluation protocol is written and frozen in `EditorialGroundTruthV1` / test configuration.

Default v4.4 acceptance policy:

| Criterion | Required interpretation |
|---|---|
| Must-Keep recall | 100% of labeled Must-Keep anchors are present in the candidate edit or explicitly escalated for human review |
| Catastrophic removal | 0 Must-Keep anchor may be confidently removed without escalation |
| Must-Remove retention | No more than 25% of labeled Must-Remove anchors may remain as recommended keeps; stricter targets may be adopted after baseline |
| Redundancy | No obvious repeated statement labeled Must-Remove may survive solely because it is transcript-valid |
| Deep-review lift | Experiment B must improve at least one objective editorial criterion or operator verdict without reducing Must-Keep recall |
| Operator continuation verdict | Operator answers YES to: "この選択なら続きを作る価値があるか" |

The 100% Must-Keep target is deliberately strict because anchor labels are sparse and represent moments the operator explicitly considers essential.

## 6.7 NO-GO procedure

A failed result must not turn into unbounded tuning.

Diagnostic order:

```text
A: coarse evidence
        |
        v
B: progressive deep review
        |
        +-- pass --> proceed
        |
        v
C: human-rich evidence control
        |
        +-- C succeeds --> evidence-generation problem
        |
        +-- C fails ----> reasoning/model problem
```

For each identified failure class, one targeted design correction and one re-run are allowed by default.

If the corrected path still fails the same critical criterion, stop downstream feature expansion and explicitly revisit the editorial approach before continuing.

### 6.7.1 `[FIRST-PUBLISH]` Measured diagnosis: corrected Arm B failure on `v44-real-01`

The corrected Arm B run `arm-B-r2` (real DirectorV2 three-pass, GPT-5.6 Sol, Moment Deep Review enabled) failed the predeclared criteria of §6.6. Measured facts:

- Must-Keep recall 72/75 (0.96) against the required 1.0,
- catastrophic Must-Keep removals 3 against the required 0,
- Must-Remove retention 2/2 (1.0) against the maximum 0.25,
- evidence quality: transcript CER 0.104, timestamp error p95 5740 ms, 31 omitted utterances, 37 duplicated utterances; proper-noun recall was 1.0.

The operator continuation verdict was YES, but the machine pass policy failed and publishability was not recorded. This run does not satisfy Gate V44-0.

Classification under §21.1: an evidence-generation/interpretation failure. Omitted and duplicated utterances and misaligned timestamps meant the decision layer received an unfaithful map of the source and made keep/remove decisions against incomplete or distorted evidence. It is not a reason to rebuild the pipeline, and it is not evidence that editorial reasoning or model selection failed.

Under the §6.7 allowance of one targeted design correction and one re-run, v4.4 applies exactly one bounded `[FIRST-PUBLISH]` correction to evidence generation: full-source audiovisual map/reduce plus a targeted video-only specialist, fused before the editorial decision. The four role boundaries are defined in §8.5. This is not a rebuild and not a generic multi-agent framework.

Arm C (§6.5) is executed only if this corrected Arm B still fails the §6.6 criteria.

### 6.7.2 `[FIRST-PUBLISH]` Measured correction-cycle record: r3 diagnosis and bounded r4 outcome on `v44-real-01` (2026-08-31)

This subsection appends the next measured cycle after §6.7.1; it does not
modify the r2 statement above. Committed sanitized evidence:
`video-pipeline/capabilities/v4.4/product-proof/v44-0/r3-failure-classification.md`
and `video-pipeline/capabilities/v4.4/product-proof/v44-0/r4-diagnostic-summary.json`.

Round 3 (reports cited there by sha256) failed harder than r2: must-keep
recall 68/75, catastrophic removals 7, and the two GT-v1 must-remove anchors
were kept (retention 2/2). The measured diagnosis classified all 9 errors
exactly once by anchor ID/span: 5 perception/input-path (3 anchors whose tail
utterances were never discovered because shot discovery used summed segment
length instead of the authoritative extent; 2 anchors dropped because the
kept-span derivation treated `optional` as removed), 2 reasoning/ineligible
cut (must-keep anchors demoted to optional on free-text rationale in arm B
only), and 2 ground-truth/specification decisions (the operator re-labeled
a001/a002 must_keep in GT v2 on 2026-08-30; 77/0).

Under the §6.7 single-correction allowance, one bounded correction wave fixed
the input-integrity and cut-policy seams (authoritative extent with exact
candidate-ID set checks, fail-closed pre-model transcript alignment, and
deterministic removal eligibility), without rebuilding the pipeline or
changing providers/models.

Corrected diagnostic re-runs A-r4/B-r4 (GT v2, operator-corrected transcript,
identical bindings across arms) delivered and kept 93/93 candidates, recalled
77/77 must-keep anchors with 0 catastrophic removals, 0 removals, and 0
escalations; the must-remove dimension is disclosed as not measured (0
anchors). The consolidated comparison shows zero editorial deltas between
arms. No r3 critical failure class repeated, so the §6.7 NO-GO branch was not
triggered. Arm C was therefore not required and was not run.

These r4 runs are diagnostic evidence in the `operator_corrected_diagnostic`
lane, not product proof. A system-ASR readiness probe in the product lane was
refused before any paid call with typed `asr-alignment-failed`
(CER 0.10372340425531915 > 0.10; timestamp p95 5740.0 ms > 500.0; 31 omitted
> 5; 37 duplicated > 5, against thresholds frozen before the run). ASR
alignment is recorded as the next measured blocker. Gate V44-2 remains
unsatisfied (`gate_v44_2_passed=false`); no Resolve build, publication work,
or learning activation was started.

### 6.7.3 `[FIRST-PUBLISH]` Measured record: Resolve auto-caption spike outcome on `v44-real-01` (2026-08-31)

This subsection appends the measured Resolve auto-caption spike outcome after
§6.7.2; it does not modify the earlier statements. Committed sanitized
evidence: `video-pipeline/capabilities/v4.4/probes/resolve-auto-caption/summary.json`,
backed by `capability-r1.json` (sha256 `f04415632aa9ffd5ab77594a4c9fd6c952dbe83ec9fb013038cb4433a5351450`),
`capability-r2.json` (sha256 `08c1654c750783d2605a2b1357b21663f20165cbca922a50aaea1f6c7ea9413c`),
and `quality-metrics.json` (sha256 `fb06cd1d794113c76fd3f569c5c927eb1a92ba7df605f78e9146e1a4f4e9a6ca`).

A disposable two-run spike on Resolve 21.0.4.5 (pinned MCP 2.98.3, pin commit
`132e134d3aa25d3d0df6bdf38f051bd29d128211`) proved the native Japanese
auto-caption capability mechanically: both runs generated and read back 79
subtitle items with an identical canonical readback sha256, and both
disposable projects were deleted with deletion proven by load refusal. This
is capability evidence only, not product quality evidence.

Scored against the corrected reference with the same functions, sample, and
pairing rule as the system-ASR readiness path, all four frozen quality limits
failed: transcript CER 0.1622340425531915 > 0.10, timestamp error p95
7849.0 ms > 500.0, 58 omitted utterances > 5, and 44 duplicated utterances
> 5. The thresholds are imported from `services.cli._v44_arm_transcript` and
were not redeclared or relaxed. The deterministic segmentation diagnosis
classified the 58 omitted utterances as 0 segmentation merge/split plus 58
actual missing, and the 44 duplicated cues as 15 segmentation split plus 29
actual duplication. Because non-segmentation causes dominate, the spike
plan's one-time deterministic merge/split correction exception was not
applicable.

No integration occurred: `integration_status=blocked-with-reason` with
`blocked_reason=blocked-quality`, provider `resolve-auto-caption`, zero
product-integration provider calls (the disposable spike's own recorded
live runs above are the only Resolve/MCP calls made), zero segmentation
adjustments, zero remeasurements, and no product code changes.
System-ASR alignment therefore remains the recorded measured blocker.
Gate V44-2 remains unsatisfied (`gate_v44_2_passed=false`); no finishing,
publication, or learning work was started.

### 6.7.4 `[FIRST-PUBLISH]` Operator decision and predeclared ASR measurement policy v2 (2026-09-01)

This subsection appends the operator-approved measurement-policy correction
after §6.7.3; it does not modify §6.7.1–§6.7.3. Committed sanitized
predeclaration: `video-pipeline/capabilities/v4.4/product-proof/v44-0/asr-measurement-policy-v2.md`.

On 2026-09-01 the operator approved (1) replacing the over-strict
omitted/duplicated/timestamp measurement definitions with meaningful
predeclared ones, and (2) a corrected-transcript finishing diagnostic
recorded as non-product evidence. Role assignment: Sol implements; Opus
reviews read-only.

A read-only pairing analysis (which first reproduced the r3 system-ASR
metrics byte-exactly) measured: corrected 93 / hypothesis 99 / paired 88;
signed start-diff median -30 ms, absolute median 320 ms; best-match CER
distribution byte-exact 62/93, materially wrong 14/93, unpairable 5; and
5 exact-text tail pairs drifting -3.3 s to -8.6 s (genuine ASR timing
drift). Constant-offset and timebase-scale hypotheses were refuted
(drop_dup is 8 of 8467 frames; regression slope has the wrong sign).

Classification: the v1 omitted/duplicated counts are strip-exact text
multiset comparisons (a one-character difference counted as omitted+1 AND
duplicated+1), and the v1 timestamp pairing is untimed greedy text matching
that legally joined a 5-character utterance to an occurrence 54 s away.
This was a measurement-instrument defect in the §6.7.1 evidence-quality
lineage — not a pipeline redesign and not threshold relaxation.

Under the §6.7 single-correction allowance, one bounded correction replaced
the measurement definitions only (`services/metrics/v44_asr_measurement.py`,
policy `v44-asr-measurement-v2`): monotonic one-to-one alignment over
normalized-CER ≤ 0.5 edges (max cardinality, then exact min cost, then
canonical earliest-index tie-break); omitted = unpaired references;
duplicated = unaligned hypothesis utterances near-matching an already-paired
reference; segmentation splits reported separately and never counted as
duplication; timestamp p95 over ALL paired diffs with genuine drift pairs
never excluded. The four numeric thresholds are carried over UNCHANGED
(0.10 / 500.0 ms / 5 / 5) and any future change requires a fresh predeclared
record committed before the next measurement.

All pre-2026-09-01 reports keep their v1 interpretation: the v1 functions
are frozen in place (docstring-marked), the closed resolve-auto-caption
spike stays reproducible on v1 wiring, and frozen evidence hashes are
pinned by `tests/capabilities/test_v44_frozen_evidence.py`.

The predeclared prediction for the remeasurement (`probe-system-asr-r2`,
zero paid calls, run only after the predeclaration commit): CER unchanged
at 0.10372340425531915 > 0.10 and timestamp p95 inside the genuine
tail-drift band → the gate refuses, and that refusal is the expected
correct outcome recorded as evidence. Gate V44-2 remains unsatisfied
(`gate_v44_2_passed=false`); no finishing, publication, or learning work is
started by this policy.

Measured outcome (2026-09-01, `probe-system-asr-r2`, sanitized evidence
`video-pipeline/capabilities/v4.4/product-proof/v44-0/asr-alignment-v2.json`):
the gate refused exactly as predeclared, on the two predicted criteria only —
transcript CER 0.10372340425531915 > 0.10 and timestamp error p95 4200.0 ms
> 500.0 (inside the predicted 3.3–8.6 s genuine-drift band). Under the v2
definitions omitted utterances are 5 (≤ 5) and duplicated utterances are 4
(≤ 5); alignment paired 88/93 references with signed median +40 ms,
absolute median 300 ms, and 23 pairs drifting over 1 s. A fresh run-arm
workspace was not built because the committed toolchain pin
(`config/toolchains/phase-0b-v1.json`) references the deleted
`.omo/start-work` bootstrap tree — a pre-existing breakage recorded for
separate repair; the measurement replays the saved `probe-system-asr-r1`
inputs through the same production gate functions at the same refusal
point. Thresholds were not relaxed; Gate V44-2 remains unsatisfied.

---

# 7. MCP Evidence Quality Fit

## 7.1 `[FIRST-PUBLISH]` Current mechanical fit is not enough

v4.3 already proved a large set of MCP operations mechanically on Resolve 21.0.4 with `davinci-resolve-mcp` 2.98.3.

That evidence remains valuable.

v4.4 adds a different question:

> Is the analysis content returned by the MCP path sufficiently accurate and useful on the operator's real Japanese material?

## 7.2 Evidence completeness

For `v44-real-01`, measure the proportion and quality of Media Intelligence fields that can be populated from the real provider path.

At minimum inspect:

- shot boundaries,
- source ranges,
- shot descriptions,
- subject/action/location,
- visual framing/motion,
- editorial-role/select-potential fields when present,
- best-moment evidence when present,
- cuttability evidence,
- transcript references,
- confidence/provenance.

A non-empty field is not automatically a useful field.

## 7.3 Japanese transcript quality

Japanese evidence quality is evaluated separately from subtitle presentation.

Evidence-side metrics include:

- transcript character error rate on a manually corrected sample,
- proper-noun exact-match recall,
- omitted/duplicated utterances,
- timestamp alignment error,
- speaker/segment boundary usefulness where applicable.

The manually corrected sample should include difficult proper nouns relevant to the actual episode.

## 7.4 Subtitle quality is a different problem

Subtitle-side quality is measured after transcript generation and includes:

- Japanese segmentation,
- line-break placement,
- punctuation,
- reading rhythm,
- display duration,
- proper-noun rendering,
- on-screen density.

A good ASR result can still produce poor subtitles, and a poor ASR result cannot be repaired solely by layout rules. These failure classes must remain separate.

## 7.5 Existing MCP failures remain explicit

Known v4.3 MCP capability failures are not erased by v4.4:

- Text+ title property path,
- transition path,
- generic audio property operation,
- BGM ducking path,
- edit-engine selects,
- alternate-shot similarity.

Existing fallbacks remain valid unless real operation demonstrates a better path.

The first publishable episode does not wait for every failed MCP capability to become native. It uses the safest accepted fallback appropriate to the episode.

---

# 8. Real Moment Deep Review

## 8.1 `[FIRST-PUBLISH]` Placeholder mode may not be used for product acceptance

The synthetic providers and deterministic placeholder assessment in the current `MomentDeepReviewV1` path remain valid for unit/regression tests.

They may not be used as evidence for Gate V44-0 or later product-quality gates.

## 8.2 Required real evidence bundle

A product Moment Deep Review should include enough local temporal context to judge timing, reaction, and moment-level value.

Depending on the material, the bundle may contain:

- dense frames or a short video segment,
- local transcript text and timestamps,
- neighboring shot context,
- audio energy/silence/ambient context,
- Episode Brief context,
- relevant reference/taste seed context.

The implementation may use a multimodal model directly on a short clip if that is more faithful than sparse frame-only prompting.

## 8.3 Review targeting

Deep Review is targeted when one or more of these are true:

- coarse model predicts high value,
- confidence is low,
- high disagreement exists between evidence sources,
- potential Must-Keep risk exists,
- a scene has strong reaction/timing characteristics,
- transcript-only evidence is insufficient,
- recall-audit sampling requests a check,
- operator explicitly asks to inspect the region.

## 8.4 Deep Review must be allowed to disagree with the shot summary

A shot marked mediocre at coarse level may contain a valuable sub-span.

The system must support:

```text
coarse shot = ordinary
        |
        v
deep review finds 1.5 s reaction
        |
        v
moment becomes high-value candidate
```

This is one of the central reasons v4.4 retains Progressive Attention.

## 8.5 `[FIRST-PUBLISH]` Video-understanding roles for the corrected Arm B

The §6.7.1 correction defines bounded evidence-generation roles only; this is not a generic multi-agent framework. Per the 2026-09-10 operator directive (GLM-5v-turbo is the fixed-rate plan; Gemini Flash is metered), the primary full-source observation path runs on GLM alone.

| Role | Model / provider surface | Responsibility | Boundary |
|---|---|---|---|
| Full-source chunked observation (primary) | `glm-5v-turbo` via its officially supported video transfer | Partition the entire Edit Source into contiguous measured-length chunks (live-probe-adopted, currently 60s) with no overlap or gap; observe each chunk's audio-stripped video together with that chunk's speech transcript (text only, passed as comparison data) | Not an editorial decision maker. Never receives audio media — speech reaches it as transcript text only. Timestamps are prompt-driven and locally validated |
| Chunk-level assist (metered, optional) | `gemini-3.7-flash` via Gemini Developer API (the only verified pin; do not silently switch model ids or provider surface) | Assist ONLY specific chunks recorded as GLM-quality-insufficient, and ONLY when the operator has explicitly approved the metered call | Not part of the primary path; no automatic switchover, no full-source re-run, unavailability is a typed blocked state |
| Fusion | local deterministic assembly | Assemble chunk observations into the existing `MomentDeepReviewV1` records (provider-neutral, typed payloads only) | Does not trust provider prose outside the typed payload |
| Editorial decision owner | GPT-5.6 Sol (DirectorV2) | The sole component allowed to choose keep/remove/order/edit intent | Consumes fused evidence before proposal validation and commit. No model writes Job State, Selection Plan, Edit Plan, or Resolve |

Only fused `MomentDeepReviewV1` records are committed and indexed as authoritative evidence; per-stage provenance remains inspectable inside those existing records. Runtime chunk/specialist payloads are rebuildable execution data and do not create a third authoritative artifact type (§0.2).

2026-09-11 addendum (A/B outcome on real-episode footage, 282s): the §8.5 roles above are superseded for the consultation-sample observation path by the dual-route decision — Gemini `gemini-3.5-flash-lite` (live-verified pin `moment-review-gemini-av`) is the PRIMARY whole-video audiovisual observation for speech/sound-relevant episodes (ONE metered call over a low-res A/V proxy, explicit per-episode AV cloud-send consent required, speech never becomes subtitle authority), and GLM `glm-5v-turbo` is the visual-only path (silent chunks, audio never sent) plus detail recheck. ASR remains the subtitle authority; GPT-5.6 Sol remains the sole editorial owner; no model writes Job State, Selection Plan, Edit Plan, or Resolve. The pre-addendum table is kept as history.

---

# 9. Editorial Intelligence production path

## 9.1 `[FIRST-PUBLISH]` Actual model required

The production editorial path must use an explicitly configured model/provider.

The deterministic heuristic planner remains available for:

- tests,
- fallback diagnostics,
- regression comparisons.

It must not silently become the production creative editor.

If the production model is unavailable, the episode is marked blocked or uses an explicitly approved fallback. It must not masquerade as an AI editorial-quality pass.

## 9.2 Three-pass planning remains

The v4.3 three-pass separation remains useful:

1. Story Plan
2. Moment Selection
3. Creative Edit Plan

The purpose is not to create more schemas. The purpose is to avoid asking one model call to simultaneously understand story, rank moments, and specify presentation details.

## 9.3 Editorial reasoning dimensions

The model considers multiple dimensions rather than one scalar "interestingness" score:

- information value,
- novelty,
- story progression,
- emotional energy,
- authenticity,
- humor/surprise,
- visual interest,
- clarity,
- redundancy,
- technical usability,
- continuity,
- cuttability,
- episode/channel intent.

Quiet visual moments may score highly.

High-energy moments may still be irrelevant.

## 9.4 Evidence-grounded decisions

Every committed selection must be traceable to source spans and evidence.

The model may form creative judgments, but it may not invent media that is absent from the source.

## 9.5 Confidence should drive behavior, not merely logging

Low-confidence high-impact decisions should trigger one of:

- deeper evidence,
- alternate model check if configured,
- operator flag.

Low-confidence low-impact presentation decisions may use conservative defaults.

---

# 10. Taste Seed v0

## 10.1 `[FIRST-PUBLISH]` Purpose

The first episode should not be forced to start from generic YouTube taste.

However, the full v4.3 reference-learning subsystem must not be required on the critical path.

v4.4 defines a lightweight runtime mode called Taste Seed v0.

## 10.2 Inputs

Taste Seed v0 accepts:

- a local reference video, or a compliant YouTube reference source,
- optional timestamp range,
- natural-language operator comment,
- one or more explicitly named preference domains,
- polarity when clear: like / dislike / neutral.

Example:

```text
Reference: creator-x-episode-12.mp4
Range: 00:00:20-00:01:10
Domain: story_structure
Comment: "この区間の喋り構成が上手"
```

## 10.3 Implementation limit before First Publish

Before Gate V44-2, Taste Seed v0 must not require:

- pairwise ranking,
- global Derived Taste Profile aggregation,
- automatic contradiction resolution,
- feature-vector learning,
- cross-episode confidence updating,
- automatic channel-profile mutation.

The intended pre-first-publish implementation is a runtime projection over the already-built reference source/annotation models.

It may pass to Editorial Director:

- the operator comment,
- domain and polarity,
- relevant reference frames/short segment,
- transcript/context from that reference when available.

## 10.4 No silent preference propagation

Domain scoping in existing `ReferenceAnnotationV1` remains binding.

A reference about color must not affect pacing unless pacing is explicitly named.

## 10.5 `[POST-PUBLISH]` Advanced preference learning

The already-implemented v4.3 capabilities for:

- feature extraction,
- pairwise preference,
- derived profile aggregation,
- contradiction records,
- cross-episode retrieval,

remain in the repository but are removed from the mandatory path until real episodes demonstrate that they improve first-preview quality or reduce human corrections.

They are not deleted.

---

# 11. Channel Production Kit bootstrap

## 11.1 `[FIRST-PUBLISH]` Existing registry is reused

The existing Production Kit registry and recipe selection machinery remain.

v4.4 does not build a second kit system.

## 11.2 Real-footage preview requirement

A recipe is not accepted merely because it renders on a synthetic test card.

For each presentation domain needed by `v44-real-01`, candidate recipes must be previewed on representative real footage from that episode.

Examples:

- subtitle A/B,
- color look A/B,
- lower-third A/B,
- punch-in treatment A/B,
- audio chain A/B.

The operator may answer simply:

- A,
- B,
- neither,
- keep current.

## 11.3 Do not create unused kit breadth

If the first real episode does not need a transition family, do not block First Publish to build one.

If it does not need Fusion graphics, do not block First Publish to prove Fusion creativity.

The kit grows from real episode needs.

---

# 12. Seven-domain finishing and applicability

## 12.1 `[KEEP]` Quality responsibility remains domain-based

The seven quality domains remain useful because they prevent "we used three effects" from masquerading as finished quality.

Domains:

1. editorial construction,
2. subtitle,
3. audio finishing,
4. color finishing,
5. framing/motion,
6. graphics/presentation,
7. delivery QC.

## 12.2 `[FIRST-PUBLISH]` First Publish uses applicable-domain semantics

All seven domains must be considered, but not every domain must receive a treatment.

Each domain receives one of:

- `applied`,
- `intentionally_not_needed`,
- `manual_fallback_required`,
- `blocked`.

A domain may be intentionally not needed only with explicit episode evidence/justification.

Examples:

- no dialogue => subtitle intentionally not needed,
- no designed graphic required => graphics intentionally not needed,
- no reframing needed => framing intentionally not needed,
- no usable audio track => audio treatment may be intentionally not needed if the episode design supports it.

For color and audio where assessment is still required but no processing is necessary, a verified no-op plan is acceptable. "No effect added" is not the same as "not assessed".

## 12.3 Publishability is separate from domain completion

A technically complete domain report does not prove that the video is good.

The final question remains:

> Would the operator publish this video?

---

# 13. Japanese subtitle requirements

## 13.1 `[FIRST-PUBLISH]` End-to-end real Japanese proof

The existing Japanese subtitle pipeline must be exercised on real Japanese speech before product acceptance.

The test must include at least one proper noun relevant to the actual episode.

## 13.2 Required quality checks

Check separately:

- transcript correctness,
- proper-noun correctness,
- cue boundaries,
- Japanese phrase segmentation,
- line breaks,
- maximum visual density,
- display duration,
- punctuation convention,
- style legibility on actual footage.

## 13.3 Correction UX

The operator should be able to say:

```text
7:41の字幕は「OpenCode」に直して
この字幕は2行じゃなく1行
ここは字幕を短く
```

The system should identify the target cue, present an interpretation when necessary, and rebuild the minimum affected region.

---

# 14. Natural-language review loop

## 14.1 `[FIRST-PUBLISH]` Review is on the critical path

The review loop is not a later convenience feature.

Even a strong first draft will require corrections.

The product becomes practical only when the correction cost is low.

Required loop:

```text
Preview
  |
  v
natural-language correction
  |
  v
structured interpretation
  |
  v
validation / confirmation if ambiguous
  |
  v
apply command
  |
  v
minimal rebuild
  |
  v
updated preview
```

## 14.2 Existing deterministic parser remains a fallback

The current bounded Japanese/English pattern parser is useful for known commands and regression.

It is not sufficient as the final interpretation layer.

v4.4 may add an LLM interpreter that produces the existing structured command contracts.

The LLM may propose interpretation only.

Existing deterministic validation/commit boundaries remain.

## 14.3 Required correction classes for First Publish

At minimum, the real episode path must handle corrections such as:

- remove this section,
- keep this moment longer,
- use another take,
- add/change B-roll,
- shorten/fix subtitle,
- remove/reduce an effect,
- lower BGM,
- match color,
- episode-only vs channel-level preference.

Only corrections relevant to the first real episode must be demonstrated before Gate V44-2.

## 14.4 Targeting

The operator should not need to type technical identifiers.

Target resolution may use:

- current player time,
- explicit timestamp,
- nearby transcript phrase,
- described visual object/action,
- current flagged issue.

Ambiguous interpretations require confirmation.

---

# 15. Episode Cockpit

## 15.1 `[FIRST-PUBLISH]` Normal entry point

The normal operator path begins in the Cockpit.

The intake experience should require only:

```text
Source folder
Episode brief
Optional reference/taste comments
Create video
```

Artifact IDs, job IDs, internal JSON paths, provider payloads, and schema versions are not normal operator inputs.

## 15.2 Intake must launch real work

Creating an episode record is not sufficient.

The Cockpit path must actually launch or enqueue the production pipeline for the episode.

The operator should see real progress from source ingestion through first preview.

## 15.3 Progress UX

Display:

- current human-readable stage,
- measured progress when available,
- no fabricated precise ETA,
- clear blocked reason if blocked,
- whether operator action is actually required.

## 15.4 Interruption Policy

Three classes:

### Continue automatically

- safe retry,
- cache miss,
- accepted fallback,
- temporary provider failure with safe recovery.

### Continue and report later

- non-critical quality fallback,
- a presentation technique substituted by an approved recipe,
- an unavailable optional capability.

### Stop and ask

- meaning-changing ambiguity,
- rights/privacy uncertainty,
- no safe path to publishable quality,
- publication approval,
- operator taste conflict that materially changes the episode.

## 15.5 UX SLO

Steady-state targets:

- CLI actions during normal episode: 0,
- JSON inspection: 0,
- direct Resolve editing: 0 under normal supported conditions,
- blocking human sessions: <= 2,
- review item click seeks directly to relevant time,
- restart retains accepted decisions and progress.

---

# 16. Real end-to-end vertical slice

## 16.1 `[FIRST-PUBLISH]` The first valid product path begins from source media

A v4.4 product acceptance run must not begin from a prebuilt Media Intelligence artifact.

It starts from the source folder.

Required chain:

```text
Cockpit intake
  |
  v
Ingest / normalize / conform
  |
  v
real media analysis
  |
  v
Media Intelligence v2
  |
  v
real Editorial Director v2
  |
  v
Editorial Preview
  |
  v
operator correction
  |
  v
partial rebuild
  |
  v
Creative/Finishing Plan
  |
  v
live Resolve build
  |
  v
technical + editorial QC
  |
  v
Final Preview
  |
  v
operator publishability decision
```

## 16.2 Test doubles remain allowed outside product gate

Fake executors and seeded UI state remain valid for fast tests.

They cannot satisfy a v4.4 product gate.

---

# 17. Publishability Review

## 17.1 `[FIRST-PUBLISH]` Operator judgment is authoritative

The first publishability decision is not a numeric AI score.

The operator answers whether the current final video is publishable.

Supported responses:

- publishable,
- publishable after listed fixes,
- not publishable.

Optional comments are encouraged because they become high-value future evidence.

## 17.2 Supporting dimensions

When useful, the review may capture comments on:

- story/structure,
- pacing,
- moment selection,
- redundancy,
- B-roll relevance,
- subtitles,
- audio,
- color,
- framing,
- graphics,
- overall watchability.

No mandatory 1-to-5 scorecard is required.

## 17.3 AI self-review is advisory

AI may flag suspected issues before the operator watches.

It may not self-certify the episode as publishable.

---

# 18. Publishing

## 18.1 `[FIRST-PUBLISH]` Package preparation

Existing publish-package generation is reused.

It may propose:

- title candidates,
- description,
- chapters,
- thumbnail notes/candidates where implemented,
- visibility,
- schedule time.

## 18.2 Publication remains explicitly approved

No autonomous public publication without `PUBLICATION_APPROVED` or its v4.4 equivalent.

## 18.3 `[STEADY-STATE]` One real upload proof

After the first episode is accepted as publishable, the existing idempotent YouTube uploader should be validated through one operator-approved real upload or schedule action.

Private/unlisted may be used for the technical proof if preferred.

---

# 19. Metrics

## 19.1 Editorial feasibility metrics

- Must-Keep recall,
- Must-Remove retention,
- wrong keep/remove count,
- redundant-keep count,
- Deep Review lift,
- operator continuation verdict.

## 19.2 Evidence-quality metrics

- Media Intelligence field usefulness/coverage,
- Japanese transcript CER,
- proper-noun recall,
- timestamp alignment,
- evidence/provider failure count,
- deep-review coverage ratio,
- analysis cost and wall-clock.

## 19.3 Product metrics

- Time to First Reviewable Preview,
- Active Human Time,
- wall-clock time,
- number of blocking human sessions,
- number of review corrections,
- number of full rebuilds vs partial rebuilds,
- direct Resolve minutes,
- publishability verdict.

## 19.4 Bootstrap vs steady-state labels

Every real-episode run is labeled as either:

- `bootstrap`, or
- `steady_state`.

AHT targets must not mix these regimes.

For `bootstrap` runs, total Bootstrap AHT is the sum of five separately recorded active-human categories:

1. ordinary edit review,
2. Production Kit bootstrap,
3. taste/reference calibration,
4. troubleshooting,
5. direct Resolve.

Direct Resolve minutes are also reported as a subset of the total, never added a second time. A missing category stays null and fails the gate rather than being estimated.

## 19.5 First Preview Acceptance

Track whether the first preview is:

- acceptable with minor corrections,
- structurally useful but requires major corrections,
- unusable / effectively needs re-edit.

The goal is to shift episodes toward the first category.

---

# 20. v4.4 gates

## Gate V44-0: Editorial Feasibility + Evidence Quality

`[FIRST-PUBLISH]`

Required:

- representative `v44-real-01` frozen,
- operator anchor labels recorded before experiment,
- actual model configured,
- actual evidence path used,
- Experiment A completed,
- Experiment B completed,
- Must-Keep criteria evaluated,
- Deep Review lift measured,
- MCP/analysis evidence quality assessed,
- Japanese transcript sample assessed when speech exists,
- operator continuation verdict recorded,
- if failed, Experiment C / NO-GO diagnosis executed before downstream expansion.

This gate answers:

> Is the core editorial approach good enough to justify finishing the product path?

## Gate V44-1: Real First-Preview Vertical Slice

`[FIRST-PUBLISH]`

Required:

- Cockpit source-folder intake launches real work,
- real ingest/analysis/editorial path completes,
- real Editorial Preview produced,
- no prebuilt MI artifact required from the operator,
- operator can issue at least one natural-language correction,
- correction resolves to a structured command,
- partial rebuild produces updated preview,
- no routine CLI/JSON/Resolve work required.

This gate answers:

> Can the operator get and correct a meaningful first draft through the intended UX?

## Gate V44-2: First Publishable Real Episode

`[FIRST-PUBLISH]`

Required:

- live Resolve build or an explicitly accepted production fallback,
- only episode-applicable finishing domains block acceptance,
- Japanese subtitles validated if applicable,
- Production Kit choices previewed on real footage where applicable,
- technical QC passes,
- editorial QC has no unresolved blocking item,
- operator marks final video `publishable`,
- Bootstrap AHT recorded as the §19.4 five-category total with direct Resolve minutes reported as a subset, and TTFRP recorded.

This is the central v4.4 product gate.

## Gate V44-3: Publication Proof

`[STEADY-STATE]`

Required:

- publish package generated from the accepted episode,
- explicit publication approval,
- one real idempotent upload/schedule path validated,
- remote result recorded.

## Gate V44-4: Steady-State Efficiency

`[STEADY-STATE]`

Run on the operator's next actual episodes, not a forced genre matrix.

Target:

- median AHT <= 30 minutes after bootstrap stabilization,
- normal blocking sessions <= 2,
- first preview not unusable,
- no repeated Must-Keep miss pattern,
- no routine direct Resolve editing.

## Gate V44-5: Learning Activation

`[POST-PUBLISH]`

Only after real episodes provide enough evidence, evaluate activation of:

- derived taste profile aggregation,
- pairwise preference learning,
- automated feature extraction from references,
- channel-level preference updates,
- audience-outcome feedback.

Activation requires a measured benefit hypothesis.

---

# 21. Failure classification

When a real episode fails, classify before adding features.

## 21.1 Evidence failure

Examples:

- missed visual event,
- bad transcript,
- wrong timestamp,
- insufficient temporal density,
- poor shot boundary,
- missing audio reaction context.

Response: improve evidence/provider/sampling.

## 21.2 Editorial reasoning failure

Examples:

- evidence clearly shows value but model removes it,
- repeated statement is kept despite clear redundancy,
- story order is incoherent despite correct source understanding.

Response: model/prompt/context strategy.

## 21.3 Planning/execution failure

Examples:

- good selection but wrong source range,
- B-roll placed at wrong moment,
- subtitle overlaps,
- Resolve operation drift.

Response: deterministic planner/execution/fallback.

## 21.4 Taste mismatch

Examples:

- technically fine but pacing feels wrong,
- color is not the operator's preference,
- graphics feel excessive.

Response: Taste Seed / Production Kit / channel profile, not evidence or source-selection logic.

## 21.5 UX failure

Examples:

- correction cannot be expressed naturally,
- too many confirmations,
- slow rebuild,
- internal identifiers leak into operator workflow.

Response: Cockpit/review loop.

---

# 22. Post-first-publish capabilities

The following existing v4.3 capabilities remain valuable but are not critical-path blockers before Gate V44-2:

- full pairwise preference learning,
- automatic Derived Taste Profile aggregation,
- contradiction-management workflows beyond simple operator clarification,
- audience-outcome optimization,
- generalized three-format acceptance matrix,
- legacy-backend removal decision,
- broad Production Kit expansion,
- effect-family breadth not demanded by actual episodes.

These are not deleted.

They are deliberately prevented from stealing attention from the first publishable real episode.

---

# 23. Security, rights, and safety

Existing v4.3/v4.2 safety controls remain binding unless explicitly superseded.

In particular:

- source media/transcript/OCR are data, not trusted instructions,
- provider/network boundaries remain explicit,
- publication requires operator approval,
- reference-video ingestion must use compliant/legal acquisition paths,
- no arbitrary shell/network/file mutation is granted to creative models,
- accepted decisions remain validated before deterministic execution,
- model/provider/template versions are recorded for product-proof runs.

---

# 24. Documentation authority

The repository's agent instruction files must not continue to direct coding agents to v4.1 or to "foundation first" after v4.4 becomes active.

When v4.4 implementation begins:

- `AGENTS.md` must name PRD v4.4 as the binding product specification,
- `CLAUDE.md` must name PRD v4.4 as the binding product specification,
- both must state the First Publish critical-path rule,
- both must state that broad post-publish subsystems are frozen until Gate V44-2 unless a measured blocker requires them,
- historical v4.3 gate evidence remains immutable rather than rewritten to appear compliant with v4.4.

---

# 25. Definition of Done for v4.4

v4.4 is not done because all unit tests pass.

v4.4 is done when:

```text
real source folder
      |
      v
real analysis + real AI editorial decision
      |
      v
useful first preview
      |
      v
natural-language correction
      |
      v
partial rebuild
      |
      v
real DaVinci finishing
      |
      v
operator says "publishable"
      |
      v
operator-approved YouTube publication path works
```

and the next actual episodes show a credible path toward steady-state AHT <= 30 minutes.

---

# 26. Explicit non-goals before First Publish

Before Gate V44-2, do not optimize for:

- exhaustive support for every YouTube genre,
- complete Resolve feature coverage,
- perfect automatic learning from references,
- autonomous channel strategy,
- automatic public publication,
- a large design-system catalog,
- removal of every legacy fallback,
- synthetic benchmark prestige,
- adding artifacts solely because a cleaner abstraction is possible.

---

# 27. Review-discussion decisions incorporated into v4.4

The following external-review points are explicitly incorporated.

| Review issue | v4.4 decision |
|---|---|
| Core editorial judgment was assumed rather than cheaply disproven | Add V44-0 Editorial Feasibility Spike before more breadth |
| MCP analysis existence does not prove quality | Add real Japanese Evidence Quality Fit |
| Progressive deep review needs proof of value | Compare Experiment A vs B |
| A failed experiment can be rationalized indefinitely | Predeclare criteria and bounded NO-GO ladder |
| Taste learning was on the critical path | Keep lightweight Taste Seed v0; freeze heavy aggregation/pairwise pre-publish |
| Three predetermined formats are product-company thinking | Validate the operator's actual next episodes instead |
| Production Kit shifts design labor upstream | Label bootstrap separately and preview candidates on real footage |
| Full PRD can cause coding agents to overbuild | Add phase tags, artifact-growth limit, and hard First Publish discipline |
| Seven domains can become a checkbox exercise | Require explicit applicability and separate publishability verdict |
| Natural-language correction is a core human-cost driver | Move review loop into the First Publish critical path |
| Synthetic Cockpit acceptance can hide integration gaps | Require source-folder-to-preview live vertical slice |

---

# 28. Final product statement

v4.4 does not ask whether davinci-agent contains enough architecture.

It asks whether the existing architecture can finally disappear behind the experience the operator wanted from the beginning:

> I give it my footage and tell it what this episode is about. It understands what matters, makes a strong first edit in my taste, finishes it properly in DaVinci Resolve, lets me correct it in ordinary language, and gets it ready for YouTube without turning me into the pipeline operator.

Every v4.4 implementation decision should shorten the distance to that statement.

---

# 29. Decision addendum: first-publish subtitle fallback (2026-09-12, codex ruling)

V44-0 measured state (evidence: capabilities/v4.4/product-proof/v44-0/): editorial judgment diagnostic evidence is valid (must-keep recall 77/77, catastrophic 0, operator continuation YES — r5-diagnostic-summary.json); fully-automatic ASR is UNSOLVED against the frozen bars (best auto text = full-audio whisper CER 0.1147 > 0.10; best timing = Gemini word intervals, operator-approved full-episode, p95 0.0 in the interval-locked proof — asr-cut-hybrid-proof.json). Numeric thresholds are UNCHANGED and auto-ASR remains recorded as FAIL.

Decision: for the First Publish bootstrap ONLY, the operator-verified vNext subtitle source (operator-confirmed texts + raw Gemini utterance times, display padding ±0.5s kept separate from measurement anchors — reference-vnext-scoring.json) is adopted as an explicit human-corrected fallback via the existing corrected-transcript input path. No new provider, framework, or artifact type. The human time spent on this correction counts toward Bootstrap Active Human Time. Steady-state automatic-subtitle improvement is a post-First-Publish tracking item (§26). V44-1/V44-2 production routes proceed.

# 30. Decision addendum: normalize verification levels, standard vs full-decode (2026-09-12, codex ruling)

Why: the full-file decoded-video sha256 costs ~50.3s on the 282s 1080p mezzanine (~10.7 min/hr at 1-hour scale) on every normal product run. What: NormalizeRecord gains an honest `verification` field — `standard` (encoded sha256 + ffprobe duration/frame-count/codec + bounded head-frame sanity check, no full decode) for normal product runs, `full_decode` (additionally the decoded-video sha256) for gate/product-proof runs. Integrity: no falsification — a `standard` record carries no decoded hash (schema refuses a mismatched level), old records read as `full_decode` since they carry the hash, and gate/proof paths keep the full evidence unchanged.
