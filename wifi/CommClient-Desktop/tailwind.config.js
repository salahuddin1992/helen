/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/renderer/**/*.{ts,tsx}'],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        primary: {
          50: '#eff6ff', 100: '#dbeafe', 200: '#bfdbfe', 300: '#93c5fd',
          400: '#60a5fa', 500: '#3b82f6', 600: '#2563eb', 700: '#1d4ed8',
          800: '#1e40af', 900: '#1e3a8a',
        },
        surface: {
          50: '#f8fafc', 100: '#f1f5f9', 200: '#e2e8f0', 300: '#cbd5e1',
          700: '#334155', 800: '#1e293b', 900: '#0f172a', 950: '#020617',
        },
        accent: { 400: '#a78bfa', 500: '#8b5cf6', 600: '#7c3aed' },
        success: '#22c55e',
        warning: '#f59e0b',
        danger: '#ef4444',
        online: '#22c55e',
        away: '#f59e0b',
        busy: '#ef4444',
        dnd: '#ef4444',
        offline: '#94a3b8',
      },
      fontFamily: {
        sans: ['Inter', 'Segoe UI', 'Tahoma', 'Arial', 'sans-serif'],
        arabic: ['Cairo', 'Segoe UI', 'Tahoma', 'sans-serif'],
      },
      animation: {
        'pulse-ring': 'pulse-ring 1.5s cubic-bezier(0.4, 0, 0.6, 1) infinite',
        'slide-in': 'slide-in 0.2s ease-out',
        'fade-in': 'fade-in 0.15s ease-out',
      },
      keyframes: {
        'pulse-ring': {
          '0%': { transform: 'scale(0.8)', opacity: '1' },
          '100%': { transform: 'scale(2)', opacity: '0' },
        },
        'slide-in': {
          '0%': { transform: 'translateX(-10px)', opacity: '0' },
          '100%': { transform: 'translateX(0)', opacity: '1' },
        },
        'fade-in': {
          '0%': { opacity: '0' },
          '100%': { opacity: '1' },
        },
      },
    },
  },
  plugins: [],
};
