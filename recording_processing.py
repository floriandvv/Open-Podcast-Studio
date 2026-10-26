"""Verified, stream-aware media finalization. Original chunks are never deleted here."""
from pathlib import Path
import hashlib
import json
import math
import os
import shutil
import subprocess
import tempfile
import time
import wave
from fastapi import HTTPException

def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
    return h.hexdigest()

def probe(studio,path):
    ffprobe=studio.a.get('FFPROBE') or shutil.which('ffprobe') or 'ffprobe'
    try:
        r=subprocess.run([ffprobe,'-v','error','-show_format','-show_streams','-of','json',str(path)],capture_output=True,timeout=120)
        if r.returncode:raise ValueError()
        p=json.loads(r.stdout)
        if not p.get('streams'):raise ValueError()
        return p
    except (OSError,subprocess.TimeoutExpired,ValueError):raise HTTPException(422,'ux.media_probe_failed') from None

def media_duration(studio,path,p,kind):
    def seconds(v):
        try:
            n=float(v)
            return n if math.isfinite(n) and n>0 else 0
        except (TypeError,ValueError):return 0
    candidates=[s for s in p['streams'] if s.get('codec_type')==kind]
    duration=max((seconds(s.get('duration')) for s in candidates),default=0)
    if not duration and len(p['streams'])==1:duration=seconds(p.get('format',{}).get('duration'))
    if duration:return duration
    # Browser WebM commonly omits duration. Scan packet timestamps to disk, not RAM.
    ffprobe=studio.a.get('FFPROBE') or shutil.which('ffprobe') or 'ffprobe'
    with tempfile.TemporaryFile() as out:
        try:
            r=subprocess.run([ffprobe,'-v','error','-select_streams','v:0' if kind=='video' else 'a:0',
                '-show_entries','packet=pts_time,duration_time','-of','csv=p=0',str(path)],stdout=out,stderr=subprocess.PIPE,timeout=1800)
            if r.returncode:raise ValueError()
            out.seek(0);end=0
            for line in out:
                values=line.decode(errors='replace').strip().split(',')
                if values:end=max(end,seconds(values[0])+seconds(values[1] if len(values)>1 else 0))
            if not end:raise ValueError()
            return end
        except (OSError,subprocess.TimeoutExpired,ValueError):raise HTTPException(422,'ux.media_probe_failed') from None

def pcm_hash(wav):
    h=hashlib.sha256()
    with wave.open(str(wav),'rb') as f:
        while True:
            b=f.readframes(65536)
            if not b:break
            h.update(b)
    return h.hexdigest()

