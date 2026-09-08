from __future__ import annotations

from ottam import dashboard
from ottam import dashboard_app  # noqa: F401 - installs final route overrides
from ottam import dashboard_script_approval_fix as fix


def _run(run_id: int, status: str, *, conclusion=None):
    return {
        "id": run_id,
        "status": status,
        "conclusion": conclusion,
        "display_title": "OTTAM Production OTTAM-TEST — A Topic",
        "html_url": f"https://example.test/{run_id}",
    }


def test_in_progress_run_beats_newer_pending_duplicate():
    pending = _run(21, "pending")
    running = _run(20, "in_progress")

    selected = fix._best_run_fixed([pending, running])

    assert selected["id"] == 20


def test_second_approval_is_deduplicated_while_continuation_is_active(monkeypatch):
    fix._approval_inflight.clear()
    active = _run(20, "in_progress")
    monkeypatch.setattr(fix.run_control, "_runs_for_episode", lambda episode_id: [active])
    monkeypatch.setattr(
        fix.run_control,
        "_snapshot_for_run",
        lambda run: {
            "episode_id": "OTTAM-TEST",
            "status": "in_progress",
            "awaiting_script_approval": False,
            "progress": {"current_stage": "Stage 6/10 · Plan visuals"},
        },
    )

    response = dashboard.app.test_client().post("/api/jobs/OTTAM-TEST/approve-script", json={})
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["deduplicated"] is True
    assert payload["already_running"] is True
    assert payload["status"] == "in_progress"


def test_dashboard_hides_review_immediately_after_approval():
    assert "script-approval-current-run-v1" in dashboard.PAGE
    assert "Approval accepted — starting continuation" in dashboard.PAGE
    assert "window.__ottamApprovedEpisode" in dashboard.PAGE
