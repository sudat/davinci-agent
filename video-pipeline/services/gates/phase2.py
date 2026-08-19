from typing import Final

PHASE_2_PARENT_COUNT: Final = 3

PHASE_2_CRITERIA: Final = (
    "phase-2-package-compiles-validated-ir",
    "phase-2-item-conformance-100",
    "phase-2-render-verified",
    "phase-2-qc-deterministic",
    "phase-2-recovery-routes",
)

PHASE_2_FIXTURES: Final = (
    "p2-stale-capability",
    "p2-partial-build-restart",
    "p2-same-duration-wrong-media",
    "p2-false-render-complete",
    "p2-blocking-qc-privacy",
)

PHASE_2_CAPABILITIES: Final = (
    "base_cut",
    "fixed_subtitle",
    "media_intro_outro",
    "basic_audio_preset",
    "render",
)
