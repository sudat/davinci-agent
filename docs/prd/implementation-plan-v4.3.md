# davinci-agent v4.3 改修実装計画

- Repository: `sudat/davinci-agent`
- Baseline commit inspected: `def2bfc56c0d8295d9ac405bf1613b0721d905db`
- Target PRD: `PRD_v4.3.md` revision 3
- External MCP inspection baseline: `samuelgursky/davinci-resolve-mcp@132e134d3aa25d3d0df6bdf38f051bd29d128211` (README v2.98.3)
- Strategy: preserve the strong production foundation, replace the narrow editorial center, adopt DaVinci Resolve MCP as the default capability provider where it passes production fixtures, learn operator taste from annotated reference videos and approved edits, express that taste through a versioned Channel Production Kit, and validate progress continuously against a real Episode 0 plus publishability/UX outcomes.

## 0. Current-state findings that drive this plan

The current repository is not an empty or failed codebase. It contains a substantial production framework across `analyze`, `artifact_registry`, `artifact_store`, `approvals`, `audit`, `build`, `compile`, `conform`, `contracts`, `editorial`, `evidence`, `execution`, `final_review`, `gates`, `ingest`, `job_runner`, `manual_finalization`, `media_query`, `metrics`, `normalize`, `plan`, `policy`, `presentation`, `preview`, `qa`, `qc`, `release`, `resolve_adapter`, `resolve_bridge`, `review_command`, `security`, `spike`, `toolchain`, and `validate`.

The latest inspected commits are release/recovery hardening, including staging ownership, diagnostic replay, job-runner lease behavior, artifact-registry concurrency, and v4.2 documentation. This confirms that the current project invested heavily in reliability.

The product gap is concentrated in three areas.

### Gap A: editorial evidence is speech-centric

`services/analyze/candidate_models.py` defines analyzer candidates only for:

- pause;
- filler;
- false start.

`services/editorial/models.py` defines declared candidate kinds:

- speech;
- pause;
- filler;
- false start.

`services/editorial/evidence.py` corroborates candidate evidence through only transcript search or silence overlap.

This makes "interesting visual moment" impossible to represent as a first-class editable candidate.

### Gap B: visual analysis is intentionally excluded from selection

`services/analyze/visual_analysis.py` computes scene changes, black spans, blur spans, exposure spans, and contact sheets, but explicitly describes the outputs as evidence-only and "never selection inputs."

The system therefore has visual evidence but not visual editorial intelligence.

### Gap C: Resolve execution duplicates capabilities now available in MCP

`services/resolve_bridge/connection.py` loads and binds the official `DaVinciResolveScript.py` directly.

`services/resolve_adapter/package.py` compiles Timeline IR into a custom `ResolvePackage` based on AppendToTimeline placement, external subtitle handling, audio/presentation sections, and render settings.

`services/build/CleanBuilder` owns staging-project creation, media import, placement, conformance readback, render, subtitle post-step, and cleanup.

This is technically disciplined, but `davinci-resolve-mcp` now already provides broad live Resolve control and workflow helpers, deep media analysis, edit-engine planning, Fusion, Fairlight/audio, grading, rendering, conform, and an advanced offline server. Rebuilding those generic functions creates maintenance cost without directly improving channel editorial quality.


### Gap D: sparse shot analysis can still miss the moment that makes a video worth watching

Deep shot metadata such as `select_potential` and `best_moment` is useful, but a few representative frames can miss a one-second reaction, visual joke, action beat, demonstration detail, or performance timing.

The rebuild therefore needs a progressive-analysis scheduler and a narrower `Moment Deep Review` artifact rather than merely "more shot metadata."

### Gap E: the previous creative Gate rewarded capability count, not finished quality

Requiring subtitles plus several effect families can be satisfied by adding visible effects while audio, color, pacing, or continuity remain mediocre.

Revision 2 replaced effect counting with seven finishing domains; revision 3 keeps that model and adds human publishability validation so technical completion is not mistaken for a good video.

### Gap F: the pipeline has strong internal controls but no first-class operator surface

The repository has approvals, job state, review commands, preview, and release machinery, but a normal episode can still degenerate into CLI + JSON + Finder + Resolve coordination unless one product surface owns the workflow.

A local `Episode Cockpit` is therefore a deliverable, not a nice-to-have.

### Gap G: numeric scoring is too burdensome as the primary way to teach taste

The operator can often say "I like this video's color", "this speaking structure is good", "this pacing is better than that one", or "the subtitles are bad" more reliably than assign calibrated 1-5 values.

The rebuild therefore needs first-class reference-video ingestion, natural-language domain annotations, optional A/B preferences, and an explainable Derived Taste Profile.

### Gap H: capability availability does not guarantee tasteful output

MCP can expose Fusion, grading, Fairlight, titles, transforms, and transitions, but unconstrained tool availability does not create a stable channel look.

A versioned `ChannelProductionKitV1` must turn semantic intents into a small set of tested recipes/templates/presets/assets with parameter bounds, provenance, and fallback.

### Gap I: real footage currently arrives too late in the implementation sequence

Waiting until the three-episode validation phase to discover that the edit still feels weak is product-risky.

One representative `Real Episode 0` must be captured before deep rebuild and rerun after every major milestone. It is the longitudinal product benchmark for Active Human Time, TTFRP, correction burden, fallback usage, and publishability.

### Gap J: starting the job and interruption behavior are not yet owned by UX

A comfortable review player is not enough if the operator still has to assemble inputs manually or answer low-level technical prompts during the run.

The Cockpit must own episode intake and an Interruption Policy that auto-resolves safe issues, records bounded degraded fallbacks, and blocks only for material editorial/rights/publication decisions.

### Gap K: seven-domain completion is necessary but not sufficient

`applied` audio/color/graphics proves a responsibility was handled, not that the resulting video is good.

Real benchmark episodes therefore require a lightweight `PublishabilityReviewV1`. Free-form comments, best/worst timestamps, publishable yes/no, optional ratings, and A/B judgments are accepted; numeric scorecards are not mandatory.

