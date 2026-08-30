import fs from 'node:fs/promises';

const registrationEndpoint = 'https://auth.magnific.com/realms/mcp/clients-registrations/openid-connect';
const tokenEndpoint = 'https://auth.magnific.com/realms/mcp/protocol/openid-connect/token';
const mcpEndpoint = 'https://mcp.magnific.com';
const out = 'runtime/magnific-token-probe';
await fs.mkdir(out, { recursive: true });

const reg = await fetch(registrationEndpoint, {
  method: 'POST',
  headers: { 'content-type': 'application/json' },
  body: JSON.stringify({
    client_name: `ottam-token-probe-${Date.now()}`,
    redirect_uris: ['http://127.0.0.1/callback'],
    grant_types: ['client_credentials'],
    response_types: [],
    token_endpoint_auth_method: 'client_secret_post',
  }),
});
if (!reg.ok) throw new Error(`registration HTTP ${reg.status}: ${(await reg.text()).slice(0, 500)}`);
const creds = await reg.json();
if (!creds.client_id || !creds.client_secret) throw new Error('No confidential credentials returned');

function decodeJwt(token) {
  const [, payload] = token.split('.');
  if (!payload) return {};
  const padded = payload.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - payload.length % 4) % 4);
  return JSON.parse(Buffer.from(padded, 'base64').toString('utf8'));
}

async function getToken(extra) {
  const body = new URLSearchParams({
    grant_type: 'client_credentials',
    client_id: creds.client_id,
    client_secret: creds.client_secret,
    scope: 'mcp:custom-audience',
    ...extra,
  });
  const r = await fetch(tokenEndpoint, {
    method: 'POST', headers: { 'content-type': 'application/x-www-form-urlencoded' }, body,
  });
  const text = await r.text();
  if (!r.ok) return { ok: false, status: r.status, error: text.slice(0, 800) };
  const data = JSON.parse(text);
  const claims = decodeJwt(data.access_token);
  return {
    ok: true,
    token: data.access_token,
    safe: {
      expires_in: data.expires_in,
      scope_response: data.scope,
      claims: {
        iss: claims.iss,
        sub: claims.sub,
        aud: claims.aud,
        azp: claims.azp,
        scope: claims.scope,
        typ: claims.typ,
        client_id: claims.client_id,
        preferred_username: claims.preferred_username,
      },
    },
  };
}

async function tryMcp(token) {
  const payload = {
    jsonrpc: '2.0', id: 1, method: 'initialize',
    params: {
      protocolVersion: '2025-06-18',
      capabilities: {},
      clientInfo: { name: 'ottam-token-probe', version: '0.1.0' },
    },
  };
  const r = await fetch(mcpEndpoint, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${token}`,
      'content-type': 'application/json',
      accept: 'application/json, text/event-stream',
    },
    body: JSON.stringify(payload),
  });
  return {
    status: r.status,
    www_authenticate: r.headers.get('www-authenticate'),
    body: (await r.text()).slice(0, 1000),
  };
}

const variants = {
  scope_only: {},
  resource: { resource: 'https://mcp.magnific.com' },
  audience_url: { audience: 'https://mcp.magnific.com' },
  audience_mcp: { audience: 'mcp' },
};
const report = {};
for (const [name, extra] of Object.entries(variants)) {
  const result = await getToken(extra);
  if (!result.ok) {
    report[name] = result;
    continue;
  }
  report[name] = { token: result.safe, mcp: await tryMcp(result.token) };
}

await fs.writeFile(`${out}/report.json`, JSON.stringify(report, null, 2));
console.log(JSON.stringify(report, null, 2));
