/* Shared localized UI helpers. All new strings are in locale/de.json and en.json. */
window.PodcastUX = {
  t(key) { return window.OpenPodcastI18n.t('ux.'+key); },
  error(error) {const key=error?.message||String(error);const i18n=window.OpenPodcastI18n;if(key.startsWith('ux.'))return i18n.t(key);if(error?.name==='RequestTimeoutError')return i18n.t('ux.request_timeout');if(error instanceof TypeError)return i18n.t('ux.network_error');return i18n.translateMessage?.(key)||key;},
  el(tag,text,attrs={}) {const n=document.createElement(tag);if(text!==undefined)n.textContent=text;for(const [k,v] of Object.entries(attrs))n.setAttribute(k,v);return n;},
  toast(message,kind='error') {
    let area=document.getElementById('podcast-toast-area');
    if(!area){area=this.el('div',undefined,{id:'podcast-toast-area'});area.style.cssText='position:fixed;right:16px;bottom:16px;z-index:10000;width:min(440px,calc(100vw - 32px));display:flex;flex-direction:column;gap:8px';document.body.append(area);}
    const item=this.el('div',undefined,{role:kind==='error'?'alert':'status','aria-live':kind==='error'?'assertive':'polite',class:'podcast-toast'});
    item.style.cssText='display:flex;align-items:start;gap:12px;padding:14px 16px;border-radius:8px;background:var(--panel,#181b22);color:var(--text,#e8eaed);border:1px solid '+(kind==='error'?'#e5484d':'var(--border,#2a2f3a)')+';box-shadow:0 4px 24px #0004;overflow-wrap:anywhere';
    const text=this.el('span',message),dismiss=this.el('button','\u00d7',{type:'button','aria-label':this.t('toast_dismiss')});
    text.style.flex='1';dismiss.style.cssText='background:none;border:0;color:inherit;font-size:20px;cursor:pointer';dismiss.onclick=()=>item.remove();
    item.append(text,dismiss);area.append(item);while(area.children.length>3)area.children[0].remove();setTimeout(()=>item.remove(),12000);return item;
  },
  format(key,values={}) {return this.t(key).replace(/\{(\w+)\}/g,(all,k)=>Object.hasOwn(values,k)?String(values[k]):all);},
  qualityMessages(report={}) {
    const keys={
      'No sealed client manifest; completeness cannot be proven':'quality_unsealed',
      'Client recording interrupted; unpersisted tail may be missing':'quality_interrupted',
      'Session end boundary unavailable':'quality_boundary',
      'MP4 conversion failed; audio and source chunks preserved':'quality_mp4',
      'Source duration differs from session duration':'quality_duration',
      'Track started outside synchronization tolerance':'quality_start',
      'Client reported device, permission, sleep or connection events; review timeline':'quality_events'
    };
    const n=value=>Number.isFinite(value)?value.toLocaleString(window.OpenPodcastI18n?.locale||'en',{minimumFractionDigits:3,maximumFractionDigits:3}):this.t('quality_unavailable');
    const values={raw:n(report.raw_duration_s),expected:n(report.expected_duration_s),final:n(report.duration_s),
      delta:n(Number.isFinite(report.raw_duration_s)&&Number.isFinite(report.expected_duration_s)?report.raw_duration_s-report.expected_duration_s:null),
      threshold:n(report.duration_tolerance_s??(Number.isFinite(report.expected_duration_s)?Math.max(0.25,report.expected_duration_s*0.005):null)),
      start:n(report.start_deviation_s),end:n(report.end_deviation_s),startThreshold:n(report.start_tolerance_s??0.25)};
    const warnings=Array.isArray(report.warnings)?report.warnings:[];
    const lines=warnings.map(w=>typeof w==='string'&&w.startsWith('ux.')?window.OpenPodcastI18n.t(w):keys[w]?this.format(keys[w],values):this.format('quality_unknown',{warning:typeof w==='string'?w:JSON.stringify(w)}));
    if(report.status==='complete_with_warnings'&&!lines.length)lines.push(this.t('quality_missing_details'));
    if(warnings.includes('Client reported device, permission, sleep or connection events; review timeline')){
      for(const e of report.telemetry?.events||[]){
        const known=['network_recovered','network_lost','sleep_or_timer_interruption','camera_lost','device_audio_lost','device_video_lost','local_storage_failure','maximum_duration','media_recorder_failed','audio_worklet_failed','media_recorder_stop_failed'];
        const event=known.includes(e.type)?this.t('quality_event_'+e.type):String(e.type||'?');
        const at=typeof e.at==='number'&&Number.isFinite(e.at)?new Date(e.at).toLocaleString(window.OpenPodcastI18n?.locale||'en'):this.t('quality_unavailable');
        lines.push(this.format('quality_event',{event,at}));
      }
    }
    for(const [track,stream] of Object.entries(report.streams||{})){
      lines.push(this.format('quality_stream_format',{track,mime:stream.reported_mime||stream.upload_ext,container:stream.container,
        codecs:[stream.video_codec,stream.audio_codec].filter(Boolean).join(' / '),fallback:this.t(stream.fallback?'quality_yes':'quality_no')}));
      lines.push(this.format('quality_stream_timing',{track,duration:n(stream.raw_duration_s),start:n(stream.start_deviation_s),end:n(stream.end_deviation_s)}));
    }
    if(Number.isFinite(report.video_audio_delta_s))lines.push(this.format('quality_sync_delta',{delta:n(report.video_audio_delta_s)}));
    if(report.flac_sample_identical)lines.push(this.t('quality_flac_identical'));
    for(const [file,value] of Object.entries(report.files||{}))lines.push(this.format('quality_file',{file,
      role:this.t('quality_role_'+(value.role||'output')),codecs:(value.codecs||[]).join(' / '),bytes:value.bytes,
      timeline:this.t('quality_timeline_'+(value.timeline||'session')),offset:n(value.start_offset_s||0)}));
    return lines;
  },
  async request(url,options={}) {
    const ctrl=new AbortController();let timedOut=false;
    const timer=setTimeout(()=>{timedOut=true;ctrl.abort();},15000);
    try {const r=await fetch(url,{...options,credentials:'same-origin',cache:'no-store',signal:ctrl.signal,headers:{'Content-Type':'application/json',...options.headers}});const d=await r.json().catch(()=>({}));
      if(!r.ok){const error=new Error(typeof d.detail==='string'?d.detail:'ux.http_error');error.status=r.status;throw error;}return d;
    }catch(error){if(timedOut){const timeout=new Error('ux.request_timeout');timeout.name='RequestTimeoutError';throw timeout;}throw error;}
    finally{clearTimeout(timer);}
  }
};
