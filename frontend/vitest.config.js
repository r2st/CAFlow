import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: './src/test/setup.js',
    css: false,
    include: ['src/**/*.test.{js,jsx}'],
    coverage: {
      provider: 'v8',
      reporter: ['text', 'html'],
      // Only the code that ships. Counting the fixtures and the setup file
      // measures the tests against themselves, which reads several points
      // higher than the app actually is.
      include: ['src/**/*.{js,jsx}'],
      exclude: ['src/test/**', 'src/main.jsx'],
      // A floor, not a target: set just under where the suite currently sits
      // so an uncovered addition trips CI, without failing the build on the
      // ordinary drift of a line or two.
      thresholds: {
        statements: 91,
        branches: 86,
        functions: 87,
        lines: 93,
      },
    },
  },
})
