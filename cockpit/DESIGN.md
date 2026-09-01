# cockpit design system (Episode Cockpit UI)

Internal operator tool. The design contract is pinned by task 45 / PRD 13.2:
clean minimal operator surface — no marketing gradients, no heavy shadows,
flat 1px borders, readable Japanese labels. This file is the token source of
truth for tasks 45/46/49/50 (all cockpit UI must trace back to these tokens).

## 0. Research Log

- Embedded/lazyweb/imagen lanes: **skipped by constraint** — this is an
  internal tool whose visual direction is fixed by the task spec (minimal,
  functional, no decoration). No external reference was needed or used.
- Layer A alignment: neutral/operational (taste-skill posture) — dashboard-
  grade readability over surface ambition.

## 1. Tokens (globals.css `:root`)

| Token | Value | Use |
|---|---|---|
| `--bg` | `#f6f7f9` | page background |
| `--surface` | `#ffffff` | cards, inputs |
| `--text` | `#1a1d21` | primary text |
| `--muted` | `#5f6672` | secondary text, captions |
| `--border` | `#d4d8de` | all borders (flat, 1px) |
| `--accent` | `#1f6feb` | primary action, focus ring |
| `--accent-ink` | `#ffffff` | text on accent |
| `--danger` | `#b42318` | error text |
| `--danger-bg` | `#fef1f0` | error notice background |
| `--radius` | `6px` | inputs, buttons, cards |
| `--space-1..6` | 4/8/12/16/24/32px | the only spacing values |
| `--font-size-*` | 12/14/16/20px | caption / body / input / heading |

Typography: system stack (`system-ui, -apple-system, "Hiragino Kaku Gothic
ProN", "Noto Sans JP", sans-serif`); IDs and technical values use
`--font-mono` (`ui-monospace, SFMono-Regular, Menlo, monospace`).

## 2. Layout rules

- Single column, `max-width: 720px`, centered (`.page`).
- One `.card` per logical form group; flat border, no shadows.
- Primary action bottom-right (`動画を作成`), disabled until the form is valid.
- Advanced/optional sections use native `<details>` collapsed by default.
- Status views use definition lists + a plain table; no invented metrics
  (ETA is never shown without measurement — PRD 13.2).

## 3. Interaction & accessibility

- Every input has a real `<label htmlFor>` (Japanese label text).
- `:focus-visible` = 2px accent outline; buttons/inputs are keyboard-first.
- Errors render via `role="alert"` (`.error-notice`) showing the backend
  error code + detail verbatim — never swallow structured errors.
- Motion: none beyond native `<details>` toggle and focus rings.

## 1b. Added tokens — finishing domains status (task finishing-panel)

| Token | Value | Use |
|---|---|---|
| `--warn` | `#8a5a00` | intentionally-skipped / manual-fallback chip border+text (amber, AA on white) |
| `--warn-bg` | `#fdf6e7` | manual-fallback chip background |

Finishing status chips (`.finishing-chip-*`) reuse this palette only:

| Status | Label | Chip class | Treatment |
|---|---|---|---|
| `applied` | 適用済み | `.finishing-chip-applied` | `--accent` solid (positive — matches `.chip-like`) |
| `intentionally_not_needed` | 意図的に不要 | `.finishing-chip-intentionally-not-needed` | `--warn` dashed — visually distinct from applied by border-style + hue (the operator must notice skipped domains before watching) |
| `manual_fallback_required` | 手動対応必要 | `.finishing-chip-manual` | `--warn` solid + `--warn-bg` |
| `blocked` | ブロック中 | `.finishing-chip-blocked` | `--danger` + `--danger-bg` (matches `.chip-dislike` / `.error-notice`) |
| unknown | raw status | `.finishing-chip-unknown` | `--muted` neutral |

The normal view uses Japanese item labels and short Japanese state explanations.
Recorded source-language reasons remain available under the native
`<details>` disclosure labeled `記録された理由（原文）`; internal domain IDs are
kept in DOM data attributes rather than operator-facing copy. While finishing
has not run, the panel rechecks every two seconds and stops after data arrives.

## 4. Extension notes (tasks 46/49/50 + finishing-panel)

- New views live under `app/<route>/page.tsx`, shared pieces in
  `components/`, all API access via `lib/api.ts` (no direct `fetch` in
  components). Reuse these tokens; do not introduce new hex values inline.
- Task 50 (reference annotation): domain chips reuse existing tokens only —
  polarity mapping `like → --accent` (`.chip-like`), `dislike → --danger`
  (`.chip-dislike`), `neutral → --muted` (`.chip-neutral`); neutral hint
  panels use `.notice-hint` (`--bg` + `--border`); the selected A/B choice
  button uses `.choice-button[aria-pressed="true"]` (accent fill). No
  numeric scorecard exists on this surface by contract.
