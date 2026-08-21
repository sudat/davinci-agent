# AI YouTube Video Production Orchestrator PRD v4.3

- Version: 4.3
- Baseline repository: `sudat/davinci-agent`
- Baseline commit inspected: `def2bfc56c0d8295d9ac405bf1613b0721d905db`
- Previous design: `docs/prd/PRD_v4.1.md`, `docs/prd/PRD_v4.2.md`
- Date: 2026-08-21
- Status: Proposed rebaseline, revision 3
- External MCP inspection baseline: `samuelgursky/davinci-resolve-mcp@132e134d3aa25d3d0df6bdf38f051bd29d128211` (README reports v2.98.3)
- Revision-3 additions: Reference Preference Learning from YouTube/local reference videos and free-form annotations, domain-scoped taste extraction, pairwise preference learning, Publishability Review, Channel Production Kit, Episode Cockpit intake + Interruption Policy, and an early Real Episode 0 vertical-slice loop. Revision-2 additions remain in force.

## 0. This revision exists because the product goal and the implemented capability drifted apart

The current repository has built a serious production-control foundation: immutable artifacts, strict schemas, evidence lineage, approval gates, deterministic frame/sample handling, clean builds, conformance readback, render/QC, release hardening, and recovery behavior.

That work is valuable and should not be discarded.

However, the current editorial capability is much narrower than the product goal. The implementation currently centers on dialogue-derived candidates such as pause, filler, and false start. The Editorial Director consumes a frozen candidate table and bounded transcript/silence evidence. The minimum visual analyzer produces black/blur/exposure/scene-change evidence and contact sheets, but explicitly does not make those visual results selection inputs. The production builder compiles and applies a deterministic base-cut-oriented Resolve package through the official scripting bridge.

This means the system can be technically disciplined while still failing the actual job to be done: turn heterogeneous filmed material into a watchable, engaging YouTube video with good selection, structure, subtitles, B-roll, presentation, audio treatment, effects, and a publish-ready output.

v4.3 changes the center of gravity.

The product is no longer defined as a talking-head pipeline with later visual expansion. It is defined as a general YouTube post-production orchestrator that uses existing DaVinci Resolve MCP capabilities wherever they are sufficient, and reserves custom code for channel-specific editorial intelligence, governance, review, reproducibility, and publishing workflow.

## 1. Product mission

Given one episode's camera files, screen captures, voice-over, B-roll, stills, and audio, the system should:

1. inspect and understand the material;
2. identify moments worth keeping and moments worth removing;
3. construct a coherent story or information flow;
4. create subtitles and presentation elements;
5. use DaVinci Resolve capabilities, primarily through `davinci-resolve-mcp`, to build a polished timeline;
6. run technical and editorial QC;
7. let the human review and correct the result with minimal active time;
8. produce a publish-ready YouTube package and, when explicitly enabled, upload it;
9. learn from approved corrections and published outcomes without silently changing channel rules.

The target is not "AI operates Resolve." The target is "raw footage becomes a useful YouTube video with little human editing labor."

## 2. Product principles

### 2.1 Value before determinism

Reproducibility matters only after the system can produce something worth reproducing.

A perfectly reproducible bad edit is not a successful product. v4.3 therefore ranks priorities as:

1. editorial usefulness;
2. reviewability and correction cost;
3. technical correctness;
4. reproducibility;
5. implementation elegance.

Reproducibility remains a requirement, but it may not be used to prohibit high-value capabilities that can be safely bounded, recorded, and reviewed.

### 2.2 MCP is a capability provider, not an autonomous authority

MCP is a protocol and tool surface. It is not inherently non-deterministic.

The pipeline may call MCP tools from deterministic code. For a committed Edit Plan, the system records the MCP server version, tool name, action, normalized parameters, request hash, response hash, Resolve version, and readback evidence. An LLM does not need to freely improvise MCP calls during production execution.

Therefore the v4.2 argument "MCP is non-deterministic, so production must use a separately reimplemented builder" is rejected as a blanket rule.

The new rule is:

- semantic decisions belong to the editorial layer;
- concrete execution may use MCP;
- production mutations remain single-writer;
- important actions are planned, version-pinned, logged, and read back;
- direct scripting remains a fallback, not the default source of duplicated functionality.

### 2.3 Reuse before reimplementation

`davinci-resolve-mcp` already exposes live Resolve control, source-safe media analysis, deep shot-level editorial analysis, selects/tighten/swap planning, timeline/conform helpers, review markers, grading, Fusion, Fairlight/audio, render, extension/template authoring, plus an optional advanced offline server for conform, project files, grading, delivery QC, provenance, and pipeline operations.

v4.3 must run a reuse test before implementing any overlapping capability.

A new component may be custom-built only when at least one of the following is true:

- MCP does not provide the capability;
- MCP provides it but cannot satisfy the required production acceptance fixture;
- MCP's representation is too weak for channel-level semantics and must be wrapped in a richer canonical contract;
- relying directly on MCP state would make channel governance, approvals, or cross-episode learning unsafe;
- cost, performance, privacy, or licensing makes the MCP path unsuitable.


### 2.4 Progressive attention, not exhaustive deep analysis

General-video support must not mean sending every source minute through the most expensive analysis path.

v4.3 uses a progressive-attention policy:

1. cheap deterministic and standard analysis over all source material;
2. coarse editorial triage over all shots;
3. high-density `Moment Deep Review` only for promising, ambiguous, story-critical, or audit-sampled regions;
4. re-open deeper analysis when the human or Editorial QC indicates a miss.

This policy is part of product quality, not merely a cost optimization. It keeps time-to-preview bounded while reserving model attention for moments where timing, facial reaction, physical action, visual comedy, demonstration detail, or micro-performance matters.

A small recall-audit sample of low-scoring material must also be deep-reviewed so the system can estimate whether its coarse pass is systematically missing valuable moments.

### 2.5 UX is a product requirement

A pipeline that saves editing labor but requires the operator to inspect JSON, remember CLI commands, watch terminal logs, or manually coordinate Resolve is not considered successful.

Normal episode operation must be possible from one `Episode Cockpit` surface. Internal approval records, gates, tool calls, and artifacts may remain granular, but the operator experience should collapse them into a small number of understandable decisions.

The target operator workflow is: provide footage + brief, wait while the system works, review a video, give natural-language corrections, approve the final result, and optionally approve publication.

### 2.6 Taste is learned from examples and comments, not forced scalar scoring

The operator must not be required to translate taste into numeric ratings before the system can learn. A statement such as "I like the color treatment in this video", "the speaking structure here is strong", "the pacing is good but the subtitles are too loud", or "I prefer B over A" is valid editorial supervision.

The system therefore treats reference videos, timestamped or whole-video comments, approved edits, explicit rules, and optional pairwise comparisons as first-class evidence. Numeric scoring remains available for benchmarking but is not the primary interaction model.

Preference evidence is domain-scoped. If the operator says the color is good, the system must not infer that the video's subtitle, pacing, story structure, graphics, or audio are also preferred. Unspecified dimensions stay unspecified unless later evidence supports them.

### 2.7 Real episodes are the product loop, not a final acceptance exercise

Real footage must enter the development loop before the full architecture is complete. v4.3 establishes a representative `Real Episode 0` at the start of migration and re-runs it after each major capability milestone.

