/* Browser capability negotiation. Extension always follows recorder.mimeType. */
(function(root){
'use strict';
function extension(mime){
 const type=String(mime).split(';')[0].trim().toLowerCase();
 const ext={'audio/webm':'webm','video/webm':'webm','audio/mp4':'mp4','video/mp4':'mp4','audio/ogg':'ogg'}[type];
 if(!ext)throw new Error('ux.media_container_mismatch');return ext;
}
function candidates(profile){
 if(!profile.video)return ['audio/webm;codecs=opus','audio/ogg;codecs=opus','audio/mp4;codecs=mp4a.40.2','audio/mp4'];
 const audio=profile.audio_quality!=='lossless';
 const types={webm_vp9:['video/webm;codecs=vp9'+(audio?',opus':'')],
   webm_vp8:['video/webm;codecs=vp8'+(audio?',opus':'')],
   mp4_h264:[audio?'video/mp4;codecs=avc1.42E01E,mp4a.40.2':'video/mp4;codecs=avc1.42E01E',audio?'video/mp4;codecs=h264,aac':'video/mp4;codecs=h264','video/mp4']};
 return [...new Set([profile.video_profile||'webm_vp9','webm_vp9','webm_vp8','mp4_h264'].flatMap(key=>types[key]||[]))];
}
function create(profile,stream,Recorder=root.MediaRecorder){
 if(!Recorder||typeof Recorder.isTypeSupported!=='function')throw new Error('ux.media_browser_unsupported');
 const choices=candidates(profile),requested=profile.video?profile.video_profile:'opus';
 for(const mime of choices){
  if(!Recorder.isTypeSupported(mime))continue;
  try{
   const options={mimeType:mime};
   if(profile.video)options.videoBitsPerSecond=profile.video_bitrate;
   if(!profile.video||profile.audio_quality!=='lossless')options.audioBitsPerSecond=profile.audio_bitrate;
   const recorder=new Recorder(stream,options),actual=recorder.mimeType||mime,ext=extension(actual);
   return {recorder,logical:profile.video?(profile.audio_quality==='lossless'?'video':'media'):'audio',
     spec:{ext,mime:actual,requested,fallback:mime!==choices[0]||actual.toLowerCase()!==mime.toLowerCase()}};
  }catch(error){/* A supported MIME can still fail at construction; try the next explicit candidate. */}
 }
 throw new Error('ux.media_browser_unsupported');
}
root.PodcastFormats={extension,candidates,create};
})(typeof window==='undefined'?globalThis:window);