## 1. Migration rule: no big-bang rewrite

The current baseline must be tagged and kept runnable.

Recommended branch/tag setup:

```text
main
  -> freeze current commit as v4.2-foundation-freeze

feature/v4.3-rebaseline
  -> all new schemas and adapters are additive first
```

During migration:

```text
execution_backend = legacy_direct | mcp
analysis_backend  = legacy_local  | mcp | hybrid
editorial_contract = phase1_v1 | multimodal_v2
```

Deletion comes only after real parity evidence.

## 2. Existing module disposition

### KEEP largely intact

| Area | Current path | Decision | Reason |
|---|---|---:|---|
| immutable artifact model | `services/artifact_store`, `artifact_registry` | KEEP | valuable source-of-truth and lineage foundation |
| human approvals | `services/approvals` | KEEP | critical for editorial/final/publication governance |
| audit/evidence | `services/audit`, `services/evidence` | KEEP | needed even more once MCP is introduced |
| ingest | `services/ingest` | KEEP | generic foundation |
| normalize/conform map | `services/normalize`, `services/conform` | KEEP | source coordinate rigor remains valuable |
| job control | `services/job_runner`, `services/gates` | KEEP/EXTEND | orchestration, lease, recovery remain project responsibilities |
| policy/security | `services/policy`, `services/security` | KEEP/EXTEND | MCP and publishing add more policy surfaces |
| review events | `services/review_command` | KEEP/EXTEND | core differentiator |
| metrics | `services/metrics` | KEEP/EXTEND | add editorial-quality and publish metrics |
| release/freeze | `services/release`, `manual_finalization` | KEEP | preserves safe exit path |
| preview | `services/preview` | EXTEND | needs B-roll/subtitle/presentation awareness |
| operator UX | none | NEW | Episode Cockpit from source intake through preview, chat corrections, interruption handling, approvals, publish |
| reference/taste learning | none | NEW | ingest annotated reference videos, derive domain-scoped taste, pairwise preferences |
| production style recipes | partial presentation config | NEW/EXTEND | versioned Channel Production Kit mapped to accepted MCP capabilities |

### EXTEND heavily

| Area | Current path | Decision | Main change |
|---|---|---:|---|
| analysis | `services/analyze` | EXTEND | ingest MCP deep vision, shot semantics, embeddings, audio/editorial evidence |
| media query | `services/media_query` | EXTEND | query shots, best moments, semantic/B-roll matches, audio energy |
| editorial | `services/editorial` | EXTEND/NEW v2 | multimodal director, Story Plan, Moment Selection Plan |
| planning | `services/plan` | EXTEND | semantic edit/presentation intents beyond hard cuts |
| compile | `services/compile` | EXTEND | Timeline IR v2 and MCP execution plan compiler |
| presentation | `services/presentation` | EXTEND | effect intents and channel style recipes |
| QC | `services/qc`, `services/qa` | EXTEND | editorial QC plus MCP technical QC aggregation |
| contracts/schemas | `services/contracts`, `schemas` | EXTEND | v2 artifacts side by side with frozen v1 |

### DEPRECATE AS DEFAULT, RETAIN AS FALLBACK

| Area | Current path | Decision | Replacement |
|---|---|---:|---|
| direct Resolve connection | `services/resolve_bridge` | LEGACY/FALLBACK | `mcp_client` for default production |
| custom Resolve package backend | `services/resolve_adapter` | PARTIAL LEGACY | semantic compiler -> `McpExecutionPlan` |
| direct CleanBuilder | `services/build/CleanBuilder` | LEGACY/FALLBACK | `McpExecutionRunner` |

No file is removed until the corresponding MCP production capability is accepted.

## 3. New target packages

Recommended additive structure:

```text
video-pipeline/services/
  mcp_client/
    client.py
    transport.py
    version_pin.py
    capability_probe.py
    call_models.py
    execution_runner.py
    response_normalize.py

  media_intelligence/
    models.py
    mcp_import.py
    reconcile.py
    shot_identity.py
    query_v2.py
    semantic_index.py
    progressive.py
    triage.py
    moment_review.py
    budget.py
    recall_audit.py

  reference_learning/
    models.py
    ingest.py
    youtube_reference.py
    annotation.py
    domain_extract.py
    feature_extract.py
    pairwise.py
    derive_profile.py
    retrieval.py

  editorial_v2/
    episode_brief.py
    story_plan.py
    moment_models.py
    evidence_v2.py
    prompt_v2.py
    director_v2.py
    proposal_validate.py
    taste_retrieval.py

  production_kit/
    models.py
    registry.py
    recipe_select.py
    capability_bindings.py
    provenance.py

  creative_plan/
    edit_models_v2.py
    presentation_intents.py
    subtitle_plan.py
    audio_finishing.py
    color_finishing.py
    quality_domains.py
    compile_ir_v2.py

  mcp_execution/
    plan_models.py
    compiler.py
    runner.py
    readback.py
    fallback.py

  episode_cockpit/
    app.py
    api.py
    intake.py
    episode_view.py
    progress.py
    player_events.py
    review_chat.py
    reference_library.py
    pairwise_prompt.py
    interruption_policy.py
    approvals.py
    errors.py

  publish/
    models.py
    youtube_client.py
    package_builder.py
    idempotency.py

  channel_learning/
    observation.py
    proposal.py
    holdout.py
```

Do not rename the existing v1 packages at the start. Side-by-side code keeps regression tests and rollback simple.

## 4. Phase 0: Freeze v4.2 and run MCP Fit Test

This phase happens before changing editorial behavior.

### 4.1 Freeze current baseline

Tasks:

1. create tag `v4.2-foundation-freeze` at `def2bfc...` or a later explicitly reviewed SHA;
2. export current F1-F4/release evidence if not already finalized;
3. record current test counts and live Resolve fixture results;
4. record target Mac hardware, OS, Resolve Studio version/build;
5. preserve `legacy_direct` as a known-good backend.

Exit criteria:

