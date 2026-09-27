import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./src/**/*.{ts,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      // РАБОТАЮТ через <alpha-value> + rgb-компоненты (--bg-rgb) из globals.css.
      // Раньше здесь было `var(--bg)`, и Tailwind МОЛЧА выбрасывал любой класс
      // с модификатором прозрачности: `bg-bg/80`, `hover:bg-surface/60`,
      // `bg-border/50` не генерировались вообще (пустой CSS) — из-за этого
      // топбар оставался без подложки (эффект стекла/блюра пропадал) и
      // ховеры в сайдбаре/волне не подсвечивались.
      colors: {
        bg: "rgb(var(--bg-rgb) / <alpha-value>)",
        surface: "rgb(var(--surface-rgb) / <alpha-value>)",
        border: "rgb(var(--border-rgb) / <alpha-value>)",
        text: "rgb(var(--text-rgb) / <alpha-value>)",
        muted: "rgb(var(--muted-rgb) / <alpha-value>)",
        accent: "rgb(var(--accent-rgb) / <alpha-value>)",
      },
      fontFamily: {
        sans: [
          "ui-sans-serif",
          "system-ui",
          "-apple-system",
          "Inter",
          "Segoe UI",
          "Roboto",
          "sans-serif",
        ],
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
      },
      borderRadius: {
        xl2: "1rem",
      },
    },
  },
  plugins: [],
};

export default config;
