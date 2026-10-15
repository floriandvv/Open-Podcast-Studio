"""Signed JaaS USAGE events. Observed unique browser devices, not a billing API."""
import base64
import binascii
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
import secrets
import time
from fastapi import HTTPException, Request

class JaaSWebhooks:
    def __init__(self, service):
        self.j=service;self.s=service.s
        self.keys=[key for key in (os.getenv('JAAS_WEBHOOK_SECRET',''),os.getenv('JAAS_WEBHOOK_PREVIOUS_SECRET','')) if key]
        try:self.tolerance=int(os.getenv('JAAS_WEBHOOK_TOLERANCE_SECONDS','300'))
        except ValueError:self.tolerance=0
        self.ready=bool(self.keys and all(len(key)>=16 for key in self.keys) and 30<=self.tolerance<=900)
        with self.s.db() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS jaas_webhook_events(app_id TEXT,event_id TEXT,body_sha TEXT,received REAL,PRIMARY KEY(app_id,event_id));
            CREATE TABLE IF NOT EXISTS jaas_usage_devices(app_id TEXT,month TEXT,device_hash TEXT,first_event REAL,PRIMARY KEY(app_id,month,device_hash));
            CREATE TABLE IF NOT EXISTS jaas_webhook_state(app_id TEXT PRIMARY KEY,first_received REAL,last_received REAL,last_usage REAL);
            CREATE TABLE IF NOT EXISTS jaas_device_salt(id INTEGER PRIMARY KEY CHECK(id=1),value TEXT NOT NULL);
            ''')
            c.execute('INSERT OR IGNORE INTO jaas_device_salt VALUES(1,?)',(secrets.token_hex(32),))
            self.salt=bytes.fromhex(c.execute('SELECT value FROM jaas_device_salt WHERE id=1').fetchone()[0])

    def verify(self, body, header, now=None):
        if not self.ready:raise HTTPException(503,'ux.webhook_not_configured')
        if not isinstance(header,str) or len(header)>4096:raise HTTPException(403,'ux.webhook_signature_invalid')
        parts=[item.strip().split('=',1) for item in header.split(',') if '=' in item]
        timestamps=[v for k,v in parts if k=='t']
        signatures=[v for k,v in parts if k=='v1']
        try:
            if len(timestamps)!=1 or not timestamps[0].isascii() or not timestamps[0].isdigit():raise ValueError()
            timestamp=int(timestamps[0])
        except (ValueError,IndexError):raise HTTPException(403,'ux.webhook_signature_invalid') from None
        if abs((time.time() if now is None else now)-timestamp)>self.tolerance:raise HTTPException(403,'ux.webhook_signature_invalid')
        message=timestamps[0].encode()+b'.'+body
        decoded=[]
        for signature in signatures:
            try:decoded.append(base64.b64decode(signature.encode('ascii'),validate=True))
            except (UnicodeError,ValueError,binascii.Error):continue
        matches=False
        for key in self.keys:
            expected=hmac.new(key.encode(),message,hashlib.sha256).digest()
            for signature in decoded:matches=hmac.compare_digest(expected,signature) or matches
        if not matches:raise HTTPException(403,'ux.webhook_signature_invalid')

    async def receive(self, request: Request):
        body=await self.s.body(request,262144)
        self.verify(body,request.headers.get('x-jaas-signature',''))
        try:p=json.loads(body)
        except (ValueError,UnicodeError):raise HTTPException(400,'ux.webhook_payload_invalid') from None
        if not isinstance(p,dict):raise HTTPException(422,'ux.webhook_payload_invalid')
        app=self.j.settings['jaas_app_id'];fqn=p.get('fqn','')
        if not app or p.get('appId')!=app or not isinstance(fqn,str) or not fqn.startswith(app+'/'):
            raise HTTPException(403,'ux.webhook_app_mismatch')
        event=p.get('eventType');event_id=p.get('idempotencyKey');ts=p.get('timestamp')
        if not isinstance(event,str) or not isinstance(event_id,str) or not 1<=len(event_id)<=200 or isinstance(ts,bool) or not isinstance(ts,int) or not 0<=ts<=int((time.time()+300)*1000):
            raise HTTPException(422,'ux.webhook_payload_invalid')
        stamp=ts/1000
        device_hashes=[]
        if event=='USAGE':
            data=p.get('data')
            if not isinstance(data,list) or not 1<=len(data)<=1000:raise HTTPException(422,'ux.webhook_payload_invalid')
            for item in data:
                if not isinstance(item,dict):raise HTTPException(422,'ux.webhook_payload_invalid')
                # Telephony can have different billing weights. Browser MAU is explicit.
                if item.get('callDirection') in ('in','out'):continue
                device=item.get('deviceId')
                if not isinstance(device,str) or not 1<=len(device)<=512:raise HTTPException(422,'ux.webhook_payload_invalid')
                device_hashes.append(hmac.new(self.salt,(app+'\0'+device).encode(),hashlib.sha256).hexdigest())
        digest=hashlib.sha256(body).hexdigest();now=time.time()
        with self.s.lock,self.s.db() as c:
            old=c.execute('SELECT body_sha FROM jaas_webhook_events WHERE app_id=? AND event_id=?',(app,event_id)).fetchone()
            if old:
                if not hmac.compare_digest(old[0],digest):raise HTTPException(409,'ux.webhook_duplicate_conflict')
                return {'ok':True,'duplicate':True}
            c.execute('INSERT INTO jaas_webhook_events VALUES(?,?,?,?)',(app,event_id,digest,now))
            c.execute('INSERT INTO jaas_webhook_state VALUES(?,?,?,NULL) ON CONFLICT(app_id) DO UPDATE SET last_received=excluded.last_received',(app,now,now))
            if event=='USAGE':
                month=datetime.fromtimestamp(stamp,timezone.utc).strftime('%Y-%m')
                for device in set(device_hashes):c.execute('INSERT OR IGNORE INTO jaas_usage_devices VALUES(?,?,?,?)',(app,month,device,stamp))
                c.execute('UPDATE jaas_webhook_state SET last_usage=? WHERE app_id=?',(now,app))
            elif event=='ROOM_DESTROYED':
                conference=fqn[len(app)+1:]
                # No room-name guessing. Conferences are unique per local call.
                row=c.execute("SELECT * FROM jitsi_calls WHERE provider='jaas' AND jaas_app_id=? AND conference=?",(app,conference)).fetchone()
                if row and stamp>=row['created']-5:
                    # Continue draining outstanding grants; a destroy event is not permission to reopen.
                    c.execute("UPDATE jitsi_calls SET state=CASE WHEN state='closed' THEN 'closed' ELSE 'closing' END,remote_verified=1,provider_destroyed_at=MAX(provider_destroyed_at,?),revision=revision+CASE WHEN state='active' THEN 1 ELSE 0 END WHERE id=?",(stamp,row['id']))
        return {'ok':True,'duplicate':False}

    def summary(self, app_id, now=None):
        now=time.time() if now is None else now
        month=datetime.fromtimestamp(now,timezone.utc).strftime('%Y-%m')
        with self.s.db() as c:
            state=c.execute('SELECT * FROM jaas_webhook_state WHERE app_id=?',(app_id,)).fetchone()
            count=c.execute('SELECT COUNT(*) FROM jaas_usage_devices WHERE app_id=? AND month=?',(app_id,month)).fetchone()[0]
        if not self.ready:return dict(ok=True,available=False,reason='ux.jaas_webhook_setup',webhook_ready=False,source='webhook',mau=None)
        if not state or state['last_usage'] is None:return dict(ok=True,available=False,reason='ux.jaas_webhook_waiting',webhook_ready=True,source='webhook',mau=None)
        return dict(ok=True,available=True,mau=count,period=month+' (UTC)',fetched_at=int(state['last_usage']),
                    source='webhook',partial=True,webhook_ready=True,first_received=int(state['first_received']))
