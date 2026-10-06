(() => {
'use strict';
const {el,t,error,request}=PodcastUX,box=document.getElementById('jitsi-settings');
let saved=null,busy=false;
const title=el('h2',t('jitsi_settings')),label=el('label',t('deployment'),{for:'jitsi-mode'}),mode=el('select',undefined,{id:'jitsi-mode'});
for(const key of ['off','public','self_hosted'])mode.append(el('option',t('deployment_'+key),{value:key}));
const urlLabel=el('label',t('public_url'),{for:'jitsi-public-url'}),url=el('input',undefined,{id:'jitsi-public-url',type:'url',maxlength:'260',autocomplete:'off',spellcheck:'false'});
const publicFields=el('div',undefined,{class:'profile-fields'});publicFields.append(urlLabel,url,el('small',t('public_hint')));
const reload=el('button',t('refresh_config'),{type:'button',class:'btn'}),hint=el('p','',{class:'sub'}),status=el('p',t('loading'),{role:'status','aria-live':'polite'});
box.replaceChildren(title,label,mode,publicFields,hint,reload,status);
function visible(){publicFields.hidden=mode.value!=='public';reload.hidden=mode.value!=='self_hosted';hint.textContent=mode.value==='off'?t('jitsi_off'):mode.value==='self_hosted'?t('self_hint'):'';}
function lock(value){busy=value;mode.disabled=value;url.disabled=value;reload.disabled=value;}
function apply(d){saved=d.settings;mode.value=saved.mode;url.value=saved.public_url;visible();status.textContent=d.active_sessions?t('settings_busy'):saved.mode==='self_hosted'&&!d.self_hosted_ready?t('self_incomplete'):t('saved');}
async function load(){if(busy)return;lock(true);try{apply(await request('/api/jitsi/settings'));}catch(e){status.textContent=error(e);}finally{lock(false);if(!saved){mode.disabled=true;url.disabled=true;reload.hidden=true;}}}
async function save(){if(busy||!saved)return;const next={mode:mode.value,public_url:mode.value==='public'?url.value.trim():saved.public_url};visible();lock(true);status.textContent=t('saving');try{apply(await request('/api/jitsi/settings',{method:'PUT',body:JSON.stringify(next)}));status.textContent=t('saved_reload');}catch(e){mode.value=saved.mode;url.value=saved.public_url;visible();status.textContent=error(e);}finally{lock(false);}}
mode.addEventListener('change',save);url.addEventListener('change',save);url.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();url.blur();}});reload.onclick=load;visible();load();
})();