- current release path can be rebuilt from the frozen commit;
- no v4.3 change modifies historical gate evidence.

### 4.2 Install and pin DaVinci Resolve MCP

Pin exact release/commit.

Record:

- core MCP version;
- advanced MCP version;
- server mode;
- Python/Node versions;
- ffmpeg availability;
- optional analysis dependencies;
- Resolve version/build.

### 4.3 Capability Fit matrix

Create `capabilities/v4.3/mcp-fit.json`.

Minimum probes:

- project/timeline creation;
- import media;
- exact source-range placement;
- source/record readback;
- subtitle capability;
- title/Text+;
- Fusion template insertion;
- clip transform/punch-in;
- at least one transition path;
- audio property operation;
- voice isolation capability if supported by target version;
- BGM track and level/ducking path;
- color/grade preset or DRX path;
- render configuration;
- render job lifecycle;
- gaps/overlaps/missing media checks;
- conform source ranges;
- analysis standard pass;
- deep shot analysis;
- edit-engine selects;
- alternate-shot/similarity support;
- advanced delivery QC.

Each row gets:

```json
{
  "capability": "fusion_lower_third_v1",
  "provider": "davinci-resolve-mcp",
  "provider_version": "...",
  "resolve_build": "...",
  "fixture": "v43-fusion-lower-third-01",
  "status": "accepted|failed|partial|not_available",
  "readback": "...",
  "fallback": "legacy_direct|template_external|manual"
}
```

Exit criteria:

- all critical capabilities are classified;
- no implementation work begins from an assumption that "MCP probably can't do it."

### 4.4 Capture Real Episode 0 baseline

Before major editorial changes, choose one representative, non-trivial episode from the operator's real footage. Freeze:

- source manifest / hashes;
- Episode Brief;
- target duration intent;
- any must-keep / must-not-misrepresent constraints;
- baseline output produced by the current path as far as practical;
- manual work required to make it publishable.

Record:

- Active Human Time;
- TTFRP / wall-clock time where measurable;
- manual Resolve minutes;
- major wrong keep/remove decisions;
- missing moments;
- finishing deficits;
- interruption points;
- lightweight Publishability Review.

Do not optimize this episode manually into a golden edit fixture. It is a longitudinal product benchmark. Re-run it at the end of Phases 1, 2, 3, 4, and 5.

Exit criteria:

- one real product benchmark exists before deep rebuild;
- its current pain points are turned into prioritized failure cases/backlog items.

## 5. Phase 1: Introduce MCP as a deterministic production provider

### 5.1 MCP client

Implement a non-LLM `McpClient` over stdio/local transport.

The client must:

- launch/connect to pinned server;
- discover tool metadata;
- normalize tool responses;
- timeout safely;
- record every call;
- reject unknown/unpinned server identity;
- expose typed application methods, not arbitrary dict pass-through to production code.

### 5.2 Execution call artifact

Each call records:

```text
provider_version
resolve_version
server_mode
tool_name
action
normalized_params_sha256
request_sha256
response_sha256
started_at
finished_at
status
readback_refs
```

This is how the project keeps traceability without recreating the tool implementation.

### 5.3 Single writer integration

Reuse the existing job/build lease.

Only `McpExecutionRunner` may issue mutating MCP calls in normal production.

Interactive LLM MCP use is a separate assisted mode and cannot silently mutate a production job.

### 5.4 Legacy backend parity harness

For base-cut fixtures, run:

```text
same Timeline IR
  -> legacy_direct
  -> mcp
```

Compare:

- source IDs;
- source in/out;
- record positions;
- track mapping;
- duration;
- render properties.

Exit criteria:

- base-cut and render parity accepted;
- rollback switch tested;
- Episode 0 can complete the same base execution path through MCP and records the delta from baseline.

## 6. Phase 2: Media Intelligence v2, mostly by reuse

Do not start by building new CV models.

### 6.1 Import MCP analysis

Use MCP's source-safe analysis and deep-shot analysis as the first provider.

Normalize fields into `MediaIntelligenceArtifact`.

MCP deep vision already provides useful groups such as:

- shot size/framing;
- camera motion;
- lighting/color mood;
- primary subject/action/location;
- editorial role;
- select potential;
- best moment;
- pacing/stillness;
- cut-in/cut-out quality;
- cut compatibility;
- confidence.

### 6.2 Preserve existing local deterministic measurements

Keep current local facts that are strong and cheap:

- black;
- blur;
- exposure;
- scene changes if still useful;
- audio loudness;
- silence;
- conform/timebase measurements.

Reconcile both providers into one canonical artifact.

### 6.3 Add semantic visual search only if MCP path is accepted

Prefer MCP's optional visual similarity/open_clip path or exported analysis DB.

Custom embeddings are implemented only if the Fit Test fails on retrieval quality or integration.

### 6.4 MediaQueryApi v2

Add typed methods:

- `shots()`;
- `shot_detail()`;
- `best_moments()`;
- `transcript_range()`;
- `semantic_shot_search()`;
- `similar_shots()`;
- `visual_quality_ranges()`;
- `audio_energy_ranges()`;
- `scene_summary()`.

Keep row/token budgets and read-only restrictions from the current API.

### 6.5 Progressive-analysis scheduler

Add `services/media_intelligence/progressive.py` and `budget.py`.

Execution stages:

```text
Universal Pass over 100% of source
  -> Editorial Triage over 100% of shots
  -> Moment Deep Review on selected windows
  -> Recall-audit deep review on a small low-score sample
```

The triage score is not a final edit score. It decides where to spend analysis budget.

Record an `AnalysisBudgetV1` artifact with:

- source duration;
- standard-analysis wall clock;
- deep-review candidate windows and trigger reason;
- reviewed source seconds;
- frame/token/cost counters;
- cache hits;
- policy limits;
- any budget expansion and why.

Default deep-review coverage target is <= 25% of source duration. The scheduler may exceed it for uncertainty/story-critical reasons, but the reason must be explicit.

### 6.6 Moment Deep Review

