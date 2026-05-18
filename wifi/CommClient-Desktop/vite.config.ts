import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import electron from 'vite-plugin-electron';
import electronRenderer from 'vite-plugin-electron-renderer';
import { resolve } from 'path';

export default defineConfig({
  plugins: [
    react(),
    electron([
      {
        entry: 'src/main/index.ts',
        onstart(args) {
          args.startup();
        },
        vite: {
          build: {
            outDir: 'dist-electron/main',
            rollupOptions: {
              // electron-updater and its transitive native-ish deps must stay
              // external — they're installed at runtime from node_modules and
              // contain platform-specific binaries Rollup can't bundle.
              external: [
                'electron',
                'electron-updater',
                'electron-log',
                'fs', 'path', 'os', 'crypto', 'url', 'child_process',
              ],
              output: {
                // package.json declares "type": "module", so we need an
                // explicit ESM output and the source provides an
                // import.meta-based __dirname polyfill.
                format: 'es',
                entryFileNames: '[name].js',
              },
            },
          },
        },
      },
      {
        entry: 'src/preload/index.ts',
        onstart(args) {
          args.reload();
        },
        vite: {
          build: {
            outDir: 'dist-electron/preload',
            // Electron loads preload via require() which cannot consume ESM.
            // Use Vite "lib" mode + .cjs extension so Node/Electron interpret
            // it as CommonJS regardless of the parent package.json
            // "type": "module" field. lib mode also reliably suppresses the
            // electron-renderer plugin's ESM rewriting.
            lib: {
              entry: 'src/preload/index.ts',
              formats: ['cjs'],
              fileName: () => 'index.cjs',
            },
            rollupOptions: {
              external: ['electron'],
            },
          },
        },
      },
    ]),
    electronRenderer(),
  ],
  resolve: {
    alias: {
      '@': resolve(__dirname, 'src/renderer'),
    },
  },
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
  },
  build: {
    outDir: 'dist-electron/renderer',
    emptyOutDir: true,
    chunkSizeWarningLimit: 1000,
    // Strip dev-only debug logs from production bundles. The codebase
    // mixes raw console.* with the AppLogger wrapper; the wrapper goes
    // through AppLogger.setLevel('INFO') in prod, but raw console.log
    // calls were leaking into the shipped renderer. Drop console.log /
    // console.debug at minify time so a packaged build is silent unless
    // something genuinely warns or errors. console.warn / console.error
    // are preserved for crash reports + ErrorBoundary.
    minify: 'esbuild',
    rollupOptions: {
      // mediasoup-client is an OPTIONAL runtime dependency. The adapter
      // guards every use with a try/catch around ``import()`` so the app
      // still runs in mesh-only mode if the package isn't present. Mark it
      // external here so Rollup does not try to statically resolve it.
      external: ['mediasoup-client'],
      output: {
        manualChunks: {
          'vendor-react': ['react', 'react-dom', 'react-router-dom'],
          'vendor-ui': ['lucide-react', 'clsx', 'react-hot-toast'],
          'vendor-state': ['zustand'],
          'vendor-socket': ['socket.io-client'],
          'vendor-webrtc': ['simple-peer'],
          'vendor-virtuoso': ['react-virtuoso'],
        },
      },
    },
  },
  optimizeDeps: {
    exclude: ['mediasoup-client'],
  },
  esbuild: {
    // Drop console.log / console.debug at build time. console.warn and
    // console.error survive — those genuinely matter in prod for ops
    // visibility (crash reports, fatal paths). Comments stripped too
    // for slightly smaller bundle. Effective only on `vite build`; dev
    // server keeps everything for local debugging.
    drop: ['debugger'],
    pure: ['console.log', 'console.debug', 'console.info'],
  },
});
