// Serves the built Vite SPA (frontend/dist) over HTTP with history-API
// fallback. Zero dependencies on purpose: the production box should not need
// an npm install to serve static files, and `vite preview` is a dev tool.
//
//   node deploy/hetzner/static-server.mjs --root /opt/CAFlow/frontend/dist \
//                                         --host 172.18.0.1 --port 3011
//
// Anything that isn't an existing file resolves to index.html, because the
// router is client-side: /clients is a real route to React and a 404 to the
// filesystem.
//
// The security headers below are the ones frontend/nginx.conf sets in the
// Docker deployment — that container is not used on this box, so this process
// is what serves the bundle and must carry them. They live here rather than in
// the shared Caddyfile for the reason nginx.conf gives: the CSP has to match
// the bundle it is serving, so it belongs with the thing that serves it. HSTS
// is the exception and stays in Caddy, which is where TLS actually terminates.
import { createReadStream } from "node:fs";
import { stat } from "node:fs/promises";
import { createServer } from "node:http";
import { extname, join, normalize, resolve, sep } from "node:path";

function arg(name, fallback) {
  const i = process.argv.indexOf(`--${name}`);
  return i !== -1 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
}

const ROOT = resolve(arg("root", "frontend/dist"));
const HOST = arg("host", "127.0.0.1");
const PORT = Number(arg("port", "3011"));

const TYPES = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".gif": "image/gif",
  ".ico": "image/x-icon",
  ".webp": "image/webp",
  ".woff": "font/woff",
  ".woff2": "font/woff2",
  ".ttf": "font/ttf",
  ".map": "application/json; charset=utf-8",
  ".txt": "text/plain; charset=utf-8",
  ".webmanifest": "application/manifest+json",
};

// The policy is what the built app actually needs and nothing more: one hashed
// script and one hashed stylesheet from this origin. `style-src` needs
// 'unsafe-inline' only because React writes `style` attributes. `connect-src
// 'self'` holds because Caddy fronts the API on this same origin — if that
// stops being true, this is what will say so loudly.
const SECURITY_HEADERS = {
  "X-Content-Type-Options": "nosniff",
  "X-Frame-Options": "DENY",
  "Referrer-Policy": "strict-origin-when-cross-origin",
  "Content-Security-Policy":
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; " +
    "img-src 'self' data:; font-src 'self'; connect-src 'self'; object-src 'none'; " +
    "base-uri 'self'; form-action 'self'; frame-ancestors 'none'",
  // Nothing here uses a camera, a microphone, a location or a payment handler.
  "Permissions-Policy":
    "accelerometer=(), camera=(), geolocation=(), gyroscope=(), " +
    "magnetometer=(), microphone=(), payment=(), usb=()",
};

// Vite fingerprints everything under /assets, so those are immutable; the
// entry HTML must never be cached or a deploy leaves clients on stale JS.
function cacheControl(pathname) {
  return pathname.startsWith("/assets/")
    ? "public, max-age=31536000, immutable"
    : "no-cache";
}

async function resolveFile(pathname) {
  // normalize() collapses `..` before the prefix check, so a crafted
  // /../../etc/passwd cannot escape ROOT.
  const candidate = join(ROOT, normalize(decodeURIComponent(pathname)));
  if (candidate !== ROOT && !candidate.startsWith(ROOT + sep)) return null;
  try {
    const s = await stat(candidate);
    if (s.isFile()) return candidate;
    if (s.isDirectory()) {
      const index = join(candidate, "index.html");
      if ((await stat(index)).isFile()) return index;
    }
  } catch {
    /* fall through to the SPA fallback */
  }
  return null;
}

const server = createServer(async (req, res) => {
  if (req.method !== "GET" && req.method !== "HEAD") {
    res.writeHead(405, { Allow: "GET, HEAD" }).end("Method Not Allowed");
    return;
  }

  const pathname = new URL(req.url, "http://localhost").pathname;
  let file = await resolveFile(pathname);

  if (!file) {
    // A missing asset is a genuine 404 — only unknown *routes* get the shell,
    // otherwise a typo'd script src silently returns HTML and the console
    // fills with "Unexpected token '<'".
    if (extname(pathname)) {
      res
        .writeHead(404, { "Content-Type": "text/plain; charset=utf-8", ...SECURITY_HEADERS })
        .end("Not Found");
      return;
    }
    file = join(ROOT, "index.html");
  }

  res.writeHead(200, {
    "Content-Type": TYPES[extname(file).toLowerCase()] ?? "application/octet-stream",
    "Cache-Control": cacheControl(pathname),
    ...SECURITY_HEADERS,
  });
  if (req.method === "HEAD") {
    res.end();
    return;
  }
  createReadStream(file)
    .on("error", () => res.destroy())
    .pipe(res);
});

server.listen(PORT, HOST, () => {
  console.log(`caflow-web: serving ${ROOT} on http://${HOST}:${PORT}`);
});
