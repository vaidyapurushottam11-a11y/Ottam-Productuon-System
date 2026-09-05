from __future__ import annotations

from typing import Any

from flask import jsonify

from . import dashboard
from . import dashboard_cold_recovery as cold
from . import dashboard_history as history
from . import dashboard_script_review as review

BUILD_LABEL = "script-review-state-v2"


def is_script_review_checkpoint(run: dict[str, Any], episode_id: str) -> bool:
    """Return True only for the intentional post-Script-QA approval pause."""
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        return False

    title = str(run.get("display_title") or "")
    if "[SCRIPT_REVIEW]" in title:
        return True
    if "[VIDEO_READY]" in title or "[CONTINUE]" in title:
        return False

    # A real finished video wins over every review signal.
    try:
        progress = dashboard._run_progress(run)
    except Exception:
        progress = {"timeline": []}
    timeline = progress.get("timeline") or []
    if any(
        str(step.get("name") or "") == "Packaging · Require VIDEO_READY"
        and step.get("conclusion") == "success"
        for step in timeline
    ):
        return False

    # Strongest review signal: dedicated checkpoint artifact.
    try:
        if dashboard._artifact(int(run["id"]), f"ottam-script-review-{episode_id}"):
            return True
    except Exception:
        pass

    # Immediate signal visible before artifact indexing catches up.
    if any(
        str(step.get("name") or "") == "Finalize · Mark script review checkpoint"
        and step.get("conclusion") == "success"
        for step in timeline
    ):
        return True

    # Last-resort source of truth for older/cold runs: inspect the preserved state.
    # This only runs when the cheap signals above were inconclusive.
    try:
        cache = dashboard._hydrate_episode_cache(episode_id, run)
        state = dashboard._episode_state(cache, episode_id) or {}
        if state.get("status") == "AWAITING_SCRIPT_APPROVAL":
            return True
        package = cache / "episodes" / episode_id / "upload_package.json"
        video = cache / "episodes" / episode_id / "final.mp4"
        if state.get("status") == "VIDEO_READY" and package.exists() and video.exists():
            return False
    except Exception:
        pass

    return False


def _review_marker(run: dict[str, Any], episode_id: str) -> dict[str, Any] | None:
    return {"detected": True} if is_script_review_checkpoint(run, episode_id) else None


review._review_marker = _review_marker


def snapshot_for_run(episode_id: str, run: dict[str, Any]) -> dict[str, Any]:
    """Single dashboard source of truth for a production workflow run."""
    if is_script_review_checkpoint(run, episode_id):
        return review._review_snapshot(episode_id, run)
    return cold._original_snapshot(episode_id, run) if hasattr(cold, "_original_snapshot") else cold._snapshot_without_blocking(episode_id, run)


def _history_rows_fixed() -> list[dict[str, Any]]:
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
        awaiting_review = is_script_review_checkpoint(run, episode_id)

        if run_status != "completed":
            status = "IN_PROGRESS"
        elif awaiting_review:
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
            "awaiting_script_approval": awaiting_review,
        }
        if status == "COMPLETED":
            row["video_url"] = f"/files/{episode_id}/video"
            row["thumbnail_url"] = f"/files/{episode_id}/thumbnail"
            row["captions_url"] = f"/files/{episode_id}/captions"
            package_path = dashboard._episode_cache(episode_id) / "episodes" / episode_id / "upload_package.json"
            row["details_cached"] = package_path.is_file()
        rows.append(row)
    return rows


history._history_rows = _history_rows_fixed


def current_job_fixed():
    runs = dashboard._runs("production.yml")
    if not runs:
        return jsonify({"active": False, "build": BUILD_LABEL})
    active = next((r for r in runs if r.get("status") != "completed"), None)
    run = active or runs[0]
    episode_id = dashboard._episode_from_run(run)
    if not episode_id:
        return jsonify({"active": False, "build": BUILD_LABEL})
    snapshot = snapshot_for_run(episode_id, run)
    snapshot["active"] = run.get("status") != "completed"
    snapshot["build"] = BUILD_LABEL
    return jsonify(snapshot)


def job_status_fixed(episode_id: str):
    run = dashboard._find_run("production.yml", f"OTTAM Production {episode_id}")
    if not run:
        payload = dashboard._production_snapshot(episode_id, run=None)
        payload["build"] = BUILD_LABEL
        return jsonify(payload)
    payload = snapshot_for_run(episode_id, run)
    payload["build"] = BUILD_LABEL
    return jsonify(payload)


def production_history_fixed():
    return jsonify({"items": _history_rows_fixed(), "build": BUILD_LABEL})


def production_history_details_fixed(episode_id: str):
    run = dashboard._find_run("production.yml", f"OTTAM Production {episode_id}")
    if not run:
        return jsonify({"error": "episode not found", "build": BUILD_LABEL}), 404

    if is_script_review_checkpoint(run, episode_id):
        snapshot = review._review_snapshot(episode_id, run)
        return jsonify(
            {
                "status": "awaiting_script_approval",
                "episode_id": episode_id,
                "script": snapshot.get("script") or "",
                "hook_qa": snapshot.get("hook_qa") or {},
                "script_qa": snapshot.get("script_qa") or {},
                "build": BUILD_LABEL,
            }
        )

    if run.get("status") == "completed" and run.get("conclusion") == "success":
        snapshot = cold._snapshot_without_blocking(episode_id, run)
        if snapshot.get("ready"):
            return jsonify(
                {
                    "status": "ready",
                    "episode_id": episode_id,
                    "package": snapshot.get("package") or {},
                    "video_url": f"/files/{episode_id}/video",
                    "thumbnail_url": f"/files/{episode_id}/thumbnail",
                    "captions_url": f"/files/{episode_id}/captions",
                    "build": BUILD_LABEL,
                }
            )
        return jsonify(
            {
                "status": "restoring",
                "episode_id": episode_id,
                "message": (snapshot.get("progress") or {}).get("current_stage") or "Restoring completed assets from GitHub",
                "build": BUILD_LABEL,
            }
        )

    snapshot = dashboard._production_snapshot(episode_id, run=run)
    return jsonify(
        {
            "status": "failed" if run.get("status") == "completed" else "in_progress",
            "episode_id": episode_id,
            "failure": snapshot.get("failure") or {},
            "run_url": run.get("html_url"),
            "build": BUILD_LABEL,
        }
    )


# Override the actual Flask endpoints last, after all composition modules have
# registered their routes. This removes import-order/monkey-patch ambiguity.
dashboard.app.view_functions["current_job"] = current_job_fixed
dashboard.app.view_functions["job_status"] = job_status_fixed
dashboard.app.view_functions["production_history"] = production_history_fixed
dashboard.app.view_functions["production_history_details"] = production_history_details_fixed


@dashboard.app.get("/api/build")
def dashboard_build():
    return jsonify({"build": BUILD_LABEL})
