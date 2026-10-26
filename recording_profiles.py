"""Versioned recording profiles; pure validation/migration, no web dependency."""
from copy import deepcopy
import math

VIDEO_PROFILES = {'webm_vp9': ('webm', 'vp9'), 'webm_vp8': ('webm', 'vp8'),
                  'mp4_h264': ('mp4', 'h264')}
BOUNDS = {'sample_rate': (8000,192000), 'channels': (1,2), 'audio_bitrate': (64000,512000),
          'width': (160,3840), 'height': (120,2160), 'fps': (1,60),
          'video_bitrate': (100000,40000000), 'chunk_ms': (1000,10000), 'max_duration_s': (60,86400)}
DEFAULT_AUDIO = dict(quality='lossless', lossless_format='wav', lossy_format='opus',
                     sample_rate=48000, channels=1, audio_bitrate=192000)
DEFAULT_VIDEO = dict(DEFAULT_AUDIO, quality='lossy', video_profile='webm_vp9',
                     width=1280, height=720, fps=30, video_bitrate=5000000)

def integer(value, key):
    lo,hi=BOUNDS[key]
    if isinstance(value,bool) or not isinstance(value,(float,int)) or not math.isfinite(value) or value!=int(value) or not lo<=value<=hi:
        raise ValueError('ux.profile_invalid')
    return int(value)

def normalize_profile(value):
    if not isinstance(value,dict): raise ValueError('ux.profile_invalid')
    p=deepcopy(value)
    if 'audio_only' not in p and 'audio_video' not in p:
        # Old presets recorded PCM for audio-only and compressed A/V. Preserve that behavior.
        audio={**DEFAULT_AUDIO,**{k:p[k] for k in DEFAULT_AUDIO if k in p}}
        video={**DEFAULT_VIDEO,**{k:p[k] for k in DEFAULT_VIDEO if k in p}}
        codec=p.get('codec','')
        video['video_profile']='mp4_h264' if 'h264' in codec else 'webm_vp8' if 'vp8' in codec else 'webm_vp9'
        p['audio_only']=audio;p['audio_video']=video
    if p.get('schema_version',2)!=2: raise ValueError('ux.profile_invalid')
    result={k:p.get(k) for k in ('id','name')}
    result.update(schema_version=2,chunk_ms=integer(p.get('chunk_ms',5000),'chunk_ms'),
                  max_duration_s=integer(p.get('max_duration_s',14400),'max_duration_s'))
    for scope,defaults in [('audio_only',DEFAULT_AUDIO),('audio_video',DEFAULT_VIDEO)]:
        s=p.get(scope)
        if not isinstance(s,dict) or set(s)-set(defaults): raise ValueError('ux.profile_invalid')
        s={**defaults,**s}
        if s['quality'] not in ('lossless','lossy') or s['lossless_format'] not in ('wav','flac') or s['lossy_format'] not in ('opus','mp3'):
            raise ValueError('ux.profile_invalid')
        if scope=='audio_video' and s['video_profile'] not in VIDEO_PROFILES: raise ValueError('ux.profile_invalid')
        for k in set(s)&set(BOUNDS):s[k]=integer(s[k],k)
        result[scope]=s
    return result

def resolve_profile(value, video=False):
    p=normalize_profile(value);scope='audio_video' if video else 'audio_only';s=p[scope]
    out={k:p[k] for k in ('id','name','schema_version','chunk_ms','max_duration_s')}
    out.update(s,scope=scope,video=video,audio_quality=s['quality'],
               output_format=s['lossless_format'] if s['quality']=='lossless' else s['lossy_format'])
    if video:
        out['container'],out['codec']=VIDEO_PROFILES[s['video_profile']]
        if s['quality']=='lossy':out['codec']+=',aac' if out['container']=='mp4' else ',opus'
    else:out.update(container='pcm' if s['quality']=='lossless' else 'webm',codec='pcm_s16le' if s['quality']=='lossless' else 'opus')
    return out

def expected_streams(profile):
    if profile.get('schema_version')!=2:return {'main'}
    if profile['video']:return {'audio','video'} if profile['audio_quality']=='lossless' else {'media'}
    return {'audio'}
