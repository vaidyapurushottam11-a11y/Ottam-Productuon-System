import fs from 'node:fs/promises';

const out = 'runtime/magnific-oauth-probe';
await fs.mkdir(out, { recursive: true });

async function getJson(url) {
  const r = await fetch(url, { redirect: 'follow' });
  const text = await r.text();
  if (!r.ok) throw new Error(`${url} -> HTTP ${r.status}: ${text.slice(0, 500)}`);
  return JSON.parse(text);
}

const resourceCandidates = [
  'https://mcp.magnific.com/.well-known/oauth-protected-resource',
  'https://mcp.magnific.com/.well-known/oauth-authorization-server',
];

const results = {};
for (const url of resourceCandidates) {
  try {
    results[url] = await getJson(url);
  } catch (e) {
    results[url] = { error: String(e) };
  }
}

let issuer = 'https://auth.magnific.com/realms/mcp';
for (const value of Object.values(results)) {
  const servers = value?.authorization_servers || value?.authorizationServers;
  if (Array.isArray(servers) && servers[0]) issuer = servers[0];
  if (value?.issuer) issuer = value.issuer;
}

const oidcCandidates = [
  `${issuer}/.well-known/openid-configuration`,
  `${issuer}/.well-known/oauth-authorization-server`,
  `https://auth.magnific.com/realms/mcp/.well-known/openid-configuration`,
];

let oidc = null;
let oidcUrl = null;
for (const url of oidcCandidates) {
  try {
    oidc = await getJson(url);
    oidcUrl = url;
    break;
  } catch (e) {
    results[url] = { error: String(e) };
  }
}
if (!oidc) throw new Error('Unable to discover Magnific OAuth metadata');

const safe = {
  discovered_from: oidcUrl,
  issuer: oidc.issuer,
  authorization_endpoint: oidc.authorization_endpoint,
  token_endpoint: oidc.token_endpoint,
  registration_endpoint: oidc.registration_endpoint,
  grant_types_supported: oidc.grant_types_supported,
  response_types_supported: oidc.response_types_supported,
  token_endpoint_auth_methods_supported: oidc.token_endpoint_auth_methods_supported,
  scopes_supported: oidc.scopes_supported,
  resource_metadata: results,
};
await fs.writeFile(`${out}/oauth-metadata.json`, JSON.stringify(safe, null, 2));
console.log(JSON.stringify(safe, null, 2));
