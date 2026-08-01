import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach, beforeEach, vi } from 'vitest'

/**
 * Node 20+ defines its own `localStorage` and `sessionStorage` globals that are
 * unavailable unless the process was started with `--localstorage-file`, and
 * they shadow the ones jsdom installs. Install plain in-memory Storages so the
 * app code under test sees the browser behaviour it expects.
 */
function installStorage(name) {
  const store = new Map()
  const storage = {
    getItem: (key) => (store.has(String(key)) ? store.get(String(key)) : null),
    setItem: (key, value) => store.set(String(key), String(value)),
    removeItem: (key) => store.delete(String(key)),
    clear: () => store.clear(),
    key: (index) => [...store.keys()][index] ?? null,
    get length() {
      return store.size
    },
  }
  for (const target of [globalThis, globalThis.window].filter(Boolean)) {
    Object.defineProperty(target, name, {
      value: storage,
      configurable: true,
      writable: true,
    })
  }
}

installStorage('localStorage')
installStorage('sessionStorage')

/**
 * jsdom hands out object URLs but cannot follow one, so the link `saveBlob()`
 * builds logs "Not implemented: navigation" on click. Removing the API sends
 * `saveBlob` down its unsupported-browser path instead — saving the file is
 * the browser's job, and the tests assert the fetch that precedes it.
 */
delete window.URL.createObjectURL

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

beforeEach(() => {
  window.localStorage.clear()
  window.sessionStorage.clear()
})
