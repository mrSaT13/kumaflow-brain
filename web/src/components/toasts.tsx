"use client";

import { createContext, useCallback, useContext, useState } from "react";

type Tone = "ok" | "err" | "info";
type Toast = { id: number; msg: string; tone: Tone };

const Ctx = createContext<(msg: string, tone?: Tone) => void>(() => {});

export const useToast = () => useContext(Ctx);

let _id = 1;

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [items, setItems] = useState<Toast[]>([]);
  const push = useCallback((msg: string, tone: Tone = "info") => {
    const id = _id++;
    setItems((prev) => [...prev.slice(-4), { id, msg, tone }]);
    setTimeout(() => setItems((prev) => prev.filter((t) => t.id !== id)), 4500);
  }, []);
  return (
    <Ctx.Provider value={push}>
      {children}
      <div className="fixed bottom-4 right-4 z-[60] flex w-[min(360px,calc(100vw-2rem))] flex-col gap-2">
        {items.map((t) => (
          <div
            key={t.id}
            className={`kuma-card kuma-fade-in px-4 py-3 text-sm border-l-4 ${
              t.tone === "ok"
                ? "!border-l-emerald-500"
                : t.tone === "err"
                  ? "!border-l-red-500"
                  : "!border-l-sky-500"
            }`}
          >
            {t.msg}
          </div>
        ))}
      </div>
    </Ctx.Provider>
  );
}

export function fmtErr(e: unknown): string {
  const s = String(e instanceof Error ? e.message : e);
  // "500: Internal Server Error" -> человекочитаемо
  if (/^500:/.test(s)) return "Сервер вернул 500 — смотри детали в логах backend (docker compose logs backend worker).";
  if (/^409:/.test(s)) return s.replace(/^409:\s*/, "");
  return s.length > 300 ? s.slice(0, 300) + "…" : s;
}
