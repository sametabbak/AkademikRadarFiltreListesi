/**
 * AkademikRadar — CORS proxy for ilan.gov.tr
 *
 * ilan.gov.tr sends no Access-Control-Allow-Origin header, so the panel
 * running on github.io cannot call its API directly. This Worker relays the
 * request and adds the header.
 *
 * Deploy (free tier, ~100k requests/day):
 *   1. dash.cloudflare.com  →  Workers & Pages  →  Create  →  Worker
 *   2. Replace the default code with this file, Deploy
 *   3. Copy the worker URL, e.g. https://akademik-cors.<subdomain>.workers.dev
 *   4. Panel → Ayarlar → İlan Getirme → Proxy adresi:
 *        https://akademik-cors.<subdomain>.workers.dev/?url={url}
 *
 * Only ilan.gov.tr is relayed, so this cannot be used as an open proxy.
 */

const ALLOWED_HOSTS = ['www.ilan.gov.tr', 'ilan.gov.tr', 'medya.ilan.gov.tr'];

// Restrict to your own origins. Add localhost for local testing.
const ALLOWED_ORIGINS = [
  'https://sametabbak.github.io',
  'http://localhost:8000',
  'http://127.0.0.1:8000',
];

function corsHeaders(origin) {
  const allow = ALLOWED_ORIGINS.includes(origin) ? origin : ALLOWED_ORIGINS[0];
  return {
    'Access-Control-Allow-Origin': allow,
    'Access-Control-Allow-Methods': 'GET,POST,OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type,Accept',
    'Access-Control-Max-Age': '86400',
  };
}

export default {
  async fetch(request) {
    const origin = request.headers.get('Origin') || '';
    const cors = corsHeaders(origin);

    // Preflight
    if (request.method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: cors });
    }

    const target = new URL(request.url).searchParams.get('url');
    if (!target) {
      return new Response(JSON.stringify({ error: 'Missing ?url= parameter' }), {
        status: 400,
        headers: { ...cors, 'Content-Type': 'application/json' },
      });
    }

    let targetUrl;
    try {
      targetUrl = new URL(target);
    } catch {
      return new Response(JSON.stringify({ error: 'Malformed url parameter' }), {
        status: 400,
        headers: { ...cors, 'Content-Type': 'application/json' },
      });
    }

    if (!ALLOWED_HOSTS.includes(targetUrl.hostname)) {
      return new Response(
        JSON.stringify({ error: `Host not allowed: ${targetUrl.hostname}` }),
        { status: 403, headers: { ...cors, 'Content-Type': 'application/json' } }
      );
    }

    // Relay the request, preserving method and body so POST /AdsByFilter works.
    const init = {
      method: request.method,
      headers: {
        'Accept': 'application/json',
        'User-Agent': 'Mozilla/5.0 (compatible; AkademikRadar/1.0)',
      },
    };
    if (request.method === 'POST') {
      init.headers['Content-Type'] =
        request.headers.get('Content-Type') || 'application/json-patch+json';
      init.body = await request.text();
    }

    try {
      const upstream = await fetch(targetUrl.toString(), init);
      const body = await upstream.text();
      return new Response(body, {
        status: upstream.status,
        headers: {
          ...cors,
          'Content-Type': upstream.headers.get('Content-Type') || 'application/json',
          'Cache-Control': 'public, max-age=300',
        },
      });
    } catch (err) {
      return new Response(JSON.stringify({ error: String(err) }), {
        status: 502,
        headers: { ...cors, 'Content-Type': 'application/json' },
      });
    }
  },
};
