import fs from 'node:fs/promises';
import crypto from 'node:crypto';

const registrationEndpoint = 'https://auth.magnific.com/realms/mcp/clients-registrations/openid-connect';
const deviceEndpoint = 'https://auth.magnific.com/realms/mcp/protocol/openid-connect/auth/device';
const tokenEndpoint = 'https://auth.magnific.com/realms/mcp/protocol/openid-connect/token';
const out = 'runtime/magnific-auth';
const encryptionSecret = process.env.MAGNIFIC_API_KEY;
if (!encryptionSecret) throw new Error('MAGNIFIC_API_KEY is required to encrypt the OAuth session');
await fs.mkdir(out, { recursive: true });

const regResponse = await fetch(registrationEndpoint, {
  method: 'POST',
  headers: { 'content-type': 'application/json' },
  body: JSON.stringify({
    client_name: `ottam-production-device-${Date.now()}`,
    redirect_uris: ['http://127.0.0.1/callback'],
    grant_types: ['urn:ietf:params:oauth:grant-type:device_code', 'refresh_token'],
    response_types: [],
    token_endpoint_auth_method: 'none',
  }),
});
if (!regResponse.ok) throw new Error(`registration HTTP ${regResponse.status}: ${(await regResponse.text()).slice(0, 500)}`);
const registration = await regResponse.json();
const clientId = registration.client_id;
if (!clientId) throw new Error('Dynamic registration returned no client_id');

const deviceBody = new URLSearchParams({
  client_id: clientId,
  scope: 'openid profile email offline_access mcp:custom-audience',
});
const deviceResponse = await fetch(deviceEndpoint, {
  method: 'POST',
  headers: { 'content-type': 'application/x-www-form-urlencoded' },
  body: deviceBody,
});
if (!deviceResponse.ok) throw new Error(`device authorization HTTP ${deviceResponse.status}: ${(await deviceResponse.text()).slice(0, 800)}`);
const device = await deviceResponse.json();
if (!device.device_code || !device.user_code) throw new Error(`Incomplete device response: ${JSON.stringify(device)}`);

const verificationUrl = device.verification_uri_complete || device.verification_uri;
console.log('MAGNIFIC AUTHORIZATION REQUIRED');
console.log(`Open: ${verificationUrl}`);
console.log(`Code: ${device.user_code}`);
console.log(`Waiting up to ${device.expires_in || 600} seconds for approval...`);

const deadline = Date.now() + Number(device.expires_in || 600) * 1000;
let interval = Math.max(5, Number(device.interval || 5));
let tokens = null;
while (Date.now() < deadline) {
  await new Promise((resolve) => setTimeout(resolve, interval * 1000));
  const tokenBody = new URLSearchParams({
    grant_type: 'urn:ietf:params:oauth:grant-type:device_code',
    client_id: clientId,
    device_code: device.device_code,
  });
  const tokenResponse = await fetch(tokenEndpoint, {
    method: 'POST',
    headers: { 'content-type': 'application/x-www-form-urlencoded' },
    body: tokenBody,
  });
  const text = await tokenResponse.text();
  let body = {};
  try { body = JSON.parse(text); } catch {}
  if (tokenResponse.ok && body.access_token) {
    tokens = body;
    break;
  }
  if (body.error === 'authorization_pending') continue;
  if (body.error === 'slow_down') { interval += 5; continue; }
  if (body.error === 'expired_token' || body.error === 'access_denied') {
    throw new Error(`Device authorization ended: ${body.error}`);
  }
  throw new Error(`Token polling HTTP ${tokenResponse.status}: ${text.slice(0, 800)}`);
}
if (!tokens) throw new Error('Magnific device authorization expired before approval');
if (!tokens.refresh_token) throw new Error('Authorization succeeded but no refresh_token was issued');

const bundle = JSON.stringify({
  client_id: clientId,
  refresh_token: tokens.refresh_token,
  access_token: tokens.access_token,
  expires_in: tokens.expires_in,
  obtained_at: new Date().toISOString(),
  token_endpoint: tokenEndpoint,
});
const key = crypto.scryptSync(encryptionSecret, 'ottam-magnific-oauth-v1', 32);
const iv = crypto.randomBytes(12);
const cipher = crypto.createCipheriv('aes-256-gcm', key, iv);
const encrypted = Buffer.concat([cipher.update(bundle, 'utf8'), cipher.final()]);
const tag = cipher.getAuthTag();
const envelope = {
  version: 1,
  algorithm: 'aes-256-gcm',
  iv: iv.toString('base64'),
  tag: tag.toString('base64'),
  ciphertext: encrypted.toString('base64'),
};
await fs.writeFile(`${out}/magnific-oauth-session.enc.json`, JSON.stringify(envelope, null, 2));
await fs.writeFile(`${out}/status.json`, JSON.stringify({ authorized: true, has_refresh_token: true, obtained_at: new Date().toISOString() }, null, 2));
console.log('Magnific authorization completed. Encrypted refresh session saved; no token was printed.');
