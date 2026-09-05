from pathlib import Path

from ottam import dashboard
from ottam import dashboard_app  # noqa: F401 - register composed routes


def _run():
    return {
        "id": 777,
        "status": "completed",
        "conclusion": "success",
        "display_title": "OTTAM Production OTTAM-TEST-REVIEW — Benjamin Franklin Effect",
        "html_url": "https://example.test/run/777",
        "created_at": "2026-09-05T09:30:16Z",
        "run_started_at": "2026-09-05T09:30:16Z",
        "updated_at": "2026-09-05T09:43:37Z",
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
            {"name": "Stage 1/10 · Research", "status": "completed", "conclusion": "success"},
            {"name": "Stage 2/10 · Write script", "status": "completed", "conclusion": "success"},
            {"name": "Stage 3/10 · Fact check", "status": "completed", "conclusion": "success"},
            {"name": "Stage 4/10 · Script QA", "status": "completed", "conclusion": "success"},
            {"name": "Packaging · Require VIDEO_READY", "status": "completed", "conclusion": "skipped"},
            {"name": "Finalize · Mark script review checkpoint", "status": "completed", "conclusion": "success"},
            {"name": "Finalize · Preserve outputs", "status": "completed", "conclusion": "success"},
        ],
    }


def _install_review_fixture(monkeypatch, tmp_path: Path):
    episode_id = "OTTAM-TEST-REVIEW"
    cache = tmp_path / "cache"
    episode_dir = cache / "episodes" / episode_id
    state_dir = cache / "state"
    episode_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    (episode_dir / "script.txt").write_text("SCRIPT MUST BE SHOWN TO THE USER", encoding="utf-8")
    (episode_dir / "topic.json").write_text('{"winner":{"title":"Benjamin Franklin Effect"}}', encoding="utf-8")
    (state_dir / f"{episode_id}.json").write_text(
        '{"episode_id":"OTTAM-TEST-REVIEW","stage":"generate_tts","status":"AWAITING_SCRIPT_APPROVAL","attempts":0,"last_error":null,"quarantined":false}',
        encoding="utf-8",
    )
    run = _run()
    monkeypatch.setattr(dashboard, "_runs", lambda workflow: [run])
    monkeypatch.setattr(dashboard, "_find_run", lambda workflow, marker: run)
    monkeypatch.setattr(dashboard, "_run_progress", lambda value: _progress())
    monkeypatch.setattr(dashboard, "_artifact", lambda *args, **kwargs: None)
    monkeypatch.setattr(dashboard, "_hydrate_episode_cache", lambda eid, value: cache)
    monkeypatch.setattr(dashboard, "_load_job", lambda eid: {"episode_id": eid, "topic": {"title": "Benjamin Franklin Effect"}})
    monkeypatch.setattr(dashboard, "_save_job", lambda *args, **kwargs: None)
    return episode_id


def test_current_job_endpoint_returns_script_review_not_completed(monkeypatch, tmp_path):
    _install_review_fixture(monkeypatch, tmp_path)
    client = dashboard.app.test_client()

    response = client.get("/api/current-job")
    assert response.status_code == 200
    payload = response.get_json()

    assert payload["status"] == "AWAITING_SCRIPT_APPROVAL"
    assert payload["awaiting_script_approval"] is True
    assert payload["ready"] is False
    assert payload["script"] == "SCRIPT MUST BE SHOWN TO THE USER"
    assert payload["progress"]["progress_percent"] == 40
    assert payload["build"] == "script-review-state-v2"


def test_history_endpoint_never_labels_review_checkpoint_completed(monkeypatch, tmp_path):
    _install_review_fixture(monkeypatch, tmp_path)
    client = dashboard.app.test_client()

    response = client.get("/api/history")
    assert response.status_code == 200
    payload = response.get_json()
    row = payload["items"][0]

    assert row["status"] == "AWAITING_SCRIPT_APPROVAL"
    assert row["awaiting_script_approval"] is True
    assert "video_url" not in row
    assert "thumbnail_url" not in row
    assert "captions_url" not in row
    assert payload["build"] == "script-review-state-v2"


def test_history_details_returns_script_review_payload(monkeypatch, tmp_path):
    episode_id = _install_review_fixture(monkeypatch, tmp_path)
    client = dashboard.app.test_client()

    response = client.get(f"/api/history/{episode_id}/details")
    assert response.status_code == 200
    payload = response.get_json()

    assert payload["status"] == "awaiting_script_approval"
    assert payload["script"] == "SCRIPT MUST BE SHOWN TO THE USER"
    assert payload["build"] == "script-review-state-v2"


def test_build_endpoint_identifies_deployed_fix():
    client = dashboard.app.test_client()
    response = client.get("/api/build")
    assert response.status_code == 200
    assert response.get_json()["build"] == "script-review-state-v2"
