import fs from 'node:fs/promises';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';

const registrationEndpoint = 'https://auth.magnific.com/realms/mcp/clients-registrations/openid-connect';
const tokenEndpoint = 'https://auth.magnific.com/realms/mcp/protocol/openid-connect/token';
const mcpEndpoint = 'https://mcp.magnific.com';
const out = 'runtime/magnific-m2m-smoke';
await fs.mkdir(out, { recursive: true });

async function failResponse(label, response) {
  const text = await response.text();
  throw new Error(`${label} HTTP ${response.status}: ${text.slice(0, 1000)}`);
}

const registrationPayload = {
  client_name: `ottam-production-smoke-${Date.now()}`,
  redirect_uris: ['http://127.0.0.1/callback'],
  grant_types: ['client_credentials'],
  response_types: [],
  token_endpoint_auth_method: 'client_secret_post',
  scope: 'mcp:custom-audience',
};

const registrationResponse = await fetch(registrationEndpoint, {
  method: 'POST',
  headers: { 'content-type': 'application/json' },
  body: JSON.stringify(registrationPayload),
});
if (!registrationResponse.ok) await failResponse('dynamic registration', registrationResponse);
const registration = await registrationResponse.json();
const clientId = registration.client_id;
const clientSecret = registration.client_secret;
if (!clientId || !clientSecret) {
  throw new Error('Dynamic registration succeeded but no confidential client credentials were returned');
}
console.log('Dynamic confidential client registration succeeded.');

const tokenBody = new URLSearchParams({
  grant_type: 'client_credentials',
  client_id: clientId,
  client_secret: clientSecret,
  scope: 'mcp:custom-audience',
});
const tokenResponse = await fetch(tokenEndpoint, {
  method: 'POST',
  headers: { 'content-type': 'application/x-www-form-urlencoded' },
  body: tokenBody,
});
if (!tokenResponse.ok) await failResponse('client credentials token', tokenResponse);
const tokenPayload = await tokenResponse.json();
const accessToken = tokenPayload.access_token;
if (!accessToken) throw new Error('Token endpoint returned no access_token');
console.log(`Machine token obtained; expires_in=${tokenPayload.expires_in ?? 'unknown'}s.`);

const transport = new StreamableHTTPClientTransport(new URL(mcpEndpoint), {
  requestInit: {
    headers: { Authorization: `Bearer ${accessToken}` },
  },
});
const client = new Client({ name: 'ottam-production-m2m-smoke', version: '0.1.0' });
await client.connect(transport);

try {
  const tools = await client.listTools();
  const names = tools.tools.map((t) => t.name);
  console.log(`Authenticated Magnific MCP connection succeeded. Tool count=${names.length}.`);
  if (!names.includes('images_generate')) throw new Error('images_generate tool is unavailable');
  if (!names.includes('creations_wait')) throw new Error('creations_wait tool is unavailable');
  if (!names.includes('creations_get')) throw new Error('creations_get tool is unavailable');

  const prompt = `Hand-drawn 2D stickman animation, pure minimalist black line stick figure with a perfectly round plain white circle head, dot eyes and flat line mouth, stick body with single-line arms and legs, NO clothing, NO color fill on the figure, NO shading on the figure, bold clean black outlines only, drawn like a rough kid's notebook doodle, set against a crude, loosely hand-drawn full-color background with flat color washes and visibly wobbly, imperfect linework. A single black stick figure sits on the edge of a simple bed at night, leaning forward with elbows on knees and looking worried. A small bedside table and window establish the bedroom. Keep the composition sparse and readable. No photorealism, no 3D rendering, no digital painting, no airbrushed gradients, no soft cinematic lighting, no glossy finish, no fine detail, no realistic textures, background kept sparse and childlike, absolutely NO text, NO words, NO letters, NO writing, NO posters, NO signage, NO labels anywhere in the image, stick figure itself remains pure black line only with no clothing and no color fill on its body, 16:9 aspect ratio, Ottam rough hand-drawn stickman explainer style.`;

  const generated = await client.callTool({
    name: 'images_generate',
    arguments: { mode: 'gpt-2', aspectRatio: '16:9', quality: 'low', count: 1, prompt },
  });
  const raw = JSON.stringify(generated);
  await fs.writeFile(`${out}/generate-response.json`, JSON.stringify(generated, null, 2));
  await fs.writeFile(`${out}/prompt.txt`, prompt);
  const identifierMatch = raw.match(/\"identifier\"\s*:\s*\"([^\"]+)\"/);
  if (!identifierMatch) throw new Error(`No creation identifier returned: ${raw.slice(0, 1500)}`);
  const identifier = identifierMatch[1];
  console.log('GPT-2 creation queued through machine OAuth.');

  let complete = false;
  for (let i = 0; i < 8; i += 1) {
    const waited = await client.callTool({
      name: 'creations_wait',
      arguments: { identifiers: [identifier], timeoutSeconds: 25 },
    });
    const waitRaw = JSON.stringify(waited);
    await fs.writeFile(`${out}/wait-${i + 1}.json`, JSON.stringify(waited, null, 2));
    if (waitRaw.includes('completed')) { complete = true; break; }
    if (waitRaw.includes('failed') || waitRaw.includes('error')) throw new Error(`Generation failed: ${waitRaw.slice(0, 1500)}`);
  }
  if (!complete) throw new Error('GPT-2 generation did not complete within polling window');

  const details = await client.callTool({ name: 'creations_get', arguments: { creationIdentifier: identifier } });
  await fs.writeFile(`${out}/creation.json`, JSON.stringify(details, null, 2));
  const detailsRaw = JSON.stringify(details);
  const urlMatch = detailsRaw.match(/https:\/\/[^\"\\\s]+/);
  if (!urlMatch) throw new Error(`No downloadable URL found: ${detailsRaw.slice(0, 1500)}`);
  const imageUrl = urlMatch[0].replace(/\\u0026/g, '&');
  const imageResponse = await fetch(imageUrl);
  if (!imageResponse.ok) throw new Error(`Image download failed HTTP ${imageResponse.status}`);
  const bytes = Buffer.from(await imageResponse.arrayBuffer());
  if (bytes.length < 1024) throw new Error(`Image payload too small: ${bytes.length}`);
  await fs.writeFile(`${out}/gpt2-m2m-smoke.png`, bytes);
  await fs.writeFile(`${out}/result.json`, JSON.stringify({ success: true, bytes: bytes.length }, null, 2));
  console.log(`SUCCESS: headless machine-to-machine GPT-2 generation completed; bytes=${bytes.length}.`);
} finally {
  await client.close();
}
