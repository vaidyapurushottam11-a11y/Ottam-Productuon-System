import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';

const [requestPath] = process.argv.slice(2);
if (!requestPath) throw new Error('Usage: node scripts/magnific_mcp_bridge.mjs <request.json>');
const secret = process.env.MAGNIFIC_API_KEY;
if (!secret) throw new Error('MAGNIFIC_API_KEY is not configured');
const authPath = process.env.MAGNIFIC_OAUTH_SESSION_FILE || 'runtime/magnific-auth/magnific-oauth-session.enc.json';

function walk(value, visitor) {
  if (Array.isArray(value)) return value.forEach((v) => walk(v, visitor));
  if (!value || typeof value !== 'object') return;
  for (const [key, child] of Object.entries(value)) {
    visitor(key, child);
    walk(child, visitor);
  }
}
function findIdentifier(value) {
  let result = null;
  walk(value, (key, child) => {
    if (!result && ['identifier', 'creationIdentifier'].includes(key) && typeof child === 'string') result = child;
  });
  if (result) return result;
  const raw = JSON.stringify(value);
  return raw.match(/(?:identifier|creationIdentifier)[^A-Za-z0-9_-]+([A-Za-z0-9_-]{6,40})/i)?.[1] || null;
}
function findUrl(value) {
  let result = null;
  walk(value, (key, child) => {
    if (!result && ['url', 'originalUrl'].includes(key) && typeof child === 'string' && /^https?:\/\//.test(child)) result = child;
  });
  if (result) return result;
  const raw = JSON.stringify(value);
  return raw.match(/https?:\/\/[^"\\\s]+/)?.[0]?.replace(/\\u0026/g, '&') || null;
}
function findCredits(value) {
  let result = null;
  walk(value, (key, child) => {
    if (result === null && key === 'credits' && Number.isFinite(Number(child))) result = Number(child);
  });
  if (result !== null) return result;
  const match = JSON.stringify(value).match(/credits(?:\\?"|\s)*[:=]\s*(\d+)/i);
  return match ? Number(match[1]) : null;
}
function deriveKey() {
  return crypto.scryptSync(secret, 'ottam-magnific-oauth-v1', 32);
}
async function decryptSession() {
  const envelope = JSON.parse(await fs.readFile(authPath, 'utf8'));
  const decipher = crypto.createDecipheriv('aes-256-gcm', deriveKey(), Buffer.from(envelope.iv, 'base64'));
  decipher.setAuthTag(Buffer.from(envelope.tag, 'base64'));
  return JSON.parse(Buffer.concat([
    decipher.update(Buffer.from(envelope.ciphertext, 'base64')),
    decipher.final(),
  ]).toString('utf8'));
}
async function encryptSession(session) {
  const iv = crypto.randomBytes(12);
  const cipher = crypto.createCipheriv('aes-256-gcm', deriveKey(), iv);
  const ciphertext = Buffer.concat([cipher.update(JSON.stringify(session), 'utf8'), cipher.final()]);
  const envelope = {
    version: 1,
    algorithm: 'aes-256-gcm',
    iv: iv.toString('base64'),
    tag: cipher.getAuthTag().toString('base64'),
    ciphertext: ciphertext.toString('base64'),
  };
  await fs.mkdir(path.dirname(authPath), { recursive: true });
  await fs.writeFile(authPath, JSON.stringify(envelope, null, 2));
}

const request = JSON.parse(await fs.readFile(requestPath, 'utf8'));
if (!request.prompt?.trim()) throw new Error('Request prompt is empty');
if (!request.output_path) throw new Error('Request output_path is required');
if (!request.metadata_path) throw new Error('Request metadata_path is required');
if ((request.mode || 'gpt-2') !== 'gpt-2') throw new Error('OTTAM production bridge only permits mode=gpt-2');
if ((request.quality || 'low') !== 'low') throw new Error('OTTAM production bridge only permits quality=low');
if ((request.aspect_ratio || '16:9') !== '16:9') throw new Error('OTTAM production bridge only permits aspect_ratio=16:9');

const session = await decryptSession();
const tokenBody = new URLSearchParams({
  grant_type: 'refresh_token',
  client_id: session.client_id,
  refresh_token: session.refresh_token,
});
const tokenResponse = await fetch(session.token_endpoint, {
  method: 'POST',
  headers: { 'content-type': 'application/x-www-form-urlencoded' },
  body: tokenBody,
});
const tokenText = await tokenResponse.text();
if (!tokenResponse.ok) throw new Error(`Magnific OAuth refresh HTTP ${tokenResponse.status}: ${tokenText.slice(0, 500)}`);
const tokens = JSON.parse(tokenText);
if (!tokens.access_token) throw new Error('Magnific OAuth refresh returned no access token');
if (tokens.refresh_token) session.refresh_token = tokens.refresh_token;
session.obtained_at = new Date().toISOString();
await encryptSession(session);

const transport = new StreamableHTTPClientTransport(new URL('https://mcp.magnific.com'), {
  requestInit: { headers: { Authorization: `Bearer ${tokens.access_token}` } },
});
const client = new Client({ name: 'ottam-production', version: '0.1.0' });
await client.connect(transport);

try {
  const generated = await client.callTool({
    name: 'images_generate',
    arguments: {
      mode: 'gpt-2',
      quality: 'low',
      aspectRatio: '16:9',
      count: 1,
      prompt: request.prompt,
    },
  });
  const identifier = findIdentifier(generated);
  if (!identifier) throw new Error('Magnific images_generate returned no creation identifier');

  let completed = false;
  for (let attempt = 0; attempt < 24; attempt += 1) {
    const waited = await client.callTool({
      name: 'creations_wait',
      arguments: { identifiers: [identifier], timeoutSeconds: 25 },
    });
    const raw = JSON.stringify(waited).toLowerCase();
    if (raw.includes('completed')) { completed = true; break; }
    if (raw.includes('failed') || raw.includes('cancelled')) throw new Error(`Magnific creation ${identifier} failed`);
  }
  if (!completed) throw new Error(`Magnific creation ${identifier} timed out`);

  const details = await client.callTool({ name: 'creations_get', arguments: { creationIdentifier: identifier } });
  const imageUrl = findUrl(details);
  if (!imageUrl) throw new Error(`Magnific creation ${identifier} returned no image URL`);
  const credits = findCredits(details);
  if (credits !== null && credits > 15) {
    throw new Error(`Magnific cost guard rejected creation ${identifier}: ${credits} credits > 15`);
  }

  const imageResponse = await fetch(imageUrl);
  if (!imageResponse.ok) throw new Error(`Magnific image download HTTP ${imageResponse.status}`);
  const bytes = Buffer.from(await imageResponse.arrayBuffer());
  if (bytes.length < 4096) throw new Error(`Magnific image ${identifier} is unexpectedly small (${bytes.length} bytes)`);

  await fs.mkdir(path.dirname(request.output_path), { recursive: true });
  await fs.mkdir(path.dirname(request.metadata_path), { recursive: true });
  await fs.writeFile(request.output_path, bytes);
  await fs.writeFile(request.metadata_path, JSON.stringify({
    identifier,
    mode: 'gpt-2',
    quality: 'low',
    aspect_ratio: '16:9',
    credits,
    byte_count: bytes.length,
    generated_at: new Date().toISOString(),
  }, null, 2));
  console.log(JSON.stringify({ success: true, identifier, credits, bytes: bytes.length }));
} finally {
  await client.close();
}
