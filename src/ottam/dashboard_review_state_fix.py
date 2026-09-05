from __future__ import annotations

from typing import Any

from . import dashboard
from . import dashboard_history as history
from . import dashboard_script_review as review


def is_script_review_checkpoint(run: dict[str, Any], episode_id: str) -> bool:
    """Classify a successful workflow pause as review, never as VIDEO_READY.

    GitHub marks the intentional Script QA pause as a successful workflow run.
    Therefore conclusion=success cannot be used as the definition of a completed
    video. Detect the explicit review checkpoint instead.
    """
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        return False

    title = str(run.get("display_title") or "")
    if "[SCRIPT_REVIEW]" in title:
        return True
    if "[VIDEO_READY]" in title or "[CONTINUE]" in title:
        return False

    # Fast path: the dedicated review artifact is the strongest signal when the
    # GitHub artifact index has caught up.
    try:
        if dashboard._artifact(int(run["id"]), f"ottam-script-review-{episode_id}"):
            return True
    except Exception:
        pass

    # Robust fallback for eventual-consistency / artifact-index delays. The
    # workflow step is visible immediately and is exactly what the screenshots
    # show as successful for an intentional review pause.
    progress = dashboard._run_progress(run)
    timeline = progress.get("timeline") or []
    review_step_succeeded = any(
        str(step.get("name") or "") == "Finalize · Mark script review checkpoint"
        and step.get("conclusion") == "success"
        for step in timeline
    )
    video_ready_gate_succeeded = any(
        str(step.get("name") or "") == "Packaging · Require VIDEO_READY"
        and step.get("conclusion") == "success"
        for step in timeline
    )
    return review_step_succeeded and not video_ready_gate_succeeded


def _review_marker(run: dict[str, Any], episode_id: str) -> dict[str, Any] | None:
    return {"detected": True} if is_script_review_checkpoint(run, episode_id) else None


# The existing script-review snapshot/cold-recovery wrappers call this function
# dynamically, so replacing it fixes current-job, explicit job polling and cold
# Render recovery without re-registering routes.
review._review_marker = _review_marker


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
        # Download buttons must only exist for genuinely finished videos.
        if status == "COMPLETED":
            row["video_url"] = f"/files/{episode_id}/video"
            row["thumbnail_url"] = f"/files/{episode_id}/thumbnail"
            row["captions_url"] = f"/files/{episode_id}/captions"
            package_path = dashboard._episode_cache(episode_id) / "episodes" / episode_id / "upload_package.json"
            row["details_cached"] = package_path.is_file()
        rows.append(row)
    return rows


# production_history() resolves _history_rows at request time, so replacing the
# module function fixes the existing /api/history route as well.
history._history_rows = _history_rows_fixed
