/* Open Podcast. One media owner; recorder and call consume independent tracks. */
(function(root){
'use strict';
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
async function mediaOperation(label, operation) {
  let timer;
  try {
    return await Promise.race([operation(),new Promise((_,reject)=>{
      timer=setTimeout(()=>reject(new Error(label+': Medienvorbereitung nicht innerhalb von 15 Sekunden abgeschlossen')),15000);
    })]);
  } finally { clearTimeout(timer); }
}
class MediaOwner extends EventTarget {
  constructor(devices=navigator.mediaDevices){super();this.devices=devices;this.source=null;this.tail=Promise.resolve();this.recording=null;this.revision=0;this.options={};}
  emit(type,detail){this.dispatchEvent(new CustomEvent(type,{detail}));}
  configure(options){
    const job=this.tail.catch(()=>{}).then(()=>this.acquire(options));this.tail=job;return job;
  }
  async acquire(options){
    const o={...this.options,...options};
    const previous=this.source;
    if(previous && previous.getTracks().every(t=>t.readyState==='live') && JSON.stringify(o)===JSON.stringify(this.options))return previous;
    // Ask only once per device transition. No permission-only GUM, no SDK GUM.
    const candidate=await this.devices.getUserMedia({audio:{deviceId:o.mic?{exact:o.mic}:undefined,sampleRate:{ideal:o.sampleRate||48000},channelCount:{ideal:o.channels||1},echoCancellation:false,noiseSuppression:false,autoGainControl:false},video:o.video?{deviceId:o.camera?{exact:o.camera}:undefined,width:{ideal:o.width||1920},height:{ideal:o.height||1080},frameRate:{ideal:o.fps||30}}:false});
    try {
      for(const kind of ['audio',...(o.video?['video']:[])]){
        const t=candidate.getTracks().find(t=>t.kind===kind);if(!t||t.readyState!=='live')throw new Error('Kein aktiver '+kind+'-Track');
        const desired=kind==='audio'?o.mic:o.camera, actual=t.getSettings().deviceId;
        if(desired && desired!=='default' && actual!==desired)throw new Error('Aktives Gerät weicht von der Auswahl ab');
      }
      // Stable recording graph is rebound before the old physical source is disposed.
      if(this.recording)await this.recording.bind(candidate);
      this.source=candidate;this.options=o;const revision=++this.revision;
      candidate.getTracks().forEach(t=>t.addEventListener('ended',()=>{if(revision===this.revision)this.emit('lost',{kind:t.kind,recording:!!this.recording});},{once:true}));
      this.emit('source',{stream:candidate,previous,revision,settings:candidate.getTracks().map(t=>({kind:t.kind,...t.getSettings()}))});
      if(previous)previous.getTracks().forEach(t=>t.stop());
      return candidate;
    } catch(e){candidate.getTracks().forEach(t=>t.stop());throw e;}
  }

  async recordingStream(profile){
    if(this.recording)throw new Error('Aufnahmegraph bereits aktiv');
    if(!this.source)throw new Error('Keine Medienquelle');
    const AC = window.AudioContext || window.webkitAudioContext;
    const ownsContext=!profile.audioContext;
    const ctx=profile.audioContext||new AC({sampleRate:profile.sample_rate||48000});
    // Resume only inside the cleanup-protected block below.
    const sink=ctx.createMediaStreamDestination();sink.channelCount=profile.channels||1;
    let input=null,raf=0,video=null,canvas=null,canvasTrack=null,lastFrame=0;
    const output=new MediaStream(sink.stream.getAudioTracks());
    if(profile.video){
      canvas=document.createElement('canvas');canvas.width=profile.width||1920;canvas.height=profile.height||1080;
      const draw=canvas.getContext('2d',{alpha:false});const stream=canvas.captureStream(profile.fps||30);canvasTrack=stream.getVideoTracks()[0];output.addTrack(canvasTrack);
      const tick=now=>{if(video?.readyState>=2 && now-lastFrame>=1000/(profile.fps||30)){draw.drawImage(video,0,0,canvas.width,canvas.height);lastFrame=now;}raf=requestAnimationFrame(tick);};raf=requestAnimationFrame(tick);
    }
    const bind=async source=>{
      const next=ctx.createMediaStreamSource(new MediaStream(source.getAudioTracks()));
      let nextVideo=null;
      try {
        if(profile.video){
          const track=source.getVideoTracks()[0];if(!track)throw new Error('Kamera der Videoaufnahme fehlt');
          nextVideo=document.createElement('video');nextVideo.muted=true;nextVideo.playsInline=true;nextVideo.srcObject=new MediaStream([track]);await mediaOperation('Video.play',()=>nextVideo.play());
        }
        next.connect(sink);if(input)input.disconnect();input=next;
        if(video){video.pause();video.srcObject=null;}video=nextVideo;
      } catch(e){next.disconnect();if(nextVideo)nextVideo.srcObject=null;throw e;}
    };
    const close=async()=>{cancelAnimationFrame(raf);if(video){video.pause();video.srcObject=null;}input?.disconnect();output.getTracks().forEach(t=>t.stop());if(ownsContext&&ctx.state!=='closed')await ctx.close();};
    try {await mediaOperation('AudioContext.resume',()=>ctx.resume());if(ctx.state!=='running')throw new Error('AudioContext ist nicht aktiv');await bind(this.source);this.recording={stream:output,bind,close};return output;}
    catch(e){await close();throw e;}
  }
  async releaseRecording(){const r=this.recording;this.recording=null;if(r)await r.close();}
  async close(){await this.releaseRecording();this.source?.getTracks().forEach(t=>t.stop());this.source=null;}
}
class UploadBudget {
  constructor({now=()=>Date.now(),sleep:wait=sleep,onState=()=>{}}={}) {
    this.now=now;this.sleep=wait;this.onState=onState;this.call=false;this.recording=false;
    this.abort=null;this.active=false;this.cancelledByPolicy=false;this.next=0;
    this.config={upload_kbps:512,upload_throttle_enabled:true,pause_uploads_during_call:true,
      pause_uploads_during_recording:false,upload_min_kbps:0};
    this.resetAdaptive();this.lastState='';
  }
  resetAdaptive(){this.samples=[];this.low=0;this.high=0;this.recovering=false;this.probeAt=0;this.rateKbps=null;}
  configure(policy={}) {
    const next={...this.config};
    for(const k of Object.keys(next))if(policy[k]!==undefined)next[k]=policy[k];
    if(JSON.stringify(next)===JSON.stringify(this.config))return;
    this.config=next;this.next=0;this.resetAdaptive();this.enforce();
  }
  update(call){if(this.call===!!call)return;this.call=!!call;this.next=0;this.resetAdaptive();this.enforce();}
  setRecording(value){this.recording=!!value;this.enforce();}
  get limited(){return this.call&&this.config.upload_throttle_enabled&&!this.config.pause_uploads_during_call;}
  get capBytes(){return this.limited?this.config.upload_kbps*1000/8:0;}
  get pausedReason(){
    if(this.recording&&this.config.pause_uploads_during_recording)return 'recording';
    if(this.call&&this.config.upload_throttle_enabled&&this.config.pause_uploads_during_call)return 'call';
    if(this.limited&&this.recovering&&this.now()<this.probeAt)return 'slow';
    return '';
  }
  get state(){return this.pausedReason|| (this.limited?(this.recovering?'probe':'limited'):'unlimited');}
  notify(){const state=this.state;if(state!==this.lastState){this.lastState=state;this.onState(state);}}
  enforce(){
    if(this.pausedReason&&this.active){this.cancelledByPolicy=true;this.abort?.abort();}
    this.notify();
  }
  async wait(){
    // Short rechecks make config/call changes effective even during a long cap delay.
    while(true){this.notify();if(!this.pausedReason&&(!this.capBytes||this.now()>=this.next))return;await this.sleep(250);}
  }
  start(){
    // Metadata/hash reads may have awaited after wait(): recheck at the send boundary.
    if(this.pausedReason||this.capBytes&&this.now()<this.next){const e=new Error('Upload policy changed');e.uploadPolicy=true;throw e;}
    this.cancelledByPolicy=false;this.abort=new AbortController();this.active=true;this.startedAt=this.now();return this.abort.signal;
  }
  account(bytes){if(this.capBytes)this.next=Math.max(this.next,this.startedAt+bytes/this.capBytes*1000);}
  observe(bytes,seconds){
    // Request throughput excludes intentional cap/cooldown waits; it is not line capacity.
    const rate=bytes*8/1000/Math.max(0.1,seconds);
    this.samples.push(rate);if(this.samples.length>3)this.samples.shift();
    this.rateKbps=this.samples.reduce((a,b)=>a+b,0)/this.samples.length;
    if(!this.limited||!this.config.upload_min_kbps)return;
    const threshold=this.config.upload_min_kbps;
    if(this.recovering){
      this.high=rate>=threshold*1.25?this.high+1:0;
      if(this.high>=2){this.recovering=false;this.low=0;this.high=0;this.samples=[];this.probeAt=0;}
      else this.probeAt=this.now()+15000;
    }else{
      this.low=rate<threshold?this.low+1:0;
      if(this.low>=3){this.recovering=true;this.high=0;this.probeAt=this.now()+15000;}
    }
    this.notify();
  }
  done(){this.active=false;this.abort=null;this.cancelledByPolicy=false;}
}
root.PodcastMedia={MediaOwner,UploadBudget};
})(typeof window==='undefined'?globalThis:window);
