"""Call orchestration, independent of recording sessions. One application worker."""
import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit
from fastapi import HTTPException, Request
from fastapi.responses import FileResponse

ROOT = Path(__file__).parent

def sign_jwt(payload, secret):
    def enc(data): return base64.urlsafe_b64encode(data).rstrip(b"=").decode()
    message = enc(b'{"alg":"HS256","typ":"JWT"}') + "." + enc(json.dumps(payload,separators=(",",":")).encode())
    return message + "." + enc(hmac.new(secret.encode(), message.encode(), hashlib.sha256).digest())

def origin(value):
    if not isinstance(value,str) or len(value)>260:
        raise HTTPException(422,"ux.invalid_url")
    match=re.fullmatch(r"https://([a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?)(?::(\d{1,5}))?/?",value.strip())
    if not match: raise HTTPException(422,"ux.invalid_url")
    host,port=match.groups()
    if '.' not in host or any(not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?",s) for s in host.split('.')) or port and not 1<=int(port)<=65535:
        raise HTTPException(422,"ux.invalid_url")
    return "https://"+host.lower()+(":"+str(int(port)) if port and int(port)!=443 else "")

class JitsiService:
    def __init__(self,api,studio):
        self.a,self.s=api,studio
        self.domain=os.getenv("JITSI_DOMAIN","").strip()
        self.self_origin=origin("https://"+self.domain) if self.domain else ""
        self.secret=os.getenv("JITSI_APP_SECRET","")
        self.app_id=os.getenv("JITSI_APP_ID","openpodcast")
        self.control_key=os.getenv("JITSI_CONTROL_KEY","")
        self.control_url=os.getenv("JITSI_CONTROL_URL","")
        if self.control_url:
            u=urlsplit(self.control_url)
            if u.scheme!='https' or not u.netloc or u.username or u.fragment:
                raise RuntimeError('JITSI_CONTROL_URL must be an HTTPS endpoint without credentials')
        with self.s.db() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS jitsi_settings(id INTEGER PRIMARY KEY CHECK(id=1),value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jitsi_calls(id TEXT PRIMARY KEY,room TEXT NOT NULL,conference TEXT NOT NULL UNIQUE,
                provider TEXT NOT NULL,mode TEXT NOT NULL,state TEXT NOT NULL,revision INTEGER NOT NULL,
                created REAL NOT NULL,deadline REAL NOT NULL,remote_verified INTEGER NOT NULL DEFAULT 0);
            CREATE UNIQUE INDEX IF NOT EXISTS jitsi_one_call ON jitsi_calls(room) WHERE state!='closed';
            CREATE TABLE IF NOT EXISTS jitsi_participants(call_id TEXT,identity TEXT,name TEXT,state TEXT,updated REAL,
                PRIMARY KEY(call_id,identity));
            ''')
            row=c.execute('SELECT value FROM jitsi_settings WHERE id=1').fetchone()
            self.settings=self.validate(json.loads(row[0]) if row else {
                'mode':os.getenv('JITSI_MODE','self_hosted' if self.domain else 'off'),
                'public_url':os.getenv('JITSI_PUBLIC_URL','https://meet.jit.si')})
            c.execute('INSERT OR IGNORE INTO jitsi_settings VALUES(1,?)',(json.dumps(self.settings),))
    @staticmethod
    def validate(p):
        if not isinstance(p,dict) or set(p)!={'mode','public_url'} or not isinstance(p['mode'],str) or p['mode'] not in ('off','public','self_hosted'):
            raise HTTPException(422,'ux.invalid_settings')
        return {'mode':p['mode'],'public_url':origin(p['public_url'])}
    def configured(self):
        if self.settings['mode']=='public':return True
        return self.settings['mode']=='self_hosted' and self.self_ready()
    def self_ready(self):
        return bool(self.self_origin and len(self.secret)>=32 and len(self.control_key)>=32 and self.control_url)
    def embed_origin(self):
        return self.settings['public_url'] if self.settings['mode']=='public' else self.self_origin if self.settings['mode']=='self_hosted' else ''
    def settings_get(self,request:Request):
        self.s.role(request,True)
        with self.s.db() as c:count=c.execute("SELECT COUNT(*) FROM jitsi_calls WHERE state!='closed'").fetchone()[0]
        return dict(ok=True,settings=self.settings,configured=self.configured(),self_hosted_ready=self.self_ready(),
                    self_hosted_domain=self.domain,active_sessions=count)
    async def settings_set(self,request:Request):
        role=self.s.role(request,True);settings=self.validate(await self.s.payload(request))
        with self.s.lock:
            with self.s.db() as c:
                if settings!=self.settings:
                    if c.execute("SELECT 1 FROM jitsi_calls WHERE state!='closed'").fetchone() or any(r.get('rec_state')=='recording' for r in self.a['ROOMS'].values()):
                        raise HTTPException(409,'ux.settings_busy')
                    c.execute('UPDATE jitsi_settings SET value=? WHERE id=1',(json.dumps(settings),))
            self.settings=settings
        self.s.audit(role,'jitsi.settings',detail=settings)
        return self.settings_get(request)
    def auth(self,request,room,host=False,write=False):
        self.s.ident(room)
        role=self.a['_session_role'](request.cookies.get(self.a['COOKIE_NAME'],''))
        if role in ('admin','host'):
            if write and not self.a['_lock_holds'](room,request.headers.get('x-host-client','')):
                raise HTTPException(423,'ux.host_readonly')
            return role,None
        if host:raise HTTPException(403,'ux.host_required')
        return 'guest',self.s.guest(request,room,admission=True)
    def latest(self,room):
        with self.s.db() as c:r=c.execute("SELECT * FROM jitsi_calls WHERE room=? AND state!='closed'",(room,)).fetchone()
        return dict(r) if r else None
    def state(self,room,request:Request):
        self.auth(request,room)
        call=self.latest(room)
        with self.s.db() as c:
            count=c.execute("SELECT COUNT(*) FROM jitsi_participants WHERE call_id=? AND state='joined' AND updated>?",(call['id'] if call else '',time.time()-30)).fetchone()[0]
        return dict(ok=True,configured=self.configured(),deployment=self.settings['mode'],origin=self.embed_origin(),
                    call=call,participants=count,profile=self.s.profile(room))
    async def set_mode(self,room,request:Request):
        role,_=self.auth(request,room,True,True);p=await self.s.payload(request)
        mode=p.get('mode')
        if not isinstance(mode,str) or mode not in ('off','audio','video'):raise HTTPException(422,'ux.invalid_mode')
        with self.s.lock:
            call=self.latest(room)
            if p.get('expected_id')!=(call['id'] if call else None) or p.get('expected_revision')!=(call['revision'] if call else 0):
                raise HTTPException(409,'ux.call_changed')
            if call and call['state']=='closing':raise HTTPException(409,'ux.call_closing')
            if mode!='off' and not self.configured():raise HTTPException(503,'ux.jitsi_unavailable')
            with self.s.db() as c:
                if mode=='off' and call:
                    c.execute("UPDATE jitsi_calls SET state='closing',revision=revision+1 WHERE id=?",(call['id'],))
                elif call:
                    c.execute('UPDATE jitsi_calls SET mode=?,revision=revision+1 WHERE id=?',(mode,call['id']))
                elif mode!='off':
                    sid=secrets.token_hex(16);now=time.time()
                    name='op-'+hmac.new(self.a['SESSION_SECRET'].encode(),(room+'\0'+sid).encode(),hashlib.sha256).hexdigest()[:40]
                    c.execute('INSERT INTO jitsi_calls VALUES(?,?,?,?,?,?,?,?,?,0)',(sid,room,name,self.settings['mode'],mode,'active',1,now,now+self.s.config['max_duration_s']))
        if mode=='off' and call:await self.close_call(call)
        self.s.audit(role,'jitsi.mode',room,{'mode':mode})
        return self.state(room,request)
    async def grant(self,room,request:Request):
        role,token=self.auth(request,room)
        if role!='guest':raise HTTPException(403,'ux.guests_only')
        p=await self.s.payload(request);name=str(p.get('name','')).strip()[:80]
        if not name:raise HTTPException(422,'ux.name_required')
        with self.s.db() as c:
            if not c.execute('SELECT 1 FROM guest_consents WHERE owner=? AND room=?',(token['id'],room)).fetchone():
                raise HTTPException(403,'ux.consent_required')
        with self.s.lock:
            call=self.latest(room)
            if not self.configured() or not call or call['state']!='active' or call['deadline']<=time.time():
                raise HTTPException(409,'ux.no_call')
            identity='guest-'+token['id']
            with self.s.db() as c:
                count=c.execute("SELECT COUNT(*) FROM jitsi_participants WHERE call_id=? AND identity!=? AND state IN ('joining','joined') AND updated>?",(call['id'],identity,time.time()-30)).fetchone()[0]
                if count>=self.s.config['max_guests']:raise HTTPException(429,'ux.call_full')
                c.execute('INSERT OR REPLACE INTO jitsi_participants VALUES(?,?,?,?,?)',(call['id'],identity,name,'joining',time.time()))
        grant=dict(ok=True,call=call,origin=self.embed_origin(),domain=urlsplit(self.embed_origin()).netloc,name=name,identity=identity)
        if call['provider']=='self_hosted':
            now=int(time.time())
            grant['jwt']=sign_jwt({'aud':'jitsi','iss':self.app_id,'sub':urlsplit(self.self_origin).hostname,'room':call['conference'],
                'iat':now,'nbf':now-5,'exp':min(now+120,int(call['deadline'])),
                'context':{'user':{'id':identity,'name':name,'moderator':False,'affiliation':'member'},'features':{'recording':False,'livestreaming':False}}},self.secret)
        return grant
    async def report(self,room,call_id,request:Request):
        role,token=self.auth(request,room)
        if role!='guest':raise HTTPException(403)
        p=await self.s.payload(request);state=p.get('state')
        if not isinstance(state,str) or state not in ('joining','joined','left','failed'):raise HTTPException(422)
        with self.s.db() as c:
            # A stale guest report can never change the call itself.
            row=c.execute('SELECT 1 FROM jitsi_calls WHERE id=? AND room=?',(call_id,room)).fetchone()
            if not row:raise HTTPException(404)
            result=c.execute('UPDATE jitsi_participants SET state=?,updated=? WHERE call_id=? AND identity=?',(state,time.time(),call_id,'guest-'+token['id']))
            if not result.rowcount:raise HTTPException(403)
        return {'ok':True}
    def remote_close(self,call):
        if call['provider']!='self_hosted':return False
        if not self.self_ready():raise RuntimeError('Lifecycle service unavailable')
        data=json.dumps({'room':call['conference'],'expires':int(time.time())+300}).encode()
        req=urllib.request.Request(self.control_url,data=data,method='POST',headers={'Content-Type':'application/json','Authorization':'Bearer '+self.control_key})
        with urllib.request.urlopen(req,timeout=10) as r:
            if r.status!=200 or json.loads(r.read(65536)).get('closed') is not True:raise RuntimeError('Unconfirmed room closure')
        return True
    async def close_call(self,call):
        with self.s.db() as c:c.execute("UPDATE jitsi_calls SET state='closing' WHERE id=? AND state!='closed'",(call['id'],))
        try:verified=await asyncio.to_thread(self.remote_close,call)
        except Exception:
            self.s.audit('server','jitsi.close_retry',call['room']);return
        with self.s.db() as c:c.execute("UPDATE jitsi_calls SET state='closed',remote_verified=? WHERE id=?",(int(verified),call['id']))
    async def worker(self):
        while True:
            try:
                with self.s.db() as c:
                    rows=[dict(r) for r in c.execute("SELECT * FROM jitsi_calls WHERE state='closing' OR (state='active' AND deadline<?)",(time.time(),))]
                for call in rows:await self.close_call(call)
                with self.s.db() as c:
                    cutoff=time.time()-self.s.config['diagnostic_days']*86400
                    c.execute('DELETE FROM jitsi_participants WHERE updated<?',(cutoff,))
                    c.execute("DELETE FROM jitsi_calls WHERE state='closed' AND deadline<?",(cutoff,))
            except Exception:self.s.audit('server','jitsi.worker_error')
            await asyncio.sleep(4)
    def bind(self):
        app=self.a['app'];self.s.jitsi_embed_origin=self.embed_origin
        for path,handler,methods in [('/settings',self.settings_get,['GET']),('/settings',self.settings_set,['PUT']),
              ('/state/{room}',self.state,['GET']),('/mode/{room}',self.set_mode,['PUT']),
              ('/grant/{room}',self.grant,['POST']),('/event/{room}/{call_id}',self.report,['POST'])]:
            app.add_api_route('/api/jitsi'+path,handler,methods=methods)
        @app.get('/jitsi/{name}')
        def asset(name:str):
            if name not in ('call.js','admin.js'):raise HTTPException(404)
            return FileResponse(ROOT/name,media_type='application/javascript')
        @app.on_event('startup')
        async def start():self.task=asyncio.create_task(self.worker())
        @app.on_event('shutdown')
        async def stop():
            self.task.cancel()
            try:await self.task
            except asyncio.CancelledError:pass
        return self

def install(api,studio):return JitsiService(api,studio).bind()
