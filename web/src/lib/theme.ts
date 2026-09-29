"use client";

/** Темы: light/dark как были + polar-light/polar-dark из плеера.
 *  polar-dark вешается вместе с .dark, чтобы dark:-утилиты Tailwind
 *  продолжали срабатывать (vars .polar-dark идут позже .dark и перебивают).
 *  По умолчанию — polar-dark, как у плееров. */
export type ThemeId = "light" | "dark" | "polar-light" | "polar-dark";

export const DEFAULT_THEME: ThemeId = "polar-dark";

const THEME_KEY = "theme";

export function isThemeId(v: unknown): v is ThemeId {
  return v === "light" || v === "dark" || v === "polar-light" || v === "polar-dark";
}

export function applyTheme(theme: ThemeId) {
  if (typeof document === "undefined") return;
  const root = document.documentElement;
  root.classList.toggle("dark", theme === "dark" || theme === "polar-dark");
  root.classList.toggle("polar-dark", theme === "polar-dark");
  root.classList.toggle("polar-light", theme === "polar-light");
  try {
    localStorage.setItem(THEME_KEY, theme);
  } catch {
    /* приватный режим — тема просто не сохранится */
  }
}

export function readTheme(): ThemeId {
  try {
    const t = localStorage.getItem(THEME_KEY);
    if (isThemeId(t)) return t;
  } catch {
    /* ignore */
  }
  return DEFAULT_THEME;
}
