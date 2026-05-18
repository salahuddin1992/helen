import { defineConfig } from 'vitest/config';
import { resolve } from 'path';

export default defineConfig({
    test: {
        globals: false,
        environment: 'jsdom',
        include: ['src/**/*.test.ts', 'src/**/*.test.tsx'],
        exclude: ['node_modules', 'dist-electron', 'release'],
        // Helps with Electron-isolated modules: mock anything they touch.
        deps: {
            optimizer: {
                web: { include: ['zustand'] },
            },
        },
    },
    resolve: {
        alias: {
            '@': resolve(__dirname, 'src/renderer'),
        },
    },
});
