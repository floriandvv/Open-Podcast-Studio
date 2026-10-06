/* Open Podcast. One media owner; recorder and call consume independent tracks. */
(function(root){
'use strict';
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
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
    const ctx=new (window.AudioContext||window.webkitAudioContext)({sampleRate:profile.sample_rate||48000});
    await ctx.resume();
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
          nextVideo=document.createElement('video');nextVideo.muted=true;nextVideo.playsInline=true;nextVideo.srcObject=new MediaStream([track]);await nextVideo.play();
        }
        next.connect(sink);if(input)input.disconnect();input=next;
        if(video){video.pause();video.srcObject=null;}video=nextVideo;
      } catch(e){next.disconnect();if(nextVideo)nextVideo.srcObject=null;throw e;}
    };
    const close=async()=>{cancelAnimationFrame(raf);if(video){video.pause();video.srcObject=null;}input?.disconnect();output.getTracks().forEach(t=>t.stop());await ctx.close();};
    try {await bind(this.source);this.recording={stream:output,bind,close};return output;}
    catch(e){await close();throw e;}
  }
  async releaseRecording(){const r=this.recording;this.recording=null;if(r)await r.close();}
  async close(){await this.releaseRecording();this.source?.getTracks().forEach(t=>t.stop());this.source=null;}
}
class UploadBudget {
  constructor(){this.call=false;this.quality='unknown';this.next=0;this.abort=null;this.active=false;this.maxDuringCall=0;}
  update(call,quality){this.call=call;this.quality=quality;if(!call)this.next=0;if(call&&(quality!=='good'||!this.maxDuringCall))this.abort?.abort();}
  async wait(bytes){
    while(this.call&&(this.quality!=='good'||!this.maxDuringCall))await sleep(1000);
    // Conservative background cap even when call is healthy: 128 kbit/s.
    if(this.call){let now=Date.now();while(this.call&&this.quality==='good'&&this.maxDuringCall&&this.next>now){await sleep(this.next-now);now=Date.now();}if(this.call&&this.quality==='good'&&this.maxDuringCall)this.next=Math.max(now,this.next)+bytes/this.maxDuringCall*1000;}
  }
  start(){this.abort=new AbortController();this.active=true;return this.abort.signal;}
  done(){this.active=false;this.abort=null;}
}
root.PodcastMedia={MediaOwner,UploadBudget};
})(typeof window==='undefined'?globalThis:window);
