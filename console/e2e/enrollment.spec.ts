/**
 * Enrollment UI E2E Tests
 *
 * Tests the admin devices page and pair page against live staging.
 * Prerequisites: staging console on :3010, staging registry on :7778
 *
 * Run: cd console && npx playwright test e2e/enrollment.spec.ts
 */

import { test, expect, Page } from '@playwright/test';

const REGISTRY = 'http://localhost:7778';

// Helper: ensure admin user exists and log in
async function login(page: Page) {
  // Ensure admin exists
  await fetch(`${REGISTRY}/api/v1/init`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: 'admin', password: 'admin123' }),
  }).catch(() => {});

  // Navigate to login and authenticate
  await page.goto('/login');
  await page.getByLabel('Username').fill('admin');
  await page.getByLabel('Password').fill('admin123');
  await page.getByRole('button', { name: /sign in/i }).click();

  // Wait for redirect to dashboard
  await page.waitForURL((url) => !url.pathname.includes('/login'), { timeout: 10000 });
}

// Helper: clean up a test device from the registry
async function deleteDevice(deviceId: string) {
  await fetch(`${REGISTRY}/api/v1/devices/${deviceId}`, {
    method: 'DELETE',
  }).catch(() => {});
}

test.describe('Admin Devices Page', () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  test('loads and shows enrollment mode selector', async ({ page }) => {
    await page.goto('/admin/devices');
    await expect(page.getByRole('heading', { name: 'Devices', exact: true })).toBeVisible();
    // Mode selector buttons
    await expect(page.getByRole('button', { name: /Access Route/ })).toBeVisible();
    await expect(page.getByRole('button', { name: /Access Target/ })).toBeVisible();
    await expect(page.getByRole('button', { name: /Both/ })).toBeVisible();
    await expect(page.getByPlaceholder('Device ID *')).toBeVisible();
  });

  test('mode selector changes visible form fields', async ({ page }) => {
    await page.goto('/admin/devices');

    // Default: Route mode — should show route dropdown
    await expect(page.getByText('how this device reaches the network')).toBeVisible();

    // Switch to Target
    await page.getByText('Access Target').first().click();
    await expect(page.getByText('how the gateway reaches this device')).toBeVisible();
    await expect(page.getByText('how this device reaches the network')).not.toBeVisible();

    // Switch to Both
    await page.getByText('Both').first().click();
    await expect(page.getByText('how this device reaches the network')).toBeVisible();
    await expect(page.getByText('how the gateway reaches this device')).toBeVisible();
  });

  test('enroll access route device shows pair link', async ({ page }) => {
    const deviceId = `pw-route-${Date.now()}`;
    await page.goto('/admin/devices');

    await page.getByPlaceholder('Device ID *').fill(deviceId);
    await page.getByPlaceholder('Type (phone, server, etc)').fill('phone');
    await page.getByRole('button', { name: 'Add & Generate Keys' }).click();

    // Route-only → immediate activation → pair link
    await expect(page.getByText('Ready for Pairing')).toBeVisible({ timeout: 10000 });
    const pairLink = page.getByRole('link', { name: 'Generate Access Key' });
    await expect(pairLink).toBeVisible();

    // Verify link points to pair with device param
    const href = await pairLink.getAttribute('href');
    expect(href).toContain(`/pair?device=${deviceId}`);
  });

  test('enroll access target device shows bootstrap', async ({ page }) => {
    const deviceId = `pw-target-${Date.now()}`;
    await page.goto('/admin/devices');

    // Switch to Target mode
    await page.getByText('Access Target').first().click();
    await page.getByPlaceholder('Device ID *').fill(deviceId);
    await page.getByPlaceholder('Type (phone, server, etc)').fill('server');

    // Select reverse-tunnel and fill SSH user
    await page.locator('select').last().selectOption('reverse-tunnel');
    await page.getByPlaceholder('SSH user').fill('testuser');

    await page.getByRole('button', { name: 'Add Device' }).click();

    // Target → pending → bootstrap instructions
    await expect(page.getByText('Awaiting Bootstrap')).toBeVisible({ timeout: 10000 });
    await expect(page.getByText('curl -s')).toBeVisible();
    await expect(page.getByText('Token:')).toBeVisible();
  });

  test('duplicate enrollment shows error', async ({ page }) => {
    const deviceId = `pw-dup-${Date.now()}`;
    await page.goto('/admin/devices');

    // First enrollment
    await page.getByPlaceholder('Device ID *').fill(deviceId);
    await page.getByRole('button', { name: 'Add & Generate Keys' }).click();
    await expect(page.getByText('Ready for Pairing')).toBeVisible({ timeout: 10000 });

    // Second enrollment with same ID
    await page.goto('/admin/devices');
    await page.getByPlaceholder('Device ID *').fill(deviceId);
    await page.getByRole('button', { name: 'Add & Generate Keys' }).click();

    await expect(page.getByText('already exists')).toBeVisible({ timeout: 10000 });
  });
});

