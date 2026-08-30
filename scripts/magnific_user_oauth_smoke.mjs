import fs from 'node:fs/promises';
import crypto from 'node:crypto';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';

const secret = process.env.MAGNIFIC_API_KEY;
if (!secret) throw new Error('MAGNIFIC_API_KEY missing');
const authPath = 'runtime/magnific-auth/magnific-oauth-session.enc.json';
const out = 'runtime/magnific-user-smoke';
await fs.mkdir(out, { recursive: true });

const envelope = JSON.parse(await fs.readFile(authPath, 'utf8'));
const key = crypto.scryptSync(secret, 'ottam-magnific-oauth-v1', 32);
const decipher = crypto.createDecipheriv('aes-256-gcm', key, Buffer.from(envelope.iv, 'base64'));
decipher.setAuthTag(Buffer.from(envelope.tag, 'base64'));
const clear = Buffer.concat([
  decipher.update(Buffer.from(envelope.ciphertext, 'base64')),
  decipher.final(),
]).toString('utf8');
const session = JSON.parse(clear);

// Always refresh instead of reusing a potentially expired access token.
const refreshBody = new URLSearchParams({
  grant_type: 'refresh_token',
  client_id: session.client_id,
  refresh_token: session.refresh_token,
});
const refreshResponse = await fetch(session.token_endpoint, {
  method: 'POST',
  headers: { 'content-type': 'application/x-www-form-urlencoded' },
  body: refreshBody,
});
const refreshText = await refreshResponse.text();
if (!refreshResponse.ok) throw new Error(`Magnific refresh HTTP ${refreshResponse.status}: ${refreshText.slice(0, 800)}`);
const tokens = JSON.parse(refreshText);
if (!tokens.access_token) throw new Error('Magnific refresh returned no access_token');
console.log(`User-bound Magnific access token refreshed; expires_in=${tokens.expires_in}s.`);

const transport = new StreamableHTTPClientTransport(new URL('https://mcp.magnific.com'), {
  requestInit: { headers: { Authorization: `Bearer ${tokens.access_token}` } },
});
const client = new Client({ name: 'ottam-production-gpt2-smoke', version: '0.1.0' });
await client.connect(transport);

const prompt = `Hand-drawn 2D stickman animation, pure minimalist black line stick figure with a perfectly round plain white circle head, dot eyes and flat line mouth, stick body with single-line arms and legs, NO clothing, NO color fill on the figure, NO shading on the figure, bold clean black outlines only, drawn like a rough kid's notebook doodle, set against a crude, loosely hand-drawn full-color background with flat color washes and visibly wobbly, imperfect linework. A single black stick figure sits on the edge of a simple bed at night, leaning forward with elbows on knees and looking worried. A small bedside table and window establish the bedroom. Keep the composition sparse and readable. No photorealism, no 3D rendering, no digital painting, no airbrushed gradients, no soft cinematic lighting, no glossy finish, no fine detail, no realistic textures, background kept sparse and childlike, absolutely NO text, NO words, NO letters, NO writing, NO posters, NO signage, NO labels anywhere in the image, stick figure itself remains pure black line only with no clothing and no color fill on its body, 16:9 aspect ratio, OTTAM rough hand-drawn stickman explainer style.`;

try {
  const tools = await client.listTools();
  const names = tools.tools.map((t) => t.name);
  if (!names.includes('images_generate')) throw new Error('images_generate unavailable');
  console.log(`Authenticated Magnific MCP connection established. tools=${names.length}`);

  const generated = await client.callTool({
    name: 'images_generate',
    arguments: { mode: 'gpt-2', aspectRatio: '16:9', quality: 'low', count: 1, prompt },
  });
  await fs.writeFile(`${out}/generate-response.json`, JSON.stringify(generated, null, 2));
  await fs.writeFile(`${out}/prompt.txt`, prompt);

  const raw = JSON.stringify(generated);
  const idMatches = [...raw.matchAll(/(?:identifier|creationIdentifier)[\\"']?\s*[:=]\s*[\\"']([^\\"']+)/g)];
  let identifier = idMatches[0]?.[1];
  if (!identifier) {
    const compact = raw.match(/\b[A-Za-z0-9_-]{8,24}\b/g) || [];
    identifier = compact.find((x) => !['images_generate','text-to-image'].includes(x));
  }
  if (!identifier) throw new Error(`Unable to find Magnific creation identifier: ${raw.slice(0, 1800)}`);
  console.log(`Queued GPT-2 low-quality creation: ${identifier}`);

  let completed = false;
  for (let i = 0; i < 12; i += 1) {
    const wait = await client.callTool({ name: 'creations_wait', arguments: { identifiers: [identifier], timeoutSeconds: 25 } });
    await fs.writeFile(`${out}/wait-${i + 1}.json`, JSON.stringify(wait, null, 2));
    const wr = JSON.stringify(wait).toLowerCase();
    if (wr.includes('completed')) { completed = true; break; }
    if (wr.includes('failed') || wr.includes('error')) throw new Error(`Magnific generation failed: ${wr.slice(0, 1500)}`);
  }
  if (!completed) throw new Error('Magnific generation did not complete within smoke polling window');

  const details = await client.callTool({ name: 'creations_get', arguments: { creationIdentifier: identifier } });
  await fs.writeFile(`${out}/creation.json`, JSON.stringify(details, null, 2));
  const dr = JSON.stringify(details);
  const urlMatch = dr.match(/https:\\/\\/[^\\"\\\\\s]+/);
  if (!urlMatch) throw new Error(`No generated image URL in creation details: ${dr.slice(0, 1800)}`);
  const imageUrl = urlMatch[0].replace(/\\u0026/g, '&').replace(/\\\//g, '/');
  const imageResponse = await fetch(imageUrl);
  if (!imageResponse.ok) throw new Error(`Image download HTTP ${imageResponse.status}`);
  const bytes = Buffer.from(await imageResponse.arrayBuffer());
  if (bytes.length < 4096) throw new Error(`Downloaded image unexpectedly small: ${bytes.length}`);
  await fs.writeFile(`${out}/gpt2-low-16x9.png`, bytes);
  await fs.writeFile(`${out}/status.json`, JSON.stringify({ success: true, identifier, bytes: bytes.length, mode: 'gpt-2', quality: 'low', aspectRatio: '16:9' }, null, 2));
  console.log(`GPT-2 low-quality 16:9 smoke completed; bytes=${bytes.length}.`);
} finally {
  await client.close();
}
