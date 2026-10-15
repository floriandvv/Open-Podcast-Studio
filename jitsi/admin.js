(() => {
'use strict';
const {el,t,error,request}=PodcastUX,box=document.getElementById('jitsi-settings');
let saved=null,busy=false,usageBusy=false;
const form=el('form'),grid=el('div',undefined,{class:'jitsi-settings-grid'});
function field(key,input,hint){const wrap=el('div',undefined,{class:'jitsi-field'}),label=el('label',t(key),{for:input.id});wrap.append(label,input);if(hint)wrap.append(el('small',t(hint)));return wrap;}
const mode=el('select',undefined,{id:'jitsi-mode'});for(const k of ['off','public','self_hosted','jaas'])mode.append(el('option',t('deployment_'+k),{value:k}));
const url=el('input',undefined,{id:'jitsi-public-url',type:'url',maxlength:'260'}),appId=el('input',undefined,{id:'jitsi-jaas-app-id',type:'text',maxlength:'119',autocomplete:'off',spellcheck:'false',translate:'no','data-i18n-ignore':''});
const publicFields=field('public_url',url,'public_hint'),jaasFields=field('jaas_app_id',appId,'jaas_app_hint');
const empty=el('input',undefined,{id:'jitsi-empty-grace',type:'number',min:'0',max:'3600',step:'1'}),absence=el('input',undefined,{id:'jitsi-host-grace',type:'number',min:'0',max:'3600',step:'1'});
grid.append(field('deployment',mode),jaasFields,publicFields,field('call_empty_grace',empty,'call_grace_hint'),field('call_host_grace',absence,'call_host_grace_hint'));
const saveButton=el('button',t('configuration_save'),{type:'submit',class:'btn btn-primary',id:'jitsi-save'}),reload=el('button',t('refresh_config'),{type:'button',class:'btn'}),hint=el('p','',{class:'sub'}),status=el('p',t('loading'),{role:'status','aria-live':'polite'}),actions=el('div',undefined,{class:'jitsi-actions'});
actions.append(saveButton,reload);form.append(grid,hint,actions,status);
// Usage is a full separate statistics row, never a child of the App ID field.
const usage=el('section',undefined,{class:'jitsi-usage',id:'jitsi-usage','aria-label':t('jaas_mau_title')}),dashboard=el('div',undefined,{class:'overview-dashboard'});
function statistic(label,id){const card=el('div',undefined,{class:'overview-card'}),value=el('div','\u2014',{class:'value',id,translate:'no','data-i18n-ignore':''}),detail=el('div','',{class:'detail'});card.append(el('div',t(label),{class:'label'}),value,detail);dashboard.append(card);return {value,detail};}
const mau=statistic('jaas_mau_title','jaas-mau-value'),period=statistic('jaas_mau_period','jaas-mau-period'),health=statistic('jaas_webhook_status','jaas-webhook-status');
const usageStatus=el('p','',{role:'status','aria-live':'polite'}),usageReload=el('button',t('jaas_mau_refresh'),{type:'button',class:'btn',id:'jaas-mau-refresh'}),consoleLink=el('a',t('jaas_console_activity'),{href:'https://jaas.8x8.vc/',target:'_blank',rel:'noopener noreferrer',class:'btn'}),usageActions=el('div',undefined,{class:'jitsi-actions'});
usageActions.append(usageReload,consoleLink);usage.append(dashboard,usageStatus,usageActions,el('p',t('jaas_mau_webhook_hint'),{class:'sub'}));
box.replaceChildren(el('h2',t('jitsi_settings')),form,usage);
function visible(){publicFields.hidden=mode.value!=='public';jaasFields.hidden=mode.value!=='jaas';usage.hidden=mode.value!=='jaas';hint.textContent=t(mode.value==='off'?'jitsi_off':mode.value==='jaas'?'jaas_hint':mode.value==='self_hosted'?'self_hint':'public_hint');}
function lock(value){busy=value;for(const n of [mode,url,appId,empty,absence,saveButton,reload])n.disabled=value;usageReload.disabled=value||usageBusy;}
function changed(){appId.setAttribute('aria-invalid','false');status.textContent=t('unsaved_changes');visible();}
for(const n of [mode,url,appId,empty,absence])n.addEventListener('change',changed);
appId.addEventListener('input',changed);
function apply(d){saved=d.settings;mode.value=saved.mode;url.value=saved.public_url;appId.value=saved.jaas_app_id||'';empty.value=saved.empty_room_grace_s??60;absence.value=saved.host_absence_grace_s??300;visible();status.textContent=d.active_sessions?t('settings_busy'):saved.mode==='jaas'&&!d.jaas_ready?t('jaas_incomplete'):saved.mode==='self_hosted'&&!d.self_hosted_ready?t('self_incomplete'):t('saved');if(saved.mode==='jaas')loadUsage();}
function showError(e){const text=error(e);status.textContent=t('configuration_not_saved')+' '+text;PodcastUX.toast(text,'error');if(e.message.includes('app_id')||e.message==='ux.jaas_enter_app_id'){appId.setAttribute('aria-invalid','true');appId.focus();}}
async function load(){if(busy)return;lock(true);try{apply(await request('/api/jitsi/settings'));}catch(e){showError(e);}finally{lock(false);if(!saved)saveButton.disabled=true;}}
async function save(e){e?.preventDefault();if(busy||!saved)return;
 const value=appId.value.trim();
 if(mode.value==='jaas'&&!value){showError(new Error('ux.jaas_enter_app_id'));return;}
 if(value.includes('/')){showError(new Error('ux.jaas_app_id_not_key'));return;}
 if(value&&!/^vpaas-magic-cookie-[a-zA-Z0-9_-]{1,100}$/.test(value)){showError(new Error('ux.invalid_jaas_app_id'));return;}
 const next={mode:mode.value,public_url:mode.value==='public'?url.value.trim():saved.public_url,jaas_app_id:value,empty_room_grace_s:Number(empty.value),host_absence_grace_s:Number(absence.value)};
 lock(true);status.textContent=t('saving');
 try{apply(await request('/api/jitsi/settings',{method:'PUT',body:JSON.stringify(next)}));}
 catch(e){showError(e);/* Keep every draft field; never silently revert the deployment. */}
 finally{lock(false);}
}
async function loadUsage(){
 if(usageBusy||!saved||saved.mode!=='jaas'){if(saved?.mode!=='jaas')usageStatus.textContent=t('configuration_not_saved');return;}
 usageBusy=true;usageReload.disabled=true;const app=saved.jaas_app_id;
 mau.value.textContent='\u2014';period.value.textContent='\u2014';health.value.textContent=t('loading');usageStatus.textContent=t('loading');
 try{const d=await request('/api/jitsi/usage');if(saved.jaas_app_id!==app)return;
  if(d.available){mau.value.textContent=new Intl.NumberFormat(window.OpenPodcastI18n.locale).format(d.mau);period.value.textContent=d.period;mau.detail.textContent=t(d.source==='webhook'?'jaas_mau_observed':'jaas_mau_provider');health.value.textContent=t(d.source==='webhook'?'jaas_webhook_receiving':'active');health.detail.textContent=t('jaas_mau_updated')+' '+new Date(d.fetched_at*1000).toLocaleString(window.OpenPodcastI18n.locale);usageStatus.textContent=d.partial?t('jaas_mau_partial'):'';}
  else{health.value.textContent=t(d.webhook_ready?'jaas_webhook_waiting_short':'jaas_webhook_setup_short');usageStatus.textContent=window.OpenPodcastI18n.t(d.reason);mau.detail.textContent=t('jaas_mau_observed');period.value.textContent=new Date().toISOString().slice(0,7)+' (UTC)';}
 }catch(e){health.value.textContent=t('jaas_webhook_error');usageStatus.textContent=error(e);}
 finally{usageBusy=false;usageReload.disabled=busy;if(saved.jaas_app_id!==app)loadUsage();}
}
form.onsubmit=save;reload.onclick=load;usageReload.onclick=loadUsage;visible();load();
})();
