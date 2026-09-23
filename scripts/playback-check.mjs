import { chromium, expect } from '@playwright/test';
const browser = await chromium.launch({ headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu', '--autoplay-policy=no-user-gesture-required'], channel: 'chromium' });
try {
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  page.on('pageerror', e => console.log('PAGE ERROR', e.message));
  await page.goto(process.env.TUBE_URL || 'http://127.0.0.1:8001');
  await expect(page.getByText('Server connected')).toBeVisible();
  await expect(page.locator('#start-preview')).toBeEnabled();
  await page.locator('#start-preview').click();
  await page.waitForFunction(() => document.querySelector('video').currentTime > 18, null, { timeout: 80000 });
  const playback = await page.locator('video').evaluate(v => ({ time: v.currentTime, width: v.videoWidth, height: v.videoHeight, error: v.error?.message }));
  if (playback.error || playback.width !== 1920) throw new Error(JSON.stringify(playback));
  await page.screenshot({ path: 'artifacts/playback.png', fullPage: true });
  await page.locator('#stop-preview').click();
  await expect(page.locator('video')).toBeHidden();
  console.log('PASS: browser HLS playback across clips and explicit stop', playback);
} finally { await browser.close(); }
