import js from '@eslint/js'
import react from 'eslint-plugin-react'
import reactHooks from 'eslint-plugin-react-hooks'
import globals from 'globals'

/**
 * Flat ESLint config.
 *
 * Deliberately close to the recommended sets — the value here is catching the
 * mistakes that survive review (a stale hook dependency, an unused import, a
 * missing key), not enforcing a house style. Formatting is left alone.
 */
export default [
  { ignores: ['dist/**', 'node_modules/**', 'coverage/**'] },
  js.configs.recommended,
  {
    files: ['**/*.{js,jsx}'],
    languageOptions: {
      ecmaVersion: 2023,
      sourceType: 'module',
      globals: { ...globals.browser, ...globals.es2021 },
      parserOptions: {
        ecmaFeatures: { jsx: true },
      },
    },
    settings: { react: { version: 'detect' } },
    plugins: { react, 'react-hooks': reactHooks },
    rules: {
      ...react.configs.flat.recommended.rules,
      ...react.configs.flat['jsx-runtime'].rules,
      ...reactHooks.configs.recommended.rules,
      // Props are documented by the components themselves; a second copy in
      // propTypes would only drift.
      'react/prop-types': 'off',
      'no-unused-vars': ['error', { argsIgnorePattern: '^_', varsIgnorePattern: '^_' }],
    },
  },
  {
    files: ['src/test/**/*.{js,jsx}', '**/*.test.{js,jsx}'],
    languageOptions: { globals: { ...globals.node } },
  },
  {
    files: ['*.config.js'],
    languageOptions: { globals: { ...globals.node } },
  },
]
