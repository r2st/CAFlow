import { useEffect } from 'react'
import { useLocation } from 'react-router-dom'
import { metaForPath } from '../seo'

function setMeta(selector, attr, value) {
  const el = document.head.querySelector(selector)
  if (el && value != null) el.setAttribute(attr, value)
}

/**
 * Keeps the head in step with the route after client-side navigation. The
 * first load already has the right tags — the build pre-renders them per
 * page — so this matters for crawlers that run script and for link previews
 * taken from an in-app URL. The tab title stays RouteAnnouncer's job; public
 * pages hand it their SEO title through metaForPath.
 */
export default function SeoHead() {
  const { pathname } = useLocation()

  useEffect(() => {
    const meta = metaForPath(pathname)
    setMeta('meta[name="description"]', 'content', meta.description)
    setMeta('meta[name="robots"]', 'content', meta.robots)
    setMeta('meta[property="og:description"]', 'content', meta.description)
    setMeta('meta[name="twitter:description"]', 'content', meta.description)
    setMeta('meta[property="og:type"]', 'content', meta.type)
    if (meta.title) {
      setMeta('meta[property="og:title"]', 'content', meta.title)
      setMeta('meta[name="twitter:title"]', 'content', meta.title)
    }
    const ld = document.head.querySelector('script[data-seo-jsonld]')
    if (ld) ld.textContent = meta.jsonLd ? JSON.stringify(meta.jsonLd) : '{}'
    if (meta.canonical) {
      setMeta('link[rel="canonical"]', 'href', meta.canonical)
      setMeta('meta[property="og:url"]', 'content', meta.canonical)
    }
  }, [pathname])

  return null
}
