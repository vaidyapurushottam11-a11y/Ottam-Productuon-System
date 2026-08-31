from __future__ import annotations

import json
import threading
import zipfile
from pathlib import Path
from typing import Any

import httpx
from flask import jsonify

from . import dashboard as dashboard


_restore_lock = threading.Lock()
_restore_state: dict[str, dict[str, Any]] = {}


def _assets_ready(episode_id: str) -> bool:
    episode_dir = dashboard._episode_cache(episode_id) / "episodes" / episode_id
    return all(
        path.exists()
        for path in (
            episode_dir / "final.mp4",
            episode_dir / "thumbnail.jpg",
            episode_dir / "upload_package.json",
        )
    )


def _restore_status(episode_id: str) -> dict[str, Any]:
    with _restore_lock:
        return dict(_restore_state.get(episode_id) or {})


def _extract_artifact_streaming(episode_id: str, run: dict[str, Any]) -> None:
    cache = dashboard._episode_cache(episode_id)
    artifact = dashboard._artifact(int(run["id"]), f"ottam-production-{episode_id}")
    if not artifact:
        raise RuntimeError("Completed production artifact is not available on GitHub")

    marker = cache / f".artifact-{artifact['id']}"
    if marker.exists() and _assets_ready(episode_id):
        return

    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / f".artifact-{artifact['id']}.zip"
    url = f"https://api.github.com/repos/{dashboard.REPO}/actions/artifacts/{artifact['id']}/zip"

    timeout = httpx.Timeout(300.0, connect=30.0)
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            with client.stream("GET", url, headers=dashboard._headers()) as response:
                response.raise_for_status()
                with archive.open("wb") as fh:
                    for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                        fh.write(chunk)
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(cache)
        marker.write_text("ok", encoding="utf-8")
    finally:
        archive.unlink(missing_ok=True)


def _restore_worker(episode_id: str, run: dict[str, Any]) -> None:
    with _restore_lock:
        _restore_state[episode_id] = {"status": "restoring", "error": None}
    try:
        _extract_artifact_streaming(episode_id, run)
        # Rebuild the small local dashboard metadata too. This is a cache only;
        # GitHub remains the source of truth after another Render restart.
        snapshot = dashboard._production_snapshot(episode_id, run=run)
        dashboard._save_job(episode_id, snapshot)
        with _restore_lock:
            _restore_state[episode_id] = {"status": "ready", "error": None}
    except Exception as exc:  # noqa: BLE001 - surface restore failures in dashboard state
        with _restore_lock:
            _restore_state[episode_id] = {"status": "failed", "error": str(exc)}


def _ensure_restore(episode_id: str, run: dict[str, Any]) -> None:
    if _assets_ready(episode_id):
        return
    state = _restore_status(episode_id)
    if state.get("status") == "restoring":
        return
    thread = threading.Thread(
        target=_restore_worker,
        args=(episode_id, dict(run)),
        name=f"ottam-restore-{episode_id}",
        daemon=True,
    )
    thread.start()


def _restoring_snapshot(episode_id: str, run: dict[str, Any]) -> dict[str, Any]:
    job = dashboard._load_job(episode_id)
    job["episode_id"] = episode_id
    job["topic_title"] = job.get("topic_title") or dashboard._topic_from_run(run)
    job["conclusion"] = "success"
    job["ready"] = False
    job["active"] = False
    job["can_retry"] = False
    job.pop("failure", None)

    restore = _restore_status(episode_id)
    if restore.get("status") == "failed":
        # A later poll will start another restore attempt. Show the reason in the
        # meantime rather than silently returning an empty dashboard.
        job["status"] = "RESTORE_RETRYING"
        restore_text = f"Asset restore retrying: {restore.get('error') or 'unknown restore error'}"
    else:
        job["status"] = "RESTORING_ASSETS"
        restore_text = "Restoring completed video, thumbnail and upload package from GitHub"

    progress = dashboard._run_progress(run)
    progress["workflow_status"] = "restoring_assets"
    progress["current_stage"] = restore_text
    progress["progress_percent"] = 100
    progress["stage_elapsed_seconds"] = None
    job["progress"] = progress
    job["asset_restore"] = restore
    return job


def _snapshot_without_blocking(episode_id: str, run: dict[str, Any]) -> dict[str, Any]:
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        return dashboard._production_snapshot(episode_id, run=run)
    if _assets_ready(episode_id):
        return dashboard._production_snapshot(episode_id, run=run)
    _ensure_restore(episode_id, run)
    return _restoring_snapshot(episode_id, run)


def current_job_cold_safe():
    runs = dashboard._runs("production.yml")
    if not runs:
        return jsonify({"active": False})
    active = next((r for r in runs if r.get("status") != "completed"), None)
    run = active or runs[0]
    episode_id = dashboard._episode_from_run(run)
    if not episode_id:
        return jsonify({"active": False})
    snapshot = _snapshot_without_blocking(episode_id, run)
    snapshot["active"] = run.get("status") != "completed"
    return jsonify(snapshot)


def job_status_cold_safe(episode_id: str):
    run = dashboard._find_run("production.yml", f"OTTAM Production {episode_id}")
    if not run:
        return jsonify(dashboard._production_snapshot(episode_id, run=None))
    return jsonify(_snapshot_without_blocking(episode_id, run))


# Keep the existing URLs and UI untouched; only swap the two view functions that
# previously performed a large synchronous artifact download on a cold request.
dashboard.app.view_functions["current_job"] = current_job_cold_safe
dashboard.app.view_functions["job_status"] = job_status_cold_safe

app = dashboard.app
