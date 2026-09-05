from __future__ import annotations

from ottam import dashboard
from ottam import dashboard_app  # noqa: F401 - compose the production dashboard
from ottam import dashboard_script_review as review


def _waiting_job():
    return {
        "episode_id": "OTTAM-ACTION-TEST",
        "topic": {"title": "Why We Replay Cringe Moments"},
        "topic_title": "Why We Replay Cringe Moments",
        "status": "AWAITING_SCRIPT_APPROVAL",
        "conclusion": "success",
        "awaiting_script_approval": True,
        "ready": False,
    }


def _capture_dispatch(monkeypatch):
    calls = []
    monkeypatch.setattr(review, "production_snapshot_with_review", lambda episode_id: _waiting_job())
    monkeypatch.setattr(dashboard, "_dispatch", lambda workflow, inputs: calls.append((workflow, inputs)))
    monkeypatch.setattr(dashboard, "_save_job", lambda *args, **kwargs: None)
    return calls


def test_approve_and_continue_dispatches_stage_five_continuation(monkeypatch):
    calls = _capture_dispatch(monkeypatch)
    response = dashboard.app.test_client().post("/api/jobs/OTTAM-ACTION-TEST/approve-script", json={})
    assert response.status_code == 200
    assert response.get_json()["status"] == "queued"
    assert len(calls) == 1
    workflow, inputs = calls[0]
    assert workflow == "production.yml"
    assert inputs["script_approved"] == "true"
    assert inputs["script_revision_instruction"] == ""
    assert inputs["resume_failed"] == "false"


def test_apply_suggested_changes_dispatches_exact_revision(monkeypatch):
    calls = _capture_dispatch(monkeypatch)
    instruction = "Make the opening more relatable and shorten the middle example."
    response = dashboard.app.test_client().post(
        "/api/jobs/OTTAM-ACTION-TEST/revise-script",
        json={"instruction": instruction},
    )
    assert response.status_code == 200
    assert response.get_json()["status"] == "queued"
    assert len(calls) == 1
    _, inputs = calls[0]
    assert inputs["script_approved"] == "false"
    assert inputs["script_revision_instruction"] == instruction


def test_reject_and_regenerate_dispatches_nonempty_default_rewrite(monkeypatch):
    calls = _capture_dispatch(monkeypatch)
    response = dashboard.app.test_client().post(
        "/api/jobs/OTTAM-ACTION-TEST/revise-script",
        json={"instruction": ""},
    )
    assert response.status_code == 200
    assert response.get_json()["status"] == "queued"
    assert len(calls) == 1
    _, inputs = calls[0]
    assert inputs["script_approved"] == "false"
    assert inputs["script_revision_instruction"].strip()
    assert "stronger" in inputs["script_revision_instruction"].lower()


def test_review_buttons_are_bound_by_late_loaded_controller():
    html = dashboard.PAGE
    marker = html.find("script-review-actions-v1")
    base_ready = html.find("restore();")
    assert marker > base_ready >= 0
    assert "approve.onclick=()=>act(" in html
    assert "apply.onclick=()=>{" in html
    assert "reject.onclick=()=>act(" in html
    assert "waiting for GitHub runner" in html