The purpose is not to make Episode 0 pass immediately. It is to expose the highest-value product gap early: what still makes the operator spend time, what looks amateur, what editorial decisions are wrong, what interrupts the workflow, and what DaVinci capability is missing or poorly used.

Synthetic fixtures prove mechanics. Repeated Real Episode 0 runs prove that the project is moving toward the operator's actual job to be done.

## 3. Success metrics

### 3.1 Primary metric

Active Human Time after source ingestion.

Measure median and P90 separately for each supported production envelope.

Initial production target after v4.3 stabilization:

- Median Active Human Time: <= 30 minutes
- P90 Active Human Time: <= 60 minutes

This target is not considered achieved until measured on real episodes, not synthetic fixtures.

### 3.2 Editorial and publishability quality metrics

Core metrics:

- First Preview Acceptance Rate
- First Resolve Build Acceptance Rate
- Human correction count per finished minute
- Manual timeline-edit minutes per episode
- Missed Valuable Moment Rate
- Unwanted Moment Survival Rate
- Subtitle correction characters per 1,000 subtitle characters
- B-roll correction count
- Presentation/effect correction count
- Blocking Editorial Defect Rate

v4.3 also adds a human-centered `Publishability Review`. This is not a requirement that the operator fill out a 1-5 scorecard on every episode. The system must accept lightweight feedback such as:

- "publishable as-is" / "not publishable yet";
- free-form comments;
- best/worst timestamp examples;
- domain comments such as "color good, pacing weak";
- optional 1-5 ratings;
- optional A/B preference judgments.

For development benchmark episodes, publishability is summarized across:

- story/structure;
- pacing;
- quality of kept moments;
- removal of low-value material;
- B-roll relevance/naturalness;
- subtitle quality;
- audio quality;
- color/visual consistency;
- graphics/effect taste;
- overall watchability.

The decisive product signal is whether a real episode becomes something the operator is willing to publish after a small number of understandable corrections, not whether every internal domain technically executed.

Reference-learning quality is tracked separately:

- domain-attribution accuracy for free-form reference comments;
- unintended cross-domain inference rate: target 0 for explicit single-domain annotations;
- pairwise preference consistency on repeated comparisons;
- percentage of accepted plans that can cite the reference/taste evidence that influenced them;
- human correction rate for derived taste statements.

### 3.3 Coverage metrics

Coverage is measured by footage composition and required capabilities, not only by genre label.

Track successful automation for episodes containing:

- talking-head A-roll;
- B-roll;
- non-verbal action;
- travel/POV footage;
- product shots;
- screen recording;
- voice-over;
- still images;
- 1-3 camera sources;
- mixed frame rates and VFR phone footage;
- music and ambient audio.

### 3.4 Production metrics

- Technical QC pass rate
- Build recovery success rate
- Rebuild structural conformance
- MCP capability fallback rate
- Manual Finalization rate
- Publish-package completion rate

### 3.5 Responsiveness and analysis-cost metrics

Track these separately from Active Human Time. A user who waits all day for the first reviewable output has a poor workflow even when active labor is low.

- `Time to First Reviewable Preview (TTFRP)` from ingest acceptance to Editorial Preview readiness;
- end-to-end wall-clock time;
- source-minutes processed per wall-clock minute;
- deep-review source-minute ratio;
- vision frames/tokens and external-model cost per source minute;
- analysis cache-hit ratio;
- re-analysis minutes after one Review Command;
- background-stage failure/retry rate.

Initial SLOs for the reference production machine are provisional until Phase 2 benchmarking, then become frozen Gate thresholds. The design target is:

- TTFRP P50 <= 30 minutes for episodes with <= 90 source minutes;
- TTFRP P50 <= 60 minutes for episodes with <= 180 source minutes;
- default Moment Deep Review coverage <= 25% of source duration, unless uncertainty, story-criticality, or recall-audit policy explicitly expands it;
- a local correction should reprocess only affected artifacts/regions whenever dependency lineage allows.

If the measured reference machine cannot meet these thresholds without unacceptable quality loss, the Gate must record the new measured threshold rather than silently pretending the target was met.

### 3.6 UX metrics

- normal-run human blocking points before publication: target <= 2;
- unsolicited mid-pipeline interruptions that could have been safely auto-resolved: target 0;
- CLI commands required for normal episode completion: 0;
- raw JSON/artifact inspection required for normal episode completion: 0;
- direct Resolve manipulation required for an accepted `youtube-general-v1` episode: target 0;
- timestamp-to-review navigation success: 100% for surfaced review items;
- new-episode start requires only source selection, a natural-language brief or defaults, optional references, and one Start action;
- recoverable job restart preserves Episode Brief, review state, and accepted artifacts without user re-entry;
- every surfaced failure provides a human-readable cause, impact, fallback taken or proposed, and next action.

The Cockpit tracks `operator interruptions per episode`, `time from Cockpit open to job start`, and `review session duration` because a low Active Human Time can still feel poor if the system repeatedly asks small technical questions.

## 4. Supported Episode Envelope v1

The old `talking-head-mvp-v1` is retained only as a regression profile. It is no longer the product boundary.

The primary contract becomes `youtube-general-v1`.

### 4.1 Intended coverage

An episode may combine:

- A-roll speech;
- interviews or multiple speakers;
- B-roll and cutaways;
- visually driven sequences with little or no speech;
- product demonstrations;
- travel/POV sequences;
- screen recordings;
- narration/voice-over;
- images or graphics;
- intro/outro assets;
- simple music and sound design.

### 4.2 Initial operating limits

These are qualification limits, not product identity.

- total source duration: target <= 180 minutes for v1;
- output duration: target 3-30 minutes;
- camera sources: 1-3 primary synchronized or unsynchronized sources;
- timeline model: one principal program timeline;
- constant and variable source frame rates allowed through existing normalization/conform layer;
- basic multicamera material allowed if MCP/Resolve capability fixture passes;
- complex reality-show style multicam, heavy compositing, custom 3D Fusion, and advanced motion graphics may fall back to assisted/manual completion.

An episode must not be rejected merely because it is not a talking-head video. Instead, eligibility reports the supported capability percentage and any manual fallback items.

## 5. High-level architecture

