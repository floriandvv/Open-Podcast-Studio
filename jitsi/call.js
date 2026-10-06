/* Call UI uses the IFrame API for both providers; the host never joins. */
(() => {
'use strict';
const {el,t,error,request}=window.PodcastUX;
let library=null,libraryOrigin='';
function loadSDK(origin){
  if(libraryOrigin&&libraryOrigin!==origin)return Promise.reject(new Error('ux.reload_after_recording'));
  if(!library){
    libraryOrigin=origin;
    library=new Promise((resolve,reject)=>{
      const script=el('script');let done=false;
      const finish=e=>{if(done)return;done=true;clearTimeout(timer);script.onload=script.onerror=null;if(e){script.remove();reject(e);}else resolve(window.JitsiMeetExternalAPI);};
      const timer=setTimeout(()=>finish(new Error('ux.provider_unavailable')),15000);
      script.src=origin+'/external_api.js';script.referrerPolicy='no-referrer';
      script.onload=()=>finish(typeof window.JitsiMeetExternalAPI==='function'?null:new Error('ux.provider_unavailable'));
      script.onerror=()=>finish(new Error('ux.embed_failed'));document.head.append(script);
    }).catch(e=>{library=null;throw e;});
  }
  return library;
}
function style(){if(document.getElementById('call-style'))return;const css=el('style',undefined,{id:'call-style'});css.textContent=`.call-panel{margin:0;padding:12px 16px;min-width:0;border:1px solid var(--border);border-radius:10px;background:var(--panel)}.call-panel[hidden]{display:none!important}.call-panel p{margin:0;font-size:13px;line-height:1.5;overflow-wrap:anywhere}.call-frame{width:100%;height:360px;margin-top:10px;min-width:0}.call-frame.prejoin{height:460px}.call-frame iframe{border:0;max-width:100%}.call-provider{color:var(--muted);font-size:12px!important}.call-toggle:focus-visible{outline:2px solid var(--brand);outline-offset:3px}@media(max-width:600px){.call-frame{height:360px}.call-frame.prejoin{height:480px}}`;document.head.append(css);}
class CallBase {
  constructor(o){this.o=o;this.room=o.room;this.snapshot=null;this.busy=false;this.disposed=false;style();}
  headers(){return this.o.token?{Authorization:'Bearer '+this.o.token}:{'X-Host-Client':this.o.clientId()};}
  path(action){return '/api/jitsi/'+action+'/'+encodeURIComponent(this.room);}
  request(action,method='GET',body){return request(this.path(action),{method,headers:this.headers(),body:body===undefined?undefined:JSON.stringify(body)});}
  begin(){this.poll();this.timer=setInterval(()=>this.poll(),3000);window.addEventListener('pagehide',()=>this.dispose());}
  dispose(){this.disposed=true;clearInterval(this.timer);}
}
class HostCall extends CallBase {
  constructor(o){super(o);this.button=el('button',t('call_off'),{type:'button',class:'meta-pill call-toggle','data-lock-control':'','aria-label':t('call_cycle')});this.button.disabled=true;document.getElementById('modeBtn').after(this.button);
    this.panel=el('section',undefined,{class:'call-panel','aria-label':t('live_call')});this.status=el('p',t('loading'),{role:'status','aria-live':'polite'});this.panel.append(this.status);document.querySelector('.wrap').append(this.panel);
    this.button.onclick=()=>this.cycle();this.begin();
  }
  render(){const s=this.snapshot;if(!s)return;const c=s.call;const m=c?.state==='active'?c.mode:'off';this.button.textContent=t('call_'+m);this.button.setAttribute('aria-label',t('call_cycle')+': '+t('call_'+m));this.button.classList.toggle('active',m!=='off');this.button.disabled=this.changing||!s.configured||c?.state==='closing'||!this.o.canControl();
    this.status.textContent=s.deployment==='off'?t('jitsi_off'):!s.configured?t('jitsi_unavailable'):c?.state==='closing'?t('call_closing'):c?t('call_'+m)+' · '+s.participants+' '+t('connected_guests'):t('call_off');
  }
  async poll(){if(this.busy||this.disposed)return;this.busy=true;try{this.snapshot=await this.request('state');if(!this.changing)this.render();}catch(e){this.status.textContent=error(e);this.button.disabled=true;}finally{this.busy=false;}}
  async cycle(){if(this.changing||!this.snapshot||!this.o.canControl())return;this.changing=true;this.button.disabled=true;const call=this.snapshot.call;const mode=call?.mode||'off';const next={off:'audio',audio:'video',video:'off'}[mode];this.status.textContent=t('saving');
    try{this.snapshot=await this.request('mode','PUT',{mode:next,expected_id:call?.id||null,expected_revision:call?.revision||0});this.changing=false;this.render();}
    catch(e){this.changing=false;await this.poll();this.status.textContent=error(e);}finally{this.changing=false;}
  }
}
class GuestCall extends CallBase {
  constructor(o){super(o);this.generation=0;this.state='left';this.retryAt=0;this.modeChain=Promise.resolve();this.panel=el('section',undefined,{class:'call-panel',hidden:'','aria-label':t('live_call')});this.status=el('p','',{role:'status','aria-live':'polite'});this.provider=el('p','',{class:'call-provider'});this.panel.append(this.status,this.provider);o.parent.append(this.panel);this.begin();}
  async poll(){if(this.busy||this.disposed)return;this.busy=true;
    try{const s=await this.request('state');this.snapshot=s;Promise.resolve(this.o.onState?.(s)).catch(()=>{});
      const call=s.call,active=s.configured&&call?.state==='active'&&call.deadline>Date.now()/1000;
      if(!active){this.disconnect();this.panel.hidden=true;this.dismissed=null;return;}
      this.panel.hidden=false;this.provider.textContent=s.deployment==='public'?t('public_call_note')+' '+s.origin:t('separate_devices');
      if(this.call?.id!==call.id){this.disconnect();this.call=call;this.dismissed=null;this.retryAt=0;}
      if(!this.api&&!this.joining&&this.dismissed!==call.id&&Date.now()>this.retryAt)await this.join();
      if(this.api&&this.appliedMode!==call.mode)await this.applyMode(call.mode);
      if(this.api)this.report(this.state);
    }catch(e){if(e.status===401||e.status===403){this.disconnect();this.panel.hidden=true;}else if(!this.panel.hidden)this.status.textContent=error(e);}
    finally{this.busy=false;}
  }
  async join(){const generation=++this.generation;this.joining=true;this.state='joining';this.status.textContent=t('prejoin_loading');
    try{const grant=await this.request('grant','POST',{name:this.o.name()});const API=await loadSDK(grant.origin);if(generation!==this.generation||this.disposed)return;
      this.call=grant.call;this.frame=el('div',undefined,{class:'call-frame prejoin'});this.panel.append(this.frame);
      const audio=grant.call.mode==='audio';const api=new API(grant.domain,{roomName:grant.call.conference,parentNode:this.frame,width:'100%',height:'100%',...(grant.jwt?{jwt:grant.jwt}:{}),lang:window.OpenPodcastI18n.locale,
        userInfo:{displayName:grant.name},configOverwrite:{prejoinConfig:{enabled:true},startAudioOnly:audio,startWithVideoMuted:audio,disableDeepLinking:true,toolbarButtons:this.toolbar(grant.call.mode)}});
      this.api=api;this.appliedMode=grant.call.mode;this.o.budget.maxDuringCall=0;this.o.budget.update(true,'unknown');api.getIFrame().title=t('live_call');api.getIFrame().referrerPolicy='no-referrer';
      const current=()=>this.api===api&&generation===this.generation;
      api.addListener('videoConferenceJoined',()=>{if(!current())return;this.state='joined';this.frame.classList.remove('prejoin');this.status.textContent=t('call_connected');this.report('joined');this.applyMode(this.snapshot.call.mode,true);});
      const ended=()=>{if(!current())return;this.dismissed=this.call.id;this.report('left');this.disconnect();this.status.textContent=t('call_left');};
      api.addListener('videoConferenceLeft',ended);api.addListener('readyToClose',ended);
      api.addListener('videoMuteStatusChanged',e=>{if(current()&&!e.muted&&this.snapshot?.call?.mode==='audio')this.enforceAudio();});
      api.addListener('errorOccurred',()=>{if(current()){this.status.textContent=t('provider_error');this.report('failed');}});
      this.status.textContent=t('prejoin_ready');
    }catch(e){if(generation===this.generation){this.disconnect();this.retryAt=Date.now()+30000;this.status.textContent=error(e);}}
    finally{this.joining=false;}
  }
  toolbar(mode){return ['microphone',...(mode==='video'?['camera']:[]),'settings','tileview','hangup'];}
  async enforceAudio(){if(this.muting||!this.api)return;const api=this.api;this.muting=true;
    try{const muted=await api.isVideoMuted();if(this.api===api&&this.snapshot?.call?.mode==='audio'&&!muted)api.executeCommand('toggleVideo');}
    catch{this.status.textContent=t('mode_provider_limit');}finally{this.muting=false;}}
  applyMode(mode,force=false){this.modeChain=this.modeChain.catch(()=>{}).then(async()=>{const api=this.api;if(!api||!force&&this.appliedMode===mode)return;
      api.executeCommand('overwriteConfig',{toolbarButtons:this.toolbar(mode)});
      api.executeCommand('setAudioOnly',mode==='audio');
      if(mode==='audio')await this.enforceAudio();
      if(this.api!==api)return;this.appliedMode=mode;
      this.status.textContent=mode==='video'?t('video_available'):t('call_audio');
      // Never auto-enable a camera, never rebuild the iframe on a mode change.
    }).catch(e=>{if(!this.disposed)this.status.textContent=error(e);});return this.modeChain;}
  disconnect(){++this.generation;const api=this.api;this.api=null;this.appliedMode=null;try{api?.dispose();}catch{}this.frame?.remove();this.frame=null;this.o.budget.update(false,'unknown');}
  dispose(){this.report('left');this.disconnect();super.dispose();}
}
// Event routes include room and call ID, unlike room-only state endpoints.
GuestCall.prototype.report=function(state){if(!this.call||!['joining','joined','left','failed'].includes(state))return;request('/api/jitsi/event/'+encodeURIComponent(this.room)+'/'+this.call.id,{method:'POST',headers:this.headers(),body:JSON.stringify({state})}).catch(()=>{});};
window.PodcastJitsi={mountHost:o=>new HostCall(o),mountGuest:o=>new GuestCall(o)};
})();