Add `MomentDeepReviewV1`.

For each selected window:

1. preserve exact source-frame coordinates;
2. include neighboring-shot context;
3. gather a denser frame bundle or provider-equivalent short-window evidence;
4. attach transcript/audio context;
5. ask for moment-level action/reaction/timing/value and best sub-span;
6. store confidence + provider lineage;
7. make the result queryable by Editorial v2.

Deep review remains proposal/evidence only. It never emits timeline mutations.

### 6.7 Recall-audit sentinel

Stratify a small sample from low coarse-score shots and deep-review it.

Track how often the sentinel finds a moment that a human or deep reviewer rates valuable. A repeated miss pattern fails the Media Intelligence Gate even if high-score candidates look good.

### 6.8 Responsiveness benchmark

Before freezing the Gate threshold, measure on the reference production machine:

- TTFRP for 30, 90, and 180 source-minute fixtures/episodes;
- universal-pass throughput;
- deep-review throughput;
- cache/re-run speed after a local correction;
- provider token/frame cost.

Initial product targets are the PRD SLOs. If they are not feasible, record the measured threshold and quality tradeoff explicitly.

### 6.9 Test fixtures

Use three source classes immediately:

- person speaking with B-roll;
- visually driven sequence with minimal speech;
- screen/product/action material.

Exit criteria:

- a useful non-verbal moment is retrievable as evidence;
- B-roll candidates can be retrieved by meaning;
- no raw MCP internal DB becomes the authoritative artifact.

## 7. Phase 3: Replace Phase-1 Editorial Director with multimodal v2

This is the main product-value phase.

### 7.1 Keep proposal-only authority model

The existing Director has a good safety boundary: it proposes and cannot commit, control Resolve, or mutate job state.

Preserve that.

Change what it can reason about.

### 7.2 New v2 contracts

Do not mutate frozen `DeclaredCandidate` or `SelectionPlanProposal` v1.

Add:

- `EpisodeBriefV1`;
- `StoryPlanV1`;
- `MomentCandidateV2`;
- `MomentSelectionProposalV2`;
- `CreativeEditPlanProposalV2`.

### 7.3 Candidate types

At minimum:

```text
speech
reaction
action
establishing
b_roll
insert
product_demo
screen_demo
ambient
transition
still
alternate_take
pause
```

### 7.4 Evidence v2

A moment may be corroborated by:

- transcript;
- deep-shot vision;
- exact frames;
- audio measurements;
- similarity results;
- scene metadata;
- source quality facts.

The "no invented IDs/spans" rule remains.

### 7.5 Reference Preference Learning

Implement first-class reference learning before asking the operator to score early episodes numerically.

New schemas:

- `ReferenceSourceV1`;
- `ReferenceAnnotationV1`;
- `PairwisePreferenceV1`;
- `DerivedTasteProfileV1`;
- `ReferenceLibraryV1`.

Accepted inputs:

- local reference video;
- YouTube URL through a compliant analysis/access path;
- operator-owned previous render/timeline;
- timestamp-scoped video/audio/still reference.

The Cockpit accepts free-form annotations such as:

```text
"I like only the color in this video."
"This speaking/story structure is strong."
"Pacing good, subtitles bad."
"This B-roll density is about right."
```

`domain_extract.py` must convert those into explicit domain-scoped preference evidence. Unspecified domains remain unspecified.

`feature_extract.py` then derives only features relevant to the named domains: story structure, pacing, color, subtitle behavior, B-roll density, framing/graphics, or audio.

Implement optional A/B comparison:

```text
Reference A vs Reference B
Domain: pacing
Choice: B
Reason: A feels too busy
```

Pairwise prompts are opportunistic, not a mandatory questionnaire. They appear only when preference ambiguity is materially affecting the plan or when explicitly requested.

`DerivedTasteProfileV1` aggregates:

- explicit Channel Profile rules;
- approved reference annotations/features;
- approved edit history;
- negative examples;
- pairwise results;
- confidence + provenance;
- unresolved contradictions.

Editorial v2 retrieves only relevant preference evidence for the current decision. A taste-influenced decision must cite the reference/profile evidence that changed it.

Acceptance tests:

- "color only" annotation affects color intent but does not alter subtitle/pacing preference;
- a negative subtitle annotation prevents that subtitle recipe from being preferred;
- an A/B pacing comparison moves pacing preference in the expected direction;
- reference evidence cannot invent source IDs/spans;
- a local supplied file path always works; YouTube URL failures degrade to a clear request for a supplied file rather than a cryptic pipeline failure.

### 7.6 Three-stage editorial planning

Pass A: Story Plan

- decide structure before exact cuts;
- map relevant source themes/moments to story blocks.

Pass B: Moment Selection

- choose exact moments and alternates;
- score reason for keep/remove;
- identify B-roll opportunities.

Pass C: Creative Edit Plan

- generate presentation/audio/subtitle intents after structural plan is committed.

### 7.7 Do not blindly reuse MCP edit_engine as the final editor

MCP edit-engine selects/tighten/swap is useful as a candidate generator and baseline.

Use it as additional evidence/proposal:

```text
MCP selects/tighten/swap
      -> candidate evidence
      -> davinci-agent Editorial Director
      -> committed channel-aware plan
```

This preserves channel-specific objectives and review lineage.

Exit criteria:

- Director can retain a valuable non-speech shot;
- Director can remove a boring section that is not merely silence/filler;
- Director can select semantically relevant B-roll;
- Story Plan contains coherent blocks;
- every selected source span is evidence-backed;
- Episode 0 is rerun and Publishability Review records whether taste/reference changes improved the actual edit.

## 8. Phase 4: Creative Edit Plan, subtitles, effects, and MCP build

### 8.1 Timeline IR v2

Extend IR to represent:

- A-roll and B-roll roles;
- multiple video tracks;
- stills/graphics;
- subtitles;
- title/lower-third/chapter intents;
- transform/punch-in intents;
- transition intents;
- audio role/cue intents;
- color look intents;
- Decision ID links.

