from __future__ import annotations

from ottam import dashboard
from ottam import dashboard_app  # noqa: F401
from ottam import dashboard_state_truth as truth


def _run(ep: str, *, status: str = "completed", conclusion: str | None = "success", rid: int = 1):
    return {
        "id": rid,
        "status": status,
        "conclusion": conclusion,
        "display_title": f"OTTAM Production {ep} — Topic {ep}",
        "created_at": "2026-09-05T12:00:00Z",
        "run_started_at": "2026-09-05T12:00:00Z",
        "updated_at": "2026-09-05T12:05:00Z",
    }


def test_script_checkpoint_is_not_completed(monkeypatch):
    run = _run("OTTAM-REVIEW", rid=11)
    monkeypatch.setattr(dashboard, "_artifact", lambda run_id, name: {"id": 7} if name.startswith("ottam-script-review-") else None)
    monkeypatch.setattr(dashboard, "_run_progress", lambda r: {"timeline": [], "completed_stages": 4})
    status, _ = truth.classify(run, "OTTAM-REVIEW")
    assert status == "AWAITING_SCRIPT_APPROVAL"


def test_completed_requires_video_ready_gate(monkeypatch):
    run = _run("OTTAM-DONE", rid=12)
    monkeypatch.setattr(dashboard, "_artifact", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        dashboard,
        "_run_progress",
        lambda r: {
            "completed_stages": 10,
            "timeline": [
                {"name": "Packaging · Require VIDEO_READY", "conclusion": "success"}
            ],
        },
    )
    status, _ = truth.classify(run, "OTTAM-DONE")
    assert status == "COMPLETED"


def test_review_beats_newer_cancelled_failure_for_current(monkeypatch):
    cancelled = _run("OTTAM-CANCELLED", conclusion="cancelled", rid=22)
    review = _run("OTTAM-REVIEW", rid=21)
    runs = [cancelled, review]
    monkeypatch.setattr(dashboard, "_runs", lambda workflow: runs if workflow == "production.yml" else [])
    monkeypatch.setattr(dashboard, "_artifact", lambda run_id, name: {"id": 9} if "OTTAM-REVIEW" in name else None)
    monkeypatch.setattr(dashboard, "_run_progress", lambda r: {"timeline": [], "completed_stages": 4 if r["id"] == 21 else 0})
    candidate = truth._current_candidate()
    assert candidate is not None
    assert candidate[0] == "OTTAM-REVIEW"
    assert candidate[2] == "AWAITING_SCRIPT_APPROVAL"


def test_in_progress_run_beats_newer_pending_duplicate_for_same_episode(monkeypatch):
    episode = "OTTAM-20260908-121706-EE1F"
    pending = _run(episode, status="pending", conclusion=None, rid=34233559771)
    running = _run(episode, status="in_progress", conclusion=None, rid=34233089186)
    # GitHub's runs endpoint is newest-first, so the accidental pending duplicate
    # appears before the run that is actually doing work.
    monkeypatch.setattr(dashboard, "_runs", lambda workflow: [pending, running] if workflow == "production.yml" else [])
    monkeypatch.setattr(
        dashboard,
        "_run_progress",
        lambda r: {
            "timeline": [{"name": "Stage 6/10 · Plan visuals", "status": "in_progress", "conclusion": None}],
            "completed_stages": 1,
            "current_stage": "Stage 6/10 · Plan visuals",
            "stage_index": 6,
        },
    )

    selected = truth._best_run([pending, running])
    assert selected is running

    candidate = truth._current_candidate()
    assert candidate is not None
    assert candidate[0] == episode
    assert candidate[1]["id"] == 34233089186


def test_dashboard_html_is_server_first_and_uses_single_history_delete_owner():
    html = dashboard.PAGE
    assert "dashboard-state-truth-v2" in html
    assert "fetch('/api/current-job" in html
    assert "historyDelete" in html
    assert "historyDeleteV2" not in html
    assert "setInterval(reconcile,3500)" in html
    # The server-first replacement must appear before the legacy localStorage lookup.
    restore_pos = html.find("async function restore()")
    assert restore_pos >= 0
    chunk = html[restore_pos:restore_pos + 900]
    assert chunk.find("/api/current-job") < chunk.find("ottam.currentEpisode")
