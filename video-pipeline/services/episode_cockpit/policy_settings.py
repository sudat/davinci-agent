"""W9: deterministic adopted-policy → plan-settings mapping (工程2.5).

The director prompt carries the adopted policy as text (LLM judges meaning);
this module is the deterministic counterpart: given the adopted policy's
STRUCTURED fields (scope flags + presentation_condition), it derives the
exact plan inputs the compile path consumes (which speech segments receive
subtitle items, how audio binds) and records every field→setting row plus
the honestly unmappable remainder. Pure functions, no IO.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from services.contracts.primitives import StrictModel
from services.editorial.models import PresentationCondition  # noqa: TC001 (pydantic runtime field)

if TYPE_CHECKING:
    from services.episode_cockpit.consultation_store import AdoptedPolicyV1

type SubtitleMode = Literal["all_kept", "boundary_only"]

LOCAL_EXCEPTION_UNADDRESSED = (
    "local_exception_targeting: 局所的な例外の対象区間は見本経路の発話区間に対応づけできないため、"
    "字幕は通常どおりに入れて対象の絞り込みは未対応"
)


class PolicySettingEntryV1(StrictModel):
    """One policy-field → plan-setting row (journal traceability)."""

    policy_field: str
    setting: str
    detail: str = ""


class PolicySettingsRecordV1(StrictModel):
    """The deterministic derivation result for one adopted policy."""

    subtitle_mode: SubtitleMode
    telop_condition: PresentationCondition
    audio_binding: Literal["mirror_video"] = "mirror_video"
    entries: tuple[PolicySettingEntryV1, ...] = ()
    unaddressed: tuple[str, ...] = ()


def derive_policy_settings(policy: AdoptedPolicyV1) -> PolicySettingsRecordV1:
    """Map structured policy fields onto plan settings (deterministic)."""

    entries: list[PolicySettingEntryV1] = []
    unaddressed: list[str] = []
    if policy.scope.composition:
        entries.append(
            PolicySettingEntryV1(
                policy_field="composition+candidate_scenes+structure+tempo_policy",
                setting="selection_basis",
                detail="adopted structure/tempo/candidate guidance rides the "
                "director request; kept segments decide video/audio items",
            )
        )
    if policy.scope.appearance:
        condition = policy.presentation_condition
        if condition == "location_change_only":
            mode: SubtitleMode = "boundary_only"
            detail = (
                "場所変更時のみ大テロップに相当する見本表現として、"
                "編集点直後の区間のみ字幕を入れる。連続採用runの先頭区間が対象"
            )
        else:
            mode = "all_kept"
            detail = "通常表示として採用区の全区間に字幕を入れる"
        entries.append(
            PolicySettingEntryV1(
                policy_field="appearance+subtitle_policy+presentation_condition="
                + condition,
                setting="review_subtitle_inclusion=" + mode,
                detail=detail,
            )
        )
        if condition == "local_exception":
            unaddressed.append(LOCAL_EXCEPTION_UNADDRESSED)
    else:
        mode = "all_kept"
        entries.append(
            PolicySettingEntryV1(
                policy_field="appearance(unadopted)",
                setting="review_subtitle_inclusion=all_kept",
                detail="見た目は未採用のため方針を適用せず、従来どおり全区間に字幕を入れる",
            )
        )
    if policy.scope.audio:
        entries.append(
            PolicySettingEntryV1(
                policy_field="audio+audio_policy",
                setting="review_audio_binding=mirror_video",
                detail="見本の音は映像に追従する。方針のBGM・音量指定は見本経路に設定がなく未対応",
            )
        )
        unaddressed.append(
            "audio_policy_detail: BGM・音量・音付きテンポの指定は見本の音設定にないため未対応"
        )
    return PolicySettingsRecordV1(
        subtitle_mode=mode,
        telop_condition=policy.presentation_condition,
        entries=tuple(entries),
        unaddressed=tuple(unaddressed),
    )


def select_policy_subtitles(
    record: PolicySettingsRecordV1,
    speech_ids_in_order: tuple[str, ...],
    kept_ids: frozenset[str] | set[str],
) -> tuple[str, ...]:
    """The exact segment ids receiving subtitle items under the record."""

    if record.subtitle_mode == "all_kept":
        return tuple(sid for sid in speech_ids_in_order if sid in kept_ids)
    selected: list[str] = []
    for position, segment_id in enumerate(speech_ids_in_order):
        if segment_id not in kept_ids:
            continue
        if position == 0 or speech_ids_in_order[position - 1] not in kept_ids:
            selected.append(segment_id)
    return tuple(selected)


__all__ = [
    "LOCAL_EXCEPTION_UNADDRESSED",
    "PolicySettingEntryV1",
    "PolicySettingsRecordV1",
    "SubtitleMode",
    "derive_policy_settings",
    "select_policy_subtitles",
]
