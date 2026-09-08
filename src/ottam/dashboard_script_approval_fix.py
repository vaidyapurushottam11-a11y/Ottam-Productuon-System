from __future__ import annotations

import threading
from typing import Any

from flask import jsonify

from . import dashboard
from . import dashboard_run_control as run_control


BUILD = "script-approval-current-run-v1"
_approval_lock = threading.Lock()
_approval_inflight: set[str] = set()


def _best_run_fixed(runs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Choose the run that represents real work for the dashboard.

    GitHub returns runs newest-first. A duplicate approval can therefore create a
    newer pending run while the original continuation is already in_progress.
    Always prefer the in-progress run so the dashboard stays attached to the
    production that is actually doing work.
    """
    if not runs:
        return None

    running = next((run for run in runs if run.get("status") == "in_progress"), None)
    if running:
        return running

    queued = next(
        (
            run
            for run in runs
            if run.get("status") not in {"completed", "in_progress"}
        ),
        None,
    )
    if queued:
        return queued

    meaningful = next((run for run in runs if run.get("conclusion") != "cancelled"), None)
    return meaningful or runs[0]


# dashboard_run_control resolves _best_run dynamically, so replacing it here
# fixes /api/current-job, /api/jobs/<episode>, and history selection together.
run_control._best_run = _best_run_fixed


_original_approve_script = dashboard.app.view_functions.get("approve_script")


def _synthetic_continuation_snapshot(episode_id: str) -> dict[str, Any]:
    job = dashboard._load_job(episode_id)
    job.update(
        {
            "episode_id": episode_id,
            "status": "queued",
            "conclusion": None,
            "active": True,
            "awaiting_script_approval": False,
            "deduplicated": True,
            "approval_inflight": True,
        }
    )
    progress = dict(job.get("progress") or {})
    progress.update(
        {
            "workflow_status": "queued",
            "current_stage": "Approval accepted — starting continuation",
            "stage_index": max(4, int(progress.get("stage_index") or 0)),
            "stage_total": 10,
            "completed_stages": max(4, int(progress.get("completed_stages") or 0)),
            "progress_percent": max(40, int(progress.get("progress_percent") or 0)),
            "stage_elapsed_seconds": None,
        }
    )
    job["progress"] = progress
    return job


def approve_script_once(episode_id: str):
    """Make Approve & Continue idempotent for the lifetime of the dashboard process."""
    if _original_approve_script is None:
        return jsonify({"error": "script approval handler is unavailable"}), 500

    with _approval_lock:
        runs = run_control._runs_for_episode(episode_id)
        active = _best_run_fixed(runs)
        if active and active.get("status") != "completed":
            payload = run_control._snapshot_for_run(active)
            payload["deduplicated"] = True
            payload["already_running"] = True
            payload["build"] = BUILD
            return jsonify(payload)

        if episode_id in _approval_inflight:
            payload = _synthetic_continuation_snapshot(episode_id)
            payload["build"] = BUILD
            return jsonify(payload)

        _approval_inflight.add(episode_id)

    response = dashboard.app.make_response(_original_approve_script(episode_id))
    if response.status_code >= 400:
        with _approval_lock:
            _approval_inflight.discard(episode_id)
    return response


if _original_approve_script is not None:
    dashboard.app.view_functions["approve_script"] = approve_script_once


SCRIPT_APPROVAL_FIX_JS = r'''
<!-- script-approval-current-run-v1 -->
<script>
(function(){
  if(window.__ottamScriptApprovalFixInstalled)return;
  window.__ottamScriptApprovalFixInstalled=true;

  const panel=document.getElementById('scriptReviewPanel');
  const approve=document.getElementById('approveScript');
  const workflowState=document.getElementById('workflowState');
  const currentStage=document.getElementById('currentStage');
  const jobStatus=document.getElementById('jobStatus');
  const bar=document.getElementById('bar');

  function showContinuationState(){
    if(panel)panel.classList.add('hidden');
    if(workflowState)workflowState.textContent='queued';
    if(currentStage)currentStage.textContent='Approval accepted — starting continuation';
    if(jobStatus)jobStatus.textContent='4/10 stages complete · continuing to narration and visuals';
    if(bar){const n=parseFloat(bar.style.width||'0')||0;bar.style.width=Math.max(40,n)+'%'}
  }

  if(typeof showJob==='function'){
    const previousShowJob=showJob;
    showJob=window.showJob=function(j){
      previousShowJob(j);
      const committed=window.__ottamApprovedEpisode;
      if(committed && j && j.episode_id===committed){
        if(j.awaiting_script_approval){
          // GitHub may briefly keep serving the completed review run while the
          // continuation run is being registered. Do not reopen the review UI.
          showContinuationState();
        }else{
          window.__ottamApprovedEpisode=null;
          if(panel)panel.classList.add('hidden');
        }
      }
    };
  }

  if(approve && typeof approve.onclick==='function'){
    const previousApprove=approve.onclick;
    approve.onclick=function(event){
      if(currentEpisode)window.__ottamApprovedEpisode=currentEpisode;
      approve.disabled=true;
      showContinuationState();
      return previousApprove.call(this,event);
    };
  }
})();
</script>
'''


if "script-approval-current-run-v1" not in dashboard.PAGE:
    dashboard.PAGE = dashboard.PAGE.replace("</body>", SCRIPT_APPROVAL_FIX_JS + "</body>")


app = dashboard.app
