/* Shared localized UI helpers. All new strings are in locale/de.json and en.json. */
window.PodcastUX = {
  t(key) { return window.OpenPodcastI18n.t('ux.'+key); },
  error(error) {const key=error?.message||String(error);return key.startsWith('ux.')?window.OpenPodcastI18n.t(key):key;},
  el(tag,text,attrs={}) {const n=document.createElement(tag);if(text!==undefined)n.textContent=text;for(const [k,v] of Object.entries(attrs))n.setAttribute(k,v);return n;},
  async request(url,options={}) {
    const ctrl=new AbortController(),timer=setTimeout(()=>ctrl.abort(),15000);
    try {const r=await fetch(url,{...options,credentials:'same-origin',cache:'no-store',signal:ctrl.signal,headers:{'Content-Type':'application/json',...options.headers}});const d=await r.json().catch(()=>({}));
      if(!r.ok){const error=new Error(typeof d.detail==='string'?d.detail:'HTTP '+r.status);error.status=r.status;throw error;}return d;
    }finally{clearTimeout(timer);}
  }
};
