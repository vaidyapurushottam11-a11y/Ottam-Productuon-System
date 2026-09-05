from __future__ import annotations

import json
from typing import Any

from flask import jsonify

from . import dashboard
from . import dashboard_cold_recovery as cold


def is_script_review(run: dict[str, Any], episode_id: str) -> bool:
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        return False

    # Strongest signal when the artifact index is available.
    try:
        if dashboard._artifact(int(run["id"]), f"ottam-script-review-{episode_id}"):
            return True
    except Exception:
        pass

    # Fallback to the actual workflow steps. A green GitHub run is not enough
    # to call a video complete because the intentional Script QA pause is green.
    progress = dashboard._run_progress(run)
    timeline = progress.get("timeline") or []
    review_done = any(
        step.get("name") == "Finalize · Mark script review checkpoint"
        and step.get("conclusion") == "success"
        for step in timeline
    )
    video_ready_done = any(
        step.get("name") == "Packaging · Require VIDEO_READY"
        and step.get("conclusion") == "success"
        for step in timeline
    )
    return review_done and not video_ready_done


def review_snapshot(episode_id: str, run: dict[str, Any]) -> dict[str, Any]:
    cache = dashboard._hydrate_episode_cache(episode_id, run)
    job = dashboard._load_job(episode_id)
    job["episode_id"] = episode_id
    job["topic_title"] = job.get("topic_title") or dashboard._topic_from_run(run)

    if not job.get("topic"):
        topic = dashboard._topic_from_cache(cache, episode_id)
        if topic:
            job["topic"] = topic

    state = dashboard._episode_state(cache, episode_id) or {}
    episode_dir = cache / "episodes" / episode_id
    script_path = episode_dir / "script.txt"
    qa_path = episode_dir / "script_qa.json"
    hook_path = episode_dir / "hook_qa.json"

    progress = dashboard._run_progress(run)
    progress.update(
        {
            "workflow_status": "awaiting_script_approval",
            "current_stage": "Script ready — waiting for your approval",
            "stage_index": 4,
            "completed_stages": 4,
            "progress_percent": 40,
            "stage_elapsed_seconds": None,
        }
    )

    job.update(
        {
            "status": "AWAITING_SCRIPT_APPROVAL",
            "conclusion": "success",
            "ready": False,
            "active": False,
            "awaiting_script_approval": True,
            "script": script_path.read_text(encoding="utf-8").strip() if script_path.exists() else "",
            "episode_state": state,
            "progress": progress,
            "can_retry": False,
        }
    )
    job.pop("failure", None)

    for key, path in (("script_qa", qa_path), ("hook_qa", hook_path)):
        if path.exists():
            try:
                job[key] = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass

    dashboard._save_job(episode_id, job)
    return job


def snapshot(episode_id: str, run: dict[str, Any]) -> dict[str, Any]:
    if is_script_review(run, episode_id):
        return review_snapshot(episode_id, run)
    return cold._snapshot_without_blocking(episode_id, run)


def current_job_direct():
    runs = dashboard._runs("production.yml")
    if not runs:
        return jsonify({"active": False})
    active = next((r for r in runs if r.get("status") != "completed"), None)
    run = active or runs[0]
    episode_id = dashboard._episode_from_run(run)
    if not episode_id:
        return jsonify({"active": False})
    payload = snapshot(episode_id, run)
    payload["active"] = run.get("status") != "completed"
    return jsonify(payload)


def job_status_direct(episode_id: str):
    run = dashboard._find_run("production.yml", f"OTTAM Production {episode_id}")
    if not run:
        return jsonify(dashboard._production_snapshot(episode_id, run=None))
    return jsonify(snapshot(episode_id, run))


def history_rows_direct() -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for run in dashboard._runs("production.yml"):
        episode_id = dashboard._episode_from_run(run)
        if not episode_id:
            continue
        created = run.get("created_at") or run.get("run_started_at")
        if episode_id not in groups:
            groups[episode_id] = {
                "episode_id": episode_id,
                "title": dashboard._topic_from_run(run) or episode_id,
                "latest_run": run,
                "created_at": created,
            }
            order.append(episode_id)
        else:
            previous = groups[episode_id].get("created_at")
            if created and (not previous or created < previous):
                groups[episode_id]["created_at"] = created

    rows: list[dict[str, Any]] = []
    for episode_id in order:
        item = groups[episode_id]
        run = item.pop("latest_run")
        run_status = str(run.get("status") or "queued")
        conclusion = run.get("conclusion")
        awaiting = is_script_review(run, episode_id)

        if run_status != "completed":
            status = "IN_PROGRESS"
        elif awaiting:
            status = "AWAITING_SCRIPT_APPROVAL"
        elif conclusion == "success":
            status = "COMPLETED"
        else:
            status = "FAILED"

        row = {
            **item,
            "status": status,
            "workflow_status": run_status,
            "conclusion": conclusion,
            "run_id": run.get("id"),
            "run_url": run.get("html_url"),
            "awaiting_script_approval": awaiting,
        }
        if status == "COMPLETED":
            row["video_url"] = f"/files/{episode_id}/video"
            row["thumbnail_url"] = f"/files/{episode_id}/thumbnail"
            row["captions_url"] = f"/files/{episode_id}/captions"
        rows.append(row)
    return rows


def production_history_direct():
    return jsonify({"items": history_rows_direct()})


def production_history_details_direct(episode_id: str):
    run = dashboard._find_run("production.yml", f"OTTAM Production {episode_id}")
    if not run:
        return jsonify({"error": "episode not found"}), 404

    if is_script_review(run, episode_id):
        payload = review_snapshot(episode_id, run)
        return jsonify(
            {
                "status": "awaiting_script_approval",
                "episode_id": episode_id,
                "script": payload.get("script") or "",
                "message": "Script is waiting for approval before narration and visuals.",
            }
        )

    if run.get("status") == "completed" and run.get("conclusion") == "success":
        payload = cold._snapshot_without_blocking(episode_id, run)
        if payload.get("ready"):
            return jsonify(
                {
                    "status": "ready",
                    "episode_id": episode_id,
                    "package": payload.get("package") or {},
                    "video_url": f"/files/{episode_id}/video",
                    "thumbnail_url": f"/files/{episode_id}/thumbnail",
                    "captions_url": f"/files/{episode_id}/captions",
                }
            )
        return jsonify(
            {
                "status": "restoring",
                "episode_id": episode_id,
                "message": (payload.get("progress") or {}).get("current_stage") or "Restoring completed assets from GitHub",
            }
        )

    payload = dashboard._production_snapshot(episode_id, run=run)
    return jsonify(
        {
            "status": "failed" if run.get("status") == "completed" else "in_progress",
            "episode_id": episode_id,
            "failure": payload.get("failure") or {},
            "run_url": run.get("html_url"),
        }
    )


# Override the actual Flask request endpoints last, after cold recovery/history
# have registered their handlers. This removes ambiguity in the live request path.
dashboard.app.view_functions["current_job"] = current_job_direct
dashboard.app.view_functions["job_status"] = job_status_direct
dashboard.app.view_functions["production_history"] = production_history_direct
dashboard.app.view_functions["production_history_details"] = production_history_details_direct

app = dashboard.app
