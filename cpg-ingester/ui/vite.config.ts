import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';
import { resolve } from 'path';

export default defineConfig({
  plugins: [react()],
  resolve: {
    dedupe: ['react', 'react-dom', 'react-router', '@patternfly/react-core', '@patternfly/react-icons'],
    alias: [
      { find: '@app', replacement: resolve(__dirname, 'src') },
      { find: /^react$/, replacement: resolve(__dirname, 'node_modules/react') },
      { find: /^react-dom$/, replacement: resolve(__dirname, 'node_modules/react-dom') },
      { find: /^react\/jsx-runtime$/, replacement: resolve(__dirname, 'node_modules/react/jsx-runtime') },
      { find: /^react\/jsx-dev-runtime$/, replacement: resolve(__dirname, 'node_modules/react/jsx-dev-runtime') },
      { find: /^react-dom\/client$/, replacement: resolve(__dirname, 'node_modules/react-dom/client') },
    ],
  },
  server: {
    port: 3003,
    proxy: {
      '/api': 'http://localhost:8095',
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/setupTests.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
    server: {
      deps: {
        inline: ['@cpg-to-acp/ui-shared', /@patternfly\//],
      },
    },
  },
});
