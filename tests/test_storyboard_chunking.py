from __future__ import annotations

from pathlib import Path

import pytest

from ottam.orchestrator import RecoverableStageError
from ottam.storyboard import StoryboardPlanner


def _planner() -> StoryboardPlanner:
    # These tests exercise deterministic planning helpers only; no xKiro client
    # or API key is needed.
    return object.__new__(StoryboardPlanner)


def _cues(count: int = 40, seconds: float = 3.0) -> list[dict]:
    return [
        {
            "index": i + 1,
            "start": round(i * seconds, 3),
            "end": round((i + 1) * seconds, 3),
            "text": f"Sentence {i + 1}",
        }
        for i in range(count)
    ]


def test_long_narration_is_split_into_small_storyboard_requests():
    planner = _planner()
    chunks = planner._timing_chunks(_cues(185, 3.2))

    assert len(chunks) > 1
    assert sum(len(chunk) for chunk in chunks) == 185
    assert all(len(chunk) <= planner.CHUNK_MAX_SENTENCES for chunk in chunks)
    for chunk in chunks:
        span = float(chunk[-1]["end"]) - float(chunk[0]["start"])
        # One final cue can cross the target before the next chunk is started.
        assert span <= planner.CHUNK_TARGET_SECONDS + 3.2


def test_sentence_ranges_create_exact_continuous_timing():
    planner = _planner()
    cues = _cues(6, 2.5)
    raw = [
        {
            "first_sentence_index": 1,
            "last_sentence_index": 2,
            "scene_description": "close reaction",
            "motion": "push_in",
            "transition": "cut",
        },
        {
            "first_sentence_index": 3,
            "last_sentence_index": 5,
            "scene_description": "memory replay",
            "motion": "static",
            "transition": "cut",
        },
        {
            "first_sentence_index": 6,
            "last_sentence_index": 6,
            "scene_description": "relieved payoff",
            "motion": "pull_out",
            "transition": "cut",
        },
    ]

    scenes = planner._materialize_chunk_scenes(raw, cues, chunk_end=15.0)

    assert scenes[0]["start"] == 0.0
    assert scenes[0]["end"] == scenes[1]["start"] == 5.0
    assert scenes[1]["end"] == scenes[2]["start"] == 12.5
    assert scenes[-1]["end"] == 15.0
    assert scenes[0]["narration"] == "Sentence 1 Sentence 2"


def test_sentence_ranges_reject_gaps_instead_of_silently_losing_narration():
    planner = _planner()
    cues = _cues(5)
    raw = [
        {"first_sentence_index": 1, "last_sentence_index": 2, "scene_description": "one"},
        {"first_sentence_index": 4, "last_sentence_index": 5, "scene_description": "two"},
    ]

    with pytest.raises(RecoverableStageError, match="not contiguous"):
        planner._materialize_chunk_scenes(raw, cues, chunk_end=15.0)


def test_validated_chunk_is_checkpointed_and_reusable(tmp_path: Path):
    planner = _planner()
    cues = _cues(4)
    fingerprint = planner._chunk_fingerprint(cues, 12.0)
    scenes = planner._materialize_chunk_scenes(
        [
            {"first_sentence_index": 1, "last_sentence_index": 2, "scene_description": "one"},
            {"first_sentence_index": 3, "last_sentence_index": 4, "scene_description": "two"},
        ],
        cues,
        12.0,
    )

    planner._save_cached_chunk(tmp_path, 1, fingerprint, scenes)

    assert planner._load_cached_chunk(tmp_path, 1, fingerprint) == scenes
    assert planner._load_cached_chunk(tmp_path, 1, "different-input") is None
