(() => {
'use strict';
const {el,t,error,request}=PodcastUX;
const box=document.getElementById('audio-profiles');
let config=null,busy=false,draft=null,dirty=false;
const openSections=new Set(),summaries=new Map();
const heading=el('h2',t('recording_settings'));
const intro=el('p',t('recording_settings_help'),{class:'sub recording-settings-intro'});
const form=el('form',undefined,{id:'recording-settings-form',novalidate:''});
const list=el('div',undefined,{class:'recording-settings-sections'});
const status=el('p',t('loading'),{id:'recording-settings-status',class:'settings-status',role:'status','aria-live':'polite',tabindex:'-1'});
const submit=el('button',t('recording_save'),{type:'submit',class:'btn btn-primary',disabled:''});
const cancel=el('button',t('recording_discard'),{type:'button',class:'btn',disabled:''});
const retry=el('button',t('recording_retry'),{type:'button',class:'btn',hidden:''});
const actions=el('div',undefined,{class:'recording-settings-actions'});
const toolbar=el('div',undefined,{class:'recording-settings-toolbar'});
actions.append(submit,cancel,retry);toolbar.append(actions,status);form.append(toolbar,list);box.append(heading,intro,form);
function activeProfile(){
 const profile=config?.profiles?.find(p=>p.id===config.default_profile);
 if(!profile||!profile.audio_only||!profile.audio_video)throw new Error('ux.recording_settings_unavailable');
 return profile;
}
function syncControls(){
 box.setAttribute('aria-busy',String(busy));
 for(const n of list.querySelectorAll('input,select'))n.disabled=busy||!!n.closest('[hidden]');
 submit.disabled=cancel.disabled=busy||!config||!dirty;retry.disabled=busy;
}
function setBusy(value){busy=value;syncControls();}
function number(value){return new Intl.NumberFormat(window.OpenPodcastI18n.locale,{maximumFractionDigits:3}).format(value);}
function updateSummaries(){
 if(!draft)return;
 const audio=draft.audio_only,video=draft.audio_video;
 const audioFormat=audio.quality==='lossless'?audio.lossless_format:audio.lossy_format;
 const videoFormat={webm_vp9:'WebM / VP9',webm_vp8:'WebM / VP8',mp4_h264:'MP4 / H.264'}[video.video_profile];
 if(summaries.has('audio_only'))summaries.get('audio_only').textContent=[t('profile_'+audio.quality),audioFormat.toUpperCase(),number(audio.sample_rate)+' Hz',t(audio.channels===1?'recording_mono':'recording_stereo')].join(' \u00b7 ');
 if(summaries.has('audio_video'))summaries.get('audio_video').textContent=[t('profile_'+video.quality),videoFormat,video.width+' \u00d7 '+video.height,video.fps+' fps'].join(' \u00b7 ');
 if(summaries.has('advanced'))summaries.get('advanced').textContent=PodcastUX.format('recording_advanced_summary',{chunk:number(draft.chunk_ms/1000),duration:number(draft.max_duration_s/60)});
}
function changed(){
 dirty=JSON.stringify(draft)!==JSON.stringify(activeProfile());
 status.textContent=dirty?t('unsaved_changes'):'';updateSummaries();syncControls();
}
function selectInput(id,options,value){
 const input=el('select',undefined,{id});
 for(const [key,label] of options)input.append(el('option',t(label),{value:key}));
 input.value=value;return input;
}
function row(key,input,hint='',unit=''){
 const r=el('div',undefined,{class:'cfg-row'+(input.type==='number'?' cfg-row-num':'')}),copy=el('div');
 copy.append(el('label',t(key),{for:input.id}));
 if(hint){const id=input.id+'-hint';copy.append(el('div',t(hint),{class:'desc',id}));input.setAttribute('aria-describedby',id);}
 r.append(copy);
 if(unit){const controls=el('div',undefined,{class:'num-wrap'});controls.append(input,el('span',unit,{class:'unit'}));r.append(controls);}else r.append(input);
 return r;
}
function numeric(parent,obj,key,label,min,max,scale=1,unit='',hint='',prefix='common'){
 const input=el('input',undefined,{id:'profile-'+prefix+'-'+key,type:'number',min,max,step:scale>1?String(1/scale):'1',required:''});
 input.value=obj[key]/scale;
 input.oninput=()=>{obj[key]=Math.round(Number(input.value)*scale);changed();};
 parent.append(row(label,input,hint,unit));return input;
}
function accordion(parent,key,label,hint){
 const details=el('details',undefined,{id:'recording-section-'+key,class:'recording-settings-section'});
 const summary=el('summary',undefined,{id:'recording-summary-'+key});
 const copy=el('span',undefined,{class:'recording-section-copy'});
 const value=el('span','',{class:'recording-section-value',translate:'no','data-i18n-ignore':''});
 copy.append(el('span',t(label),{class:'recording-section-title',id:'heading-'+key}),value);summary.append(copy);
 const content=el('div',undefined,{class:'recording-section-content',role:'group','aria-labelledby':'recording-summary-'+key});
 if(hint)content.append(el('p',t(hint),{class:'sub recording-section-hint'}));
 details.append(summary,content);details.open=openSections.has(key);
 details.ontoggle=()=>{if(details.open)openSections.add(key);else openSections.delete(key);};
 summaries.set(key,value);parent.append(details);return content;
}
function scopeEditor(parent,scope){
 const p=draft[scope],section=accordion(parent,scope,'profile_'+scope,'recording_'+scope+'_help');
 const grid=el('div',undefined,{class:'settings-fields'});
 const quality=selectInput('profile-'+scope+'-quality',[['lossless','profile_lossless'],['lossy','profile_lossy']],p.quality);
 grid.append(row('profile_quality',quality,'profile_quality_hint'));
 const lossless=el('div'),lossy=el('div');
 const output=selectInput('profile-'+scope+'-lossless',[['wav','profile_wav'],['flac','profile_flac']],p.lossless_format);
 lossless.append(row('profile_output',output,'profile_pcm_hint'));
 const format=selectInput('profile-'+scope+'-lossy',[['opus','profile_opus'],['mp3','profile_mp3']],p.lossy_format);
 lossy.append(row('profile_audio_format',format,'profile_opus_hint'));
 numeric(lossy,p,'audio_bitrate','profile_audio_bitrate',64,512,1000,'kbit/s','profile_bitrate_hint',scope);
 const mp3Hint=el('p',t('profile_mp3_hint'),{class:'sub'});lossy.append(mp3Hint);grid.append(lossless,lossy);
 numeric(grid,p,'sample_rate','sample_rate',8000,192000,1,'Hz','',scope);
 numeric(grid,p,'channels','channels',1,2,1,'','',scope);
 if(scope==='audio_video'){
  const video=selectInput('profile-video-format',[['webm_vp9','profile_webm_vp9'],['webm_vp8','profile_webm_vp8'],['mp4_h264','profile_mp4_h264']],p.video_profile);
  video.onchange=()=>{p.video_profile=video.value;changed();};grid.append(row('profile_video_format',video,'profile_video_hint'));
  numeric(grid,p,'video_bitrate','profile_video_bitrate',0.1,40,1000000,'Mbit/s','',scope);
  numeric(grid,p,'width','video_width',160,3840,1,'px','',scope);
  numeric(grid,p,'height','video_height',120,2160,1,'px','',scope);
  numeric(grid,p,'fps','video_fps',1,60,1,'fps','',scope);
 }
 const preview=el('p','',{class:'profile-result',role:'status','aria-live':'polite'});
 function update(){
  lossless.hidden=p.quality!=='lossless';lossy.hidden=p.quality!=='lossy';mp3Hint.hidden=p.lossy_format!=='mp3';
  for(const group of [lossless,lossy])for(const n of group.querySelectorAll('input,select'))n.disabled=group.hidden||busy;
  const key=scope==='audio_only'?(p.quality==='lossless'?'profile_result_audio_lossless':'profile_result_audio_lossy'):(p.quality==='lossless'?'profile_result_video_lossless':'profile_result_video_lossy');
  preview.textContent=PodcastUX.format(key,{format:p.lossless_format.toUpperCase()})+(p.quality==='lossy'&&p.lossy_format==='mp3'?' '+t('profile_result_mp3'):'');
 }
 quality.onchange=()=>{p.quality=quality.value;update();changed();};
 output.onchange=()=>{p.lossless_format=output.value;update();changed();};
 format.onchange=()=>{p.lossy_format=format.value;update();changed();};
 section.append(grid,preview);update();
}
function render(){
 for(const details of list.querySelectorAll('details')){
  const key=details.id.slice('recording-section-'.length);
  if(details.open)openSections.add(key);else openSections.delete(key);
 }
 list.replaceChildren();summaries.clear();
 if(!draft)draft=structuredClone(activeProfile());
 scopeEditor(list,'audio_only');scopeEditor(list,'audio_video');
 const advanced=accordion(list,'advanced','recording_advanced','recording_advanced_help');
 const fields=el('div',undefined,{class:'settings-fields'});
 numeric(fields,draft,'chunk_ms','recording_chunk_duration',1000,10000,1,'ms','recording_chunk_hint');
 numeric(fields,draft,'max_duration_s','recording_max_duration',60,86400,1,'s','recording_duration_hint');
 advanced.append(fields);updateSummaries();syncControls();
}
function validate(){
 const invalid=[...list.querySelectorAll('input,select')].find(n=>!n.disabled&&!n.checkValidity());
 if(!invalid)return true;
 for(let n=invalid.parentElement;n;n=n.parentElement)if(n.tagName.toLowerCase()==='details'){
  n.open=true;openSections.add(n.id.slice('recording-section-'.length));
 }
 status.textContent=t('recording_check_fields');invalid.reportValidity();invalid.focus();return false;
}
form.onsubmit=async event=>{
 event.preventDefault();if(busy||!dirty||!config||!validate())return;
 const snapshot=structuredClone(draft);
 const next={profiles:config.profiles.map(p=>p.id===snapshot.id?snapshot:p)};
 setBusy(true);status.textContent=t('saving');
 try{
  const result=await request('/api/studio/settings',{method:'PUT',body:JSON.stringify(next)});
  config={...config,profiles:result.config.profiles,default_profile:result.config.default_profile};
  draft=structuredClone(activeProfile());dirty=false;render();status.textContent=t('saved');status.focus();
 }catch(e){status.textContent=error(e);}finally{setBusy(false);}
};
cancel.onclick=()=>{
 if(busy||!dirty||!confirm(t('recording_discard_confirm')))return;
 draft=structuredClone(activeProfile());dirty=false;render();status.textContent=t('recording_discarded');status.focus();
};
window.addEventListener('beforeunload',e=>{if(dirty){e.preventDefault();e.returnValue='';}});
const consent=document.getElementById('consent-settings'),consentForm=el('form',undefined,{class:'profile-fields'});
consent.append(el('h2',t('consent_settings')),el('p',t('consent_help'),{class:'sub'}));
const globalLanguage=document.getElementById('cfgLocale');
let consentLocale=window.OpenPodcastI18n.locale==='en'?'en':'de',consentBusy=false;
const consentDrafts={de:'',en:''},consentSaved={de:'',en:''};
const languageHint=el('small',t('consent_global_language'));
// One editor, independent drafts per locale; never overwrite the other translation.
const consentInput=el('textarea',undefined,{id:'consent-text',required:'',maxlength:'12000',rows:'6',disabled:'',translate:'no','data-i18n-ignore':''});
const consentLabel=el('label',t('consent_text_label'),{for:'consent-text',class:'consent-editor'});consentLabel.append(consentInput);
const consentSave=el('button',t('save'),{type:'submit',class:'btn btn-primary',disabled:''}),consentStatus=el('p','',{role:'status','aria-live':'polite'});
consentForm.append(consentLabel,languageHint,consentSave,consentStatus);consent.append(consentForm);
function consentKey(code){return code==='en'?'consent_text_en':'consent_text';}
function showConsentDraft(){consentInput.value=consentDrafts[consentLocale];consentInput.lang=consentLocale;languageHint.textContent=t('consent_global_language')+' '+t('language_'+consentLocale);consentStatus.textContent=consentDrafts[consentLocale]!==consentSaved[consentLocale]?t('unsaved_changes'):'';}
consentInput.oninput=()=>{consentDrafts[consentLocale]=consentInput.value;consentStatus.textContent=t('unsaved_changes');};
function useConsentLocale(code){
 const next=code==='en'?'en':'de';if(next===consentLocale)return;
 consentDrafts[consentLocale]=consentInput.value;consentLocale=next;if(config)showConsentDraft();
}
globalLanguage?.addEventListener('change',()=>useConsentLocale(globalLanguage.value));
document.addEventListener('i18n:applied',e=>useConsentLocale(e.detail.locale));
consentForm.onsubmit=async e=>{
 e.preventDefault();if(consentBusy||!config||!consentForm.reportValidity())return;
 const code=consentLocale,key=consentKey(code),value=consentInput.value.trim();
 if(!value){consentStatus.textContent=t('consent_empty');return;}
 consentBusy=true;for(const n of [consentInput,consentSave])n.disabled=true;consentStatus.textContent=t('saving');
 try{const result=await request('/api/studio/settings',{method:'PUT',body:JSON.stringify({[key]:value})});
   const stored=result.config?.[key]??value;config[key]=stored;consentSaved[code]=stored;consentDrafts[code]=stored;if(consentLocale===code){consentInput.value=stored;consentStatus.textContent=t('saved');}
 }catch(e){consentStatus.textContent=error(e);}finally{consentBusy=false;for(const n of [consentInput,consentSave])n.disabled=false;}
};
window.addEventListener('beforeunload',e=>{consentDrafts[consentLocale]=consentInput.value;if(config&&['de','en'].some(code=>consentDrafts[code]!==consentSaved[code])){e.preventDefault();e.returnValue='';}});

async function loadSettings(){
 setBusy(true);status.textContent=t('loading');retry.hidden=true;
 try{
  const data=await request('/api/studio/settings');config=data.config;activeProfile();render();
  for(const code of ['de','en'])consentSaved[code]=consentDrafts[code]=config[consentKey(code)];
  showConsentDraft();for(const n of [consentInput,consentSave])n.disabled=false;
  status.textContent='';consentStatus.textContent='';
 }catch(e){config=null;status.textContent=error(e);consentStatus.textContent=error(e);retry.hidden=false;}
 finally{setBusy(false);}
}
retry.onclick=loadSettings;loadSettings();
})();