```text
Reference Library ----------------------------+
  YouTube URL / local video                  |
  + free-form annotation                    |
  + optional A/B preference                 |
          |                                  |
          v                                  |
Reference Analyzer -> Derived Taste Profile  |
          |                                  |
          +------------------+               |
                             v               |
Episode Intake -> Episode Brief + Channel Profile
  source folder     + Channel Production Kit
  natural-language brief     |
  optional references        |
                |             |
                +-------------+
                      |
                      v
Immutable Originals
                      |
                      v
Ingest / Normalize / Conform Map
                      |
                      v
Progressive Media Intelligence
  Stage 1: universal cheap pass
     - technical facts / ASR / shot boundaries / standard vision / audio
                      |
                      v
  Stage 2: coarse editorial triage
     - shot roles / select potential / story relevance / uncertainty
                      |
                      +-----------------------------+
                      |                             |
                      v                             v
  Stage 3: Moment Deep Review              Recall-audit sample
     - dense multi-frame context           of low-score material
     - transcript + audio context
     - reaction/action/timing review
                      |                             |
                      +---------------+-------------+
                                      v
Canonical Media Intelligence + Moment Review Artifacts
                                      |
                                      v
Editorial Intelligence
  Story Plan -> Moment Selection Plan -> Creative Edit Plan
          ^                 ^                    |
          |                 |                    |
          |         Derived Taste Profile       |
          |         + approved references       |
          |         + approved past decisions   |
          |                                      |
          +---------------- Review corrections --+
                                      |
                                      v
Timeline IR v2 + Quality-Domain Plans
  Edit / Subtitle / Audio / Color / Framing / Graphics / Delivery
          |                     |
          |                     +--> Channel Production Kit
          |                          recipes/templates/assets
          |
          |                     +--> Editorial / Presentation Preview
          |                                  |
          |                             Episode Cockpit
          |                   preview + flags + chat + approvals
          |                     + Interruption Policy
          |                                  |
          +----------------------------------+
                                      |
                                      v
MCP Execution Planner
                                      |
                                      v
Single-Writer MCP Execution Runner
  |                  |
  |                  +--> davinci-resolve-advanced-mcp where appropriate
  +--> davinci-resolve-mcp live server
                                      |
                                      v
DaVinci Resolve Timeline / Render
                                      |
                                      v
Technical QC + Editorial QC + Seven-Domain Quality Gate
                                      |
                                      v
Publishability Review + Episode Cockpit Final Review
                                      |
                                      v
Publish Package -> optional YouTube Upload
                                      |
                                      v
Performance Observation -> Channel Learning Proposal
                                      |
                                      +--> Reference/Taste Profile proposals
```

The existing `services/resolve_bridge` and `services/build/CleanBuilder` are not deleted during migration.

They become `legacy_direct` / diagnostic backends until MCP parity is proven. After parity, only unique fallback logic is retained.

The `Episode Cockpit` is deliberately not a replacement NLE. It is the operator surface from episode intake through preview, corrections, approvals, and publication. Resolve remains the finishing engine behind it.

`Derived Taste Profile` and `Channel Production Kit` solve different problems: the Taste Profile describes what the operator prefers; the Production Kit describes the tested Resolve-ready recipes/assets that can reliably express those preferences.

## 6. Ownership boundaries

### 6.1 davinci-resolve-mcp owns implementation of generic Resolve capabilities when accepted

Examples:

- Resolve application/project/timeline control;
- media import and metadata operations;
- source-safe media analysis;
- transcription integration where suitable;
- deep per-shot visual/editorial descriptors;
- selects/tighten/swap helper planning as candidate-generation signals;
- timeline probing and conform checks;
- title/text and Fusion operations;
- audio/Fairlight operations;
- color and grade helpers;
- render queue control and validation;
- technical delivery QC where the advanced server is a better fit;
- extension/template operations.

The project should not maintain a second general-purpose Resolve SDK wrapper if MCP already covers the same behavior and passes our fixtures.

### 6.2 davinci-agent owns channel-level semantics

The repository remains authoritative for:

- Episode Brief;
- Channel Profile;
- Reference Library, Reference Annotations, Pairwise Preferences, and Derived Taste Profile;
- Channel Production Kit and its recipe/version governance;
- Supported Episode Envelope;
- artifact lineage and approvals;
- Story Plan;
- Moment Selection Plan;
- Creative Edit Plan;
- Timeline IR as the semantic bridge;
- human review commands/events;
- editorial locks;
- channel preference governance;
- cross-episode learning proposals;
- Active Human Time metrics;
- publish approval and publish package;
- policy around when MCP capabilities are allowed;
- Manual Finalization/Freeze.

### 6.3 Legacy direct scripting becomes a fallback lane

The existing `services/resolve_bridge` and `services/build/CleanBuilder` are not deleted during migration.

They become `legacy_direct` / diagnostic backends until MCP parity is proven. After parity, only unique fallback logic is retained.

## 7. Media Intelligence v2

### 7.1 Goal

Turn footage into an evidence graph that an AI editor can reason over.

The current minimum visual analyzer is retained for deterministic technical facts, but visual editorial understanding must no longer be isolated from selection.

### 7.2 Source evidence categories

Each source/shot/moment can carry:

- transcript and word/segment timing;
- speaker identity when available;
- silence and filler evidence;
- loudness/energy/ambient measurements;
- shot boundaries;
- shot size and framing;
- camera movement;
- primary subject and action;
- location and visible text;
- visual quality problems;
- editorial role;
- select potential;
- best moment candidate;
- pacing and stillness type;
- cut-in/cut-out quality;
- visual similarity/embedding references;
- objects or product references;
- face/reaction cues when confidently available;
- provenance and confidence per field.

The MCP deep-shot schema already supplies many of these fields. v4.3 should import them rather than recreate them unless a real-footage gap is demonstrated.

### 7.3 Canonical `MediaIntelligenceArtifact`

The artifact normalizes data from MCP and existing local analyzers without making MCP's internal DB the project-wide source of truth.

Illustrative shape:

```json
{
  "schema_version": "media-intelligence-v2",
  "episode_id": "ep-001",
  "sources": [],
  "shots": [
    {
      "shot_id": "shot-...",
      "source_span": {"start_frame": 1200, "end_frame": 1840},
      "description": "出演者が製品を持ち上げて比較点を説明する",
      "visual": {
        "shot_size": "medium_close",
        "camera_motion": "handheld",
        "quality_flags": []
      },
      "editorial": {
        "role": "coverage",
        "select_potential": "high",
        "best_moment": {"frame": 1510, "why": "比較差が視覚的に分かる"},
        "pacing": "moderate",
        "cuttability": {"in": "clean", "out": "clean"}
      },
      "transcript_refs": ["tr-018"],
      "evidence_refs": ["mcp-analysis:..."],
      "confidence": {"editorial": "medium", "visual": "high"}
    }
  ]
}
```

### 7.4 Query surface v2

The current seven-method MediaQuery surface is too narrow for general editing.

v2 must add bounded, typed queries for:

- shots in range;
- shot editorial attributes;
- transcript by source/range;
- best-moment candidates;
- visually similar shots;
- B-roll candidates matching a concept;
- audio energy/pacing range;
- visual quality issues;
- screen/visible text candidates;
- episode and scene summaries.

The Editorial Director still receives bounded evidence, but "bounded" must not mean "speech-only."


### 7.5 Progressive Analysis policy

Every source is scanned, but not every source region receives equal model attention.

Stage 1, `Universal Pass`, runs across all material and produces cheap evidence: source facts, ASR, shot boundaries, technical visual checks, standard visual summaries, contact frames, and audio measurements.

Stage 2, `Editorial Triage`, assigns coarse values such as story relevance, select potential, novelty, visual usefulness, uncertainty, and candidate role. It is allowed to be approximate because it does not make final keep/remove decisions.

Stage 3, `Moment Deep Review`, is triggered for:

- high-value or high-novelty candidates;
- uncertain candidates that could materially change the story;
- visually driven moments with weak transcript evidence;
- reactions, demonstrations, action, visual comedy, or timing-sensitive beats;
- B-roll candidates near a planned story block;
- Editorial QC flags;
- explicit human review requests;
- recall-audit samples drawn from low-scoring material.

