// ESLint (flat config) — RD Mídia Indoor frontend
import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'

export default tseslint.config(
  { ignores: ['dist', 'node_modules', 'coverage', 'public'] },
  {
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    files: ['**/*.{ts,tsx}'],
    languageOptions: {
      ecmaVersion: 2022,
      globals: globals.browser,
    },
    plugins: {
      'react-hooks': reactHooks,
      'react-refresh': reactRefresh,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      'react-refresh/only-export-components': ['warn', { allowConstantExport: true }],
      '@typescript-eslint/no-unused-vars': ['error', { argsIgnorePattern: '^_', varsIgnorePattern: '^_' }],
      // Proibido polling (P001 §70 / E2 #826): nada de refetchInterval no painel.
      'no-restricted-syntax': [
        'error',
        {
          selector: "Property[key.name='refetchInterval']",
          message: 'Polling é proibido (P001 §70). Use eventos de tempo real (RealtimeProvider).',
        },
        {
          selector: "Property[key.name='refetchIntervalInBackground']",
          message: 'Polling é proibido (P001 §70).',
        },
      ],
    },
  },
  {
    files: ['**/*.test.{ts,tsx}', 'src/test/**'],
    languageOptions: { globals: { ...globals.browser, ...globals.node } },
    rules: {
      'react-refresh/only-export-components': 'off',
      'no-restricted-syntax': 'off',
    },
  },
  {
    files: ['vite.config.ts', 'tailwind.config.ts', 'postcss.config.js', 'eslint.config.js'],
    languageOptions: { globals: globals.node },
  },
)