### 8.2 Subtitle v2

Implement full subtitle flow:

- ASR timing source;
- Japanese punctuation/normalization;
- line breaking;
- reading-speed limits;
- edit reconciliation after cuts;
- style profile;
- native/Text+/external fallback;
- Review Command edits.

Acceptance target for first three real episodes:

- subtitle timing coverage 100% of intended dialogue;
- no overlap/out-of-order cues;
- correction burden measured;
- proper nouns dictionary can be applied without re-running ASR.

### 8.3 Seven-domain quality contracts

Add `QualityDomainStatus` and `QualityDomainReportV1`.

Every episode must explicitly evaluate:

1. editorial construction;
2. subtitle;
3. audio finishing;
4. color finishing;
5. framing/motion;
6. graphics/presentation;
7. delivery/QC.

A domain is `applied`, `intentionally_not_needed`, `manual_fallback_required`, or `blocked`.

The Gate does not count effect families. It checks whether each relevant finishing responsibility was handled.

### 8.4 Audio finishing

Implement `AudioFinishingPlanV1` and MCP compilation for the accepted ladder:

```text
dialogue cleanup/noise handling
-> dialogue level normalization
-> optional EQ/compression/voice isolation
-> BGM placement
-> ducking
-> ambience preservation/repair
-> optional SFX
-> loudness/peak QC
```

Use Fairlight/live MCP/advanced audio when accepted. Keep external mix fallback.

Acceptance fixtures must include clean dialogue that should receive minimal processing, noisy dialogue, dialogue + BGM ducking, and an ambience-led visual sequence.

### 8.5 Color finishing

Implement `ColorFinishingPlanV1`.

Separate:

- exposure/WB correction;
- camera/shot matching;
- channel/episode look;
- reference/skin/product sanity checks where relevant;
- visual QC.

Prefer MCP live grading and calibrated advanced DRX/QC instead of building a second grading engine.

Acceptance fixtures must prove match consistency across at least two source clips/cameras and prove that a justified no-op does not introduce unnecessary grade changes.

### 8.6 Channel Production Kit

Implement `ChannelProductionKitV1` as a versioned registry of tested production recipes.

Initial minimum kit:

```text
subtitle/default
subtitle/emphasis
title/opening
title/lower-third
motion/punch-in
motion/screen-highlight
transition/default
audio/dialogue-chain
audio/bgm-ducking
color/technical-normalize
color/channel-look
branding/fonts-logo-safe-margins
```

Each recipe must declare:

- semantic intent;
- accepted MCP capability bindings;
- parameter bounds;
- applicability conditions;
- relevant taste domains;
- preview fixture/readback;
- provenance/license;
- fallback.

The creative planner emits semantic style intent. `production_kit.recipe_select` chooses a tested recipe. The execution compiler then emits typed MCP operations. Do not let the LLM invent arbitrary Fusion/grade/audio chains in normal production.

Reference-derived taste may change recipe selection or parameters only inside accepted bounds.

### 8.7 Presentation Intent compiler

Map semantic intents such as title/lower-third, punch-in, B-roll cutaway, transition, PiP, keyword text, screen highlight, and SFX accent to accepted MCP capabilities.

No effect is inserted merely to satisfy a Gate.

### 8.8 MCP Execution Plan

Add `services/mcp_execution/compiler.py`.

The compiler translates IR/intents to exact typed MCP operations and expected readback.

The LLM never emits raw MCP calls into the committed plan.

### 8.9 Technical QC aggregation

Prefer MCP/advanced server for generic checks.

The project QC report points to provider evidence rather than reimplementing every detector.

### 8.10 Editorial QC

Add render/proxy-based checks for:

- duplicate content;
- abrupt narrative jump;
- awkward cut timing;
- irrelevant B-roll;
- subtitle mismatch;
- excessive effect density;
- long low-value segment;
- audio transition problems.

### 8.11 Quality-domain Gate

Before Final Review, aggregate actual execution/QC into `QualityDomainReportV1`.

Reject publication while any domain is `blocked`. Surface `manual_fallback_required` in the Episode Cockpit.

Exit criteria:

- raw mixed footage -> approved creative plan -> MCP Resolve build -> render/QC works end to end;
- all seven quality domains have explicit statuses;
- subtitle/audio/color/framing/graphics are not silently skipped;
- no effect-count quota is used.

### 8.12 Publishability Review

Add `PublishabilityReviewV1` for Episode 0 and benchmark episodes.

Human input schema must allow sparse feedback:

- publishable as-is / after small corrections / not yet;
- free-form overall comment;
- best timestamp/comment;
- weakest timestamp/comment;
- optional domain ratings;
- optional A/B comparison.

The AI may pre-score or summarize, but human judgment is authoritative during calibration. The operator is never required to complete all fields.

Track whether Episode 0 requires fewer/lighter corrections after this phase than before. Seven-domain completion without improved publishability is a phase failure signal.

## 9. Phase 5: Build Episode Cockpit and UX flow

This phase turns the production framework into an operator workflow.

### 9.1 Episode intake + local application boundary

The first Cockpit workflow is:

```text
Choose/drop source folder
-> enter natural-language brief
-> optional target length/profile/reference inputs
-> Create video
```

Do not expose artifact schemas or required internal IDs in normal intake. Advanced options are collapsible.


Implement a loopback-only local web app. Reuse existing job state, preview, approvals, Review Events, and publish services rather than creating a second state machine.

Minimum routes/views:

- episode list and current status;
- Episode Brief view/edit before run;
- stage progress with completed/remaining units;
- Editorial/Presentation Preview player;
- flagged review list with timestamp jump;
- review chat;
- parsed structured command preview when ambiguity is material;
- rebuild/retry;
- final approval;
- publication approval and upload status.

### 9.2 Human-stop budget

Internal approval purposes remain separate in storage, but compatible approvals are bundled in the UX.

Normal target:

1. editorial/presentation review session;
2. final/publication review session.

Privacy/rights/manual-fallback exceptions may create an extra stop, but they must be grouped and explained.

