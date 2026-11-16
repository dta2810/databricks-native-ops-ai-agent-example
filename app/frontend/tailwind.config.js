// Databricks brand tokens (databricks-impeccable brand-tokens.json); Lava 600 is a 10% accent only
export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {
      colors: {
        lava: { 600: '#FF3621', 50: '#FFF1EE' },
        navy: { 900: '#0B2026', 800: '#143D4A', 700: '#1B5162', 500: '#618794' },
        oat: { light: '#F9F7F4', medium: '#EEEDE9' },
        ink: { 900: '#0B2026', 700: '#4A5568', 500: '#A0AEC0', 300: '#CBD5E0' },
        line: '#C4CCD6',
        ok: { DEFAULT: '#00A972', 50: '#E6F6F0', 800: '#006B48' },
        warn: { DEFAULT: '#FBB300', 50: '#FFF7E0', 800: '#7A5600' },
        info: { DEFAULT: '#4462C9', 50: '#ECEFFA', 800: '#2B3F86' },
        rose: { 900: '#98102A', 50: '#FBEDEF' },
      },
      fontFamily: {
        sans: ['"DM Sans"', 'ui-sans-serif', 'system-ui', '-apple-system', 'sans-serif'],
        mono: ['"DM Mono"', 'ui-monospace', 'SFMono-Regular', 'Menlo', 'monospace'],
      },
      borderRadius: { card: '12px' },
      transitionTimingFunction: { quart: 'cubic-bezier(0.25, 1, 0.5, 1)' },
    },
  },
  plugins: [],
}
