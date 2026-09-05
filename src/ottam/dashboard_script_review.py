from __future__ import annotations

import json
from typing import Any

from flask import jsonify, request

from . import dashboard
from . import dashboard_cold_recovery as cold
from . import dashboard_history  # noqa: F401 - importing registers history routes/UI


def _review_marker(run: dict[str, Any], episode_id: str) -> dict[str, Any] | None:
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        return None
    return dashboard._artifact(int(run["id"]), f"ottam-script-review-{episode_id}")


def _review_snapshot(episode_id: str, run: dict[str, Any]) -> dict[str, Any]:
    cache = dashboard._hydrate_episode_cache(episode_id, run)
    job = dashboard._load_job(episode_id)
    job["episode_id"] = episode_id
    job["topic_title"] = job.get("topic_title") or dashboard._topic_from_run(run)
    if not job.get("topic"):
        cached_topic = dashboard._topic_from_cache(cache, episode_id)
        if cached_topic:
            job["topic"] = cached_topic

    state = dashboard._episode_state(cache, episode_id) or {}
    script_path = cache / "episodes" / episode_id / "script.txt"
    qa_path = cache / "episodes" / episode_id / "script_qa.json"
    hook_path = cache / "episodes" / episode_id / "hook_qa.json"

    progress = dashboard._run_progress(run)
    progress["workflow_status"] = "awaiting_script_approval"
    progress["current_stage"] = "Script ready — waiting for your approval"
    progress["stage_index"] = 4
    progress["completed_stages"] = max(4, int(progress.get("completed_stages") or 0))
    progress["progress_percent"] = 40
    progress["stage_elapsed_seconds"] = None

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
    if qa_path.exists():
        try:
            job["script_qa"] = json.loads(qa_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    if hook_path.exists():
        try:
            job["hook_qa"] = json.loads(hook_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    dashboard._save_job(episode_id, job)
    return job


_original_snapshot = dashboard._production_snapshot


def production_snapshot_with_review(episode_id: str, run: dict[str, Any] | None = None) -> dict[str, Any]:
    run = run or dashboard._find_run("production.yml", f"OTTAM Production {episode_id}")
    if run and _review_marker(run, episode_id):
        return _review_snapshot(episode_id, run)
    return _original_snapshot(episode_id, run=run)


dashboard._production_snapshot = production_snapshot_with_review


_original_cold_snapshot = cold._snapshot_without_blocking


def cold_snapshot_with_review(episode_id: str, run: dict[str, Any]) -> dict[str, Any]:
    if _review_marker(run, episode_id):
        # Script-review artifacts are small and intentionally do not contain a
        # final.mp4. Hydrate them directly instead of starting the completed-video
        # background restore loop.
        return _review_snapshot(episode_id, run)
    return _original_cold_snapshot(episode_id, run)


cold._snapshot_without_blocking = cold_snapshot_with_review


def _topic_for_job(job: dict[str, Any]) -> dict[str, Any] | None:
    topic = job.get("topic")
    return topic if isinstance(topic, dict) else None


def _dispatch_script_phase(episode_id: str, *, approved: bool, instruction: str = "") -> dict[str, Any]:
    job = production_snapshot_with_review(episode_id)
    if not job.get("awaiting_script_approval"):
        return {"error": "episode is not waiting for script approval", "http_status": 409}
    topic = _topic_for_job(job)
    if not topic:
        return {"error": "selected topic could not be recovered", "http_status": 409}
    title = str(job.get("topic_title") or topic.get("title") or "Selected topic")[:120]
    dashboard._dispatch(
        "production.yml",
        {
            "episode_id": episode_id,
            "selected_topic_title": title,
            "selected_topic_json": json.dumps(topic, separators=(",", ":"), ensure_ascii=False),
            "resume_failed": "false",
            "script_approved": "true" if approved else "false",
            "script_revision_instruction": instruction,
        },
    )
    job["status"] = "queued"
    job["conclusion"] = None
    job["awaiting_script_approval"] = False
    dashboard._save_job(episode_id, job)
    return {"status": "queued", "episode_id": episode_id}


@dashboard.app.post("/api/jobs/<episode_id>/approve-script")
def approve_script(episode_id: str):
    result = _dispatch_script_phase(episode_id, approved=True)
    status = int(result.pop("http_status", 200))
    return jsonify(result), status


@dashboard.app.post("/api/jobs/<episode_id>/revise-script")
def revise_script(episode_id: str):
    body = request.get_json(silent=True) or {}
    instruction = str(body.get("instruction") or "").strip()
    if not instruction:
        instruction = (
            "Reject this version and rewrite it with a substantially stronger, more relatable opening hook, "
            "a cleaner curiosity arc, less generic explanation, and a stronger final payoff. Preserve factual boundaries."
        )
    result = _dispatch_script_phase(episode_id, approved=False, instruction=instruction)
    status = int(result.pop("http_status", 200))
    return jsonify(result), status


def retry_job_with_script_phase(episode_id: str):
    job = production_snapshot_with_review(episode_id)
    if job.get("ready"):
        return jsonify({"error": "episode is already VIDEO_READY"}), 409
    if not job.get("conclusion") or job.get("conclusion") == "success":
        return jsonify({"error": "episode is not in a failed state"}), 409

    active = next(
        (
            run
            for run in dashboard._runs("production.yml")
            if f"OTTAM Production {episode_id}" in str(run.get("display_title") or "") and run.get("status") != "completed"
        ),
        None,
    )
    if active:
        return jsonify({"error": "a retry is already running", "run_id": active.get("id")}), 409

    topic = _topic_for_job(job)
    if not topic:
        return jsonify({"error": "selected topic could not be recovered for retry"}), 409
    title = str(job.get("topic_title") or topic.get("title") or "Selected topic")[:120]
    failed_index = int((job.get("failure") or {}).get("stage_index") or 0)
    dashboard._dispatch(
        "production.yml",
        {
            "episode_id": episode_id,
            "selected_topic_title": title,
            "selected_topic_json": json.dumps(topic, separators=(",", ":"), ensure_ascii=False),
            "resume_failed": "true",
            # A failure at/after narration could only occur after the user had
            # approved the script, so retries must stay past the approval gate.
            "script_approved": "true" if failed_index >= 5 else "false",
            "script_revision_instruction": "",
        },
    )
    job["status"] = "queued"
    job["conclusion"] = None
    job["ready"] = False
    job["failure"] = None
    job["can_retry"] = False
    job["retry_count"] = int(job.get("retry_count") or 0) + 1
    dashboard._save_job(episode_id, job)
    return jsonify({"status": "queued", "episode_id": episode_id, "retry_count": job["retry_count"]})


dashboard.app.view_functions["retry_job"] = retry_job_with_script_phase


SCRIPT_REVIEW_FRAGMENT = r'''
<style>
.scriptReview{border-color:#6e5b1b;background:#1c1a10}.scriptReview textarea.scriptBody{min-height:420px;line-height:1.55;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:14px;resize:vertical}.scriptReview .reviewNote{margin:8px 0 14px;color:#f2cc60}.scriptReview .reviewActions{display:flex;gap:9px;flex-wrap:wrap;margin-top:10px}.scriptReview .approve{background:#238636}.scriptReview .reject{background:#b62324}.scriptReview .changeBox{margin-top:14px}
</style>
<div id="scriptReviewPanel" class="card scriptReview hidden">
  <div class="row"><div><h2 style="margin:0">Review Script Before Production</h2><div class="reviewNote">Narration and Magnific visuals will not start until you approve this script.</div></div><span id="hookScore" class="muted"></span></div>
  <textarea id="scriptReviewText" class="scriptBody" readonly></textarea>
  <div class="changeBox"><textarea id="scriptChangeInstruction" placeholder="Optional changes: e.g. make the first 20 seconds more relatable, shorten the middle example, stronger payoff..."></textarea></div>
  <div class="reviewActions">
    <button id="approveScript" class="approve">Approve & Continue</button>
    <button id="applyScriptChanges" class="secondary">Apply Suggested Changes</button>
    <button id="rejectScript" class="reject">Reject & Regenerate</button>
    <span id="scriptReviewStatus" class="muted"></span>
  </div>
</div>
<script>
(function(){
const panel=document.getElementById('scriptReviewPanel'),text=document.getElementById('scriptReviewText'),instruction=document.getElementById('scriptChangeInstruction'),status=document.getElementById('scriptReviewStatus'),score=document.getElementById('hookScore');
function renderReview(j){const waiting=!!j.awaiting_script_approval;panel.classList.toggle('hidden',!waiting);if(!waiting)return;text.value=j.script||'';const h=j.hook_qa?.history||[];const last=h.length?h[h.length-1]:null;score.textContent=last?`Hook QA ${Math.round(last.average_score||0)}/100`:'';if(j.progress){document.getElementById('workflowState').textContent='awaiting script approval';document.getElementById('currentStage').textContent='Script review';document.getElementById('jobStatus').textContent='4/10 stages complete · waiting for your approval';document.getElementById('bar').style.width='40%'}}
const previousShowJob=window.showJob||showJob;window.showJob=showJob=function(j){previousShowJob(j);renderReview(j)};
async function act(path,payload,message){document.getElementById('approveScript').disabled=true;document.getElementById('applyScriptChanges').disabled=true;document.getElementById('rejectScript').disabled=true;status.textContent=message;try{await post('/api/jobs/'+currentEpisode+'/'+path,payload||{});panel.classList.add('hidden');status.textContent='';await sleep(1200);if(!polling)pollJob()}catch(e){status.textContent=e.message;document.getElementById('approveScript').disabled=false;document.getElementById('applyScriptChanges').disabled=false;document.getElementById('rejectScript').disabled=false}}
document.getElementById('approveScript').onclick=()=>act('approve-script',{},'Approved — continuing from narration…');document.getElementById('applyScriptChanges').onclick=()=>{const v=instruction.value.trim();if(!v){status.textContent='Type the changes you want first.';return}act('revise-script',{instruction:v},'Revising script, then re-running fact-check and hook QA…')};document.getElementById('rejectScript').onclick=()=>act('revise-script',{instruction:''},'Regenerating a stronger script…');
setTimeout(async()=>{try{if(currentEpisode){const j=await get('/api/jobs/'+currentEpisode);renderReview(j)}}catch(e){}},600);
})();
</script>
'''

if "id=\"scriptReviewPanel\"" not in dashboard.PAGE:
    dashboard.PAGE = dashboard.PAGE.replace('<div id="resultPanel"', SCRIPT_REVIEW_FRAGMENT + '<div id="resultPanel"')

app = dashboard.app