### 9.3 Interruption Policy

Implement typed interruption classification:

```text
SAFE_AUTO_RESOLVE
DEGRADED_BUT_RECOVERABLE
HUMAN_DECISION_REQUIRED
```

Rules:

- safe accepted fallback/retry continues automatically and is logged;
- bounded degraded fallback continues and is surfaced in final review;
- only material meaning, rights/privacy, destructive ambiguity, manual-finalization quality impact, or publication authority blocks the user;
- low-confidence candidates should normally be omitted/flagged for grouped review rather than cause individual popups.

Acceptance: inject subtitle-path fallback, transient MCP failure, unavailable optional effect, low-confidence B-roll, rights ambiguity, and publication approval; verify only the last two classes that truly require human authority block.

### 9.4 Recovery UX

The UI must survive process/browser restart by reading authoritative job/artifact state.

Errors must display:

- failed stage/capability;
- affected output;
- whether retry is safe;
- fallback available;
- next action.

No raw stack trace is the primary operator message.

### 9.5 UX acceptance

Run one full episode without terminal commands after server launch.

Acceptance:

- a normal episode starts from source selection + natural-language brief + one Start action, with references optional;
- no normal JSON inspection;
- all review flags timestamp-jump correctly;
- one correction triggers only affected rebuild work when lineage allows;
- restart/resume works;
- safe and bounded degraded fallbacks do not generate avoidable blocking prompts;
- normal blocking review sessions <= 2 before publication;
- Episode 0 is run from intake through approval in the Cockpit and its AHT/publishability delta is recorded.

## 10. Phase 6: Expand real-episode validation beyond Episode 0

This phase is not a synthetic test phase. Episode 0 has already been exercised throughout the rebuild.

Now add three real episodes with materially different footage so coverage expands beyond the longitudinal benchmark instead of waiting until this phase for the first real test.

### 10.1 Episode A: talking-head + B-roll

Expected:

- remove weak/redundant speech;
- preserve strong explanations;
- insert relevant B-roll;
- subtitles;
- titles/punch-ins/music;
- final render.

### 10.2 Episode B: visual-first vlog/travel/product

Expected:

- select high-value non-verbal moments;
- remove boring/duplicative shots;
- build visual sequence;
- preserve ambience or music intentionally;
- optional narration/text;
- use transitions selectively.

### 10.3 Episode C: mixed media

Include at least two of:

- talking head;
- screen recording;
- voice-over;
- B-roll;
- product demo;
- still images.

Expected:

- cross-source structure makes sense;
- screen/B-roll used when semantically useful;
- subtitle/audio/presentation remain coherent.

### 10.4 Measurement per episode

Record:

- total source minutes;
- output minutes;
- TTFRP;
- total wall-clock time;
- universal-pass and deep-review wall-clock;
- deep-review source-minute ratio;
- vision frames/tokens/cost;
- cache-hit ratio;
- human review minutes;
- number of human blocking sessions;
- manual Resolve edit minutes;
- number of keep/remove corrections;
- B-roll corrections;
- subtitle corrections;
- effect corrections;
- missed valuable moments;
- blocking defects;
- manual fallback capabilities;
- seven-domain quality statuses;
- Publishability Review result and qualitative reasons;
- reference/taste evidence used by the plan;
- Production Kit recipes used, overridden, or manually corrected;
- interruption count by `SAFE_AUTO_RESOLVE`, `DEGRADED_BUT_RECOVERABLE`, and `HUMAN_DECISION_REQUIRED`;
- whether any CLI/JSON/direct-Resolve operation was required.

Do not claim the 30-minute target from three episodes. The purpose here is product-scope validation.

Exit criteria:

- no episode fails merely because it is not talking-head;
- at least 80% of requested normal YouTube edit actions are automated or handled through an explicit MCP-supported path;
- each remaining manual item has a capability reason.

## 11. Phase 7: YouTube publish path

### 11.1 New publish package

Add `PublishPackageV1` with:

- render hash;
- title;
- description;
- chapters;
- thumbnail;
- visibility;
- schedule;
- playlist;
- approval refs;
- rights/privacy refs;
- idempotency key;
- resulting remote video ID.

### 11.2 YouTube publisher

Implement OAuth-backed uploader outside model context.

Requirements:

- credential alias only in artifacts;
- private/unlisted test upload first;
- retry-safe idempotency;
- progress/recovery;
- no duplicate upload after network failure;
- Publication Approval required before public publish.

### 11.3 Packaging AI

Optional LLM proposal for:

- title alternatives;
- description;
- chapters;
- thumbnail brief.

Human or channel policy commits the final package.

Exit criteria:

- private/unlisted upload completes and can be retried safely;
- public transition remains approval-bound.

## 12. Phase 8: Channel learning and performance loop

### 12.1 Editorial + reference preference learning

Unify four evidence classes without collapsing them:

- explicit Channel Profile rules;
- approved Reference Annotations / Derived Taste Profile;
- approved Review Events / final edit behavior;
- audience outcomes.

Generate `ChannelProfileChangeProposal` only from explicit request or repeated approved evidence.

Reference learning requirements:

- new reference URL/local file can be added from the Cockpit;
- free-form comment can be corrected if domain extraction is wrong;
- derived preferences retain provenance;
- contradictory references remain visible;
- optional A/B comparison can resolve ambiguity;
- no single external reference becomes a hard global rule automatically.

Scoring remains optional. Natural comments and approved edits are valid primary supervision.

### 12.2 Audience outcome ingestion

Add `PerformanceObservation`.

Initial fields can be manually imported if API integration is not yet justified:

- views;
- CTR;
- average view duration;
- average percentage viewed;
- notable retention points;
- common comment themes.

### 12.3 Never auto-optimize from a single metric

The system must not automatically shorten every intro because one episode dipped at 30 seconds.

Audience outcome creates a learning proposal, not a silent rule mutation.

Exit criteria:

- one approved preference change affects later planning;
- one audience observation can create a proposal without modifying profile automatically.

