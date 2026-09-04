/*
 * Copyright (c) 2024-2025 Datalayer, Inc.
 *
 * Distributed under the terms of the Modified BSD License.
 */

const js = require('@eslint/js');
const tseslint = require('typescript-eslint');
const prettierRecommended = require('eslint-plugin-prettier/recommended');

module.exports = tseslint.config(
  {
    ignores: [
      '*.js',
      'coverage/**',
      'dist/**',
      'docs/**',
      'jupyter_server_nbmodel/**',
      'lib/**',
      'node_modules/**',
      'ui-tests/**',
      '**/*.d.ts',
      '**/__tests__/**',
      '**/tests/**'
    ]
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  prettierRecommended,
  {
    files: ['**/*.ts', '**/*.tsx'],
    languageOptions: {
      sourceType: 'module',
      parserOptions: {
        project: 'tsconfig.json',
        tsconfigRootDir: __dirname
      }
    },
    rules: {
      '@typescript-eslint/naming-convention': [
        'error',
        {
          selector: 'interface',
          format: ['PascalCase'],
          custom: {
            regex: '^I[A-Z]',
            match: true
          }
        }
      ],
      '@typescript-eslint/no-unused-vars': ['warn', { args: 'none' }],
      '@typescript-eslint/no-explicit-any': 'off',
      '@typescript-eslint/no-namespace': 'off',
      '@typescript-eslint/no-use-before-define': 'off',
      curly: ['error', 'all'],
      eqeqeq: 'error',
      'prefer-arrow-callback': 'error'
    }
  }
);
