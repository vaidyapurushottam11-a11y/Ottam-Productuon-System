from pathlib import Path

from ottam import dashboard
from ottam import dashboard_review_direct as direct


def _run():
    return {
        "id": 501,
        "status": "completed",
        "conclusion": "success",
        "display_title": "OTTAM Production OTTAM-TEST-REVIEW — Benjamin Franklin Effect",
        "html_url": "https://example.test/run/501",
        "created_at": "2026-09-05T09:30:00Z",
    }


def _progress():
    return {
        "workflow_status": "completed",
        "current_stage": "Finalize · Preserve outputs",
        "stage_index": 4,
        "stage_total": 10,
        "completed_stages": 4,
        "progress_percent": 100,
        "timeline": [
            {"name": "Stage 4/10 · Script QA", "status": "completed", "conclusion": "success"},
            {"name": "Packaging · Require VIDEO_READY", "status": "completed", "conclusion": "skipped"},
            {"name": "Finalize · Mark script review checkpoint", "status": "completed", "conclusion": "success"},
        ],
    }


def _prepare(monkeypatch, tmp_path: Path):
    run = _run()
    cache = tmp_path / "cache"
    ep = cache / "episodes" / "OTTAM-TEST-REVIEW"
    st = cache / "state"
    ep.mkdir(parents=True)
    st.mkdir(parents=True)
    (ep / "script.txt").write_text("Viewer must approve this script before production continues.", encoding="utf-8")
    (st / "OTTAM-TEST-REVIEW.json").write_text(
        '{"episode_id":"OTTAM-TEST-REVIEW","stage":"generate_tts","status":"AWAITING_SCRIPT_APPROVAL","attempts":0,"last_error":null,"quarantined":false}',
        encoding="utf-8",
    )
    monkeypatch.setattr(dashboard, "_runs", lambda workflow: [run])
    monkeypatch.setattr(dashboard, "_find_run", lambda workflow, marker: run)
    monkeypatch.setattr(dashboard, "_artifact", lambda *args, **kwargs: None)
    monkeypatch.setattr(dashboard, "_run_progress", lambda value: _progress())
    monkeypatch.setattr(dashboard, "_hydrate_episode_cache", lambda episode_id, value: cache)
    monkeypatch.setattr(dashboard, "_load_job", lambda episode_id: {"episode_id": episode_id, "topic": {"title": "Benjamin Franklin Effect"}})
    monkeypatch.setattr(dashboard, "_save_job", lambda *args, **kwargs: None)
    return run


def test_current_job_endpoint_returns_script_review_not_completed(monkeypatch, tmp_path):
    _prepare(monkeypatch, tmp_path)
    client = dashboard.app.test_client()
    response = client.get("/api/current-job")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "AWAITING_SCRIPT_APPROVAL"
    assert payload["awaiting_script_approval"] is True
    assert payload["ready"] is False
    assert payload["progress"]["progress_percent"] == 40
    assert "Viewer must approve" in payload["script"]


def test_job_status_endpoint_returns_script_review(monkeypatch, tmp_path):
    _prepare(monkeypatch, tmp_path)
    client = dashboard.app.test_client()
    response = client.get("/api/jobs/OTTAM-TEST-REVIEW")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "AWAITING_SCRIPT_APPROVAL"
    assert payload["ready"] is False


def test_history_endpoint_does_not_mark_review_completed(monkeypatch, tmp_path):
    _prepare(monkeypatch, tmp_path)
    client = dashboard.app.test_client()
    response = client.get("/api/history")
    assert response.status_code == 200
    item = response.get_json()["items"][0]
    assert item["status"] == "AWAITING_SCRIPT_APPROVAL"
    assert item["awaiting_script_approval"] is True
    assert "video_url" not in item
    assert "thumbnail_url" not in item


def test_true_video_ready_run_stays_completed(monkeypatch):
    run = _run()
    progress = _progress()
    progress["timeline"] = [
        {"name": "Finalize · Mark script review checkpoint", "status": "completed", "conclusion": "skipped"},
        {"name": "Packaging · Require VIDEO_READY", "status": "completed", "conclusion": "success"},
    ]
    monkeypatch.setattr(dashboard, "_artifact", lambda *args, **kwargs: None)
    monkeypatch.setattr(dashboard, "_run_progress", lambda value: progress)
    assert direct.is_script_review(run, "OTTAM-TEST-REVIEW") is False
