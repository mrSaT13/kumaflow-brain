"use client";

import { Bell, Menu, Moon, Sun } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";
import useSWR from "swr";
import { api } from "@/lib/api";

const KIND_DOT: Record<string, string> = {
  success: "bg-green-500",
  warn: "bg-amber-500",
  error: "bg-red-500",
  info: "bg-sky-500",
};

function BellBox() {
  const [open, setOpen] = useState(false);
  const { data: count, mutate: mutateCount } = useSWR("/api/notifications/unread-count?all", () => api.unreadCount(true), { refreshInterval: 15000 });
  const { data: list, error: listError, isLoading: listLoading, mutate: mutateList } = useSWR(open ? "/api/notifications?all" : null, () => api.notifications(30, true));
  const unread = count?.unread ?? 0;
  const items = list?.notifications ?? [];

  async function openBox() {
    setOpen((v) => !v);
  }

  async function readAll() {
    await api.markAllNotificationsRead(true);
    mutateCount();
    mutateList();
  }

  async function readOne(id: string) {
    await api.markNotificationRead(id);
    mutateCount();
    mutateList();
  }

  // Открыли колокол — обновили счётчик (прочитанным помечаем явно по кнопке/клику)
  useEffect(() => {
    if (open) mutateCount();
  }, [open, mutateCount]);

  return (
    <div className="relative shrink-0">
      <button onClick={openBox} className="kuma-pill relative" aria-label="Уведомления">
        <Bell className="w-3 h-3" />
        {unread > 0 && (
          <span className="absolute -top-1.5 -right-1.5 min-w-4 h-4 px-1 rounded-full bg-red-500 text-white text-[10px] font-bold flex items-center justify-center tabular-nums">
            {unread > 99 ? "99+" : unread}
          </span>
        )}
      </button>
      {open && (
        <>
          <div className="fixed inset-0 z-20" onClick={() => setOpen(false)} />
          <div className="absolute right-0 mt-2 w-80 max-w-[90vw] kuma-card p-2 z-30 max-h-96 overflow-auto">
            <div className="flex items-center justify-between px-2 py-1">
              <span className="text-sm font-medium">Уведомления</span>
              <button className="kuma-pill text-xs" onClick={readAll}>Прочитать все</button>
            </div>
            {(listLoading || (!list && !listError)) && (
              <div className="text-xs text-muted px-2 py-4 text-center">Загрузка…</div>
            )}
            {!listLoading && listError && (
              <div className="text-xs text-red-500 px-2 py-4 text-center">Не смог загрузить: {String(listError instanceof Error ? listError.message : listError)}</div>
            )}
            {!listLoading && !listError && items.length === 0 && (
              <div className="text-xs text-muted px-2 py-4 text-center">Пока тихо — итоги ночных задач появятся здесь.</div>
            )}
            {items.map((n) => (
              <div
                key={n.id}
                onClick={() => { if (!n.read_at) readOne(n.id); }}
                className={`px-2 py-2 rounded-lg text-sm flex gap-2 items-start ${n.read_at ? "opacity-60" : "cursor-pointer hover:bg-surface"}`}
              >
                <span className={`mt-1.5 w-2 h-2 rounded-full shrink-0 ${KIND_DOT[n.kind] ?? KIND_DOT.info}`} />
                <div className="min-w-0">
                  <div className="font-medium leading-tight">{n.title}</div>
                  {n.body && <div className="text-xs text-muted leading-snug mt-0.5">{n.body}</div>}
                  <div className="flex items-center gap-2 mt-1">
                    {n.created_at && <span className="text-[10px] text-muted">{new Date(n.created_at).toLocaleString("ru-RU")}</span>}
                    {n.link && (
                      <Link href={n.link as never} className="kuma-link text-[11px]" onClick={() => setOpen(false)}>
                        Открыть →
                      </Link>
                    )}
                  </div>
                </div>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}

export function Topbar({ onMenu }: { onMenu?: () => void } = {}) {
  const [dark, setDark] = useState(false);
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    setMounted(true);
    setDark(document.documentElement.classList.contains("dark"));
  }, []);

  function toggle() {
    const next = !dark;
    setDark(next);
    document.documentElement.classList.toggle("dark", next);
    try {
      localStorage.setItem("theme", next ? "dark" : "light");
    } catch {
      /* приватный режим — тема просто не сохранится */
    }
  }

  if (!mounted) {
    return (
      <div className="h-14 border-b border-border bg-bg/80 backdrop-blur sticky top-0 z-10 flex items-center justify-between px-4 md:px-8 gap-2">
        <div className="flex items-center gap-2 min-w-0">
          <button onClick={onMenu} className="md:hidden kuma-pill p-2" aria-label="Меню">
            <Menu className="w-4 h-4" />
          </button>
          <div className="text-sm text-muted truncate">Музыкальная аналитика</div>
        </div>
        <span className="kuma-pill opacity-0 shrink-0" aria-hidden>
          Тёмная
        </span>
      </div>
    );
  }
  return (
    <div className="h-14 border-b border-border bg-bg/80 backdrop-blur sticky top-0 z-10 flex items-center justify-between px-4 md:px-8 gap-2">
      <div className="flex items-center gap-2 min-w-0">
        <button onClick={onMenu} className="md:hidden kuma-pill p-2 shrink-0" aria-label="Меню">
          <Menu className="w-4 h-4" />
        </button>
        <div className="text-sm text-muted truncate hidden sm:block">Музыкальная аналитика и рекомендации для вашей библиотеки</div>
        <div className="text-sm text-muted truncate sm:hidden">KumaFlow Brain</div>
      </div>
      <div className="flex items-center gap-2 shrink-0">
        <BellBox />
        <button onClick={toggle} className="kuma-pill shrink-0" aria-label="Переключить тему" suppressHydrationWarning>
          {dark ? <Sun className="w-3 h-3" /> : <Moon className="w-3 h-3" />}
          <span className="hidden sm:inline">{dark ? "Светлая" : "Тёмная"}</span>
        </button>
      </div>
    </div>
  );
}