The scheduler records why every deep review was triggered and its analysis cost.

### 7.6 `MomentDeepReviewArtifact`

A deep-reviewed moment is narrower and richer than a shot summary. Its purpose is to capture value that can disappear in sparse frame sampling.

It contains, where available:

- exact source window and neighboring-shot context;
- a denser sequence of representative frames or equivalent short-window visual evidence;
- transcript words overlapping the window;
- audio energy, silence, laughter/reaction, or ambient context when measurable;
- subject/action evolution across the window;
- micro-performance or reaction notes;
- candidate best sub-span rather than only best shot;
- keep/remove rationale candidates;
- cut-in/cut-out handles;
- confidence and uncertainty;
- provider/version/cost lineage.

A Moment Deep Review never commits an edit. It upgrades the evidence available to Editorial Intelligence.

### 7.7 Recall-audit sentinel

To avoid a self-reinforcing coarse-ranking failure, each real episode deep-reviews a small stratified sample of material that the coarse pass scored low.

If the sentinel sample repeatedly discovers valuable missed moments, the coarse triage policy is considered degraded and must be recalibrated before the system can claim a low `Missed Valuable Moment Rate`.

## 8. Editorial Intelligence v2

### 8.1 The largest functional change

The current Editorial Director can only reason over declared candidate kinds `speech`, `pause`, `filler`, and `false_start`, and its evidence corroboration is transcript search or silence overlap.

v4.3 replaces that Phase-1 contract with multimodal editorial planning.

### 8.2 Editorial reasoning dimensions

The AI editor should judge a moment using multiple dimensions, not a single "interesting score."

Recommended dimensions:

- information value;
- story progression;
- novelty;
- emotional energy;
- authenticity;
- humor or surprise where supported;
- visual interest;
- clarity;
- redundancy;
- technical usability;
- continuity/cuttability;
- relationship to the Episode Brief;
- relationship to channel style.

Quiet scenes must not be penalized automatically. A low-energy shot may be essential as tension, reflection, transition, or breathing room.

### 8.3 Episode Brief

Every episode starts with a compact human- or AI-assisted brief.

Required fields:

- audience hypothesis;
- viewer promise;
- episode objective;
- must-include ideas/moments;
- must-not-misrepresent constraints;
- target duration range;
- CTA if any;
- pacing target;
- preferred editing intensity;
- required assets;
- publication constraints.

The brief may be generated from a transcript or user's natural-language direction, but the approved version becomes an artifact.

### 8.4 Reference Preference Learning

Textual Channel Profile rules are not enough to communicate taste, and numeric scoring is too burdensome as the only supervision path. v4.3 therefore replaces the narrow `Reference Edit Set` concept with a broader `Reference Preference Learning` system.

Accepted reference inputs include:

- a YouTube URL when a legally compliant access/analysis path is available;
- a local downloaded/reference video supplied by the operator;
- the operator's own previously approved timelines/renders;
- a timestamp range within any accepted reference;
- a still or audio excerpt when the preference is specifically visual or sonic.

A reference is not automatically a positive example for every dimension. The operator may annotate it in natural language, for example:

```text
"I like the color treatment in this video."
"The speaking/story structure here is strong."
"I like the pacing, but I dislike the subtitles."
"The B-roll density from 03:10-04:00 is about right."
"Do not imitate this transition style."
```

The system converts the comment into a structured `ReferenceAnnotation` with:

- reference source ID;
- optional timestamp range;
- preference domain(s);
- polarity: `like | dislike | neutral | unspecified`;
- operator rationale;
- extracted measurable/semantic features;
- extraction confidence;
- scope: `channel | series | episode`;
- provenance and human approval.

Explicit domain scoping is a hard rule. If the operator praises color, every other domain remains `unspecified` unless the same annotation or other evidence says otherwise.

### 8.5 Reference Analyzer

The analyzer derives useful features from the domain named by the operator rather than storing only the comment text.

Examples:

- story/speaking structure: hook timing, time to thesis, setup/evidence/payoff pattern, example placement, topic-transition cadence, repetition, CTA placement;
- pacing: shot/segment duration distributions, preserved pauses, jump-cut density, information density, silence tolerance;
- color: contrast, exposure tendency, white-balance tendency, saturation, skin/product handling, highlight/shadow behavior, scene-to-scene consistency;
- subtitles: characters per cue, reading speed, line breaks, position, emphasis frequency, typography family, animation density;
- B-roll: frequency, duration, semantic distance from narration, lead/lag timing, cutaway length;
- framing/graphics: punch-in density, crop magnitude, lower-third/title frequency, transition density;
- audio: dialogue-to-music relationship, ambience preservation, ducking behavior, loudness character.

The purpose is not to copy the reference video. It is to convert the operator's qualitative judgment into evidence the planner can use. Copyrighted media is never copied into the output merely because it was used as a reference.

### 8.6 Derived Taste Profile

`DerivedTasteProfileV1` aggregates approved preference evidence into a versioned, explainable profile. It contains:

- explicit rules from Channel Profile;
- domain-scoped reference-derived preferences;
- approved edit history;
- approved negative examples;
- optional pairwise-preference results;
- confidence and supporting evidence per preference;
- contradictions that require human resolution rather than silent averaging.

The profile is primarily retrieval/configuration evidence, not opaque model fine-tuning in v4.3. An editorial decision influenced by taste must be able to cite the relevant preference/reference evidence.

### 8.7 Pairwise Preference Learning

The Cockpit may optionally ask comparisons that are easier than scoring:

```text
Which pacing do you prefer?
[A] Reference A
[B] Reference B

Operator: B, because A feels too busy.
```

Comparisons are domain-specific and sparse. The system does not nag the operator with a training questionnaire. They are used when two plausible styles remain ambiguous, when preferences conflict, or when the operator explicitly wants to calibrate the channel.

Pairwise evidence can refine statements such as:

```text
high information density preferred
BUT
very high jump-cut density disliked
AND
short natural pauses are acceptable
```

### 8.8 Reference Library governance

Reference assets and annotations form a versioned `Reference Library`. References may be positive, negative, mixed, or unspecified by domain.

The system does not silently scrape arbitrary public videos into a training corpus. External references require an explicit, legally appropriate input path and are treated as analysis/reference evidence, not copied production assets.

For model context, the orchestrator retrieves only references/preferences relevant to the current story block and edit operation.

### 8.9 Story Plan

Before choosing exact frame ranges, the system creates a structure.

Example story blocks:

- hook;
- setup;
- problem;
- evidence/demo;
- escalation;
- payoff;
- takeaway;
- CTA.

Vlog/travel episodes may instead use temporal, location, or experience blocks. The schema must allow different story shapes rather than forcing a talking-head chapter model.

### 8.10 Moment Selection Plan v2

Replace dialogue-only candidate semantics with `MomentCandidate`.

Candidate types may include:

- speech;
- reaction;
- action;
- establishing;
- b_roll;
- insert;
- product_demo;
- screen_demo;
- ambient;
- transition;
- graphic;
- still;
- pause;
- alternate_take.

Each decision stores:

- source span;
- story block;
- keep/remove/optional intent;
- viewer value rationale;
- visual/audio/text evidence;
- redundancy group;
- confidence;
- handles;
- must-include/lock state;
- provenance.

