(() => {
'use strict';
const {el,t,error,request}=PodcastUX;
const box=document.getElementById('audio-profiles');let config=null,busy=false;
const heading=el('h2',t('audio_profiles')),intro=el('p',t('profiles_global'),{class:'sub'}),list=el('div',undefined,{class:'preset-grid'}),status=el('p',t('loading'),{role:'status','aria-live':'polite'}),add=el('button',t('add_profile'),{type:'button',class:'btn'});
box.append(heading,intro,list,add,status);add.disabled=true;
const fields=[['name','profile_name','text'],['sample_rate','sample_rate','number',8000,192000],['channels','channels','number',1,2],['audio_bitrate','audio_bitrate','number',64000,512000],['chunk_ms','chunk_ms','number',1000,10000],['max_duration_s','max_duration','number',60,86400],['width','video_width','number',160,3840],['height','video_height','number',120,2160],['fps','video_fps','number',1,60],['video_bitrate','video_bitrate','number',100000,40000000]];
function setBusy(value){busy=value;box.querySelectorAll('button,input,textarea').forEach(n=>n.disabled=value);}
async function save(next){if(busy)return;setBusy(true);status.textContent=t('saving');try{await request('/api/studio/settings',{method:'PUT',body:JSON.stringify(next)});config={...config,...next};render();status.textContent=t('saved');}catch(e){status.textContent=error(e);}finally{setBusy(false);}}
function editor(card,profile,isNew){
 const form=el('form',undefined,{class:'profile-fields'}),inputs={};
 for(const [key,label,type,min,max] of fields){const id='profile-'+profile.id+'-'+key;const wrap=el('label',t(label),{for:id});const input=el('input',undefined,{id,type,required:''});input.value=profile[key];if(type==='number'){input.min=min;input.max=max;input.step=1;}else input.maxLength=100;wrap.append(input);inputs[key]=input;form.append(wrap);}
 const submit=el('button',t('save'),{type:'submit',class:'btn btn-primary'}),cancel=el('button',t('cancel'),{type:'button',class:'btn'});cancel.onclick=()=>render();form.append(submit,cancel);
 form.onsubmit=async e=>{e.preventDefault();if(busy||!form.reportValidity())return;const updated={...profile};for(const [key,,type] of fields)updated[key]=type==='number'?Number(inputs[key].value):inputs[key].value.trim();updated.video=false;updated.container='pcm';updated.codec='pcm_s16le';await save({profiles:isNew?[...config.profiles,updated]:config.profiles.map(p=>p.id===profile.id?updated:p)});};card.append(form);
}
function render(){list.replaceChildren();for(const p of config.profiles){const active=p.id===config.default_profile;const card=el('article',undefined,{class:'preset-card'});if(active)card.style.borderColor='var(--brand)';card.append(el('strong',p.name),el('small',p.sample_rate/1000+' kHz / '+p.channels+' '+t('channels')+' / '+Math.round(p.audio_bitrate/1000)+' kbit/s'));
 const actions=el('div',undefined,{class:'preset-card-actions'}),select=el('button',t(active?'active':'select'),{type:'button',class:active?'btn btn-primary':'btn','aria-pressed':String(active)}),edit=el('button',t('edit'),{type:'button',class:'btn'}),remove=el('button',t('delete'),{type:'button',class:'btn'});
 select.onclick=()=>save({default_profile:p.id});edit.onclick=()=>{if(!busy){render();const current=[...list.children].find(n=>n.dataset.profile===p.id);editor(current,p,false);}};
 remove.onclick=()=>{if(active){status.textContent=t('active_profile_delete');return;}if(confirm(t('delete_profile_confirm')))save({profiles:config.profiles.filter(x=>x.id!==p.id)});};
 card.dataset.profile=p.id;actions.append(select,edit,remove);card.append(actions);list.append(card);}
}
add.onclick=()=>{if(busy||!config)return;render();const p={...config.profiles.find(p=>p.id===config.default_profile),id:'audio-'+crypto.randomUUID(),name:t('new_profile')};const card=el('article',undefined,{class:'preset-card'});list.append(card);editor(card,p,true);};
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
request('/api/studio/settings').then(d=>{config=d.config;render();for(const code of ['de','en'])consentSaved[code]=consentDrafts[code]=config[consentKey(code)];showConsentDraft();for(const n of [consentInput,consentSave])n.disabled=false;add.disabled=false;status.textContent='';}).catch(e=>{status.textContent=error(e);consentStatus.textContent=error(e);});
})();
