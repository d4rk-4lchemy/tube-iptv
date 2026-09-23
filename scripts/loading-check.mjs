import { chromium } from '@playwright/test';
const browser = await chromium.launch({ headless: true, channel: 'chromium', args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu'] });
try {
  const page = await browser.newPage();
  const errors = [];
  let firstSegmentBytes = null;
  page.on('response', async response => {
    if (firstSegmentBytes === null && /\/segments\/\d+\.ts/.test(response.url()) && response.ok()) {
      firstSegmentBytes = Number(response.headers()['content-length']);
    }
  });
  page.on('pageerror', e => errors.push(e.message));
  page.on('console', msg => console.log('BROWSER:', msg.text()));
  await page.route('**/player.js', async route => {
    const response = await route.fetch();
    await route.fulfill({response, body:(await response.text()).replace("if (data.fatal)", "console.log('HLS ERROR', data.details, data.fatal, data.error?.message); if (data.fatal)")});
  });
  await page.goto(process.env.TUBE_URL || 'http://127.0.0.1:8001');
  await page.locator('#start-preview').click();
  const start = Date.now();
  let slate = false, video = false;
  for (let i = 0; i < 140; i++) {
    const frame = await page.locator('video').evaluate(v => {
      if (!v.videoWidth || v.readyState < 2) return null;
      const canvas = document.createElement('canvas');
      canvas.width = 1920; canvas.height = 1080;
      const ctx = canvas.getContext('2d', {willReadFrequently:true}); ctx.drawImage(v, 0, 0, 1920, 1080);
      const pixel = (x, y) => Array.from(ctx.getImageData(x, y, 1, 1).data).slice(0, 3);
      const center = ctx.getImageData(700, 450, 520, 180).data;
      let white = 0;
      for (let j = 0; j < center.length; j += 4) if (center[j] > 180 && center[j+1] > 180 && center[j+2] > 180) white++;
      return {width:v.videoWidth, height:v.videoHeight, time:v.currentTime, white, left:pixel(100,540), top:pixel(960,100), middle:pixel(960,540), content:pixel(300,540), error:v.error?.message};
    });
    if (frame) {
      if (frame.error || frame.width !== 1920 || frame.height !== 1080) throw Error(JSON.stringify(frame));
      if (!slate && frame.white > 1000 && frame.top.every(x => x < 10)) {
        slate = true;
        console.log('PASS: actual LOADING text decoded in browser', {seconds:(Date.now()-start)/1000, ...frame});
        await page.screenshot({path:'artifacts/loading.png'});
      }
      if (frame.middle[0] > 200 && frame.middle[1] < 50 && frame.content[0] > 200) {
        if (!frame.left.every(x => x < 10)) throw Error('Missing pillarbox: '+JSON.stringify(frame));
        video = true;
        console.log('PASS: automatic handover to 4:3 video upscaled to 1080p with black side bars', frame);
        await page.screenshot({path:'artifacts/loading-to-video.png'});
        break;
      }
    }
    await page.waitForTimeout(500);
  }
  if (!slate || !video || errors.length) await page.screenshot({path:'artifacts/loading-failed.png'});
  if (!slate || !video || errors.length) throw Error(JSON.stringify({slate,video,errors}));
  if (!(firstSegmentBytes > 1400000 && firstSegmentBytes < 1800000)) throw Error('Loading slate bitrate too low/high: ' + firstSegmentBytes);
  console.log('PASS: loading slate has approximately 3 Mbps of real data', {firstSegmentBytes});
  await page.locator('#stop-preview').click();
} finally { await browser.close(); }
