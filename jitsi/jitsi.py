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
import urllib.error
from pathlib import Path
from urllib.parse import urlsplit
from fastapi import HTTPException, Request
from fastapi.responses import FileResponse

ROOT = Path(__file__).parent
JAAS_ORIGIN = "https://8x8.vc"
JAAS_APP_ID = re.compile(r"^vpaas-magic-cookie-[a-zA-Z0-9_-]{1,100}$")


def sign_jaas_jwt(payload, private_key, key_id):
    """Only the server owns the RSA key. No wildcard room or admin guest grants."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    def enc(value):
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode()
    header = {"alg": "RS256", "typ": "JWT", "kid": key_id}
    message = enc(json.dumps(header, separators=(",", ":")).encode()) + "." + enc(json.dumps(payload, separators=(",", ":")).encode())
    signature = private_key.sign(message.encode(), padding.PKCS1v15(), hashes.SHA256())
    return message + "." + enc(signature)


def load_jaas_key():
    """Missing/invalid credentials disable JaaS without breaking other providers."""
    path = os.getenv("JAAS_PRIVATE_KEY_FILE", "").strip()
    pem = os.getenv("JAAS_PRIVATE_KEY", "")
    if path and pem:
        return None
    try:
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
        data = Path(path).read_bytes() if path else pem.replace("\\n", "\n").encode()
        key = load_pem_private_key(data, password=None)
        return key if isinstance(key, RSAPrivateKey) and key.key_size >= 2048 else None
    except (ImportError, OSError, ValueError, TypeError):
        return None

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

class NoUsageRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward a usage credential to a redirected endpoint.


class JitsiService:
    def __init__(self,api,studio):
        self.a,self.s=api,studio
        self.closing_jobs=set()
        self.domain=os.getenv("JITSI_DOMAIN","").strip()
        self.self_origin=origin("https://"+self.domain) if self.domain else ""
        self.secret=os.getenv("JITSI_APP_SECRET","")
        self.app_id=os.getenv("JITSI_APP_ID","openpodcast")
        self.control_key=os.getenv("JITSI_CONTROL_KEY","")
        self.control_url=os.getenv("JITSI_CONTROL_URL","")
        # Opt-in adapter ONLY for an endpoint/schema confirmed by the account provider.
        # There is no assumed public MAU endpoint or reuse of conference JWT credentials.
        self.usage_url=os.getenv('JAAS_USAGE_API_URL','').strip()
        self.usage_bearer=os.getenv('JAAS_USAGE_API_BEARER','').strip()
        self.usage_mau_path=os.getenv('JAAS_USAGE_MAU_PATH','').strip()
        self.usage_period_path=os.getenv('JAAS_USAGE_PERIOD_PATH','').strip()
        self.usage_cache=None
        self.usage_lock=asyncio.Lock()
        self.jaas_key_id=os.getenv("JAAS_API_KEY_ID","").strip()
        self.jaas_key=load_jaas_key()
        try:
            self.jaas_ttl=int(os.getenv("JAAS_TOKEN_TTL_SECONDS","120"))
        except ValueError:
            self.jaas_ttl=0
        # Invalid security configuration fails closed; no key details reach the UI.
        self.jaas_env_ready=bool(self.jaas_key and 30<=self.jaas_ttl<=300 and
            re.fullmatch(r"vpaas-magic-cookie-[a-zA-Z0-9_-]{1,100}/[a-zA-Z0-9_-]{1,200}",self.jaas_key_id))
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
            CREATE TABLE IF NOT EXISTS jitsi_participants(call_id TEXT,identity TEXT,name TEXT,state TEXT,updated REAL,
                PRIMARY KEY(call_id,identity));
            ''')
            columns={r[1] for r in c.execute('PRAGMA table_info(jitsi_calls)')}
            for column,definition in [('jaas_app_id',"TEXT NOT NULL DEFAULT ''"),('grant_expires','REAL NOT NULL DEFAULT 0'),('empty_since','REAL'),('host_absent_since','REAL'),('had_guest','INTEGER NOT NULL DEFAULT 0'),('auto_close_reason',"TEXT NOT NULL DEFAULT ''"),('detached','INTEGER NOT NULL DEFAULT 0'),('close_started','REAL NOT NULL DEFAULT 0'),('close_attempts','INTEGER NOT NULL DEFAULT 0'),('close_last_attempt','REAL NOT NULL DEFAULT 0'),('close_next_retry','REAL NOT NULL DEFAULT 0'),('close_error',"TEXT NOT NULL DEFAULT ''"),('close_http_status','INTEGER NOT NULL DEFAULT 0'),('close_accepted_at','REAL NOT NULL DEFAULT 0'),('provider_destroyed_at','REAL NOT NULL DEFAULT 0'),('close_outcome',"TEXT NOT NULL DEFAULT ''")]:
                if column not in columns:c.execute('ALTER TABLE jitsi_calls ADD COLUMN '+column+' '+definition)
            c.execute('DROP INDEX IF EXISTS jitsi_one_call')
            c.execute("CREATE UNIQUE INDEX jitsi_one_call ON jitsi_calls(room) WHERE state!='closed' AND detached=0")
            row=c.execute('SELECT value FROM jitsi_settings WHERE id=1').fetchone()
            stored=json.loads(row[0]) if row else {
                'mode':os.getenv('JITSI_MODE','self_hosted' if self.domain else 'off'),
                'public_url':os.getenv('JITSI_PUBLIC_URL','https://meet.jit.si')}
            stored.setdefault('jaas_app_id','')
            self.settings=self.validate(stored)
            c.execute('INSERT OR REPLACE INTO jitsi_settings VALUES(1,?)',(json.dumps(self.settings),))
        from jitsi.webhooks import JaaSWebhooks
        self.webhooks=JaaSWebhooks(self)
        self.repair_room_reuse()
    def retire_room(self, room):
        """Make the room name reusable while old provider grants still drain."""
        with self.s.lock,self.s.db() as c:
            rows=[dict(row) for row in c.execute("SELECT * FROM jitsi_calls WHERE room=? AND state!='closed' AND detached=0",(room,))]
            c.execute("UPDATE jitsi_calls SET detached=1,state='closing',revision=revision+1 WHERE room=? AND state!='closed' AND detached=0",(room,))
        for call in rows:self.s.audit('server','jitsi.room_retired',room,{'call_id':call['id']})
        return rows

    def repair_room_reuse(self):
        """Repair pre-fix deletions using the persistent registry creation boundary."""
        db=self.a.get('_db_conn')
        if not db:return
        with db() as c:
            registered={row['room']:row['created_at'] for row in c.execute('SELECT room,created_at FROM rooms')}
        with self.s.db() as c:
            rows=[dict(row) for row in c.execute("SELECT * FROM jitsi_calls WHERE state!='closed' AND detached=0")]
        rooms={call['room'] for call in rows if call['room'] not in registered or call['created']<registered[call['room']]}
        for room in rooms:
            # Detach individual old incarnations; never retire a legitimate newer call.
            with self.s.lock,self.s.db() as c:
                for call in rows:
                    if call['room']==room and (room not in registered or call['created']<registered[room]):
                        c.execute("UPDATE jitsi_calls SET detached=1,state='closing',revision=revision+1 WHERE id=? AND detached=0",(call['id'],))
            self.s.audit('server','jitsi.room_reuse_repaired',room)

    @staticmethod
    def validate(p):
        if not isinstance(p,dict) or not {'mode','public_url'}<=set(p) or set(p)-{'mode','public_url','jaas_app_id','empty_room_grace_s','host_absence_grace_s'} or p['mode'] not in ('off','public','self_hosted','jaas'):
            raise HTTPException(422,'ux.invalid_settings')
        app_id=p.get('jaas_app_id','')
        if not isinstance(app_id,str):raise HTTPException(422,'ux.invalid_jaas_app_id')
        app_id=app_id.strip()
        if '/' in app_id:raise HTTPException(422,'ux.jaas_app_id_not_key')
        if app_id and not JAAS_APP_ID.fullmatch(app_id) or p['mode']=='jaas' and not app_id:
            raise HTTPException(422,'ux.invalid_jaas_app_id')
        result={'mode':p['mode'],'public_url':origin(p['public_url']),'jaas_app_id':app_id}
        for key,default in [('empty_room_grace_s',60),('host_absence_grace_s',300)]:
            value=p.get(key,default)
            if isinstance(value,bool) or not isinstance(value,int) or value not in (0,) and not 10<=value<=3600:
                raise HTTPException(422,'ux.invalid_call_grace')
            result[key]=value
        return result
    def configured(self):
        if self.settings['mode']=='public':return True
        if self.settings['mode']=='jaas':return self.jaas_ready()
        return self.settings['mode']=='self_hosted' and self.self_ready()
    def self_ready(self):
        return bool(self.self_origin and len(self.secret)>=32 and len(self.control_key)>=32 and self.control_url)
    def jaas_ready(self,app_id=None):
        app_id=self.settings['jaas_app_id'] if app_id is None else app_id
        return bool(app_id and self.jaas_env_ready and self.jaas_key_id.split('/')[0]==app_id)
    def embed_origin(self):
        if self.settings['mode']=='jaas':return JAAS_ORIGIN
        return self.settings['public_url'] if self.settings['mode']=='public' else self.self_origin if self.settings['mode']=='self_hosted' else ''
    def sdk_url(self):
        if self.settings['mode']=='jaas':
            return JAAS_ORIGIN+'/'+self.settings['jaas_app_id']+'/external_api.js' if self.settings['jaas_app_id'] else ''
        return self.embed_origin()+'/external_api.js' if self.embed_origin() else ''
    def settings_get(self,request:Request):
        self.s.role(request,True)
        with self.s.db() as c:count=c.execute("SELECT COUNT(*) FROM jitsi_calls WHERE state!='closed' AND detached=0").fetchone()[0]
        return dict(ok=True,settings=self.settings,configured=self.configured(),self_hosted_ready=self.self_ready(),
                    jaas_ready=self.jaas_ready(),jaas_credentials_ready=self.jaas_env_ready,webhook_ready=self.webhooks.ready,
                    self_hosted_domain=self.domain,active_sessions=count)
    def usage_configured(self):
        try:
            u=urlsplit(self.usage_url.replace('{app_id}', self.settings['jaas_app_id']))
            host=u.hostname or ''
            trusted=host in ('8x8.vc','8x8.com') or host.endswith(('.8x8.vc','.8x8.com'))
            paths=all(re.fullmatch(r'[a-zA-Z0-9_-]+(?:\.[a-zA-Z0-9_-]+)*', p or '') for p in (self.usage_mau_path,self.usage_period_path))
            return bool(trusted and u.scheme=='https' and u.port in (None,443) and not u.username and not u.password and not u.fragment
                and self.usage_bearer and '\r' not in self.usage_bearer and '\n' not in self.usage_bearer and paths)
        except ValueError:
            return False

    @staticmethod
    def usage_field(data, path):
        for key in path.split('.'):
            if not isinstance(data,dict) or key not in data:raise ValueError('Usage schema mismatch')
            data=data[key]
        return data

    def fetch_usage(self, app_id):
        # Bounded, server-side HTTPS request; response contains only count and period.
        url=self.usage_url.replace('{app_id}',app_id)
        req=urllib.request.Request(url,headers={'Authorization':'Bearer '+self.usage_bearer,'Accept':'application/json'})
        opener=urllib.request.build_opener(NoUsageRedirect())
        with opener.open(req,timeout=8) as response:
            if response.status!=200:raise ValueError('Usage request failed')
            content=response.read(65537)
            if len(content)>65536:raise ValueError('Usage response too large')
        data=json.loads(content)
        mau=self.usage_field(data,self.usage_mau_path)
        period=self.usage_field(data,self.usage_period_path)
        if isinstance(mau,bool) or not isinstance(mau,int) or not 0<=mau<=10**12:
            raise ValueError('Invalid MAU')
        if not isinstance(period,str) or not period.strip() or len(period)>120:
            raise ValueError('Invalid billing period')
        return dict(ok=True,available=True,mau=mau,period=period.strip(),fetched_at=int(time.time()),source='provider')

    async def usage(self, request: Request):
        self.s.role(request,True)
        app_id=self.settings['jaas_app_id']
        if self.settings['mode']!='jaas' or not app_id or not self.usage_configured():
            return self.webhooks.summary(app_id)
        async with self.usage_lock:
            if self.usage_cache and self.usage_cache[0]==app_id and time.time()-self.usage_cache[1]<60:
                return dict(self.usage_cache[2])
            try:
                result=await asyncio.to_thread(self.fetch_usage,app_id)
            except Exception:
                # Provider errors, URLs and credentials must never leak to the browser/logs.
                raise HTTPException(502,'ux.jaas_usage_unavailable') from None
            if self.settings['jaas_app_id']!=app_id or self.settings['mode']!='jaas':
                raise HTTPException(409,'ux.call_changed')
            self.usage_cache=(app_id,time.time(),result)
            return result

    async def settings_set(self,request:Request):
        role=self.s.role(request,True);settings=self.validate(await self.s.payload(request))
        with self.s.lock:
            with self.s.db() as c:
                if settings!=self.settings:
                    if c.execute("SELECT 1 FROM jitsi_calls WHERE state!='closed' AND detached=0").fetchone() or any(r.get('rec_state')=='recording' for r in self.a['ROOMS'].values()):
                        raise HTTPException(409,'ux.settings_busy')
                    c.execute('UPDATE jitsi_settings SET value=? WHERE id=1',(json.dumps(settings),))
            self.settings=settings
            self.usage_cache=None
        self.s.audit(role,'jitsi.settings',detail=settings)
        return self.settings_get(request)
    def auth(self,request,room,host=False,write=False):
        self.s.ident(room)
        exists=self.a.get('_room_exists')
        if exists and not exists(room):raise HTTPException(404,'ux.room_not_found')
        if not host and request.headers.get('authorization','').startswith('Bearer '):
            return 'guest',self.s.guest(request,room,admission=True)
        role=self.a['_session_role'](request.cookies.get(self.a['COOKIE_NAME'],''))
        if role in ('admin','host'):
            if write and not self.a['_lock_holds'](room,request.headers.get('x-host-client','')):
                raise HTTPException(423,'ux.host_readonly')
            return role,None
        if host:raise HTTPException(403,'ux.host_required')
        return 'guest',self.s.guest(request,room,admission=True)
    def latest(self,room):
        with self.s.db() as c:r=c.execute("SELECT * FROM jitsi_calls WHERE room=? AND state!='closed' AND detached=0",(room,)).fetchone()
        return dict(r) if r else None
    def state(self,room,request:Request):
        self.auth(request,room)
        call=self.latest(room)
        with self.s.db() as c:
            count=c.execute("SELECT COUNT(*) FROM jitsi_participants WHERE call_id=? AND state='joined' AND updated>?",(call['id'] if call and call['state']=='active' else '',time.time()-30)).fetchone()[0]
        return dict(ok=True,configured=self.configured(),deployment=self.settings['mode'],origin=self.embed_origin(),
                    call=call,participants=count,profile=self.s.profile(room),auto_close=self.auto_close_snapshot(call),closure=self.closure_snapshot(call))
    async def set_mode(self,room,request:Request):
        role,_=self.auth(request,room,True,True);p=await self.s.payload(request)
        mode=p.get('mode')
        if not isinstance(mode,str) or mode not in ('off','audio','video'):raise HTTPException(422,'ux.invalid_mode')
        with self.s.lock:
            exists=self.a.get('_room_exists')
            if exists and not exists(room):raise HTTPException(404,'ux.room_not_found')
            call=self.latest(room)
            if p.get('expected_id')!=(call['id'] if call else None) or p.get('expected_revision')!=(call['revision'] if call else 0):
                raise HTTPException(409,'ux.call_changed')
            if call and call['state']=='closing' and mode!='off':raise HTTPException(409,'ux.call_closing')
            if mode!='off' and not self.configured():raise HTTPException(503,'ux.jitsi_unavailable')
            with self.s.db() as c:
                if mode=='off' and call:
                    c.execute("UPDATE jitsi_calls SET state='closing',revision=revision+CASE WHEN state='active' THEN 1 ELSE 0 END WHERE id=?",(call['id'],))
                elif call:
                    c.execute('UPDATE jitsi_calls SET mode=?,revision=revision+1 WHERE id=?',(mode,call['id']))
                elif mode!='off':
                    sid=secrets.token_hex(16);now=time.time()
                    name='op-'+hmac.new(self.a['SESSION_SECRET'].encode(),(room+'\0'+sid).encode(),hashlib.sha256).hexdigest()[:40]
                    c.execute('INSERT INTO jitsi_calls(id,room,conference,provider,mode,state,revision,created,deadline,remote_verified,jaas_app_id) VALUES(?,?,?,?,?,?,?,?,?,0,?)',
                        (sid,room,name,self.settings['mode'],mode,'active',1,now,now+self.s.config['max_duration_s'],self.settings['jaas_app_id'] if self.settings['mode']=='jaas' else ''))
        if mode=='off' and call:await self.close_call(call,force=True)
        self.s.audit(role,'jitsi.mode',room,{'mode':mode})
        return self.state(room,request)
    async def grant(self,room,request:Request):
        role,token=self.auth(request,room)
        if role!='guest':raise HTTPException(403,'ux.guests_only')
        p=await self.s.payload(request);name=p.get('name','')
        if not isinstance(name,str) or not name.strip():raise HTTPException(422,'ux.name_required')
        name=name.strip()[:80]
        with self.s.lock:
            with self.s.db() as c:
                receipt=c.execute('SELECT digest FROM guest_consents WHERE owner=? AND room=?',(token['id'],room)).fetchone()
            if not receipt:raise HTTPException(403,'ux.consent_required')
            if not hmac.compare_digest(receipt[0],self.s.consent_policy(room)['consent_digest']):
                raise HTTPException(409,'ux.consent_changed')
            call=self.latest(room)
            if not self.configured() or not call or call['state']!='active' or call['deadline']<=time.time():
                raise HTTPException(409,'ux.no_call')
            identity='guest-'+token['id']
            with self.s.db() as c:
                count=c.execute("SELECT COUNT(*) FROM jitsi_participants WHERE call_id=? AND identity!=? AND state IN ('joining','joined') AND updated>?",(call['id'],identity,time.time()-30)).fetchone()[0]
                if count>=self.s.config['max_guests']:raise HTTPException(429,'ux.call_full')
                c.execute('INSERT OR REPLACE INTO jitsi_participants VALUES(?,?,?,?,?)',(call['id'],identity,name,'joining',time.time()))
            grant=dict(ok=True,call=call,origin=self.embed_origin(),domain=urlsplit(self.embed_origin()).netloc,
                sdk_url=self.sdk_url(),room_name=call['conference'],name=name,identity=identity)
            if call['provider']=='self_hosted':
                now=int(time.time())
                grant['jwt']=sign_jwt({'aud':'jitsi','iss':self.app_id,'sub':urlsplit(self.self_origin).hostname,'room':call['conference'],
                    'iat':now,'nbf':now-5,'exp':min(now+120,int(call['deadline'])),
                    'context':{'user':{'id':identity,'name':name,'moderator':False,'affiliation':'member'},'features':{'recording':False,'livestreaming':False}}},self.secret)
            elif call['provider']=='jaas':
                app_id=call['jaas_app_id']
                if not self.jaas_ready(app_id):raise HTTPException(503,'ux.jaas_incomplete')
                now=int(time.time());expires=min(now+self.jaas_ttl,int(call['deadline']))
                grant['origin']=JAAS_ORIGIN;grant['domain']='8x8.vc'
                grant['sdk_url']=JAAS_ORIGIN+'/'+app_id+'/external_api.js'
                grant['room_name']=app_id+'/'+call['conference']
                grant['jwt']=sign_jaas_jwt({'aud':'jitsi','iss':'chat','sub':app_id,'room':call['conference'],
                    'iat':now,'nbf':now-5,'exp':expires,'jti':secrets.token_hex(16),
                    'context':{'user':{'id':identity,'name':name,'moderator':'false'},'room':{'regex':False},
                    'features':{k:False for k in ('recording','livestreaming','transcription','outbound-call','inbound-call','sip-inbound-call','sip-outbound-call','file-upload')}}},
                    self.jaas_key,self.jaas_key_id)
                with self.s.db() as c:c.execute('UPDATE jitsi_calls SET grant_expires=MAX(grant_expires,?) WHERE id=?',(expires,call['id']))
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
        if call['provider']=='jaas':
            app_id=call['jaas_app_id']
            if not self.jaas_ready(app_id):raise RuntimeError('JaaS security configuration unavailable')
            now=int(time.time())
            # Admin claim is restricted to this server-to-provider request, never a guest grant.
            jwt=sign_jaas_jwt({'aud':'jitsi','iss':'chat','sub':app_id,'admin':True,
                'iat':now,'nbf':now-5,'exp':now+60},self.jaas_key,self.jaas_key_id)
            data=json.dumps({'action':'DESTROY','payload':{'conferenceFullName':call['conference']+'@conference.'+app_id+'.8x8.vc'}}).encode()
            req=urllib.request.Request(JAAS_ORIGIN+'/v1/_jaas/conference-commands/v1/meeting',
                data=data,method='POST',headers={'Content-Type':'application/json','Authorization':'Bearer '+jwt})
            with urllib.request.urlopen(req,timeout=10) as r:
                if not 200<=r.status<300:raise RuntimeError('JaaS did not accept closure')
                r.read(65536)
            # An accepted command is not a verified room-destroyed webhook.
            return False
        if call['provider']!='self_hosted':return False
        if not self.self_ready():raise RuntimeError('Lifecycle service unavailable')
        data=json.dumps({'room':call['conference'],'expires':int(time.time())+300}).encode()
        req=urllib.request.Request(self.control_url,data=data,method='POST',headers={'Content-Type':'application/json','Authorization':'Bearer '+self.control_key})
        with urllib.request.urlopen(req,timeout=10) as r:
            if r.status!=200 or json.loads(r.read(65536)).get('closed') is not True:raise RuntimeError('Unconfirmed room closure')
        return True
    def closure_snapshot(self, call):
        if not call or call['state']!='closing':return None
        now=time.time()
        until=(call['grant_expires']+5) if call['provider']=='jaas' and call['grant_expires'] else 0
        return dict(stage='retry' if call['close_error'] else 'draining' if until>now else 'requesting',
                    wait_until=until,remaining_seconds=max(0,int(until-now+0.999)),
                    next_retry=call['close_next_retry'],retry_in_seconds=max(0,int(call['close_next_retry']-now+0.999)),error=call['close_error'],http_status=call['close_http_status'],
                    attempts=call['close_attempts'],in_flight=call['id'] in self.closing_jobs,
                    command_accepted=bool(call['close_accepted_at']))

    def close_confirmed(self, call, now):
        # A destroy event predating grant expiry is not sufficient: an old JWT
        # might still recreate the conference. Require a fresh post-drain event.
        until=call['grant_expires']+5 if call['grant_expires'] else call['created']
        return bool(call['provider']=='jaas' and call['provider_destroyed_at']>=until and now>=until)

    def finish_close(self, call_id, outcome, verified=False):
        with self.s.lock,self.s.db() as c:
            c.execute("UPDATE jitsi_calls SET state='closed',close_outcome=?,close_error='',close_http_status=0,close_next_retry=0,remote_verified=MAX(remote_verified,?) WHERE id=? AND state='closing'",
                      (outcome,int(verified),call_id))

    async def close_call(self,call,force=False):
        call_id=call['id']
        with self.s.lock:
            if call_id in self.closing_jobs:return
            self.closing_jobs.add(call_id)
        try:
            now=time.time()
            with self.s.lock,self.s.db() as c:
                row=c.execute("SELECT * FROM jitsi_calls WHERE id=? AND state!='closed'",(call_id,)).fetchone()
                if not row:return
                call=dict(row)
                c.execute("UPDATE jitsi_calls SET state='closing',close_started=CASE WHEN close_started=0 THEN ? ELSE close_started END,revision=revision+CASE WHEN state='active' THEN 1 ELSE 0 END WHERE id=?",(now,call_id))
                used=c.execute('SELECT 1 FROM jitsi_participants WHERE call_id=? LIMIT 1',(call_id,)).fetchone()
            # Enabling the Host toggle does NOT create a conference at JaaS.
            # No grants ever issued and no participant records -> nothing remote
            # was authorized by this application. Do not wait for a nonexistent room.
            if call['provider']=='jaas' and not call['grant_expires'] and not used and not call['provider_destroyed_at']:
                self.finish_close(call_id,'never_started');return
            if self.close_confirmed(call,now):
                self.finish_close(call_id,'webhook_confirmed',True);return
            until=call['grant_expires']+5 if call['provider']=='jaas' and call['grant_expires'] else 0
            if force:
                if call['close_last_attempt'] and now-call['close_last_attempt']<2:return
            elif now<call['close_next_retry']:return
            # A successful initial command only needs a final post-expiry attempt.
            if call['close_accepted_at'] and now<until and not call['close_error']:return
            with self.s.db() as c:
                c.execute('UPDATE jitsi_calls SET close_attempts=close_attempts+1,close_last_attempt=? WHERE id=?',(now,call_id))
            failure=None;status=0;verified=False
            try:
                verified=await asyncio.to_thread(self.remote_close,call)
            except urllib.error.HTTPError as exc:
                status=int(exc.code);failure='ux.call_close_auth' if status in (401,403) else 'ux.call_close_http'
                exc.close()
            except (TimeoutError,urllib.error.URLError,OSError):failure='ux.call_close_network'
            except Exception:
                failure='ux.call_close_config' if call['provider']=='jaas' and not self.jaas_ready(call['jaas_app_id']) else 'ux.call_close_failed'
            # Re-read after network I/O: a signed webhook may have arrived while
            # DESTROY was in flight. Never overwrite that evidence with stale data.
            with self.s.lock,self.s.db() as c:
                row=c.execute("SELECT * FROM jitsi_calls WHERE id=? AND state!='closed'",(call_id,)).fetchone()
                if not row:return
                current=dict(row);finished=time.time()
            if self.close_confirmed(current,finished):
                self.finish_close(call_id,'webhook_confirmed',True);return
            if failure:
                delay=min(60,4*(2**min(max(current['close_attempts']-1,0),4)))
                with self.s.db() as c:
                    c.execute("UPDATE jitsi_calls SET close_error=?,close_http_status=?,close_next_retry=? WHERE id=? AND state='closing'",(failure,status,finished+delay,call_id))
                self.s.audit('server','jitsi.close_retry',call['room'],{'call_id':call_id,'error':failure,'http_status':status,'attempt':current['close_attempts']})
                return
            with self.s.db() as c:
                c.execute("UPDATE jitsi_calls SET close_accepted_at=?,close_error='',close_http_status=0,close_next_retry=? WHERE id=? AND state='closing'",(finished,max(finished,until),call_id))
            # Use the request START time: a response finishing after expiry does
            # not prove that the provider received a post-expiry destroy command.
            if now<until:return
            self.finish_close(call_id,'provider_confirmed' if verified else 'command_accepted' if call['provider']=='jaas' else 'local_disconnect',verified)
        finally:
            with self.s.lock:self.closing_jobs.discard(call_id)

    def auto_close_snapshot(self, call):
        if not call or call['state']!='active':return None
        candidates=[]
        for field,key,reason in [('empty_since','empty_room_grace_s','empty'),('host_absent_since','host_absence_grace_s','host_absent')]:
            grace=self.settings.get(key,60 if field=='empty_since' else 300)
            if grace and call.get(field) is not None:candidates.append((call[field]+grace,reason))
        if not candidates:return None
        deadline,reason=min(candidates)
        return dict(deadline=deadline,reason=reason)

    async def lifecycle_tick(self, now=None):
        now=time.time() if now is None else now
        with self.s.db() as c:calls=[dict(row) for row in c.execute("SELECT * FROM jitsi_calls WHERE state='active' AND detached=0")]
        for call in calls:
            room=call['room'];r=self.a['ROOMS'].get(room,{})
            with self.s.lock:
                sockets=sum(value==room for value in getattr(self.s,'guest_socket_rooms',{}).values())
                recent=any(now-g.get('last_seen',0)<30 for g in r.get('guests',{}).values())
                guests=bool(sockets or recent)
                hosts=bool(self.a.get('_WS_HOSTS',{}).get(room))
                recording=r.get('rec_state')=='recording'
                with self.s.db() as c:
                    # Re-read under the same lock as manual mode changes / recording triggers.
                    row=c.execute("SELECT * FROM jitsi_calls WHERE id=? AND state='active' AND detached=0",(call['id'],)).fetchone()
                    if not row:continue
                    call=dict(row)
                    had=call['had_guest'] or guests
                    empty=None if recording or guests or not had else (call['empty_since'] if call['empty_since'] is not None else now)
                    absent=None if recording or hosts else (call['host_absent_since'] if call['host_absent_since'] is not None else now)
                    call.update(empty_since=empty,host_absent_since=absent,had_guest=int(had))
                    automatic=self.auto_close_snapshot(call)
                    due=automatic and now>=automatic['deadline'] and not recording
                    c.execute('UPDATE jitsi_calls SET empty_since=?,host_absent_since=?,had_guest=?,auto_close_reason=? WHERE id=?',
                              (empty,absent,int(had),automatic['reason'] if due else '',call['id']))
                    if due:c.execute("UPDATE jitsi_calls SET state='closing',revision=revision+CASE WHEN state='active' THEN 1 ELSE 0 END WHERE id=?",(call['id'],))
                if due:self.s.audit('server','jitsi.auto_close',room,{'reason':automatic['reason']})
            if due:await self.close_call(call)

    async def worker(self):
        while True:
            try:
                await self.lifecycle_tick()
                with self.s.db() as c:
                    rows=[dict(r) for r in c.execute("SELECT * FROM jitsi_calls WHERE state='closing' OR (state='active' AND deadline<?)",(time.time(),))]
                # A slow retired conference must not starve all other closes.
                slots=asyncio.Semaphore(4)
                async def close_one(call):
                    async with slots:
                        try:await self.close_call(call)
                        except Exception:self.s.audit('server','jitsi.close_worker_error',call['room'])
                await asyncio.gather(*(close_one(call) for call in rows))
                with self.s.db() as c:
                    cutoff=time.time()-self.s.config['diagnostic_days']*86400
                    c.execute('DELETE FROM jitsi_participants WHERE updated<?',(cutoff,))
                    c.execute("DELETE FROM jitsi_calls WHERE state='closed' AND deadline<?",(cutoff,))
            except Exception:self.s.audit('server','jitsi.worker_error')
            await asyncio.sleep(4)
    def bind(self):
        app=self.a['app'];self.s.jitsi_embed_origin=self.embed_origin;self.s.jitsi_sdk_url=self.sdk_url
        for path,handler,methods in [('/settings',self.settings_get,['GET']),('/settings',self.settings_set,['PUT']),
              ('/usage',self.usage,['GET']),('/webhook',self.webhooks.receive,['POST']),('/state/{room}',self.state,['GET']),('/mode/{room}',self.set_mode,['PUT']),
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
