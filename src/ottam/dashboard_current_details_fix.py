from __future__ import annotations

from . import dashboard

BUILD = "current-details-v1"

CURRENT_DETAILS_JS = r'''
<!-- current-details-v1 -->
<script>
(function(){
  if(window.__ottamCurrentDetailsV1Installed)return;
  window.__ottamCurrentDetailsV1Installed=true;

  async function fetchJson(url){
    const r=await fetch(url,{cache:'no-store'});
    if(!r.ok)throw new Error(await r.text());
    return r.json();
  }

  async function loadEpisode(ep){
    if(!ep)return false;
    try{
      const j=await fetchJson('/api/jobs/'+encodeURIComponent(ep)+'?ui_build=current-details-v1');
      if(!j||!j.episode_id)return false;
      currentEpisode=j.episode_id;
      localStorage.setItem('ottam.currentEpisode',currentEpisode);
      if(typeof showJob==='function')showJob(j);
      if(j.ready && typeof showResult==='function')showResult(j.package);
      else if(!j.awaiting_script_approval && !j.failure && typeof pollJob==='function'){
        polling=false;
        pollJob();
      }
      return true;
    }catch(e){
      console.error('Unable to restore episode details',e);
      return false;
    }
  }

  async function restoreCurrentDetails(){
    try{
      const current=await fetchJson('/api/current-job?ui_build=current-details-v1');
      if(current && current.episode_id){
        currentEpisode=current.episode_id;
        localStorage.setItem('ottam.currentEpisode',currentEpisode);
        if(typeof showJob==='function')showJob(current);
        if(current.ready && typeof showResult==='function')showResult(current.package);
        else if(!current.awaiting_script_approval && !current.failure && typeof pollJob==='function'){
          polling=false;
          pollJob();
        }
        return;
      }
    }catch(e){
      console.error('Current-job restore failed',e);
    }

    // Fallback for failed/stopped runs: production history is still authoritative.
    // Prefer the newest actionable episode instead of leaving the main details card hidden.
    try{
      const history=await fetchJson('/api/history?ui_build=current-details-v1');
      const items=history.items||[];
      const actionable=items.find(x=>x.status==='IN_PROGRESS'||x.status==='AWAITING_SCRIPT_APPROVAL'||x.status==='FAILED');
      if(actionable && actionable.episode_id){
        await loadEpisode(actionable.episode_id);
        return;
      }
    }catch(e){
      console.error('History fallback restore failed',e);
    }

    const saved=localStorage.getItem('ottam.currentEpisode');
    if(saved)await loadEpisode(saved);
  }

  // Make failed/in-progress history cards open the in-page production details card too.
  document.addEventListener('click',function(e){
    const item=e.target.closest('.historyItem');
    if(!item)return;
    const meta=item.querySelector('.historyMeta');
    if(!meta)return;
    const m=meta.textContent.match(/(OTTAM-[A-Z0-9-]+)/);
    if(!m)return;
    const badge=item.querySelector('.historyBadge');
    const status=(badge?.textContent||'').trim().toUpperCase();
    if(status==='FAILED'||status==='IN PROGRESS'||status==='AWAITING SCRIPT APPROVAL'){
      loadEpisode(m[1]).then(ok=>{
        if(ok){
          const panel=document.getElementById('jobPanel');
          if(panel)panel.scrollIntoView({behavior:'smooth',block:'start'});
        }
      });
    }
  });

  setTimeout(restoreCurrentDetails,100);
  setTimeout(restoreCurrentDetails,900);
})();
</script>
'''

if BUILD not in dashboard.PAGE:
    dashboard.PAGE = dashboard.PAGE.replace("</body>", CURRENT_DETAILS_JS + "</body>")

app = dashboard.app
