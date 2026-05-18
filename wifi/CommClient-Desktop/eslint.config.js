// eslint.config.js — flat config for ESLint v9+.
// Replaces the legacy .eslintrc.* that stopped working when ESLint
// switched to the flat-config-only model. Scope: src/**/*.{ts,tsx}.

import tseslint from 'typescript-eslint';
import reactHooks from 'eslint-plugin-react-hooks';

export default tseslint.config(
  {
    ignores: [
      'dist-electron/**',
      'release/**',
      'node_modules/**',
      'build/**',
      'scripts/**/*.mjs',
      'scripts/**/*.cjs',
      '**/*.cjs',
    ],
  },
  ...tseslint.configs.recommended,
  {
    files: ['src/**/*.{ts,tsx}'],
    languageOptions: {
      parserOptions: {
        ecmaVersion: 'latest',
        sourceType: 'module',
        ecmaFeatures: { jsx: true },
      },
    },
    plugins: { 'react-hooks': reactHooks },
    rules: {
      // We're not chasing zero-warning purity here — focus on the rules
      // that actually catch live bugs (unused vars, deps, missing
      // returns) and let stylistic prefs slide.
      'no-unused-vars': 'off',
      '@typescript-eslint/no-unused-vars': ['warn', {
        argsIgnorePattern: '^_',
        varsIgnorePattern: '^_',
      }],
      '@typescript-eslint/no-explicit-any': 'off',     // we use `any` deliberately at IPC seams
      '@typescript-eslint/no-empty-function': 'off',
      '@typescript-eslint/no-non-null-assertion': 'off',
      '@typescript-eslint/ban-ts-comment': 'off',
      // Project-wide preexisting code that we're not blocking on:
      '@typescript-eslint/no-require-imports': 'off',
      '@typescript-eslint/no-unused-expressions': 'off',
      'prefer-const': 'off',
      'react-hooks/rules-of-hooks': 'error',
      'react-hooks/exhaustive-deps': 'warn',
      'no-empty': ['warn', { allowEmptyCatch: true }],
      'no-constant-condition': ['warn', { checkLoops: false }],
    },
  },
);