### 8.11 Creative Edit Plan

The Edit Plan must express more than a sequence of hard cuts.

It can contain semantic operations:

- primary clip placement;
- B-roll insert/overlay;
- cutaway/reaction;
- J-cut/L-cut intent;
- subtitle track intent;
- title/lower-third/chapter-card intent;
- emphasis punch-in/crop intent;
- still image placement;
- transition intent;
- music cue/ducking intent;
- voice isolation/noise cleanup intent;
- SFX cue intent;
- color-look intent;
- speed-change intent only when capability verified;
- manual-required effect intent.

The plan should specify "what editorial result is wanted" rather than embedding raw Resolve API calls.

## 9. Subtitle system becomes first-class

Automatic subtitles are a core YouTube requirement, not a presentation afterthought.

### 9.1 Subtitle pipeline

1. transcription;
2. timing normalization against edit-source coordinates;
3. text normalization and punctuation;
4. removal/reconciliation after editorial cuts;
5. semantic line breaking;
6. reading-speed checks;
7. channel style application;
8. Resolve/MCP placement or safe external render path;
9. subtitle-vs-audio QC;
10. human correction through Review Commands.

### 9.2 Japanese subtitle requirements

- configurable characters per line;
- configurable lines per cue;
- avoid breaking syntactic units when possible;
- punctuation normalization;
- explicit handling of fillers intentionally retained or removed;
- common proper-noun correction dictionary from Channel Profile;
- editable styling profile;
- cue overlap and timing validation.

### 9.3 Native vs external subtitle strategy

Do not hard-code the current external `mov_text` post-render path as the only production route.

Capability order:

1. MCP/Resolve native subtitle or Text+/template path if accepted by fixture;
2. styled subtitle asset/template path;
3. external ASS/SRT-based render path;
4. manual fallback.

## 10. Seven-domain finishing quality model

### 10.1 Quality is evaluated by finishing domains, not by effect count

v4.3 does not define a good edit as "subtitles + three effects." Counting effects encourages visible gimmicks and allows an otherwise weak video to pass.

Every finished episode is evaluated across seven domains:

| Domain | Required capability | Typical implementation paths |
|---|---|---|
| Editorial construction | A-roll/B-roll, cutaways, timing, J/L-style audio continuity where useful, coherent sequence | Timeline IR + MCP editing |
| Subtitle | accurate timing, readable Japanese segmentation, style consistency | Resolve subtitle/Text+/external fallback |
| Audio finishing | dialogue cleanup/leveling, BGM, ducking, ambience/SFX policy, loudness QC | Fairlight/live MCP/advanced audio/external fallback |
| Color finishing | exposure/WB correction, camera/shot match, channel look, visual QC | live grading + advanced DRX/QC + presets |
| Framing and motion | crop/reframe, punch-in, stabilization/retime only when justified and supported | MCP transforms/effects |
| Graphics and presentation | title, lower-third, chapter/keyword graphics, transitions/Fusion when useful | Text+, Fusion, templates, external assets |
| Delivery and QC | render settings, codec/format, conform/readback, delivery checks | MCP/advanced deliverable + project QC |

Each domain receives one of:

- `applied`;
- `intentionally_not_needed`;
- `manual_fallback_required`;
- `blocked`.

A domain may be intentionally unnecessary. For example, a dialogue-free montage may not need subtitles, and a clean locked-off shot may not need stabilization. But a domain cannot be ignored without an explicit status.

### 10.2 Presentation/effect intents remain semantic

The AI proposes semantic intents such as:

- `emphasis_punch_in`;
- `broll_cutaway`;
- `lower_third`;
- `keyword_text`;
- `chapter_card`;
- `simple_dissolve`;
- `motion_transition`;
- `picture_in_picture`;
- `screen_highlight`;
- `sfx_accent`.

The compiler maps those intents to verified MCP actions, templates, presets, or external assets. Effects are selected because they improve communication, pacing, or style, not because a Gate requires a quota.

### 10.3 Audio finishing is a first-class plan

`AudioFinishingPlan` is generated from dialogue, music, ambience, and channel policy. It may specify:

```text
Dialogue cleanup / noise handling
        -> dialogue level normalization
        -> EQ/compression or voice isolation only when accepted and useful
        -> BGM placement
        -> music ducking around dialogue
        -> ambience preservation or repair
        -> optional SFX accents
        -> loudness / peak QC
```

The plan stores semantic goals and target ranges. Resolve/Fairlight/MCP details are compiled later. The system must avoid "improving" already-good audio merely to prove it can apply processing.

### 10.4 Color finishing is a first-class plan

`ColorFinishingPlan` separates technical correction from creative look:

```text
Exposure / white-balance normalization
        -> camera-to-camera and shot-to-shot matching
        -> channel/episode look
        -> skin/product/reference sanity checks where relevant
        -> render-side visual QC
```

The preferred path is to reuse MCP live grading and the advanced server's calibrated DRX/QC functions where fixtures pass. A per-shot generative grade is not required. Stable channel looks, matching, and readable images matter more than random stylistic variation.

### 10.5 Channel Production Kit

The system must distinguish "Resolve can perform this operation" from "this channel can reliably produce a tasteful result with this operation."

`ChannelProductionKitV1` is a versioned set of tested production recipes, templates, presets, assets, constraints, and license evidence. It may include:

```text
subtitles/
  default
  emphasis

titles/
  opening
  chapter
  lower-third

motion/
  punch-in
  product-focus
  screen-highlight

transitions/
  default
  montage

audio/
  dialogue-chain
  bgm-ducking
  ambience
  sfx-library

color/
  technical-normalize
  talking-head-look
  outdoor-look

branding/
  fonts
  logo
  safe-margins
```

Each recipe declares:

- semantic intent it serves;
- accepted MCP/Resolve capability IDs;
- tunable parameter bounds;
- applicable footage/scene conditions;
- reference/taste domains it can express;
- preview fixture and expected readback;
- license/provenance for external assets;
- fallback.

Editorial AI selects a semantic intent and desired style. The Production Kit translates that into a small, tested visual/audio vocabulary instead of inventing arbitrary Fusion/grade/audio chains every episode.

### 10.6 Channel Presentation Profile

The profile controls how the Production Kit is used:

- allowed recipe families and density;
- title/lower-third choices;
- subtitle style;
- transition preferences;
- punch-in range;
- audio finishing policy and target ranges;
- BGM/SFX policy;
- color correction/matching policy and channel look;
- intro/outro rules;
- brand assets and license evidence.

The Presentation Profile is policy; the Production Kit is the concrete, verified implementation vocabulary.

## 11. DaVinci Resolve MCP integration

### 11.1 Pinning

Each production run records:

- `davinci-resolve-mcp` version/commit;
- server mode;
- advanced server version/commit if used;
- DaVinci Resolve product/version/build;
- enabled optional dependencies;
- capability snapshot hash.

### 11.2 Three execution surfaces

#### Live MCP server

Preferred for live Resolve operations.

Use for timeline, media pool, titles, Fusion, audio, grading, render, project lifecycle, and readback operations.

#### Advanced MCP server

Use when its offline deterministic engines provide better leverage, for example conform, project file operations, grade generation/QC, delivery QC, provenance, or DB-as-truth helper functions.

