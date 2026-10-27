import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { render, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import RouteAnnouncer from '../components/RouteAnnouncer'
import SeoHead from '../components/SeoHead'
import { ARTICLES } from '../content/articles'
import { PRERENDER_PAGES, PUBLIC_PAGES, metaForPath, renderHead, sitemapXml } from '../seo'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
const indexHtml = readFileSync(resolve(root, 'index.html'), 'utf8')
const robots = readFileSync(resolve(root, 'public/robots.txt'), 'utf8')

describe('per-page meta', () => {
  it('gives every public page its own title and description', () => {
    const titles = PUBLIC_PAGES.map((p) => p.title)
    const descriptions = PUBLIC_PAGES.map((p) => p.description)
    expect(new Set(titles).size).toBe(titles.length)
    expect(new Set(descriptions).size).toBe(descriptions.length)
    for (const page of PUBLIC_PAGES) {
      expect(page.title).toMatch(/DoAide Reach/)
      expect(page.description.length).toBeGreaterThan(50)
    }
  })

  it('covers every blog article', () => {
    for (const a of ARTICLES) {
      expect(metaForPath(`/blog/${a.slug}`)).toMatchObject({
        title: `${a.title} | DoAide Reach Blog`,
        robots: 'index, follow',
        type: 'article',
      })
    }
  })

  it('keeps the sign-in pages and the app out of the index', () => {
    expect(metaForPath('/login').robots).toBe('noindex, follow')
    expect(metaForPath('/clients').robots).toBe('noindex, nofollow')
    expect(metaForPath('/clients').canonical).toBeNull()
  })

  it('treats a trailing slash as the same page', () => {
    expect(metaForPath('/blog/').canonical).toBe('https://reach.doaide.com/blog')
  })
})

describe('the pre-rendered head', () => {
  it('rewrites every tag a crawler reads for that page', () => {
    const html = renderHead(indexHtml, '/outreach')
    expect(html).toContain('<title>Free Outreach Workspace — No Sign-up | DoAide Reach</title>')
    expect(html).toContain('<link rel="canonical" href="https://reach.doaide.com/outreach" />')
    expect(html).toContain('<meta property="og:url" content="https://reach.doaide.com/outreach" />')
    expect(html).toContain('<meta name="robots" content="index, follow" />')
    expect(html).toMatch(/<meta name="twitter:title" content="Free Outreach Workspace/)
  })

  it('works on the real index.html for every page it is asked to render', () => {
    // renderHead throws if a tag it rewrites has gone missing from the template.
    for (const page of PRERENDER_PAGES) expect(() => renderHead(indexHtml, page.path)).not.toThrow()
  })

  it('escapes quotes so a title cannot break out of its attribute', () => {
    const html = renderHead(indexHtml, '/blog/outreach-vs-ads-roi-comparison')
    expect(html).not.toMatch(/content="[^"]*"[^ />]/)
  })
})

describe('crawler h1 and structured data', () => {
  const jsonLdIn = (html) =>
    JSON.parse(html.match(/<script type="application\/ld\+json" data-seo-jsonld>([\s\S]*?)<\/script>/)[1])

  it('gives every public page one h1 in the pre-rendered HTML', () => {
    for (const page of PUBLIC_PAGES) {
      const html = renderHead(indexHtml, page.path)
      expect(html.match(/<h1>/g), page.path).toHaveLength(1)
    }
  })

  it('keeps the product FAQ on the home page only', () => {
    expect(JSON.stringify(jsonLdIn(renderHead(indexHtml, '/')))).toContain('FAQPage')
    expect(JSON.stringify(jsonLdIn(renderHead(indexHtml, '/outreach')))).not.toContain('FAQPage')
  })

  it('marks each article as a dated BlogPosting', () => {
    for (const page of PUBLIC_PAGES.filter((p) => p.type === 'article')) {
      const ld = jsonLdIn(renderHead(indexHtml, page.path))
      expect(ld['@type']).toBe('BlogPosting')
      expect(ld.datePublished).toMatch(/^\d{4}-\d{2}-\d{2}$/)
    }
  })

  it('gives sign-in pages no structured data', () => {
    expect(jsonLdIn(renderHead(indexHtml, '/login'))).toEqual({})
  })
})

describe('sitemap and robots', () => {
  it('lists every public page and nothing private', () => {
    const xml = sitemapXml()
    for (const page of PUBLIC_PAGES) expect(xml).toContain(`<loc>https://reach.doaide.com${page.path}</loc>`)
    expect(xml).not.toContain('/login')
    expect(xml).not.toContain('reach.doaide.com/dashboard<')
    expect(xml).toMatch(/<lastmod>\d{4}-\d{2}-\d{2}<\/lastmod>/)
  })

  it('points crawlers at the sitemap and away from the app', () => {
    expect(robots).toContain('Sitemap: https://reach.doaide.com/sitemap.xml')
    for (const path of ['/dashboard', '/clients', '/portal', '/api/']) expect(robots).toContain(`Disallow: ${path}`)
    expect(robots).not.toMatch(/Disallow: \/outreach|Disallow: \/blog/)
  })
})

describe('after client-side navigation', () => {
  function renderAt(path) {
    return render(
      <MemoryRouter initialEntries={[path]}>
        <RouteAnnouncer />
        <SeoHead />
      </MemoryRouter>,
    )
  }

  function head() {
    document.head.innerHTML = indexHtml.match(/<head>([\s\S]*)<\/head>/)[1]
  }

  it('titles the home page as the home page, not the dashboard', async () => {
    head()
    renderAt('/')
    await waitFor(() => expect(document.title).toBe('DoAide Reach — Free Outreach Tracker with WhatsApp Follow-ups'))
  })

  it('updates description, canonical and robots for the page shown', async () => {
    head()
    renderAt('/outreach/dashboard')
    await waitFor(() =>
      expect(document.head.querySelector('link[rel="canonical"]').getAttribute('href')).toBe(
        'https://reach.doaide.com/outreach/dashboard',
      ),
    )
    expect(document.head.querySelector('meta[name="description"]').getAttribute('content')).toMatch(/reply rate/)
    expect(document.title).toBe('Outreach Dashboard — Reply Rate & Follow-ups | DoAide Reach')
  })

  it('marks a signed-in screen noindex', async () => {
    head()
    renderAt('/billing')
    await waitFor(() =>
      expect(document.head.querySelector('meta[name="robots"]').getAttribute('content')).toBe('noindex, nofollow'),
    )
    expect(document.title).toBe('Billing · DoAide Reach')
  })
})
