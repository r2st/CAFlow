// Per-page titles, descriptions and robots directives — read by SeoHead at
// runtime and by the Vite build, which writes a pre-rendered index.html per
// public route plus the sitemap. One table, so the tags a crawler sees in the
// raw HTML and the ones the SPA sets after navigation cannot drift apart.
import { ARTICLES } from './content/articles.js'

export const SITE_URL = 'https://reach.doaide.com'
export const SITE_NAME = 'DoAide Reach'

const DEFAULT_DESCRIPTION =
  'DoAide Reach — free outreach workspace for Indian startups and businesses. Track prospects, send personalised WhatsApp and email messages, and never miss a follow-up.'

/** Public pages: indexed, listed in the sitemap, pre-rendered at build. */
export const PUBLIC_PAGES = [
  {
    path: '/',
    title: 'DoAide Reach — Free Outreach Tracker with WhatsApp Follow-ups',
    description: DEFAULT_DESCRIPTION,
    changefreq: 'weekly',
    priority: '1.0',
  },
  {
    path: '/outreach',
    title: 'Free Outreach Workspace — No Sign-up | DoAide Reach',
    description:
      'Track prospects, write personalised cold messages from templates and send them on WhatsApp or email. Free, no account needed — your list stays in your browser.',
    changefreq: 'monthly',
    priority: '0.9',
  },
  {
    path: '/outreach/dashboard',
    title: 'Outreach Dashboard — Reply Rate & Follow-ups | DoAide Reach',
    description:
      'See prospects contacted, reply rate, meetings booked and who needs a follow-up today. Free outreach metrics with no sign-up.',
    changefreq: 'monthly',
    priority: '0.7',
  },
  {
    path: '/blog',
    title: 'Outreach Blog — Cold Email & WhatsApp Tips for India | DoAide Reach',
    description:
      'Email outreach strategies, deliverability guides and lead-generation tips for Indian startups and businesses.',
    changefreq: 'weekly',
    priority: '0.8',
  },
  ...ARTICLES.map((a) => ({
    path: `/blog/${a.slug}`,
    title: `${a.title} | DoAide Reach Blog`,
    description: a.excerpt,
    changefreq: 'monthly',
    priority: '0.7',
    type: 'article',
    lastmod: new Date(a.date).toISOString().slice(0, 10),
  })),
]

/** Reachable without signing in, but nothing a search result should land on. */
const NOINDEX_PAGES = [
  { path: '/login', title: 'Sign in | DoAide Reach' },
  { path: '/register', title: 'Create your account | DoAide Reach' },
  { path: '/portal', title: 'Client portal | DoAide Reach' },
]

export const PRERENDER_PAGES = [...PUBLIC_PAGES, ...NOINDEX_PAGES.map((p) => ({ ...p, noindex: true }))]

/**
 * The meta for a path. Anything not in the table is the signed-in app, which
 * is never indexed — it would only ever show a crawler the sign-in redirect.
 */
export function metaForPath(pathname) {
  const path = pathname.length > 1 ? pathname.replace(/\/+$/, '') : pathname
  const page = PRERENDER_PAGES.find((p) => p.path === path)
  if (page) {
    return {
      title: page.title,
      description: page.description ?? DEFAULT_DESCRIPTION,
      canonical: `${SITE_URL}${page.path === '/' ? '/' : page.path}`,
      robots: page.noindex ? 'noindex, follow' : 'index, follow',
      type: page.type ?? 'website',
    }
  }
  return {
    title: null,
    description: DEFAULT_DESCRIPTION,
    canonical: null,
    robots: 'noindex, nofollow',
    type: 'website',
  }
}

export function sitemapXml(pages = PUBLIC_PAGES) {
  const urls = pages
    .map((p) => {
      const lastmod = p.lastmod ? `<lastmod>${p.lastmod}</lastmod>` : ''
      return `  <url><loc>${SITE_URL}${p.path}</loc>${lastmod}<changefreq>${p.changefreq}</changefreq><priority>${p.priority}</priority></url>`
    })
    .join('\n')
  return `<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n${urls}\n</urlset>\n`
}

function escapeAttr(value) {
  return String(value).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;')
}

/**
 * Rewrites the built index.html's head for one page. Each tag is replaced in
 * place, so the template stays the single source of everything else.
 */
export function renderHead(html, pathname) {
  const meta = metaForPath(pathname)
  const set = (pattern, replacement) => {
    if (!pattern.test(html)) throw new Error(`index.html is missing ${pattern}`)
    html = html.replace(pattern, replacement)
  }
  const title = escapeAttr(meta.title)
  const description = escapeAttr(meta.description)
  set(/<title>[^<]*<\/title>/, `<title>${title}</title>`)
  set(/<meta name="description" content="[^"]*" \/>/, `<meta name="description" content="${description}" />`)
  set(/<meta name="robots" content="[^"]*" \/>/, `<meta name="robots" content="${meta.robots}" />`)
  set(/<link rel="canonical" href="[^"]*" \/>/, `<link rel="canonical" href="${meta.canonical}" />`)
  set(/<meta property="og:title" content="[^"]*" \/>/, `<meta property="og:title" content="${title}" />`)
  set(/<meta property="og:description" content="[^"]*" \/>/, `<meta property="og:description" content="${description}" />`)
  set(/<meta property="og:url" content="[^"]*" \/>/, `<meta property="og:url" content="${meta.canonical}" />`)
  set(/<meta property="og:type" content="[^"]*" \/>/, `<meta property="og:type" content="${meta.type}" />`)
  set(/<meta name="twitter:title" content="[^"]*" \/>/, `<meta name="twitter:title" content="${title}" />`)
  set(/<meta name="twitter:description" content="[^"]*" \/>/, `<meta name="twitter:description" content="${description}" />`)
  return html
}
