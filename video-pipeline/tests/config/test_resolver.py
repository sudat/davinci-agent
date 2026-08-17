"""Resolver tests: layer precedence, narrowing-only cloud permissions, snapshots."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from services.config.models import (
    ApprovedOverride,
    BudgetPolicy,
    ChannelConfig,
    CloudAllowlistEntry,
    EpisodeConfig,
    GenreConfig,
    NetworkPosture,
    PathAllowlist,
    ResolvedConfig,
    RetentionPolicy,
    StageDataClasses,
    SystemConfig,
)
from services.config.resolver import ResolutionError, resolve

REVIEW_ENTRY = CloudAllowlistEntry(
    data_class="review_instruction_text", stage="review_translate", fixture_only=True
)
AUDIO_ENTRY = CloudAllowlistEntry(data_class="audio", stage="asr_upload", fixture_only=False)


def system_config() -> SystemConfig:
    return SystemConfig(
        schema_version="system-config-v1",
        retention=RetentionPolicy(authoritative="permanent", rebuildable_days=30),
        data_classes=(
            StageDataClasses(stage="review_translate", classes=("review_instruction_text",)),
            StageDataClasses(stage="asr_upload", classes=("audio", "transcript")),
        ),
        cloud_allowlist=(REVIEW_ENTRY, AUDIO_ENTRY),
        network=NetworkPosture(builder="loopback", builder_endpoint="127.0.0.1:8432"),
        path_allowlist=PathAllowlist(roots=("/pipeline/jobs", "/pipeline/assets")),
        budget=BudgetPolicy(
            transient_max_attempts=3,
            permanent_max_attempts=1,
            blocking_human_max_attempts=1,
            max_stage_cost_units=1000,
            max_job_cost_units=10000,
        ),
    )


def retention(days: int) -> RetentionPolicy:
    return RetentionPolicy(authoritative="permanent", rebuildable_days=days)


def test_system_floor_with_episode_only_resolves_and_verifies() -> None:
    resolved = resolve(system_config(), episode=EpisodeConfig(episode_id="ep-01"))

    assert isinstance(resolved, ResolvedConfig)
    assert resolved.episode_id == "ep-01"
    assert resolved.layers_applied == ("ep-01",)
    assert resolved.retention.rebuildable_days == 30
    assert resolved.verify_hash()
    assert resolved.cloud_allowlist == (AUDIO_ENTRY, REVIEW_ENTRY)  # canonical order


@pytest.mark.parametrize(
    ("genre", "channel", "episode", "expected_days", "expected_layers"),
    [
        (
            GenreConfig(genre_id="travel", retention=retention(60)),
            None,
            EpisodeConfig(episode_id="ep-base"),
            60,
            ("travel", "ep-base"),
        ),
        (
            GenreConfig(genre_id="travel", retention=retention(60)),
            ChannelConfig(channel_id="ch-1", retention=retention(70)),
            EpisodeConfig(episode_id="ep-base"),
            70,
            ("travel", "ch-1", "ep-base"),
        ),
        (
            GenreConfig(genre_id="travel", retention=retention(60)),
            ChannelConfig(channel_id="ch-1", retention=retention(70)),
            EpisodeConfig(episode_id="ep-03", retention=retention(80)),
            80,
            ("travel", "ch-1", "ep-03"),
        ),
    ],
)
def test_each_layer_overrides_the_one_below(
    genre: GenreConfig,
    channel: ChannelConfig | None,
    episode: EpisodeConfig,
    expected_days: int,
    expected_layers: tuple[str, ...],
) -> None:
    resolved = resolve(system_config(), genre=genre, channel=channel, episode=episode)

    assert resolved.retention.rebuildable_days == expected_days
    assert resolved.layers_applied == expected_layers


def test_override_wins_over_all_layers() -> None:
    resolved = resolve(
        system_config(),
        genre=GenreConfig(genre_id="travel", retention=retention(60)),
        channel=ChannelConfig(channel_id="ch-1", retention=retention(70)),
        episode=EpisodeConfig(episode_id="ep-02", retention=retention(80)),
        override=ApprovedOverride(
            override_id="ov-1",
            approved_by="operator",
            reason="disk pressure",
            retention=retention(90),
        ),
    )

    assert resolved.retention.rebuildable_days == 90
    assert resolved.layers_applied == ("travel", "ch-1", "ep-02", "ov-1")


def test_precedence_system_genre_channel_episode_override() -> None:
    resolved = resolve(
        system_config(),
        genre=GenreConfig(genre_id="travel", retention=retention(60)),
        channel=ChannelConfig(channel_id="ch-1", retention=retention(70)),
        episode=EpisodeConfig(episode_id="ep-04", retention=retention(80)),
        override=ApprovedOverride(
            override_id="ov-2",
            approved_by="operator",
            reason="retention change",
            retention=retention(90),
        ),
    )

    assert resolved.retention.rebuildable_days == 90
    assert resolved.layers_applied == ("travel", "ch-1", "ep-04", "ov-2")


def test_episode_overrides_channel_and_genre() -> None:
    resolved = resolve(
        system_config(),
        genre=GenreConfig(genre_id="travel", retention=retention(60)),
        channel=ChannelConfig(channel_id="ch-1", retention=retention(70)),
        episode=EpisodeConfig(episode_id="ep-05", retention=retention(80)),
    )

    assert resolved.retention.rebuildable_days == 80


def test_layer_may_narrow_cloud_allowlist_to_a_subset() -> None:
    resolved = resolve(
        system_config(),
        episode=EpisodeConfig(episode_id="ep-06", cloud_allowlist=(REVIEW_ENTRY,)),
    )

    assert resolved.cloud_allowlist == (REVIEW_ENTRY,)


def test_layer_may_tighten_fixture_only_flag() -> None:
    tightened = CloudAllowlistEntry(
        data_class="audio", stage="asr_upload", fixture_only=True
    )

    resolved = resolve(
        system_config(),
        episode=EpisodeConfig(episode_id="ep-07", cloud_allowlist=(tightened,)),
    )

    assert resolved.cloud_allowlist == (tightened,)


def test_layer_widening_cloud_allowlist_with_a_new_entry_is_a_resolution_error() -> None:
    invented = CloudAllowlistEntry(
        data_class="sampled_frame", stage="asr_upload", fixture_only=True
    )

    with pytest.raises(ResolutionError, match="widen"):
        resolve(
            system_config(),
            episode=EpisodeConfig(episode_id="ep-08", cloud_allowlist=(invented,)),
        )


def test_layer_loosening_fixture_only_flag_is_a_resolution_error() -> None:
    loosened = CloudAllowlistEntry(
        data_class="review_instruction_text", stage="review_translate", fixture_only=False
    )

    with pytest.raises(ResolutionError, match="fixture_only"):
        resolve(
            system_config(),
            episode=EpisodeConfig(episode_id="ep-09", cloud_allowlist=(loosened,)),
        )


def test_override_widening_cloud_permissions_is_a_resolution_error() -> None:
    invented = CloudAllowlistEntry(data_class="audio", stage="editorial", fixture_only=False)

    with pytest.raises(ResolutionError, match="widen"):
        resolve(
            system_config(),
            episode=EpisodeConfig(episode_id="ep-10"),
            override=ApprovedOverride(
                override_id="ov-3",
                approved_by="operator",
                reason="cannot escalate",
                cloud_allowlist=(invented,),
            ),
        )


def test_layer_may_narrow_stage_data_classes() -> None:
    resolved = resolve(
        system_config(),
        episode=EpisodeConfig(
            episode_id="ep-11",
            data_classes=(StageDataClasses(stage="asr_upload", classes=("audio",)),),
        ),
    )

    assert resolved.data_classes == (
        StageDataClasses(stage="asr_upload", classes=("audio",)),
    )


def test_layer_granting_new_data_class_is_a_resolution_error() -> None:
    with pytest.raises(ResolutionError, match="data class"):
        resolve(
            system_config(),
            episode=EpisodeConfig(
                episode_id="ep-12",
                data_classes=(
                    StageDataClasses(stage="asr_upload", classes=("audio", "sampled_frame")),
                ),
            ),
        )


def test_layer_granting_new_stage_is_a_resolution_error() -> None:
    with pytest.raises(ResolutionError, match="stage"):
        resolve(
            system_config(),
            episode=EpisodeConfig(
                episode_id="ep-13",
                data_classes=(StageDataClasses(stage="qc", classes=("audio",)),),
            ),
        )


def test_layer_may_narrow_path_allowlist_roots() -> None:
    resolved = resolve(
        system_config(),
        episode=EpisodeConfig(
            episode_id="ep-14", path_allowlist=PathAllowlist(roots=("/pipeline/jobs/ep-14",))
        ),
    )

    assert resolved.path_allowlist.roots == ("/pipeline/jobs/ep-14",)


def test_layer_widening_path_allowlist_outside_the_floor_is_a_resolution_error() -> None:
    with pytest.raises(ResolutionError, match="path"):
        resolve(
            system_config(),
            episode=EpisodeConfig(
                episode_id="ep-15", path_allowlist=PathAllowlist(roots=("/etc",))
            ),
        )


def test_identical_inputs_produce_byte_identical_snapshots() -> None:
    first = resolve(system_config(), episode=EpisodeConfig(episode_id="ep-16"))
    second = resolve(system_config(), episode=EpisodeConfig(episode_id="ep-16"))

    assert first.canonical_bytes() == second.canonical_bytes()
    assert first.resolved_config_sha256 == second.resolved_config_sha256


def test_changed_inputs_change_the_snapshot_hash() -> None:
    base = resolve(system_config(), episode=EpisodeConfig(episode_id="ep-17"))
    changed = resolve(
        system_config(),
        episode=EpisodeConfig(episode_id="ep-17", retention=retention(99)),
    )

    assert base.resolved_config_sha256 != changed.resolved_config_sha256


def test_snapshot_is_immutable() -> None:
    resolved = resolve(system_config(), episode=EpisodeConfig(episode_id="ep-18"))

    with pytest.raises(ValidationError):
        resolved.episode_id = "ep-other"


def test_snapshot_hash_drift_is_detectable() -> None:
    resolved = resolve(system_config(), episode=EpisodeConfig(episode_id="ep-19"))

    tampered = resolved.model_copy(update={"retention": retention(999)})

    assert resolved.verify_hash()
    assert not tampered.verify_hash()


def test_duplicate_cloud_allowlist_entries_are_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate"):
        SystemConfig(
            schema_version="system-config-v1",
            retention=retention(30),
            data_classes=(
                StageDataClasses(stage="review_translate", classes=("review_instruction_text",)),
            ),
            cloud_allowlist=(REVIEW_ENTRY, REVIEW_ENTRY),
            network=NetworkPosture(builder="loopback", builder_endpoint="127.0.0.1:8432"),
            path_allowlist=PathAllowlist(roots=("/pipeline/jobs",)),
            budget=BudgetPolicy(
                transient_max_attempts=3,
                permanent_max_attempts=1,
                blocking_human_max_attempts=1,
                max_stage_cost_units=1000,
                max_job_cost_units=10000,
            ),
        )


def test_unknown_data_class_is_rejected() -> None:
    with pytest.raises(ValidationError):
        CloudAllowlistEntry.model_validate(
            {"data_class": "wiretapped_audio", "stage": "asr_upload", "fixture_only": True}
        )


def test_relative_path_root_is_rejected() -> None:
    with pytest.raises(ValidationError, match="absolute"):
        PathAllowlist(roots=("jobs/relative",))


def test_extra_fields_are_forbidden() -> None:
    with pytest.raises(ValidationError):
        ApprovedOverride.model_validate(
            {
                "override_id": "ov-4",
                "approved_by": "operator",
                "reason": "x",
                "arbitrary_shell": True,
            }
        )


def test_budget_policy_pins_no_retry_classes_to_one_attempt() -> None:
    with pytest.raises(ValidationError):
        BudgetPolicy.model_validate(
            {
                "transient_max_attempts": 3,
                "permanent_max_attempts": 2,
                "blocking_human_max_attempts": 1,
                "max_stage_cost_units": 1000,
                "max_job_cost_units": 10000,
            }
        )
