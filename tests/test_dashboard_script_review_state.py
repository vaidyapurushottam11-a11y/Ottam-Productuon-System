from pathlib import Path

from ottam import dashboard
from ottam import dashboard_history as history
from ottam import dashboard_review_state_fix as fix
from ottam import dashboard_script_review as review


def _run(title="OTTAM Production OTTAM-TEST-1 — Test Topic"):
    return {
        "id": 101,
        "status": "completed",
        "conclusion": "success",
        "display_title": title,
        "html_url": "https://example.test/run/101",
        "created_at": "2026-09-05T09:00:00Z",
    }


def _review_progress():
    return {
        "workflow_status": "completed",
        "current_stage": "Finalize · Preserve outputs",
        "stage_index": 4,
        "stage_total": 10,
        "completed_stages": 4,
        "progress_percent": 100,
        "timeline": [
            {"name": "Stage 4/10 · Script QA", "conclusion": "success", "status": "completed"},
            {"name": "Packaging · Require VIDEO_READY", "conclusion": "skipped", "status": "completed"},
            {"name": "Finalize · Mark script review checkpoint", "conclusion": "success", "status": "completed"},
        ],
    }


def test_review_checkpoint_detected_from_workflow_step_when_artifact_lookup_misses(monkeypatch):
    monkeypatch.setattr(dashboard, "_artifact", lambda *args, **kwargs: None)
    monkeypatch.setattr(dashboard, "_run_progress", lambda run: _review_progress())

    assert fix.is_script_review_checkpoint(_run(), "OTTAM-TEST-1") is True


def test_video_ready_run_is_not_misclassified_as_review(monkeypatch):
    progress = _review_progress()
    progress["timeline"] = [
        {"name": "Finalize · Mark script review checkpoint", "conclusion": "skipped", "status": "completed"},
        {"name": "Packaging · Require VIDEO_READY", "conclusion": "success", "status": "completed"},
    ]
    monkeypatch.setattr(dashboard, "_artifact", lambda *args, **kwargs: None)
    monkeypatch.setattr(dashboard, "_run_progress", lambda run: progress)

    assert fix.is_script_review_checkpoint(_run(), "OTTAM-TEST-1") is False


def test_review_snapshot_exposes_script_and_never_marks_video_ready(monkeypatch, tmp_path: Path):
    episode_id = "OTTAM-TEST-1"
    cache = tmp_path / "cache"
    episode_dir = cache / "episodes" / episode_id
    state_dir = cache / "state"
    episode_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    (episode_dir / "script.txt").write_text("This is the script the user must review.", encoding="utf-8")
    (state_dir / f"{episode_id}.json").write_text(
        '{"episode_id":"OTTAM-TEST-1","stage":"generate_tts","status":"AWAITING_SCRIPT_APPROVAL","attempts":0,"last_error":null,"quarantined":false}',
        encoding="utf-8",
    )

    monkeypatch.setattr(dashboard, "_artifact", lambda *args, **kwargs: {"id": 999})
    monkeypatch.setattr(dashboard, "_hydrate_episode_cache", lambda eid, run: cache)
    monkeypatch.setattr(dashboard, "_load_job", lambda eid: {"episode_id": eid, "topic": {"title": "Test Topic"}})
    monkeypatch.setattr(dashboard, "_save_job", lambda *args, **kwargs: None)
    monkeypatch.setattr(dashboard, "_run_progress", lambda run: _review_progress())

    snapshot = review.production_snapshot_with_review(episode_id, run=_run())

    assert snapshot["status"] == "AWAITING_SCRIPT_APPROVAL"
    assert snapshot["awaiting_script_approval"] is True
    assert snapshot["ready"] is False
    assert snapshot["script"] == "This is the script the user must review."
    assert snapshot["progress"]["progress_percent"] == 40


def test_history_does_not_offer_completed_downloads_for_review_checkpoint(monkeypatch):
    run = _run()
    monkeypatch.setattr(dashboard, "_runs", lambda workflow: [run])
    monkeypatch.setattr(dashboard, "_artifact", lambda *args, **kwargs: None)
    monkeypatch.setattr(dashboard, "_run_progress", lambda value: _review_progress())

    rows = fix._history_rows_fixed()

    assert len(rows) == 1
    assert rows[0]["status"] == "AWAITING_SCRIPT_APPROVAL"
    assert rows[0]["awaiting_script_approval"] is True
    assert "video_url" not in rows[0]
    assert "thumbnail_url" not in rows[0]
    assert "captions_url" not in rows[0]


def test_dashboard_html_contains_script_review_controls():
    assert 'id="scriptReviewPanel"' in dashboard.PAGE
    assert 'id="approveScript"' in dashboard.PAGE
    assert 'id="applyScriptChanges"' in dashboard.PAGE
    assert 'id="rejectScript"' in dashboard.PAGE
