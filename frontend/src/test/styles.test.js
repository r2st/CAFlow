import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

/**
 * The stylesheet's two sticky layers, asserted against each other.
 *
 * jsdom does not lay anything out or evaluate a media query, so these read
 * the CSS rather than a rendered page — they cannot prove the bar is visible,
 * only that the rule which makes it so is still there. That is worth having:
 * the failure they guard against was invisible on a desktop, invisible in
 * every test, and total on a phone.
 */

const css = readFileSync(
  resolve(dirname(fileURLToPath(import.meta.url)), '../styles/index.css'),
  'utf8',
)

/** The body of the first rule matching `selector`, optionally within `scope`. */
function rule(selector, scope = css) {
  return scope.match(new RegExp(`\\${selector}\\s*\\{([^}]*)\\}`))?.[1]
}

const mobile = css.slice(css.indexOf('@media (max-width: 860px)'))

describe('the two things that stick to the top of a phone', () => {
  it('collapses the sidebar into a sticky header', () => {
    const sidebar = rule('.sidebar', mobile)
    expect(sidebar).toContain('position: sticky')
    expect(sidebar).toContain('top: 0')
  })

  it('drops the bulk bar below that header rather than under it', () => {
    // Both are sticky and the header wins on z-index, so a bulk bar at top: 0
    // is not merely overlapped — its first line, the selected count, is
    // covered by an opaque header and the user cannot see what they picked.
    expect(rule('.bulk-bar', mobile)).toContain('top: var(--mobile-header)')
  })

  it('keeps the header painting over the bar, not the reverse', () => {
    // The other way round would hide the menu button behind the bulk bar.
    const sidebarZ = Number(rule('.sidebar', mobile).match(/z-index:\s*(\d+)/)[1])
    const barZ = Number(rule('.bulk-bar').match(/z-index:\s*(\d+)/)[1])
    expect(sidebarZ).toBeGreaterThan(barZ)
  })

  it('leaves the bar at the very top on a desktop, where nothing is above it', () => {
    // The sidebar is a static column there, so there is nothing to clear.
    expect(rule('.bulk-bar')).toContain('top: 0')
  })

  it('states the header height as a variable the two rules share', () => {
    expect(rule(':root')).toMatch(/--mobile-header:\s*\d+px/)
  })
})
