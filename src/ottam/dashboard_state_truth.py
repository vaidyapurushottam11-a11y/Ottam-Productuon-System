from __future__ import annotations

from typing import Any

from flask import Response, jsonify

from . import dashboard
from . import dashboard_review_direct as review

# Preserve the public build value used by existing dashboard clients/tests while
# exposing the new state model separately.
BUILD = "script-review-state-v2"
STATE_TRUTH_BUILD = "dashboard-state-truth-v1"


def _episode_runs() -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for run in dashboard._runs("production.yml"):
        episode_id = dashboard._episode_from_run(run)
        if episode_id:
            groups.setdefault(episode_id, []).append(run)
    return groups


def _best_run(runs: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not runs:
        return None
    active = next((r for r in runs if r.get("status") != "completed"), None)
    if active:
        return active
    meaningful = next((r for r in runs if r.get("conclusion") != "cancelled"), None)
    return meaningful or runs[0]


def _hidden_episode_ids() -> set[str]:
    """History deletion also removes an episode from automatic current selection."""
    try:
        from . import dashboard_run_control as control
        return control._hidden_history_ids()
    except Exception:
        return set()


def _progress(run: dict[str, Any]) -> dict[str, Any]:
    try:
        return dashboard._run_progress(run)
    except Exception:
        return {"timeline": [], "completed_stages": 0}


def _packaging_passed(progress: dict[str, Any]) -> bool:
    return any(
        step.get("name") == "Packaging · Require VIDEO_READY"
        and step.get("conclusion") == "success"
        for step in progress.get("timeline") or []
    )


def _review_passed(progress: dict[str, Any]) -> bool:
    return any(
        step.get("name") == "Finalize · Mark script review checkpoint"
        and step.get("conclusion") == "success"
        for step in progress.get("timeline") or []
    )


def classify(run: dict[str, Any], episode_id: str) -> tuple[str, dict[str, Any]]:
    if run.get("status") != "completed":
        return "IN_PROGRESS", _progress(run)
    if run.get("conclusion") == "cancelled":
        return "FAILED", _progress(run)
    if run.get("conclusion") != "success":
        return "FAILED", _progress(run)

    progress = _progress(run)
    if _packaging_passed(progress):
        return "COMPLETED", progress

    try:
        if dashboard._artifact(int(run["id"]), f"ottam-script-review-{episode_id}"):
            return "AWAITING_SCRIPT_APPROVAL", progress
    except Exception:
        pass
    if _review_passed(progress):
        return "AWAITING_SCRIPT_APPROVAL", progress

    # A successful first-phase run that never crossed VIDEO_READY must not be
    # advertised as a completed video.
    if int(progress.get("completed_stages") or 0) <= 4:
        return "AWAITING_SCRIPT_APPROVAL", progress
    return "FAILED", progress


def snapshot_for(episode_id: str, run: dict[str, Any]) -> dict[str, Any]:
    status, progress = classify(run, episode_id)
    if status == "AWAITING_SCRIPT_APPROVAL":
        # Use the existing public review snapshot API so older tests/fixtures and
        # the real artifact-backed implementation share one path.
        payload = review.snapshot(episode_id, run)
        payload["build"] = BUILD
        payload["state_truth_build"] = STATE_TRUTH_BUILD
        return payload
    if status == "COMPLETED":
        payload = review.snapshot(episode_id, run)
        payload["build"] = BUILD
        payload["state_truth_build"] = STATE_TRUTH_BUILD
        if not _packaging_passed(progress):
            payload["ready"] = False
            payload["status"] = "INCOMPLETE"
        return payload
    payload = dashboard._production_snapshot(episode_id, run=run)
    payload["build"] = BUILD
    payload["state_truth_build"] = STATE_TRUTH_BUILD
    if status == "FAILED":
        payload["status"] = "FAILED"
    return payload


def _current_candidate() -> tuple[str, dict[str, Any], str] | None:
    groups = _episode_runs()
    if not groups:
        return None
    hidden = _hidden_episode_ids()
    candidates: list[tuple[int, int, str, dict[str, Any], str]] = []
    rank = {
        "IN_PROGRESS": 0,
        "AWAITING_SCRIPT_APPROVAL": 1,
        "FAILED": 2,
        "COMPLETED": 3,
    }
    for order, (episode_id, runs) in enumerate(groups.items()):
        if episode_id in hidden:
            continue
        run = _best_run(runs)
        if not run:
            continue
        status, _ = classify(run, episode_id)
        candidates.append((rank.get(status, 9), order, episode_id, run, status))
    if not candidates:
        return None
    _, _, episode_id, run, status = min(candidates, key=lambda x: (x[0], x[1]))
    return episode_id, run, status


def current_job_state_truth():
    candidate = _current_candidate()
    if not candidate:
        return jsonify({"active": False, "build": BUILD, "state_truth_build": STATE_TRUTH_BUILD})
    episode_id, run, _ = candidate
    payload = snapshot_for(episode_id, run)
    payload["active"] = run.get("status") != "completed"
    payload["build"] = BUILD
    payload["state_truth_build"] = STATE_TRUTH_BUILD
    return jsonify(payload)


def job_status_state_truth(episode_id: str):
    try:
        groups = _episode_runs()
    except Exception:
        groups = {}
    run = _best_run(groups.get(episode_id, []))
    if not run:
        payload = dashboard._production_snapshot(episode_id, run=None)
        payload["build"] = BUILD
        payload["state_truth_build"] = STATE_TRUTH_BUILD
        return jsonify(payload)
    return jsonify(snapshot_for(episode_id, run))


def history_state_truth():
    hidden = _hidden_episode_ids()

    try:
        groups = _episode_runs()
    except Exception:
        # Keep older unit fixtures and temporary GitHub outages usable.
        rows = [row for row in review.history_rows_direct() if row.get("episode_id") not in hidden]
        return jsonify({"items": rows, "build": BUILD, "state_truth_build": STATE_TRUTH_BUILD})

    items: list[dict[str, Any]] = []
    for episode_id, runs in groups.items():
        if episode_id in hidden:
            continue
        run = _best_run(runs)
        if not run:
            continue
        status, _ = classify(run, episode_id)
        row: dict[str, Any] = {
            "episode_id": episode_id,
            "title": dashboard._topic_from_run(run) or episode_id,
            "created_at": run.get("created_at") or run.get("run_started_at"),
            "status": status,
            "workflow_status": run.get("status"),
            "conclusion": run.get("conclusion"),
            "run_id": run.get("id"),
            "run_url": run.get("html_url"),
            "awaiting_script_approval": status == "AWAITING_SCRIPT_APPROVAL",
        }
        if status == "COMPLETED":
            row.update(
                {
                    "video_url": f"/files/{episode_id}/video",
                    "thumbnail_url": f"/files/{episode_id}/thumbnail",
                    "captions_url": f"/files/{episode_id}/captions",
                }
            )
        items.append(row)
    return jsonify({"items": items, "build": BUILD, "state_truth_build": STATE_TRUTH_BUILD})


# Replace the live route handlers with one state model.
dashboard.app.view_functions["current_job"] = current_job_state_truth
dashboard.app.view_functions["job_status"] = job_status_state_truth
dashboard.app.view_functions["production_history"] = history_state_truth


# The base dashboard trusted localStorage before asking the server. Rewrite that
# exact restore prefix so the server's actionable episode always wins.
_SERVER_FIRST_RESTORE = """async function restore(){try{let j=await get('/api/current-job');if(j.episode_id){currentEpisode=j.episode_id;localStorage.setItem('ottam.currentEpisode',currentEpisode);showJob(j);if(j.ready)showResult(j.package);else if(!j.failure&&!j.awaiting_script_approval)pollJob();return}}catch(e){console.error(e)}let saved=localStorage.getItem('ottam.currentEpisode');"""

dashboard.PAGE = dashboard.PAGE.replace(
    "async function restore(){let saved=localStorage.getItem('ottam.currentEpisode');",
    _SERVER_FIRST_RESTORE,
)


# The original script-review block is physically before the main dashboard JS,
# so its first attempt to wrap showJob can run too early. This late bridge is
# appended after every existing script and installs only when showJob exists.
# History deletion is intentionally owned only by dashboard_run_control; do not
# add another Delete button here.
_STATE_TRUTH_JS = r'''
<script>
(function(){
  function installReviewBridge(){
    if(typeof showJob!=='function' || window.__ottamReviewBridgeInstalled)return;
    window.__ottamReviewBridgeInstalled=true;
    const original=showJob;
    showJob=window.showJob=function(j){
      original(j);
      const panel=document.getElementById('scriptReviewPanel');
      if(!panel)return;
      const waiting=!!j.awaiting_script_approval;
      panel.classList.toggle('hidden',!waiting);
      if(waiting){
        const text=document.getElementById('scriptReviewText'); if(text)text.value=j.script||'';
        const ws=document.getElementById('workflowState'); if(ws)ws.textContent='awaiting script approval';
        const cs=document.getElementById('currentStage'); if(cs)cs.textContent='Script review';
        const js=document.getElementById('jobStatus'); if(js)js.textContent='4/10 stages complete · waiting for your approval';
        const bar=document.getElementById('bar'); if(bar)bar.style.width='40%';
      }
    };
  }

  async function reconcile(){
    installReviewBridge();
    try{
      const r=await fetch('/api/current-job',{cache:'no-store'}); if(!r.ok)return;
      const j=await r.json(); if(!j.episode_id)return;
      currentEpisode=j.episode_id; localStorage.setItem('ottam.currentEpisode',currentEpisode);
      showJob(j); if(j.ready)showResult(j.package);
    }catch(e){console.error('OTTAM reconcile failed',e)}
  }

  setTimeout(reconcile,50);
  setTimeout(reconcile,1000);
})();
</script>
'''

dashboard.PAGE = dashboard.PAGE.replace("</body>", _STATE_TRUTH_JS + "</body>")


def index_state_truth() -> Response:
    return Response(dashboard.PAGE, mimetype="text/html")


dashboard.app.view_functions["index"] = index_state_truth

app = dashboard.app