The advanced package may be called by MCP or imported as a library when that provides a safer deterministic integration.

#### Legacy direct bridge

Use only for accepted gaps or as rollback while MCP parity is being established.

### 11.3 MCP Execution Plan

Timeline IR and Presentation Intents compile into an `McpExecutionPlan`.

Each step contains:

- tool surface;
- action;
- normalized params;
- expected preconditions;
- destructive flag;
- dry-run/plan token if supported;
- expected readback;
- retry class;
- fallback.

The production runner executes this plan serially under the existing single-writer lease.

### 11.4 No free-form production agent mutation

An LLM may use MCP interactively during development, exploration, or explicitly approved manual-assist mode.

Normal production does not give an agent unrestricted authority to mutate the timeline step by step.

The normal path is:

```text
Committed Creative Edit Plan
        -> Timeline IR
        -> deterministic MCP Execution Plan
        -> single-writer execution
        -> readback/conformance
```

This preserves much of v4.2's safety model without reimplementing MCP.

## 12. Timeline IR v2

Timeline IR remains a core asset.

v2 extends the representation to cover:

- multiple video tracks;
- B-roll/cutaway roles;
- stills/graphics;
- subtitle cues;
- audio roles;
- semantic transitions;
- presentation/effect intents;
- source-to-record relationships;
- Decision ID linkage.

Resolve-specific tool calls do not belong in Timeline IR.

## 13. Preview, Episode Cockpit, and human review

The pre-Resolve Editorial Preview remains valuable and is retained. Presentation-level review is also retained, but normal operation is surfaced through one local `Episode Cockpit`.

### 13.1 Two preview levels

#### Editorial Preview

Fast proxy showing:

- selected clips and story order;
- rough B-roll;
- approximate subtitles;
- placeholder titles;
- rough music/ambience cues;
- Moment Deep Review flags where confidence is low.

Purpose: validate content decisions cheaply before expensive finishing.

#### Presentation Preview

Generated after quality-domain compilation, either with a lightweight renderer or low-cost Resolve/MCP build.

Purpose: validate subtitles, audio balance, color/matching, graphics, framing, and overall style before final delivery.

### 13.2 Episode Cockpit

The Episode Cockpit is a local web application owned by davinci-agent. It is not a timeline editor.

The first screen must make starting an episode trivial:

```text
New Episode

[Choose / drop source folder]

What is this video about?
[ free-form natural-language brief ]

Target length: [auto / optional range]
Channel profile: [default profile]
Optional references: [add URL / local file / saved reference]

[Create video]
```

Advanced fields such as must-include moments, publication constraints, target audience, or presentation intensity remain available but are not mandatory when defaults are sufficient.

After start, the minimum surface is:

```text
Episode title / brief / target length
Current stage + progress + completed/remaining work
Editorial Preview / Presentation Preview player
Flagged review items with timestamp jump
Natural-language correction box
Structured interpretation of the pending correction
Rebuild / retry affected stage
Before-vs-after review summary
Optional reference/taste annotation from the current preview
Final approval / publication approval
Publish status and remote video link after upload
```

The UI shows an ETA only when backed by measured historical stage timing. Otherwise it shows stage and progress rather than inventing precision.

### 13.2.1 Interruption Policy

The pipeline must make routine technical decisions without asking the operator to babysit it. Every potential interruption is classified into one of three levels:

```text
SAFE_AUTO_RESOLVE
  -> choose accepted default/fallback, continue, record what happened

DEGRADED_BUT_RECOVERABLE
  -> take the documented fallback when quality impact is bounded, continue, surface it in final review

HUMAN_DECISION_REQUIRED
  -> stop only for material editorial meaning, rights/privacy, destructive ambiguity, or publication authority
```

Examples that normally must not interrupt the user one-by-one:

- one accepted subtitle implementation falls back to another;
- a non-critical effect recipe is unavailable and a documented lower-complexity recipe is safe;
- an analysis provider transiently retries;
- a B-roll candidate is low confidence but the planner can omit it safely.

Examples that may require a stop:

- two interpretations materially change the episode's claim;
- rights/privacy policy cannot be resolved;
- the only build path requires manual intervention that changes expected quality materially;
- public publication approval.

### 13.3 Review commands

Natural language is converted to structured commands such as:

- remove this section;
- keep 2 seconds more before the cut;
- use the other take;
- insert more B-roll here;
- this shot is boring;
- leave this quiet moment longer;
- make subtitles shorter;
- remove the zoom effect;
- lower the BGM here;
- make these shots match in color;
- use this lower-third style for the whole channel;
- this correction is only for this episode.

Every accepted correction becomes a Review Event tied to artifact versions.

### 13.4 UX SLO

Normal operation must satisfy these product constraints:

- no CLI is required for a normal accepted episode;
- no JSON/artifact inspection is required;
- no direct Resolve manipulation is required unless a capability falls to Manual Finalization;
- surfaced review items jump directly to the relevant timestamp;
- low-confidence/blocked issues are grouped instead of interrupting the operator one by one;
- `SAFE_AUTO_RESOLVE` and bounded `DEGRADED_BUT_RECOVERABLE` conditions do not create blocking prompts;
- the Cockpit starts a normal episode from source selection + natural-language brief + optional references without exposing internal artifact fields;
- internal Editorial/Presentation/Privacy/Rights/Final/Publication records may remain separate, but the UI should bundle compatible approvals into at most two normal blocking review sessions before publication;
- after restart or transient failure, the Cockpit resumes from persisted job/artifact state;
- each error states what failed, what output is affected, whether retry is safe, and what the user can do next.

A dedicated full NLE UI remains out of scope. A comfortable review-and-approval cockpit is in scope.


## 14. Build, conform, and QC

### 14.1 Technical QC

Prefer MCP/advanced-server capabilities for generic technical checks where they pass fixtures.

Examples:

- missing media;
- source frame ranges;
- gaps and overlaps;
- render format/codec;
- loudness;
- blanking/black frames;
- conform completeness;
- timeline readback;
- delivery manifest.

The existing project-level QC artifact may aggregate MCP results rather than duplicate their implementation.

### 14.2 Editorial QC

Custom AI/editorial checks remain necessary.

Check for:

- obvious narrative discontinuity;
- duplicated explanation;
- missing must-include block;
- awkward jump or too-short hold;
- poor B-roll relevance;
- long low-value section;
- subtitle mismatch;
- excessive effect density;
- inconsistent title/subtitle style;
- abrupt audio transition;
- sensitive/private content candidates;
- unsupported manual-finalization items.

Editorial QC creates candidates, not final truth. Critical issues require review.

### 14.3 Seven-domain quality gate

Before Final Review, the system emits a `QualityDomainReport` covering all seven domains from section 10. A video cannot pass merely because several effects were successfully invoked.

The Gate requires:

- Editorial construction has no blocking continuity/story defect;
- Subtitle is `applied` or `intentionally_not_needed`;
- Audio finishing has an explicit plan/result and passes configured technical checks;
- Color finishing has an explicit correction/match/look result or a justified no-op;
- Framing/motion and graphics are either intentionally applied or intentionally not needed;
- Delivery/QC passes;
- any `manual_fallback_required` item is surfaced in the Cockpit before final approval;
- no domain remains `blocked` at publication time.

