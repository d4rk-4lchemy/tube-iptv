// Mutates programme settings. Run ONLY against an empty disposable instance.
import { chromium, expect } from '@playwright/test';
const base = process.env.TUBE_URL || 'http://127.0.0.1:8003';
const browser = await chromium.launch({ headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  const initial = await (await page.request.get(base + '/api/channels/main/programmes')).json();
  if (initial.length) throw new Error('Use an empty disposable instance');
  await page.goto(base);
  await expect(page.locator('#connection')).toHaveText('Server connected');
  await page.locator('#new-programme').click();
  await page.locator('#programme-name').fill('Weekly test');
  await page.locator('#programme-duration').fill('60');
  await page.locator('#programme-rules input[type=time]').fill('18:00');
  await page.locator('#save-programme').click();
  await expect(page.locator('#programme-list')).toContainText('Weekly test');
  await page.locator('#close-programme').click();
  await expect(page.locator('.schedule-block[draggable=true]')).toHaveCount(7);
  const data = await page.evaluate(async () => (await fetch('/api/channels/main/schedule')).json());
  const wednesday = data.occurrences.find(o => o.programme_id && o.weekday === 2);
  const button = page.locator(`[data-occurrence="${wednesday.id}"]`);
  const track = page.locator('.schedule-track[data-day="2"]');
  await button.scrollIntoViewIfNeeded();
  const transfer = await page.evaluateHandle(() => new DataTransfer());
  await button.dispatchEvent('dragstart', { dataTransfer: transfer });
  const box = await track.boundingBox();
  const save = page.waitForResponse(r => r.url().includes('/programmes/') && r.request().method() === 'PUT');
  await track.dispatchEvent('drop', { dataTransfer: transfer, clientY: box.y + 40 * 28, clientX: box.x + 80 });
  await save;
  const programmes = await (await page.request.get(base + '/api/channels/main/programmes')).json();
  expect(programmes[0].rules).toHaveLength(2);
  expect(programmes[0].rules.find(r => r.time === '20:00').weekdays).toEqual([2]);
  expect(programmes[0].rules.find(r => r.time === '18:00').weekdays).toEqual([0, 1, 3, 4, 5, 6]);
  console.log('PASS: programme form and drag split one weekday from a weekly rule');
  // Create an overlap; API rejects it and the editor remains available.
  await page.locator('#new-programme').click();
  await page.locator('#programme-name').fill('Conflict');
  await page.locator('#save-programme').click();
  await expect(page.locator('#toast')).toContainText('Conflict:');
  await expect(page.locator('#programme-dialog')).toBeVisible();
  await page.locator('#close-programme').click();
  console.log('PASS: overlap rejected without losing the editing form');
  await page.locator('#gap-mode').selectOption('sources');
  await expect.poll(async () => (await (await page.request.get(base + '/api/status')).json()).gap_mode).toBe('sources');
  // Civil days expose all repeated labels, including offset, in the API and the grid.
  const autumn = await (await page.request.get(base + '/api/channels/main/schedule?week=2026-10-19')).json();
  expect(autumn.days[6].ticks.filter(t => t.label.startsWith('02:00'))).toHaveLength(2);
  // Navigate from the current week to the transition week using the actual controls.
  let week = data.week;
  while (week !== autumn.week) {
    const forward = week < autumn.week;
    await page.locator(forward ? '#next-week' : '#previous-week').click();
    const day = new Date(week + 'T12:00:00Z'); day.setUTCDate(day.getUTCDate() + (forward ? 7 : -7));
    week = day.toISOString().slice(0, 10);
    await expect(page.locator('#schedule-week')).toContainText(week);
  }
  await expect(page.locator('.schedule-day').last().locator('.schedule-tick').filter({ hasText: '02:00' })).toHaveCount(2);
  await page.locator('#programmes').scrollIntoViewIfNeeded();
  await page.screenshot({ path: 'artifacts/programmes-desktop.png', fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: 'artifacts/programmes-mobile.png', fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect(errors).toEqual([]);
  await page.request.delete(base + '/api/channels/main/programmes/' + programmes[0].id);
  await page.request.patch(base + '/api/settings', { data: { gap_mode: 'black' } });
  console.log('PASS: gap preference, 25-hour DST calendar, desktop/mobile layout and no JavaScript errors');
} finally { await browser.close(); }
