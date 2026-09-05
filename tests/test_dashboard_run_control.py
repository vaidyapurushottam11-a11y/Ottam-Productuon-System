from pathlib import Path

from ottam import dashboard
from ottam import dashboard_app  # noqa: F401 - installs final route overrides
from ottam import dashboard_run_control as control


def _run(episode_id: str, title: str, *, status="completed", conclusion="success", run_id=1):
    return {
        "id": run_id,
        "status": status,
        "conclusion": conclusion,
        "display_title": f"OTTAM Production {episode_id} — {title}",
        "html_url": f"https://example.test/{run_id}",
        "created_at": "2026-09-05T12:00:00Z",
    }


def test_current_job_skips_newer_cancelled_duplicate(monkeypatch):
    title = "The Cringe Replay: Why Your Brain Refuses to Forget That Moment"
    cancelled = _run("OTTAM-CANCELLED", title, conclusion="cancelled", run_id=2)
    review = _run("OTTAM-SURVIVING", title, conclusion="success", run_id=1)
    monkeypatch.setattr(dashboard, "_runs", lambda workflow: [cancelled, review])
    monkeypatch.setattr(control.review, "snapshot", lambda ep, run: {"episode_id": ep, "status": "AWAITING_SCRIPT_APPROVAL", "awaiting_script_approval": True})

    payload = dashboard.app.test_client().get("/api/current-job").get_json()

    assert payload["episode_id"] == "OTTAM-SURVIVING"
    assert payload["status"] == "AWAITING_SCRIPT_APPROVAL"
    assert payload["awaiting_script_approval"] is True


def test_same_selection_key_dispatches_only_once(monkeypatch, tmp_path: Path):
    control._selection_memory.clear()
    monkeypatch.setattr(dashboard, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(dashboard, "_runs", lambda workflow: [])
    calls = []
    monkeypatch.setattr(dashboard, "_dispatch", lambda workflow, inputs: calls.append((workflow, inputs)))
    monkeypatch.setattr(dashboard, "_save_job", lambda *args, **kwargs: None)
    monkeypatch.setattr(dashboard, "_load_job", lambda ep: {"episode_id": ep})

    client = dashboard.app.test_client()
    body = {"topic": {"title": "A Topic"}, "idempotency_key": "same-click-key"}
    first = client.post("/api/produce", json=body).get_json()
    second = client.post("/api/produce", json=body).get_json()

    assert first["episode_id"] == second["episode_id"]
    assert len(calls) == 1
    assert second["deduplicated"] is True


def test_active_same_topic_is_reused_even_without_matching_key(monkeypatch, tmp_path: Path):
    control._selection_memory.clear()
    monkeypatch.setattr(dashboard, "DATA_ROOT", tmp_path)
    active = _run("OTTAM-ACTIVE", "Same Topic", status="in_progress", conclusion=None, run_id=9)
    monkeypatch.setattr(dashboard, "_runs", lambda workflow: [active] if workflow == "production.yml" else [])
    monkeypatch.setattr(control.review, "snapshot", lambda ep, run: {"episode_id": ep, "topic_title": "Same Topic", "status": "in_progress"})
    monkeypatch.setattr(dashboard, "_dispatch", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not dispatch duplicate")))

    payload = dashboard.app.test_client().post(
        "/api/produce",
        json={"topic": {"title": "Same Topic"}, "idempotency_key": "new-key"},
    ).get_json()

    assert payload["episode_id"] == "OTTAM-ACTIVE"
    assert payload["deduplicated"] is True


def test_history_filters_hidden_episode(monkeypatch):
    monkeypatch.setattr(
        control.review,
        "history_rows_direct",
        lambda: [
            {"episode_id": "OTTAM-KEEP", "status": "COMPLETED"},
            {"episode_id": "OTTAM-HIDE", "status": "FAILED"},
        ],
    )
    monkeypatch.setattr(control, "_hidden_history_ids", lambda: {"OTTAM-HIDE"})

    payload = dashboard.app.test_client().get("/api/history").get_json()

    assert [row["episode_id"] for row in payload["items"]] == ["OTTAM-KEEP"]


def test_delete_history_dispatches_persistent_tombstone(monkeypatch):
    calls = []
    monkeypatch.setattr(dashboard, "_dispatch", lambda workflow, inputs: calls.append((workflow, inputs)))

    response = dashboard.app.test_client().post("/api/history/OTTAM-TEST-123/delete", json={})

    assert response.status_code == 200
    assert calls == [("history-hide.yml", {"episode_id": "OTTAM-TEST-123"})]
    assert response.get_json()["status"] == "hidden"


def test_dashboard_contains_double_click_guard_and_history_delete_ui():
    assert "Starting this topic once" in dashboard.PAGE
    assert "idempotency_key" in dashboard.PAGE
    assert "historyDelete" in dashboard.PAGE
    assert "recoverMeaningfulCurrent" in dashboard.PAGE