### 14.4 Publishability Review

Seven-domain completion proves that finishing responsibilities were handled. It does not prove the result is a good video.

For Real Episode 0 and the first benchmark episodes, v4.3 therefore records a `PublishabilityReviewV1` after Presentation/Final Preview. Human input may be as lightweight as:

```text
Would you publish this after these corrections? yes/no
What feels best? free text / timestamp
What still feels weak? free text / timestamp
Optional domain rating or A/B comparison
```

The system may produce an AI pre-assessment, but the human review is authoritative during calibration. Numeric scoring is optional.

Development acceptance is based on trend: the same representative episode should need fewer and smaller corrections as Media Intelligence, Taste Profile, Production Kit, and Editorial Director mature. A technically complete seven-domain build that the operator would not publish does not satisfy the product Gate.

## 15. Publishing

v4.3 adds a real publishing boundary.

### 15.1 Publish Package artifact

Contains:

- approved render reference;
- title candidates and selected title;
- description;
- chapters;
- tags/keywords if used;
- thumbnail reference;
- playlist target;
- visibility;
- scheduled publish time if any;
- rights/privacy approval references;
- channel/account target;
- upload status and remote video ID after publication.

### 15.2 Upload behavior

Automatic YouTube upload is opt-in per channel.

Default safety rule:

- automatic preparation is allowed;
- upload may be automated;
- public visibility requires `PUBLICATION_APPROVED` unless the channel explicitly enables a separate reviewed scheduling policy.

The uploader must be idempotent and avoid duplicate public uploads after retry.

## 16. Channel learning

### 16.1 Four evidence streams must stay distinguishable

#### Explicit editorial rules

What the operator directly states as policy.

Examples:

- never use this subtitle style;
- keep intros under a stated range for this series;
- always preserve product disclaimers.

#### Reference-derived taste

What the operator says they like/dislike in external or owned references, plus approved features derived from those references.

Examples:

- likes the color treatment of Reference A;
- prefers the story structure of Reference B;
- prefers B over A for pacing, specifically because A has too many jump cuts.

#### Approved edit behavior

What can be inferred from repeated accepted corrections and final decisions.

Examples:

- leave reactions longer;
- use fewer punch-ins;
- subtitles should be concise;
- prefer product close-ups over wide shots.

#### Audience outcome

What happened after publication.

Examples:

- CTR;
- average view duration;
- retention drops;
- common comments;
- chapter-level engagement when available.

Audience outcomes never automatically rewrite operator taste. Reference-derived taste never silently becomes a global hard rule when it is based on one ambiguous example.

### 16.2 Profile change governance

A preference may become a durable channel rule only through one of:

1. explicit human request;
2. repeated approved reference/edit evidence;
3. Profile Change Proposal;
4. human approval;
5. profile version update;
6. holdout evaluation on later episodes.

Contradictory evidence is surfaced. It is not silently averaged into an inscrutable taste vector.

### 16.3 Calibration UX

The system must support three low-friction learning paths:

```text
Reference video + comment
Approved correction / final edit
Optional score or A/B comparison
```

Scoring is never the only path. The system should prefer natural comments and observed approved edits, and ask an A/B question only when the expected information gain justifies interrupting the operator.

## 17. Control plane and artifact model

Keep the existing strengths:

- immutable/versioned authoritative artifacts;
- Proposal vs Commit separation;
- artifact registry;
- SQLite runtime state;
- CAS/lease semantics;
- audit/evidence ledger;
- human approval records;
- code/tool/version pinning;
- job recovery;
- Manual Finalization/Freeze.

### 17.1 New authoritative artifacts

- `episode-brief-v1`
- `media-intelligence-v2`
- `analysis-budget-v1`
- `moment-deep-review-v1`
- `reference-source-v1`
- `reference-annotation-v1`
- `pairwise-preference-v1`
- `derived-taste-profile-v1`
- `reference-library-v1`
- `story-plan-v1`
- `moment-selection-plan-v2`
- `creative-edit-plan-v2`
- `audio-finishing-plan-v1`
- `color-finishing-plan-v1`
- `quality-domain-report-v1`
- `channel-production-kit-v1`
- `publishability-review-v1`
- `timeline-ir-v2`
- `mcp-capability-snapshot-v1`
- `mcp-execution-plan-v1`
- `mcp-execution-report-v1`
- `editorial-qc-report-v1`
- `publish-package-v1`
- `performance-observation-v1`
- `channel-learning-proposal-v1`

### 17.2 Source-of-truth rule

MCP analysis stores and MCP internal plan files are evidence/providers, not the channel-wide source of truth.

Committed editorial decisions and approvals remain in davinci-agent artifacts.

## 18. Capability acceptance model

Replace the broad v4.2 notion of "API capability" with two levels.

### 18.1 Provider capability

Can the pinned MCP/Resolve combination perform the operation at all?

### 18.2 Production capability

Does the operation pass our real fixture with correct readback, acceptable stability, and a defined fallback?

Examples of production capabilities:

- `base_cut_v2`
- `native_subtitles_jp_v1`
- `fusion_lower_third_v1`
- `punch_in_transform_v1`
- `broll_overlay_v1`
- `simple_transition_v1`
- `voice_isolation_v1`
- `music_ducking_v1`
- `color_look_template_v1`
- `render_youtube_4k_v1`

A capability can exist in MCP but remain disabled in production until its fixture passes.

## 19. Failure and fallback policy

Per feature, the preferred order is:

```text
MCP verified workflow/helper
-> MCP granular tool
-> advanced MCP/library
-> existing direct scripting gap adapter
-> external asset/render path
-> manual finalization
-> unsupported with explicit report
```

The system must never silently downgrade a requested effect or editorial operation to something different without reporting the change.

## 20. Security

Retain v4.2's security posture.

Additional rules:

- MCP servers must be local/loopback unless a specific network permit is approved;
- MCP package/version must be pinned for production runs;
- no model receives raw arbitrary shell access through this integration;
- source media/transcript/OCR remain untrusted data;
- publication credentials never enter model prompts;
- YouTube OAuth tokens are stored outside artifacts and referenced only by credential alias;
- external AI media uploads follow episode/channel policy.

## 21. Migration strategy from v4.2

### 21.1 Freeze the current foundation before change

Tag the inspected v4.2 foundation state and preserve its release evidence.

Do not rewrite passed v4.2 gate evidence to pretend the old system covered v4.3 requirements.

### 21.2 Add v4.3 schemas side by side

Do not mutate frozen Phase-1 schemas in place.

Introduce `v2` artifacts and adapters while preserving the old talking-head regression path.

### 21.3 Dual execution backend during migration

```text
execution_backend = legacy_direct | mcp
```

Run both on selected fixtures until MCP parity is established.

### 21.4 Do not delete working foundation prematurely

The following are initially preserved:

- artifact store/registry;
- approvals;
- audit/evidence;
- ingest;
- normalize/conform;
- job runner;
- metrics;
- review events;
- preview;
- release/freeze;
- security.

Direct Resolve bridge/builder code is deprecated only after accepted MCP equivalents pass real fixtures.

### 21.5 Establish Real Episode 0 before deep rebuild

