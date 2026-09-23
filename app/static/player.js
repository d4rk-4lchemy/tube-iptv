import { $, toast } from './api.js';
let hls = null;
let active = false;
let revision = null;
const viewer = crypto.randomUUID?.() || Math.random().toString(36).slice(2);
export function syncPreview(data) {
  if (revision !== null && revision !== data.stream_revision && active) {
    if (data.stream_available) startPreview(data.stream_url);
    else stopPreview();
  }
  revision = data.stream_revision;
}
export function stopPreview() {
  active = false;
  hls?.destroy(); hls = null;
  const video = $('video');
  video.pause(); video.removeAttribute('src'); video.load(); video.hidden = true;
  $('screen-idle').hidden = false;
  $('stop-preview').hidden = true;
  $('shuffle-label').hidden = false;
}
export function startPreview(url) {
  stopPreview();
  active = true;
  const video = $('video');
  const source = new URL(url, location.href);
  source.searchParams.set('viewer', viewer);
  $('screen-idle').hidden = true; video.hidden = false;
  $('stop-preview').hidden = false; $('shuffle-label').hidden = true;
  toast('Tuning in. Initial buffering may take a few seconds.');
  if (window.Hls?.isSupported()) {
    hls = new window.Hls({ liveSyncDurationCount: 3, backBufferLength: 12,
      manifestLoadPolicy: { default: { maxTimeToFirstByteMs: 100000, maxLoadTimeMs: 110000,
        timeoutRetry: { maxNumRetry: 2, retryDelayMs: 1000, maxRetryDelayMs: 5000 },
        errorRetry: { maxNumRetry: 2, retryDelayMs: 2000, maxRetryDelayMs: 5000 } } } });
    hls.loadSource(source.href); hls.attachMedia(video);
    hls.on(window.Hls.Events.MANIFEST_PARSED, () => video.play().catch(() => {}));
    hls.on(window.Hls.Events.ERROR, (_, data) => {
      if (data.fatal) { toast('Could not play the channel. Check the sources and broadcast log.', true); stopPreview(); }
    });
  } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
    video.src = source.href; video.play().catch(() => {});
  } else { toast('This browser does not support HLS. Open the playlist in VLC.', true); stopPreview(); }
}
// A paused preview is not an active viewer; release the HLS loader as well.
$('video').addEventListener('pause', () => { if (active) stopPreview(); });
$('stop-preview').addEventListener('click', stopPreview);
window.addEventListener('pagehide', stopPreview);

$('video').addEventListener('ended', stopPreview);