## 13. Phase 9: Production KPI evaluation and cleanup

After scope validation, run at least 10 real episodes across the supported envelope.

Only then evaluate:

- median Active Human Time <= 30 min;
- P90 <= 60 min;
- manual Resolve editing trend;
- first-preview acceptance trend;
- TTFRP P50/P90 by source-duration bucket;
- deep-review ratio and analysis cost per source minute;
- human blocking-session count;
- Episode Cockpit completion rate without CLI/JSON/direct Resolve;
- seven-domain fallback/block rate;
- Publishability Review trend and publishable-after-first-review rate;
- reference domain-attribution error and unintended cross-domain inference rate;
- Production Kit recipe override/manual-style-fix rate;
- interruptions per episode by policy class;
- manual fallback rate;
- coverage by footage type.

At this point decide which legacy direct-builder code to remove.

Remove code only when:

- MCP production capability has passed at least three real uses;
- fallback is no longer required;
- no unique conformance/guard behavior would be lost.

## 14. Detailed implementation backlog

### P0: rebaseline

- [ ] freeze v4.2 commit/tag
- [ ] add `PRD_v4.3.md`
- [ ] add v4.3 capability matrix folder
- [ ] add MCP version pin file
- [ ] implement MCP doctor/probe command
- [ ] create backend feature flags
- [ ] create parity test harness

### P0: Media Intelligence

- [ ] MCP standard analysis adapter
- [ ] MCP deep-shot adapter
- [ ] canonical `MediaIntelligenceArtifact`
- [ ] shot identity reconciliation with current Source Manifest/Conform Map
- [ ] MediaQueryApi v2
- [ ] non-speech moment retrieval tests
- [ ] semantic B-roll retrieval tests
- [ ] Progressive Analysis scheduler
- [ ] AnalysisBudgetV1 artifact
- [ ] MomentDeepReviewV1 artifact + query path
- [ ] recall-audit sentinel sampling
- [ ] TTFRP/cost benchmark harness

### P0: Editorial v2

- [ ] Episode Brief schema
- [ ] Story Plan schema
- [ ] Moment Candidate v2 schema
- [ ] multimodal evidence bundle
- [ ] Director prompt v2
- [ ] hallucinated-ID/span validation
- [ ] Moment Selection commit path
- [ ] Review Event compatibility
- [ ] ReferenceSourceV1 / ReferenceAnnotationV1 / ReferenceLibraryV1
- [ ] local reference video ingest
- [ ] compliant YouTube reference path + clear local-file fallback
- [ ] free-form domain/polarity extraction
- [ ] domain-specific feature extraction
- [ ] DerivedTasteProfileV1
- [ ] PairwisePreferenceV1 + optional Cockpit A/B flow
- [ ] relevant taste/reference retrieval
- [ ] positive/negative/mixed reference acceptance tests
- [ ] no-cross-domain-inference tests

### P0: Creative build

- [ ] Timeline IR v2
- [ ] subtitle plan v2
- [ ] presentation intent schema
- [ ] MCP execution plan schema
- [ ] deterministic compiler to MCP actions
- [ ] single-writer MCP runner
- [ ] readback mapper to Build Report
- [ ] legacy fallback routing

### P0: finishing quality domains

- [ ] QualityDomainReportV1
- [ ] AudioFinishingPlanV1
- [ ] dialogue cleanup/level recipe
- [ ] BGM/ducking recipe
- [ ] voice-isolation recipe where accepted
- [ ] ambience/SFX policy
- [ ] audio loudness/peak QC integration
- [ ] ColorFinishingPlanV1
- [ ] exposure/WB correction recipe
- [ ] shot/camera matching recipe
- [ ] channel look recipe
- [ ] advanced DRX/QC adapter
- [ ] lower-third/title recipe
- [ ] punch-in/reframe recipe
- [ ] B-roll overlay recipe
- [ ] verified transition recipe
- [ ] style density guard
- [ ] ChannelProductionKitV1 registry
- [ ] recipe capability binding + parameter bounds
- [ ] recipe provenance/license model
- [ ] recipe selection from semantic/taste intent
- [ ] Production Kit preview fixtures
- [ ] PublishabilityReviewV1

### P1: editorial QC

- [ ] render/proxy sampling
- [ ] duplicate/repetition candidate
- [ ] awkward-cut candidate
- [ ] B-roll relevance candidate
- [ ] subtitle mismatch candidate
- [ ] audio transition candidate
- [ ] structured QC review commands

### P0: Episode Cockpit UX

- [ ] local loopback web app shell
- [ ] new-episode source-folder/drop intake
- [ ] natural-language brief + profile/default selection
- [ ] optional reference URL/file attachment
- [ ] episode status/progress API
- [ ] preview player + timestamp flags
- [ ] review chat -> structured command flow
- [ ] affected-stage rebuild/retry action
- [ ] approval bundling view
- [ ] resume-after-restart test
- [ ] human-readable failure/fallback cards
- [ ] final/publication approval view
- [ ] typed Interruption Policy engine
- [ ] grouped non-blocking fallback notices
- [ ] reference annotation / pairwise calibration surface

### P1: publishing

- [ ] Publish Package
- [ ] YouTube uploader
- [ ] upload idempotency
- [ ] publication approval wiring
- [ ] upload result artifact

### P0: longitudinal real-episode validation

- [ ] freeze Real Episode 0 source/brief
- [ ] baseline current AHT/manual effort/publishability
- [ ] rerun harness after Phases 1-5
- [ ] comparable Episode 0 metric report

### P2: learning

- [ ] performance observation
- [ ] profile change proposal
- [ ] holdout evaluation
- [ ] outcome vs preference separation

## 15. Test strategy

### Unit tests

Continue strict Pydantic/schema tests and property tests for frame/sample conversion.

Add:

