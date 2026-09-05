from __future__ import annotations

from ottam import dashboard_state_truth as truth


def _run(episode_id: str, rid: int) -> dict:
    return {
        "id": rid,
        "status": "completed",
        "conclusion": "failure",
        "display_title": f"OTTAM Production {episode_id} — Topic",
        "created_at": f"2026-09-05T12:0{rid}:00Z",
    }


def test_hidden_episode_is_excluded_from_current_candidate(monkeypatch):
    benjamin = "OTTAM-BENJAMIN"
    cringe = "OTTAM-CRINGE"
    groups = {
        benjamin: [_run(benjamin, 1)],
        cringe: [_run(cringe, 2)],
    }
    monkeypatch.setattr(truth, "_episode_runs", lambda: groups)
    monkeypatch.setattr(truth, "_hidden_episode_ids", lambda: {benjamin})
    monkeypatch.setattr(truth, "classify", lambda run, episode_id: ("FAILED", {}))

    candidate = truth._current_candidate()

    assert candidate is not None
    assert candidate[0] == cringe


def test_all_hidden_episodes_leave_no_current_candidate(monkeypatch):
    episode = "OTTAM-HIDDEN"
    monkeypatch.setattr(truth, "_episode_runs", lambda: {episode: [_run(episode, 1)]})
    monkeypatch.setattr(truth, "_hidden_episode_ids", lambda: {episode})

    assert truth._current_candidate() is None
