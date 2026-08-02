import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

/**
 * What nginx promises the browser, and whether the app can keep the promise.
 *
 * A Content-Security-Policy is only worth having if it is tight, and a tight
 * one breaks the moment someone adds a CDN font or an inline analytics
 * snippet. Nothing else in the pipeline would notice: the build succeeds, the
 * tests pass, and the breakage first appears as a blank page in production.
 * These assert the two halves against each other — the policy that is served,
 * and the document it has to allow.
 */

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
const nginxConf = readFileSync(resolve(root, 'nginx.conf'), 'utf8')
const indexHtml = readFileSync(resolve(root, 'index.html'), 'utf8')

function directive(name) {
  const policy = nginxConf.match(/add_header Content-Security-Policy "([^"]+)"/)?.[1]
  return policy
    ?.split(';')
    .map((part) => part.trim())
    .find((part) => part.startsWith(`${name} `))
}

describe('the served security headers', () => {
  it.each([
    'X-Content-Type-Options nosniff',
    'X-Frame-Options DENY',
    'Referrer-Policy strict-origin-when-cross-origin',
  ])('sends %s with the app shell', (header) => {
    expect(nginxConf).toContain(`add_header ${header} always`)
  })

  it('carries nosniff onto the assets too', () => {
    // nginx drops every inherited add_header in a block that declares one of
    // its own, and /assets/ declares Cache-Control — so this has to be
    // repeated there or the script and stylesheet go out bare.
    const assets = nginxConf.match(/location \/assets\/ \{[^}]+\}/)?.[0]
    expect(assets).toBeDefined()
    expect(assets).toContain('X-Content-Type-Options nosniff')
  })

  it('denies the hardware it never asks for', () => {
    const policy = nginxConf.match(/add_header Permissions-Policy "([^"]+)"/)?.[1]
    for (const feature of ['camera', 'geolocation', 'microphone', 'payment']) {
      expect(policy).toContain(`${feature}=()`)
    }
  })
})

describe('the content security policy', () => {
  it.each([
    ['default-src', "default-src 'self'"],
    ['script-src', "script-src 'self'"],
    ['connect-src', "connect-src 'self'"],
    ['object-src', "object-src 'none'"],
    ['base-uri', "base-uri 'self'"],
    ['form-action', "form-action 'self'"],
    ['frame-ancestors', "frame-ancestors 'none'"],
  ])('locks %s down', (name, expected) => {
    expect(directive(name)).toBe(expected)
  })

  it('never allows inline or evaluated script', () => {
    // These two are what turn a CSP from a defence into decoration.
    const policy = nginxConf.match(/add_header Content-Security-Policy "([^"]+)"/)?.[1]
    expect(policy).not.toContain("script-src 'self' 'unsafe-inline'")
    expect(policy).not.toContain('unsafe-eval')
  })

  it('allows inline style only, because React writes style attributes', () => {
    expect(directive('style-src')).toBe("style-src 'self' 'unsafe-inline'")
  })
})

describe('the document the policy has to allow', () => {
  it('has no inline script for script-src to reject', () => {
    // `script-src 'self'` blocks a <script> with a body. Vite emits none;
    // this is here for the day someone pastes one in by hand.
    const scripts = indexHtml.match(/<script\b[^>]*>([\s\S]*?)<\/script>/g) ?? []
    for (const tag of scripts) {
      expect(tag.replace(/<script\b[^>]*>|<\/script>/g, '').trim()).toBe('')
    }
  })

  it('loads nothing from another origin', () => {
    // default-src 'self' means a CDN font or script is a blank page, not a
    // slow one. Catch it here rather than in production.
    const remote = indexHtml.match(/(?:src|href)="(https?:)?\/\/[^"]+"/g) ?? []
    expect(remote).toEqual([])
  })
})
