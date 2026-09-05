from __future__ import annotations

from . import dashboard


SCRIPT_REVIEW_ACTIONS_JS = r'''
<!-- script-review-actions-v1 -->
<script>
(function(){
  if(window.__ottamScriptReviewActionsInstalled)return;
  window.__ottamScriptReviewActionsInstalled=true;

  const panel=document.getElementById('scriptReviewPanel');
  const text=document.getElementById('scriptReviewText');
  const instruction=document.getElementById('scriptChangeInstruction');
  const status=document.getElementById('scriptReviewStatus');
  const score=document.getElementById('hookScore');
  const approve=document.getElementById('approveScript');
  const apply=document.getElementById('applyScriptChanges');
  const reject=document.getElementById('rejectScript');
  if(!panel||!approve||!apply||!reject)return;

  function setDisabled(value){approve.disabled=value;apply.disabled=value;reject.disabled=value}

  function renderReview(j){
    const waiting=!!j.awaiting_script_approval;
    panel.classList.toggle('hidden',!waiting);
    if(!waiting)return;
    if(text)text.value=j.script||'';
    const h=j.hook_qa?.history||[];
    const last=h.length?h[h.length-1]:null;
    if(score)score.textContent=last?`Hook QA ${Math.round(last.average_score||0)}/100`:'';
    const ws=document.getElementById('workflowState'); if(ws)ws.textContent='awaiting script approval';
    const cs=document.getElementById('currentStage'); if(cs)cs.textContent='Script review';
    const js=document.getElementById('jobStatus'); if(js)js.textContent='4/10 stages complete · waiting for your approval';
    const bar=document.getElementById('bar'); if(bar)bar.style.width='40%';
    setDisabled(false);
  }

  if(typeof showJob==='function'){
    const previous=showJob;
    showJob=window.showJob=function(j){previous(j);renderReview(j)};
  }

  async function waitForNewRun(label){
    const episode=currentEpisode;
    for(let i=0;i<40;i++){
      await sleep(1500);
      if(!episode||episode!==currentEpisode)return;
      try{
        const j=await get('/api/jobs/'+encodeURIComponent(episode));
        if(j.awaiting_script_approval){
          if(status)status.textContent=label+' queued — waiting for GitHub runner…';
          continue;
        }
        if(status)status.textContent='';
        showJob(j);
        if(j.ready){showResult(j.package);return}
        if(!j.failure){polling=false;pollJob()}
        return;
      }catch(e){
        if(status)status.textContent=label+' queued — waiting for dashboard state…';
      }
    }
    if(status)status.textContent=label+' was queued. GitHub is taking longer than usual; refresh shortly to see progress.';
  }

  async function act(path,payload,label,initialMessage){
    if(!currentEpisode){if(status)status.textContent='No episode is selected.';return}
    setDisabled(true);
    if(status)status.textContent=initialMessage;
    try{
      await post('/api/jobs/'+encodeURIComponent(currentEpisode)+'/'+path,payload||{});
      await waitForNewRun(label);
    }catch(e){
      if(status)status.textContent='Action failed: '+e.message;
      setDisabled(false);
    }
  }

  approve.onclick=()=>act(
    'approve-script',{},'Approval','Approved — starting narration and the remaining production stages…'
  );

  apply.onclick=()=>{
    const value=(instruction?.value||'').trim();
    if(!value){if(status)status.textContent='Type the changes you want first.';return}
    act(
      'revise-script',{instruction:value},'Script revision',
      'Applying your changes — then re-running fact-check and hook/script QA…'
    );
  };

  reject.onclick=()=>act(
    'revise-script',{instruction:''},'Script regeneration',
    'Rejected — regenerating a stronger script, then re-running fact-check and QA…'
  );

  setTimeout(async()=>{
    try{
      if(currentEpisode){const j=await get('/api/jobs/'+encodeURIComponent(currentEpisode));renderReview(j)}
    }catch(e){console.error('script review action restore failed',e)}
  },250);
})();
</script>
'''


if "script-review-actions-v1" not in dashboard.PAGE:
    dashboard.PAGE = dashboard.PAGE.replace("</body>", SCRIPT_REVIEW_ACTIONS_JS + "</body>")


app = dashboard.app