Choose one representative, non-trivial raw footage set owned by the operator and freeze its source manifest plus the intended brief. Capture the current baseline result and manual effort.

Re-run this same episode after:

- MCP execution parity;
- Progressive Media Intelligence;
- Editorial v2 + taste learning;
- Channel Production Kit + seven-domain finishing;
- Episode Cockpit.

The episode is a longitudinal product benchmark, not a golden-output fixture. The expected edit may improve over time, but every run must retain comparable human-time, correction, fallback, TTFRP, and Publishability Review measurements.

## 22. v4.3 phase gates

### Gate V43-0: Foundation Freeze and MCP Fit

Must prove:

- current v4.2 release baseline is reproducibly frozen;
- MCP server works on the target Mac/Resolve build;
- capability inventory is generated;
- at least base-cut, subtitle path, Fusion title/lower third, audio operation, color operation, render, and readback are exercised;
- fallback ownership is documented;
- MCP auto-update cannot silently change the production dependency between accepted runs.

### Gate V43-0.5: Real Episode 0 baseline and vertical slice

Must prove:

- one representative real raw-footage set and Episode Brief are frozen as the longitudinal product benchmark;
- current baseline Active Human Time, manual Resolve work, TTFRP, and publishability feedback are recorded;
- the episode can be rerun after later phases without changing source identity;
- product gaps are captured as prioritized failure cases rather than deferred until the three-episode final validation.

### Gate V43-1: Progressive Media Intelligence

Must prove on at least three materially different source sets:

- shot structure captured;
- standard visual/editorial fields available;
- transcript/audio linked to shots;
- Progressive Analysis routes all material through a universal pass;
- Moment Deep Review is triggered selectively and produces exact-window evidence;
- a valuable visually driven moment can be surfaced even when it contains no speech;
- recall-audit sentinel samples low-score material;
- deep-review ratio, TTFRP, token/frame cost, and cache behavior are recorded;
- evidence is imported into canonical artifacts.

### Gate V43-2: Editorial Intelligence + Reference Preference Learning

Must prove:

- Story Plan generated;
- Moment Selection includes speech and non-speech candidates;
- boring/redundant material can be removed for reasons beyond silence;
- B-roll can be selected for semantic relevance;
- local reference video ingestion works;
- YouTube URL reference path works when a compliant analysis path is available, otherwise the UI clearly requests a local supplied file rather than failing opaquely;
- free-form comments such as "I like only the color" become correctly domain-scoped annotations;
- unspecified domains are not silently promoted to likes;
- positive, negative, mixed, and timestamp-scoped references are supported;
- optional pairwise preference can change Derived Taste Profile in an expected, explainable direction;
- Derived Taste Profile can change Story/Moment/Creative planning in an expected, explainable direction;
- every taste-influenced decision can cite evidence;
- no invented source spans/IDs;
- human can correct decisions via structured Review Events.

### Gate V43-3: Seven-domain Creative Resolve Build

Must prove:

- Timeline IR v2 compiles to MCP execution;
- automatic subtitles are usable when dialogue requires them;
- AudioFinishingPlan is executed and checked;
- ColorFinishingPlan performs correction/matching/look or records a justified no-op;
- B-roll and primary footage coexist correctly;
- framing/motion and graphics intents are supported where editorially justified;
- each of the seven quality domains has an explicit status;
- at least the active subtitle, audio, color, framing/graphics recipes are resolved through a versioned Channel Production Kit rather than ad-hoc unbounded generation;
- Production Kit recipe provenance/license and fallback are recorded;
- no `blocked` domain passes Final Review;
- readback verifies source/record mapping;
- render/QC completes;
- Real Episode 0 receives a Publishability Review and shows whether technical completion is translating into a video the operator would actually publish;
- legacy backend remains available for rollback.

There is deliberately no "three effect families" quota. Quality-domain completion replaces effect counting.

### Gate V43-4: General YouTube Reference Episodes + UX

Run at least three real reference episodes:

1. talking-head + B-roll;
2. visually driven vlog/travel/product-style episode;
3. mixed format episode including screen recording or voice-over.

For each, record human correction time, TTFRP, total wall-clock, deep-review ratio, analysis cost, defect categories, and fallback usage.

UX must prove:

- a new episode can be started from source selection + natural-language brief + optional references;
- the episode can be completed from Episode Cockpit without normal CLI/JSON use;
- timestamp review jump works;
- natural-language correction -> structured command -> partial rebuild works;
- transient restart resumes;
- normal blocking review sessions are <= 2 before publication;
- safe fallback/retry conditions obey Interruption Policy and do not create avoidable prompts;
- each episode records lightweight Publishability Review feedback without requiring a full numeric scorecard;
- any direct Resolve manipulation is recorded as Manual Finalization rather than hidden.

### Gate V43-5: Publish

Must prove:

- Publish Package generation;
- idempotent private/unlisted test upload;
- no duplicate upload on retry;
- Publication Approval binding;
- upload result stored as artifact and surfaced in Episode Cockpit.

### Gate V43-6: Channel Learning

Must prove:

- corrections can generate a Profile Change Proposal;
- one-off correction does not silently become global policy;
- approved change affects a later episode;
- Reference Library, Reference Annotations, and Derived Taste Profile can be versioned under human approval;
- one reference can affect only its named domain without contaminating unspecified domains;
- optional pairwise preference can resolve an ambiguous style preference;
- outcome metrics can be stored separately from preference.


## 23. Definition of Done for v4.3

v4.3 is not complete because all unit tests pass.

It is complete when:

- the pipeline handles at least the three real reference episode shapes above;
- valuable non-verbal footage can be selected;
- low-value footage can be cut for semantic/editorial reasons;
- automatic subtitles are useful with limited correction;
- B-roll, graphics, framing, audio finishing, and color finishing are applied or explicitly marked unnecessary by the seven-domain model;
- MCP is the default production capability provider for accepted functions;
- the existing foundation still provides audit, approvals, versioning, recovery, and review;
- a publish-ready YouTube package is produced;
- optional upload works safely;
- Active Human Time, TTFRP, wall-clock time, deep-review ratio, and analysis cost are measured on real episodes;
- a normal accepted episode can be reviewed, corrected, rebuilt, approved, and published from Episode Cockpit without CLI/JSON inspection;
- YouTube/local reference videos plus free-form comments can seed domain-scoped taste before enough channel history exists for learning;
- optional pairwise preferences can refine taste without forcing numeric scoring;
- a versioned Channel Production Kit turns taste/semantic intents into tested Resolve-ready recipes instead of arbitrary per-episode effects;
- Real Episode 0 is rerun through major milestones and shows a decreasing correction burden / improving publishability trend;
- normal safe fallbacks do not interrupt the operator;
- no major generic Resolve capability has been reimplemented without a documented MCP Fit Test failure.

## 24. Explicit product statement

The system should be describable in one sentence without qualifications such as "talking-head only":

> Give it filmed material, a brief, and the channel owner's accumulated taste evidence, and it can understand the footage, choose and structure worthwhile moments, express the preferred style through verified DaVinci Resolve production recipes, let the human correct the result quickly, and produce a publish-ready YouTube video.

If the implementation cannot truthfully satisfy that sentence for the supported envelope, the project is not finished.
