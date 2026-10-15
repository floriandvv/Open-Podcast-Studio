/* Shared localized UI helpers. All new strings are in locale/de.json and en.json. */
window.PodcastUX = {
  t(key) { return window.OpenPodcastI18n.t('ux.'+key); },
  error(error) {const key=error?.message||String(error);const i18n=window.OpenPodcastI18n;if(key.startsWith('ux.'))return i18n.t(key);if(error?.name==='AbortError')return i18n.t('ux.request_timeout');if(error instanceof TypeError)return i18n.t('ux.network_error');return i18n.translateMessage?.(key)||key;},
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
  async request(url,options={}) {
    const ctrl=new AbortController(),timer=setTimeout(()=>ctrl.abort(),15000);
    try {const r=await fetch(url,{...options,credentials:'same-origin',cache:'no-store',signal:ctrl.signal,headers:{'Content-Type':'application/json',...options.headers}});const d=await r.json().catch(()=>({}));
      if(!r.ok){const error=new Error(typeof d.detail==='string'?d.detail:'ux.http_error');error.status=r.status;throw error;}return d;
    }finally{clearTimeout(timer);}
  }
};