test.describe('Pair Page', () => {
  test('loads with device name field', async ({ page }) => {
    await page.goto('/pair');
    await expect(page.getByRole('heading', { name: 'Pair Device' })).toBeVisible();
    await expect(page.getByLabel('Device Name')).toBeVisible();
  });

  test('pre-fills device from query param and shows curl command', async ({ page }) => {
    await login(page);
    await page.goto('/pair?device=my-test-phone');
    await expect(page.getByText('my-test-phone')).toBeVisible();
    // Device name input should be hidden (pre-filled)
    await expect(page.getByLabel('Device Name')).not.toBeVisible();
    // Should show curl command (primary action)
    await expect(page.getByText('curl -s')).toBeVisible();
  });

  test('generates key via browser flow', async ({ page }) => {
    await login(page);
    const deviceName = `pw-pair-${Date.now()}`;
    await page.goto('/pair');
    await page.getByLabel('Device Name').fill(deviceName);

    // Click "generate in browser" secondary flow
    await page.getByText('generate in browser').click();
    await page.getByRole('button', { name: 'Generate in Browser' }).click();

    await expect(page.getByRole('heading', { name: 'Key Generated' })).toBeVisible({ timeout: 10000 });
    await expect(page.getByText('Save your private key now')).toBeVisible();
    await expect(page.getByText('Public Key')).toBeVisible();
    await expect(page.getByText('Connect')).toBeVisible();
  });

  test('a pair link shows a QR whose address shows the command with no session', async ({ page, browser }) => {
    await login(page);
    await page.goto(`/pair?device=pw-qr-${Date.now()}`);
    await page.getByRole('button', { name: 'Make Pair Command' }).click();
    const qr = page.getByRole('img', { name: /\/p\/[A-Z0-9]{4}-[A-Z0-9]{4}$/ });
    await expect(qr).toBeVisible({ timeout: 10000 });
    const address = await qr.getAttribute('aria-label');

    // The new device: no session cookie. Opening the address spends nothing.
    const device = await (await browser.newContext()).newPage();
    await device.goto(address!);
    await expect(device.getByRole('heading', { name: 'Pair this device' })).toBeVisible();
    await expect(device.getByText(/curl -fsS .*\/api\/v1\/pair\/[A-Z0-9]{4}-[A-Z0-9]{4} \| bash/)).toBeVisible();
  });

  test('shows error for non-existent user', async ({ page }) => {
    // Not logged in — should show username field
    await page.goto('/pair');
    await page.getByLabel('Username').fill('nobody_exists_12345');
    await page.getByLabel('Device Name').fill('test');

    // Need to expand browser flow
    await page.getByText('generate in browser').click();
    await page.getByRole('button', { name: 'Generate in Browser' }).click();

    await expect(page.getByText('not found')).toBeVisible({ timeout: 10000 });
  });
});

test.describe('End-to-End Flow', () => {
  test('enroll route device → navigate to pair → generate key', async ({ page }) => {
    await login(page);

    const deviceId = `pw-flow-${Date.now()}`;

    // Step 1: Enroll on admin page
    await page.goto('/admin/devices');
    await page.getByPlaceholder('Device ID *').fill(deviceId);
    await page.getByPlaceholder('Type (phone, server, etc)').fill('phone');
    await page.getByRole('button', { name: 'Add & Generate Keys' }).click();

    // Step 2: Click pair link
    await expect(page.getByRole('link', { name: 'Generate Access Key' })).toBeVisible({ timeout: 10000 });
    await page.getByRole('link', { name: 'Generate Access Key' }).click();

    // Step 3: Pair page with device pre-filled, curl command shown
    await expect(page).toHaveURL(new RegExp(`/pair\\?device=${deviceId}`));
    await expect(page.getByText(deviceId)).toBeVisible();
    await expect(page.getByText('curl -s')).toBeVisible();

    // Step 4: Use browser flow to generate key
    await page.getByText('generate in browser').click();
    await page.getByRole('button', { name: 'Generate in Browser' }).click();

    // Step 5: Success
    await expect(page.getByRole('heading', { name: 'Key Generated' })).toBeVisible({ timeout: 10000 });
    await expect(page.getByText(`admin@${deviceId}`, { exact: true })).toBeVisible();
  });
});
