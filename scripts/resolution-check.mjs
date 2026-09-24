// Run only against an empty disposable container. Media stays in RAM.
import { chromium, expect } from '@playwright/test';
import { spawnSync } from 'node:child_process';
import { writeFile } from 'node:fs/promises';

const base = process.env.TUBE_URL || 'http://127.0.0.1:8002';
const source = process.env.TEST_VIDEO_URL || 'https://www.youtube.com/watch?v=hjBpcekaqcs';
const browser = await chromium.launch({ headless: true, channel: 'chromium',
  args: ['--no-sandbox', '--disable-dev-shm-usage', '--autoplay-policy=no-user-gesture-required'] });
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  const api = async (path, method = 'GET', data) => {
    const response = await page.request.fetch(`${base}/api/${path}`, { method, data });
    if (!response.ok()) throw new Error(`${method} ${path}: ${response.status()}`);
    return response.json();
  };
  const initial = await api('status');
  expect(initial.sources).toHaveLength(0);
  expect(await api('channels')).toHaveLength(1);
  await api('settings', 'PATCH', { resolution: '4k', fps: process.env.TEST_FPS ? Number(process.env.TEST_FPS) : 'original' });
  await api('sources', 'POST', { url: source });
  await expect.poll(async () => {
    const state = await api('status');
    if (state.sources.some(s => s.state === 'error')) throw new Error('Source extraction failed');
    return state.media_count;
  }, { timeout: 180000, intervals: [2000] }).toBe(1);
  await page.goto(base);
  await expect(page.locator('#channel-resolution')).toHaveValue('4k');
  await expect(page.locator('#start-preview')).toBeEnabled();
  await page.locator('#start-preview').click();
  await expect.poll(async () => {
    const state = await api('status');
    console.log(JSON.stringify({ state: state.state, segments: state.segments, diagnostics: state.diagnostics }));
    return state.diagnostics.ready_seconds;
  }, { timeout: 240000, intervals: [10000] }).not.toBeNull();
  await page.waitForFunction(() => {
    const video = document.querySelector('video');
    return video.videoWidth === 3840 && video.videoHeight === 2160 && video.currentTime > 12 && !video.paused;
  }, null, { timeout: 120000 });
  const start = await page.locator('video').evaluate(video => video.currentTime);
  await page.waitForFunction(start => document.querySelector('video').currentTime > start + 20,
    start, { timeout: 90000 });
  const manifest = await page.request.get(`${base}/channels/main/index.m3u8?viewer=resolution-probe`);
  expect(manifest.ok()).toBeTruthy();
  const segment = (await manifest.text()).split('\n').filter(line => line.startsWith('segments/')).at(-1);
  const media = await page.request.get(new URL(segment, manifest.url()).href);
  expect(media.ok()).toBeTruthy();
  const probe = spawnSync('ffprobe', ['-v', 'error', '-show_entries', 'stream=codec_name,width,height,r_frame_rate',
    '-of', 'json', 'pipe:0'], { input: await media.body(), maxBuffer: 16 * 1024 * 1024, timeout: 30000 });
  expect(probe.status).toBe(0);
  const streams = JSON.parse(probe.stdout).streams;
  expect(streams.some(s => s.codec_name === 'h264' && s.width === 3840 && s.height === 2160)).toBeTruthy();
  expect(streams.some(s => s.codec_name === 'aac')).toBeTruthy();
  const playback = await page.locator('video').evaluate(video => ({ time: video.currentTime,
    width: video.videoWidth, height: video.videoHeight, error: video.error?.message || null,
    frames: video.getVideoPlaybackQuality().totalVideoFrames,
    droppedFrames: video.getVideoPlaybackQuality().droppedVideoFrames }));
  expect(playback.error).toBeNull();
  expect(errors).toEqual([]);
  const state = await api('status');
  expect(state.buffer_bytes).toBeLessThanOrEqual(192 * 1024 * 1024);
  await page.screenshot({ path: 'artifacts/playback-4k.png', fullPage: true });
  const result = { source, streams, playback, diagnostics: state.diagnostics, buffer_bytes: state.buffer_bytes };
  await writeFile('artifacts/playback-4k.json', JSON.stringify(result, null, 2));
  console.log('PASS: 4K H.264/AAC container playback, browser progress and bounded RAM', JSON.stringify(result));
  await page.locator('#stop-preview').click();
} finally { await browser.close(); }
