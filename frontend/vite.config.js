import { mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { PRERENDER_PAGES, renderHead, sitemapXml } from './src/seo.js'

/**
 * Writes an index.html per public route with that page's own title,
 * description, canonical and robots tags, plus sitemap.xml — so a crawler
 * that does not run script still sees per-page meta. The static server
 * already resolves /blog to dist/blog/index.html; unknown routes keep falling
 * back to the root shell.
 */
function seoPages() {
  let outDir
  return {
    name: 'reach-seo-pages',
    apply: 'build',
    configResolved(config) {
      outDir = resolve(config.root, config.build.outDir)
    },
    closeBundle() {
      const template = readFileSync(join(outDir, 'index.html'), 'utf8')
      for (const page of PRERENDER_PAGES) {
        const file = join(outDir, page.path.slice(1), 'index.html')
        mkdirSync(dirname(file), { recursive: true })
        writeFileSync(file, renderHead(template, page.path))
      }
      writeFileSync(join(outDir, 'sitemap.xml'), sitemapXml())
    },
  }
}

export default defineConfig({
  plugins: [react(), seoPages()],
  server: {
    port: 5173,
    proxy: {
      // Lets the dev server talk to the API without CORS or a baked-in host.
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
  // Test settings live in vitest.config.js, which takes precedence over this
  // file — keeping a second copy here would only drift out of step.
})