- MCP response normalization;
- execution-plan deterministic serialization;
- provider version drift refusal;
- MomentCandidate ID derivation;
- Story Plan constraints;
- progressive-analysis routing and budget accounting;
- Moment Deep Review exact-window identity;
- Reference annotation domain/polarity extraction;
- no unintended cross-domain preference inference;
- Derived Taste Profile provenance/contradiction handling;
- pairwise preference update/retrieval;
- subtitle cue split rules;
- effect intent compilation;
- AudioFinishingPlan and ColorFinishingPlan validation;
- seven-domain status completeness;
- Channel Production Kit recipe binding/parameter bounds/provenance;
- Publishability Review sparse-input validation;
- Episode Cockpit intake/defaulting/command parsing/state recovery;
- Interruption Policy classification and blocking behavior;
- publish idempotency.

### Contract tests

Mock MCP with recorded real responses.

Every accepted production capability gets a contract fixture.

### Live Resolve tests

Keep explicit `resolve_live` marker.

Add MCP-live markers separately so failures distinguish:

- Resolve unavailable;
- MCP unavailable;
- capability unsupported;
- behavior drift;
- test failure.

### Real-episode evaluation

Synthetic fixtures prove safety and mechanics.

Only real episodes prove editorial value and UX. Real Episode 0 is run continuously from the start; the three diverse episodes later expand coverage rather than represent the first real test.

For every real episode, preserve:

- source/output duration;
- TTFRP and total wall-clock;
- analysis budget and deep-review ratio;
- correction categories;
- human blocking sessions;
- manual Resolve time;
- seven-domain quality result;
- Cockpit-only completion yes/no;
- missed-valuable-moment audit.

The test report must separate mechanical correctness, editorial quality, and operator UX claims.

## 16. Rollback plan

Every migration phase keeps a last-known-good path.

If MCP production execution fails:

```text
mcp backend fails
  -> retry if classified transient
  -> accepted granular/advanced fallback
  -> legacy_direct backend for supported operation
  -> manual finalization package
```

If Editorial v2 underperforms:

- preserve v1 talking-head path;
- capture correction events;
- do not roll back artifact/control-plane improvements;
- improve evidence/prompt/selection logic using real failure cases.

## 17. What not to do during this rebuild

- Do not delete the existing foundation because the current product value is disappointing.
- Do not build a second deep-vision stack before testing MCP's existing one.
- Do not wrap all 353 MCP tools into custom Python methods in advance.
- Do not let an LLM freely drive production MCP mutations as a substitute for an execution plan.
- Do not keep talking-head restrictions merely because v4.2 fixtures are built around them.
- Do not call the project complete after synthetic gates pass.
- Do not add effects just to demonstrate capability; every effect must be channel-profile controlled and reviewable.
- Do not deep-analyze 100% of footage by default merely because the model can; use progressive attention and measure recall.
- Do not call a CLI-driven workflow "automated" if normal operation still requires terminal/JSON coordination.
- Do not treat audio and color as optional cosmetic effect families; they are first-class finishing domains.
- Do not auto-publish publicly without a bound publication policy/approval.
- Do not force the operator to numerically score taste when a reference + natural-language comment conveys it better.
- Do not infer that every property of a liked reference is liked; preference is domain-scoped.
- Do not treat raw MCP effect capability as channel style; use the tested Channel Production Kit.
- Do not wait until the end of the rebuild to run real footage; keep Episode 0 in the loop.
- Do not interrupt the operator for safe technical fallback decisions.

## 18. Milestone definition

### M0: foundation protected

Current v4.2 is frozen and rollbackable.

### M1: MCP is real, not theoretical

Target Resolve can be controlled through pinned MCP, and core capabilities have acceptance statuses.

### M2: progressive intelligence finds moments, not just shots

A valuable visual-only or timing-sensitive moment can be found, deep-reviewed, explained, and selected without deep-analyzing the entire source by default.

### M3: AI understands the operator's taste without requiring a scorecard

A local/YouTube reference plus free-form domain comment can produce approved, domain-scoped taste evidence. Optional A/B preferences refine ambiguity. The resulting Derived Taste Profile changes planning in an explainable way without contaminating unspecified domains.

### M4: AI can make a real edit plan

The plan can combine story structure, A-roll, B-roll, subtitles, audio/color finishing, framing, and graphics intents.

### M5: Resolve output is finished across seven domains and a stable production vocabulary

The build is not judged by effect count. All seven quality domains are handled, intentionally unnecessary, or explicitly fall back, and visible/audio style is implemented through a versioned Channel Production Kit rather than arbitrary per-episode tool improvisation.

### M5.5: technical completion becomes publishable quality

Real Episode 0 Publishability Review shows that the finished result is becoming something the operator would actually publish, with fewer/smaller corrections than earlier runs.

### M6: operator workflow is comfortable from intake to approval

A real episode can be started from source folder + natural-language brief, then reviewed, corrected, rebuilt, and approved from Episode Cockpit with no normal CLI/JSON/direct Resolve work. Safe fallbacks do not interrupt the operator, and normal blocking sessions remain no more than two before publication.

### M7: talking-head limitation is gone in practice

Episode 0 has improved across repeated milestone runs, and three additional diverse real episodes complete end to end with TTFRP, publishability, taste/reference, quality, interruption, and fallback measurements.

### M8: pipeline reaches YouTube

An approved render can be safely uploaded as private/unlisted, then published under approval policy from the Cockpit.

### M9: channel improves over time

Corrections and audience results can propose, but not silently force, future changes.


## 19. Final implementation objective

The rebuild is successful when the repository stops being "a highly reliable silence/filler cutter" and becomes a production system whose strongest custom code is the part MCP does not provide: channel-aware editorial judgment, structured human correction, cross-episode governance, and end-to-end production orchestration.

Everything generic that MCP can already do should be consumed, verified, and recorded rather than rebuilt. The system is not considered rebuilt until it also produces a seven-domain-finished and human-publishable video, learns the operator's taste from annotated references/approved edits without requiring scorecards, expresses that taste through a tested Channel Production Kit, finds moment-level value beyond transcript/silence, and lets the operator start, review, correct, approve, and publish from one comfortable Episode Cockpit with minimal interruptions.
