import fs from 'node:fs/promises';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';

const apiKey = process.env.MAGNIFIC_API_KEY;
if (!apiKey) throw new Error('MAGNIFIC_API_KEY is not configured');

const endpoint = new URL('https://api.magnific.com/mcp');
const transport = new StreamableHTTPClientTransport(endpoint, {
  requestInit: {
    headers: {
      'x-magnific-api-key': apiKey,
    },
  },
});

const client = new Client({ name: 'ottam-production-smoke', version: '0.1.0' });
await client.connect(transport);

try {
  const tools = await client.listTools();
  const names = tools.tools.map((t) => t.name);
  console.log(`Connected to Magnific MCP. Tool count=${names.length}`);
  if (!names.includes('images_generate')) {
    throw new Error(`images_generate not exposed by MCP. Available sample: ${names.slice(0, 30).join(', ')}`);
  }

  const prompt = `Hand-drawn 2D stickman animation, pure minimalist black line stick figure with a perfectly round plain white circle head, dot eyes and flat line mouth, stick body with single-line arms and legs, NO clothing, NO color fill on the figure, NO shading on the figure, bold clean black outlines only, drawn like a rough kid's notebook doodle, set against a crude, loosely hand-drawn full-color background with flat color washes and visibly wobbly, imperfect linework. A single black stick figure sits on the edge of a simple bed at night, leaning forward with elbows on knees and looking worried. A small bedside table and window establish the bedroom. Keep the composition sparse and readable. No photorealism, no 3D rendering, no digital painting, no airbrushed gradients, no soft cinematic lighting, no glossy finish, no fine detail, no realistic textures, background kept sparse and childlike, absolutely NO text, NO words, NO letters, NO writing, NO posters, NO signage, NO labels anywhere in the image, stick figure itself remains pure black line only with no clothing and no color fill on its body, 16:9 aspect ratio, Ottam rough hand-drawn stickman explainer style.`;

  const generated = await client.callTool({
    name: 'images_generate',
    arguments: {
      mode: 'gpt-2',
      aspectRatio: '16:9',
      quality: 'low',
      count: 1,
      prompt,
    },
  });

  await fs.mkdir('runtime/magnific-mcp-smoke', { recursive: true });
  await fs.writeFile('runtime/magnific-mcp-smoke/generate-response.json', JSON.stringify(generated, null, 2));
  await fs.writeFile('runtime/magnific-mcp-smoke/prompt.txt', prompt);

  const raw = JSON.stringify(generated);
  const identifierMatch = raw.match(/\"identifier\"\s*:\s*\"([^\"]+)\"/);
  if (!identifierMatch) throw new Error(`Could not find creation identifier in images_generate response: ${raw.slice(0, 2000)}`);
  const identifier = identifierMatch[1];
  console.log(`Queued GPT-2 creation: ${identifier}`);

  if (!names.includes('creations_wait')) {
    throw new Error('creations_wait not exposed by MCP');
  }

  let terminal = null;
  for (let i = 0; i < 8; i += 1) {
    const waited = await client.callTool({
      name: 'creations_wait',
      arguments: { identifiers: [identifier], timeoutSeconds: 25 },
    });
    await fs.writeFile(`runtime/magnific-mcp-smoke/wait-${i + 1}.json`, JSON.stringify(waited, null, 2));
    const waitRaw = JSON.stringify(waited);
    if (waitRaw.includes('completed')) {
      terminal = waited;
      break;
    }
    if (waitRaw.includes('failed') || waitRaw.includes('error')) {
      throw new Error(`Magnific creation failed: ${waitRaw.slice(0, 2000)}`);
    }
  }
  if (!terminal) throw new Error('Magnific creation did not complete within smoke-test polling window');

  if (!names.includes('creations_get')) {
    throw new Error('creations_get not exposed by MCP');
  }
  const details = await client.callTool({ name: 'creations_get', arguments: { creationIdentifier: identifier } });
  await fs.writeFile('runtime/magnific-mcp-smoke/creation.json', JSON.stringify(details, null, 2));
  const detailsRaw = JSON.stringify(details);
  const urlMatch = detailsRaw.match(/https:\/\/[^\"\\\s]+/);
  if (!urlMatch) throw new Error(`No generated image URL found: ${detailsRaw.slice(0, 2000)}`);

  const imageUrl = urlMatch[0].replace(/\\u0026/g, '&');
  const imageResponse = await fetch(imageUrl);
  if (!imageResponse.ok) throw new Error(`Image download failed: HTTP ${imageResponse.status}`);
  const image = Buffer.from(await imageResponse.arrayBuffer());
  if (image.length < 1024) throw new Error(`Downloaded image too small: ${image.length} bytes`);
  await fs.writeFile('runtime/magnific-mcp-smoke/gpt2-smoke.png', image);
  console.log(`MCP GPT-2 smoke test complete. Downloaded ${image.length} bytes.`);
} finally {
  await client.close();
}