def build_recording(studio,room,guest,session,repair=False):
    with studio.lock:
        t=studio.track(room,guest,session)
        meta=json.loads(t['meta']);profile=meta['profile'];modern=profile.get('schema_version')==2
        sr,ch=meta['sample_rate'],meta['channels'];dest=studio.dest(room,guest,session)
        manifests=meta.get('streams') or {'main':dict(started_at=meta.get('started_at',0))}
        counts=json.loads(t['stream_expected']) if t.get('stream_expected') else {'main':t['expected']} if t['expected'] is not None else None
        with studio.db() as c:
            rows=[dict(r) for r in c.execute('SELECT * FROM chunks WHERE room=? AND session=? AND guest=? ORDER BY logical,idx',(room,session,guest))]
        if set(r['logical'] for r in rows)!=set(manifests):raise HTTPException(409,'ux.stream_manifest_invalid')
        groups={k:[r for r in rows if r['logical']==k] for k in manifests}
        for logical,group in groups.items():
            n=counts[logical] if counts else max((r['idx'] for r in group),default=-1)+1
            indices={r['idx'] for r in group};missing=sorted(set(range(n))-indices)
            if not group or missing or indices!=set(range(n)):
                raise HTTPException(409,dict(reason='missing_chunks',logical=logical,missing=missing[:1000],expected=n,received=len(group)))
            if len({r['ext'] for r in group})!=1:raise HTTPException(409,'ux.stream_manifest_invalid')
            for r in group:
                p=studio.chunk_path(room,guest,session,logical,r['idx'],r['ext'])
                if not p.exists() or p.stat().st_size!=r['size'] or digest(p)!=r['sha']:
                    raise HTTPException(409,dict(reason='corrupt_chunk',logical=logical,idx=r['idx']))
        warnings=[]
        if counts is None:warnings.append('No sealed client manifest; completeness cannot be proven')
        if t['interrupted']:warnings.append('Client recording interrupted; unpersisted tail may be missing')
        with studio.a['_db_conn']() as c:
            timeline=c.execute('SELECT start_at,stop_at FROM recording_sessions WHERE room=? AND session=?',(room,session)).fetchone()
        duration=(timeline['stop_at']-timeline['start_at'])/1000 if timeline and timeline['stop_at'] and timeline['start_at'] else None
        if duration is None:warnings.append('Session end boundary unavailable')
        elif duration<=0 or duration>86400:raise HTTPException(409,'ux.media_timeline_invalid')
        working=Path(tempfile.mkdtemp(prefix='.build-',dir=dest))
        try:
            wav=working/'full.wav';sources={};streams={};video_source=None;audio_key=None
            for logical,group in groups.items():
                spec=manifests[logical];ext=group[0]['ext']
                source=working/f'source-{logical}.{ext}'
                with source.open('wb') as out:
                    for r in group:
                        with studio.chunk_path(room,guest,session,logical,r['idx'],r['ext']).open('rb') as f:shutil.copyfileobj(f,out,1024*1024)
                sources[logical]=source
                offset=(spec.get('started_at',meta['started_at'])-timeline['start_at'])/1000 if timeline and timeline['start_at'] else 0
                if abs(offset)>86400:raise HTTPException(422,'ux.media_timeline_invalid')
                if ext=='pcm':
                    if source.stat().st_size%(ch*2):raise HTTPException(422,'ux.stream_manifest_invalid')
                    raw=source.stat().st_size/(sr*ch*2)
                    with wave.open(str(wav),'wb') as w:
                        w.setparams((ch,2,sr,0,'NONE','not compressed'))
                        with source.open('rb') as f:
                            for block in iter(lambda:f.read(1024*1024),b''):w.writeframesraw(block)
                    actual=dict(container='pcm',audio_codec='pcm_s16le',video_codec=None)
                    audio_key=logical
                else:
                    p=probe(studio,source);types={s['codec_type']:s for s in p['streams'] if s['codec_type'] in ('audio','video')}
                    formats=p.get('format',{}).get('format_name','').split(',')
                    container='webm' if 'webm' in formats or 'matroska' in formats else 'mp4' if 'mp4' in formats or 'mov' in formats else 'ogg' if 'ogg' in formats else None
                    if container is None or modern and container!=ext:raise HTTPException(422,'ux.media_container_mismatch')
                    if not modern and container!=ext:warnings.append('ux.quality_legacy_container')
                    if modern:
                        want={'video'} if logical=='video' else {'audio','video'} if logical=='media' else {'audio'}
                        if set(types)!=want:raise HTTPException(422,'ux.media_tracks_mismatch')
                    if 'video' in types:video_source=logical
                    if 'audio' in types:
                        audio_key=logical
                        studio.ffmpeg(['-i',str(source),'-map','0:a:0','-vn','-c:a','pcm_s16le','-ar',str(sr),'-ac',str(ch),str(wav)])
                    raw=media_duration(studio,source,p,'video' if 'video' in types else 'audio')
                    actual=dict(container=container,audio_codec=types.get('audio',{}).get('codec_name'),video_codec=types.get('video',{}).get('codec_name'))
                if raw>86410:raise HTTPException(422,'ux.media_timeline_invalid')
                fallback=bool(spec.get('fallback'))
                if modern and ext!='pcm':
                    video_expected={'webm_vp9':'vp9','webm_vp8':'vp8','mp4_h264':'h264'}.get(profile.get('video_profile'))
                    if video_source==logical:
                        fallback=fallback or actual['video_codec']!=video_expected
                        if logical=='media' and profile.get('audio_quality')=='lossy':
                            expected_audio='aac' if actual['container']=='mp4' else 'opus'
                            fallback=fallback or actual['audio_codec']!=expected_audio
                    elif logical=='audio':
                        fallback=fallback or actual['audio_codec']!='opus'
                if fallback:warnings.append('ux.quality_codec_fallback')
                streams[logical]=dict(**actual,upload_ext=ext,reported_mime=spec.get('mime'),requested=spec.get('requested'),fallback=fallback,
                    raw_duration_s=raw,start_deviation_s=offset,end_deviation_s=offset+raw-duration if duration else None,
                    expected_chunks=counts.get(logical) if counts else None,chunks=len(group))
                if duration and abs(raw-duration)>max(.25,duration*.005):warnings.append('Source duration differs from session duration')
                if abs(offset)>.25:warnings.append('Track started outside synchronization tolerance')
            if audio_key is None or not wav.exists():raise HTTPException(422,'ux.media_tracks_mismatch')
            raw_audio=streams[audio_key]['raw_duration_s'];offset=streams[audio_key]['start_deviation_s']
            with wave.open(str(wav)) as w:raw_audio=w.getnframes()/w.getframerate()
            if duration:
                af=(f'adelay={round(offset*1000)}:all=1,' if offset>0 else f'atrim=start={-offset},asetpts=PTS-STARTPTS,' if offset<0 else '')
                normal=working/'normalized.wav'
                studio.ffmpeg(['-i',str(wav),'-af',af+f'apad,atrim=duration={duration}','-ar',str(sr),'-ac',str(ch),'-c:a','pcm_s16le',str(normal)])
                os.replace(normal,wav)
            studio.a['_wav_add_markers'](wav,studio.markers(room,session))
            with wave.open(str(wav)) as w:frames=w.getnframes();rate=w.getframerate();channels=w.getnchannels();width=w.getsampwidth()
            if rate!=sr or channels!=ch or width!=2 or frames<1 or duration and abs(frames-round(duration*sr))>1:
                raise HTTPException(422,'ux.media_audio_validation')
            lossless=profile.get('audio_quality','lossy' if video_source else 'lossless')=='lossless'
            output=profile.get('output_format','wav');roles={'full.wav':'output' if lossless and output=='wav' or not modern else 'internal_mixdown'}
            video_delta=None
            if video_source:
                v=streams[video_source];codec=v['video_codec']
                if codec not in ('vp8','vp9','h264'):raise HTTPException(422,'ux.media_codec_unsupported')
                container=v['container'] if modern else 'mp4'
                final_video=working/('full.'+container)
                vf='setpts=PTS-STARTPTS'
                voffset=v['start_deviation_s']
                if voffset>0:vf+=f',tpad=start_duration={voffset}:start_mode=clone'
                elif voffset<0:vf+=f',trim=start={-voffset},setpts=PTS-STARTPTS'
                if duration:vf+=f',tpad=stop_duration={duration}:stop_mode=clone,trim=duration={duration}'
                args=['-i',str(sources[video_source])]
                if not lossless:args+=['-i',str(wav)]
                args+=['-map','0:v:0','-vf',vf,'-c:v','libx264' if container=='mp4' else 'libvpx-vp9' if codec=='vp9' else 'libvpx',
                    '-b:v',str(profile.get('video_bitrate',5000000))]
                if container=='mp4':args+=['-preset','fast','-pix_fmt','yuv420p','-movflags','+faststart']
                else:args+=['-deadline','good','-cpu-used','4']
                if lossless:args+=['-an']
                else:args+=['-map','1:a:0','-c:a','aac' if container=='mp4' else 'libopus','-b:a',str(profile.get('audio_bitrate',192000))]
                if duration:args+=['-t',str(duration)]
                studio.ffmpeg(args+[str(final_video)])
                vp=probe(studio,final_video);has_audio=any(s['codec_type']=='audio' for s in vp['streams'])
                if has_audio==lossless:raise HTTPException(422,'ux.media_tracks_mismatch')
                video_duration=media_duration(studio,final_video,vp,'video');video_delta=video_duration-frames/rate
                tolerance=max(.08,2/profile.get('fps',30))
                if abs(video_delta)>tolerance:raise HTTPException(422,'ux.media_sync_failed')
                roles[final_video.name]='output'
                warnings.append('ux.quality_video_transcoded' if lossless else 'ux.quality_av_transcoded')
                if abs(v['raw_duration_s']-raw_audio)>.25:warnings.append('ux.quality_av_source_drift')
            if lossless and output=='flac':
                flac=working/'full.flac'
                studio.ffmpeg(['-i',str(wav),'-c:a','flac',str(flac)])
                decoded=working/'verify.wav';studio.ffmpeg(['-i',str(flac),'-c:a','pcm_s16le',str(decoded)])
                if pcm_hash(wav)!=pcm_hash(decoded):raise HTTPException(422,'ux.media_flac_validation')
                roles['full.flac']='output'
            elif not lossless and not video_source and output=='opus':
                src=sources[audio_key];opus=working/'full.opus'
                if streams[audio_key]['audio_codec']=='opus':studio.ffmpeg(['-i',str(src),'-map','0:a:0','-c:a','copy',str(opus)])
                else:
                    studio.ffmpeg(['-i',str(src),'-map','0:a:0','-c:a','libopus','-b:a',str(profile['audio_bitrate']),str(opus)])
                    warnings.append('ux.quality_opus_transcoded')
                roles['full.opus']='output'
            if not lossless and output=='mp3':
                studio.ffmpeg(['-i',str(wav),'-ar','48000','-c:a','libmp3lame','-b:a',str(min(320000,profile.get('audio_bitrate',192000))),str(working/'full.mp3')])
                roles['full.mp3']='export';warnings.append('ux.quality_mp3_generation')
            telemetry=json.loads(t['telemetry'])
            if telemetry.get('events'):warnings.append('Client reported device, permission, sleep or connection events; review timeline')
            warnings=list(dict.fromkeys(warnings));files={}
            for name,role in roles.items():
                p=working/name;item=dict(sha256=digest(p),bytes=p.stat().st_size,role=role)
                output_probe=probe(studio,p)
                item.update(container=output_probe.get('format',{}).get('format_name'),
                    codecs=[s.get('codec_name') for s in output_probe['streams']],
                    timeline='source' if name=='full.opus' else 'session',
                    start_offset_s=offset if name=='full.opus' else 0)
                files[name]=item
            state='incomplete' if t['interrupted'] or counts is None else 'complete_with_warnings' if warnings else 'complete'
            report=dict(status=state,validated_at=time.time(),warnings=warnings,expected_duration_s=duration,
                raw_duration_s=raw_audio,duration_s=frames/rate,start_deviation_s=offset,
                duration_tolerance_s=max(.25,duration*.005) if duration else None,start_tolerance_s=.25,
                end_deviation_s=offset+raw_audio-duration if duration else None,sample_rate=rate,channels=channels,
                frames=frames,size_bytes=wav.stat().st_size,missing_chunks=[],retried_chunks=sum(r['attempts']>1 for r in rows),
                duplicate_requests=sum(r['attempts']-1 for r in rows),chunks=len(rows),profile=profile,
                browser=meta.get('browser'),os=meta.get('os'),telemetry=telemetry,files=files,streams=streams,
                video_audio_delta_s=video_delta,flac_sample_identical=output=='flac' and lossless,
                normalization='session boundaries; Opus remux preserves source timing')
            for name in files:os.replace(working/name,dest/name)
            # Commit the report last. A failed conversion never marks a track as finalized.
            from studio_core import atomic_bytes,json_bytes
            atomic_bytes(dest/'quality.json',json_bytes(report))
            with studio.db() as c:
                c.execute('UPDATE tracks SET finalized=1,report=?,updated=? WHERE room=? AND session=? AND guest=?',(json.dumps(report),time.time(),room,session,guest))
            studio.a['_mixdown_path'](room,session).unlink(missing_ok=True)
            studio.audit('admin' if repair else 'guest:'+t['owner'],'track.rebuild' if repair else 'track.finalize',f'{room}/{session}/{guest}',dict(status=state))
            return report
        finally:shutil.rmtree(working,ignore_errors=True)
