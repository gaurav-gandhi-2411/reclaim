from __future__ import annotations

from reclaim.ai.cold_start_priority import (
    compute_cold_start_priority,
    location_weight,
    mtime_staleness_days,
)


def test_mtime_staleness_days_never_negative_for_future_mtime() -> None:
    now = 1_700_000_000.0
    assert mtime_staleness_days(now + 1000.0, now=now) == 0.0


def test_mtime_staleness_days_computes_real_age() -> None:
    now = 1_700_000_000.0
    ten_days_ago = now - 10 * 86400.0
    assert mtime_staleness_days(ten_days_ago, now=now) == 10.0


def test_location_weight_known_classes() -> None:
    assert location_weight("downloads") > location_weight("other")
    assert location_weight("git_repo") < location_weight("other")


def test_location_weight_unknown_class_defaults_neutral() -> None:
    assert location_weight("some_never_seen_class") == 1.0


def test_larger_file_scores_higher_priority_all_else_equal() -> None:
    now = 1_700_000_000.0
    small = compute_cold_start_priority(
        size_bytes=1_000, mtime=now, now=now, path_class="other", cluster_size=1
    )
    large = compute_cold_start_priority(
        size_bytes=10_000_000_000, mtime=now, now=now, path_class="other", cluster_size=1
    )
    assert large.combined > small.combined


def test_staler_file_scores_higher_priority_all_else_equal() -> None:
    now = 1_700_000_000.0
    fresh = compute_cold_start_priority(
        size_bytes=1_000, mtime=now, now=now, path_class="other", cluster_size=1
    )
    stale = compute_cold_start_priority(
        size_bytes=1_000, mtime=now - 365 * 86400.0, now=now, path_class="other", cluster_size=1
    )
    assert stale.combined > fresh.combined


def test_downloads_scores_higher_priority_than_git_repo_all_else_equal() -> None:
    now = 1_700_000_000.0
    downloads = compute_cold_start_priority(
        size_bytes=1_000, mtime=now, now=now, path_class="downloads", cluster_size=1
    )
    git_repo = compute_cold_start_priority(
        size_bytes=1_000, mtime=now, now=now, path_class="git_repo", cluster_size=1
    )
    assert downloads.combined > git_repo.combined


def test_larger_cluster_scores_higher_priority_all_else_equal() -> None:
    now = 1_700_000_000.0
    solo = compute_cold_start_priority(
        size_bytes=1_000, mtime=now, now=now, path_class="other", cluster_size=1
    )
    clustered = compute_cold_start_priority(
        size_bytes=1_000, mtime=now, now=now, path_class="other", cluster_size=8
    )
    assert clustered.combined > solo.combined


def test_result_always_labeled_heuristic_not_ml() -> None:
    now = 1_700_000_000.0
    result = compute_cold_start_priority(
        size_bytes=1_000, mtime=now, now=now, path_class="other", cluster_size=1
    )
    assert result.is_heuristic is True
