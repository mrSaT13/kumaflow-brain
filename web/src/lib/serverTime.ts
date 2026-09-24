"use client";

// Сервер хранит время в naive UTC, а «домашняя» зона (APP_TIMEZONE) живёт
// только на бэке. Чтобы веб показывал локальное время правильно, узнаём
// сдвиг сервера из /api/version и интерпретируем naive-метки как стенку
// серверной зоны.

let offsetSec: number | null = null;
let pending: Promise<number> | null = null;

export function getServerOffset(): number {
  return offsetSec ?? 0;
}

export function setServerOffset(v: number) {
  offsetSec = v;
}

/** Один раз за сессию подтягивает utc_offset_sec из /api/version. */
export function initServerOffset(): Promise<number> {
  if (offsetSec !== null) return Promise.resolve(offsetSec);
  if (pending) return pending;
  pending = (async () => {
    try {
      const r = await fetch("/api/version", { cache: "no-store" });
      const j = await r.json();
      const v = Number(j?.utc_offset_sec ?? 0);
      offsetSec = Number.isFinite(v) ? v : 0;
    } catch {
      offsetSec = 0;
    }
    return offsetSec as number;
  })();
  return pending;
}

const TZ_SUFFIX = /([Zz]|[+-]\d{2}:?\d{2})$/;

/** Naive-метка сервера («2026-09-23T18:18:00») → instant с учётом зоны сервера. */
export function serverInstant(iso: string): number {
  const s = iso.trim();
  if (TZ_SUFFIX.test(s)) return Date.parse(s);
  // стенка серверной зоны → UTC: вычитаем сдвиг
  return Date.parse(`${s}Z`) - getServerOffset() * 1000;
}
