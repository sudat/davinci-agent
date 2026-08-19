"""Route recomputation for the Phase-2 recovery-routes criterion.

The declared fault routing (package/readback/render/QC/retry/human route)
is re-derived from the raw per-fixture records and compared against the
frozen manifest expectations. Fixture-label truthfulness (records marked
``fixture_only``, bundles marked, no synthetic-as-real approval) and the
no-manual-UI invariant are enforced here too.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.foundation_io import sha256_file
from services.gates.phase2 import PHASE_2_CRITERIA
from services.job_runner.gate_p2_ir import manifest_for
from services.job_runner.gate_p2_models import ROUTE_KEYS

if TYPE_CHECKING:
    from services.fixtures.manifest_phase2 import Phase2FixtureManifest
    from services.job_runner.gate_p2_checks import CheckState
    from services.job_runner.gate_p2_models import P2FixtureObservation

C_COMPILE, C_CONFORM, C_RENDER, C_QC, C_RECOVERY = PHASE_2_CRITERIA
REFUSED_INCOMPLETE_CODES: Final = frozenset({"render-incomplete", "render-false-complete"})


def _load_json(path: Path) -> dict[str, object]:
    document = json.loads(path.read_bytes())
    if not isinstance(document, dict):
        raise TypeError(f"not a JSON object: {path}")
    return document


def _int_of(payload: dict[str, object], key: str) -> int:
    value = payload.get(key, 0)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    raise TypeError(f"{key} is not an integer: {value!r}")


def _route(**values: str) -> dict[str, str]:
    base: dict[str, str] = dict.fromkeys(ROUTE_KEYS, "")
    base.update(values)
    return base


def derive_route(observation: P2FixtureObservation) -> dict[str, str]:
    fields = observation.fields
    fault = manifest_for(observation.fixture_id).fault
    kind = fault.kind
    if kind == "stale-capability":
        code = str(_load_json(Path(fields["fault_probe"])).get("code", ""))
        return _route(
            package_compilation="typed-failure",
            failure_code=code,
            readback="not-attempted",
            render="not-attempted",
            qc="not-attempted",
            retry="none-input-fix",
            human_route="refresh-capability-matrix",
        )
    if kind == "same-duration-wrong-media":
        code = str(_load_json(Path(fields["fault_probe"])).get("code", ""))
        return _route(
            package_compilation="typed-failure",
            failure_code=code,
            readback="not-attempted",
            render="blocked",
            qc="blocked",
            retry="none-input-fix",
            human_route="media-source-review",
        )
    if kind == "partial-build-restart":
        return _route(
            package_compilation="succeeds",
            failure_code="",
            readback="partial-then-clean-rebuild",
            render="verified-after-restart",
            qc="deterministic-after-restart",
            retry="clean-rebuild-restart",
            human_route="none",
        )
    if kind == "false-render-complete":
        record = _load_json(Path(fields["false_complete"]))
        code = str(record.get("render_failure_code", ""))
        conformant = "conformant" if record.get("readback_conformant") else "non-conformant"
        failure = code if code in REFUSED_INCOMPLETE_CODES else "render-incomplete"
        return _route(
            package_compilation="succeeds",
            failure_code=failure,
            readback=conformant,
            render="refused-incomplete",
            qc="blocked",
            retry="bounded-auto-retry",
            human_route="render-job-review",
        )
    return _route(
        package_compilation="succeeds",
        failure_code="privacy-flag-blocks-publish",
        readback="conformant",
        render="verified",
        qc="typed-failure-blocks-publish",
        retry="none-blocking",
        human_route="privacy-dismissal-required",
    )


def check_recovery(
    observation: P2FixtureObservation,
    manifest: Phase2FixtureManifest,
    state: CheckState,
) -> None:
    fixture = observation.fixture_id
    declared = {
        "package_compilation": manifest.fault.expected_package_compilation,
        "failure_code": getattr(manifest.fault, "expected_failure_code", "") or "",
        "readback": manifest.fault.expected_readback,
        "render": manifest.fault.expected_render,
        "qc": manifest.fault.expected_qc,
        "retry": manifest.fault.expected_retry,
        "human_route": manifest.fault.expected_human_route,
    }
    derived = derive_route(observation)
    if derived != declared:
        state.fail(
            C_RECOVERY,
            "route-mismatch",
            f"{fixture}: derived {derived} != declared {declared}",
        )
    if observation.manual_resolve_ui_used != "none":
        state.fail(C_RECOVERY, "manual-ui-dependency", f"{fixture}: manual Resolve UI was used")
    for operation in observation.operations:
        state.note(C_RECOVERY, hashlib.sha256(operation.model_dump_json().encode()).hexdigest())
    _check_recovery_records(observation, manifest, state, fixture)


def _check_recovery_records(
    observation: P2FixtureObservation,
    manifest: Phase2FixtureManifest,
    state: CheckState,
    fixture: str,
) -> None:
    fields = observation.fields
    kind = manifest.fault.kind
    if kind == "partial-build-restart":
        _check_restart_recovery(fields, manifest, state, fixture)
    elif kind == "false-render-complete":
        if _load_json(Path(fields["false_complete"])).get("swept"):
            state.fail(C_RECOVERY, "staging-leftover", f"{fixture}: owned stagings remain")
    elif kind == "blocking-qc-privacy":
        _check_privacy_refusal(fields, state, fixture)
    else:
        _check_human_route(fields, manifest, state, fixture)


def _check_restart_recovery(
    fields: dict[str, str],
    manifest: Phase2FixtureManifest,
    state: CheckState,
    fixture: str,
) -> None:
    interrupt = _load_json(Path(fields["interrupt"]))
    if not interrupt.get("interrupted"):
        state.fail(C_RECOVERY, "interruption-missing", f"{fixture}: no kill seam observed")
        return
    placed = _int_of(interrupt, "placed_items")
    total = _int_of(interrupt, "total_placements")
    declared_after = getattr(manifest.fault, "interrupt_after_placed_items", 0)
    if not 0 < placed < total or placed < declared_after:
        state.fail(
            C_RECOVERY,
            "interruption-not-partial",
            f"{fixture}: placed {placed} of {total} (declared after {declared_after})",
        )
    if not _load_json(Path(fields["drift"])).get("drift_detected"):
        state.fail(
            C_RECOVERY, "drift-unevidenced", f"{fixture}: partial staging drift not detected"
        )
    approval_path = fields.get("approval_path")
    if approval_path is None:
        state.fail(C_RECOVERY, "final-approval-missing", f"{fixture}: clean path unapproved")
        return
    state.note(C_RECOVERY, sha256_file(Path(approval_path)))
    _check_fixture_labels(fields, state, fixture)


def _check_privacy_refusal(fields: dict[str, str], state: CheckState, fixture: str) -> None:
    refusal_path = fields.get("approval_refusal_path")
    if refusal_path is None:
        state.fail(C_RECOVERY, "privacy-refusal-missing", f"{fixture}: no refusal recorded")
        return
    refusal = _load_json(Path(refusal_path))
    if refusal.get("code") != "privacy-unresolved":
        state.fail(
            C_RECOVERY,
            "privacy-route-drift",
            f"{fixture}: refusal {refusal.get('code')!r} != privacy-unresolved",
        )
    state.note(C_RECOVERY, sha256_file(Path(refusal_path)))
    _check_fixture_labels(fields, state, fixture)


def _check_human_route(
    fields: dict[str, str],
    manifest: Phase2FixtureManifest,
    state: CheckState,
    fixture: str,
) -> None:
    human_path = fields.get("human_route")
    if human_path is None:
        state.fail(C_RECOVERY, "human-route-missing", f"{fixture}: no human route recorded")
        return
    route = _load_json(Path(human_path))
    if route.get("human_route") != manifest.fault.expected_human_route:
        state.fail(
            C_RECOVERY,
            "human-route-drift",
            f"{fixture}: route {route.get('human_route')!r} != declared",
        )
    if route.get("fixture_only") is not True:
        state.fail(
            C_RECOVERY, "human-route-unmarked", f"{fixture}: human route not fixture-marked"
        )


def _check_fixture_labels(fields: dict[str, str], state: CheckState, fixture: str) -> None:
    bundle_path = fields.get("bundle_path")
    if bundle_path is not None:
        bundle = _load_json(Path(bundle_path))
        if bundle.get("fixture_only") is not True:
            state.fail(C_RECOVERY, "bundle-unmarked", f"{fixture}: bundle not fixture-marked")
    records_path = fields.get("records_path")
    if records_path is not None:
        for line in Path(records_path).read_text().splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("fixture_only") is not True:
                state.fail(
                    C_RECOVERY,
                    "synthetic-as-real-approval",
                    f"{fixture}: an operation record is presented as real",
                )
                return

__all__ = ["check_recovery", "derive_route"]
