"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import clsx from "clsx";
import { useEffect, useState } from "react";
import {
  LayoutDashboard,
  Library,
  History,
  ListMusic,
  Sparkles,
  Users,
  Settings as Cog,
  Activity,
} from "lucide-react";

type NavItem = { href: string; label: string; icon: typeof LayoutDashboard };

const sections: { title: string; items: NavItem[] }[] = [
  { title: "Библиотека", items: [
    { href: "/", label: "Главная", icon: LayoutDashboard },
    { href: "/library", label: "Библиотека", icon: Library },
    { href: "/playlists", label: "Плейлисты", icon: ListMusic },
    { href: "/cold-start", label: "Холодный старт", icon: Sparkles },
  ]},
  { title: "Задачи", items: [
    { href: "/scans", label: "Задачи и логи", icon: Activity },
    { href: "/history", label: "История", icon: History },
  ]},
  { title: "Управление", items: [
    { href: "/users", label: "Пользователи", icon: Users },
    { href: "/settings", label: "Настройки", icon: Cog },
  ]},
];
const items: NavItem[] = sections.flatMap(s => s.items);

export function Sidebar({ open, onClose }: { open?: boolean; onClose?: () => void } = {}) {
  // usePathname() может вернуть null на первом клиентском рендере —
  // приводим к строке, чтобы серверный и клиентский HTML совпадали,
  // а подсветка активного пункта включается только после монтирования.
  const pathname = usePathname() ?? "";
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);
  // десктопный сайдбар + мобильный дровер
  return (
    <>
      {/* desktop */}
      <aside className="w-[240px] border-r border-border bg-bg sticky top-0 h-screen hidden md:flex md:flex-col">
      <div className="px-6 py-6 flex items-center gap-2">
        <img src="/app-icon.png" alt="KumaFlow" className="w-6 h-6 rounded-full" />
        <div>
          <div className="font-semibold tracking-tight">KumaFlow</div>
          <div className="text-[11px] text-muted">brain · v0.1.0</div>
        </div>
      </div>
      <nav suppressHydrationWarning className="flex-1 px-3 space-y-3 overflow-y-auto">
        {sections.map((sec) => (
          <div key={sec.title}>
            <div className="px-3 py-1 text-[10px] uppercase tracking-wider text-muted">{sec.title}</div>
            <div className="space-y-1">
              {sec.items.map((it) => {
                const active = mounted ? pathname === it.href || (it.href !== "/" && pathname.startsWith(it.href)) : false;
                const Icon = it.icon;
                return (
                  <Link
                    key={it.href}
                    href={it.href as any}
                    className={clsx(
                      "flex items-center gap-3 px-3 py-2 rounded-lg text-sm transition-colors",
                      active ? "bg-surface text-text" : "text-muted hover:text-text hover:bg-surface/60",
                    )}
                  >
                    <Icon className="w-4 h-4" />
                    {it.label}
                  </Link>
                );
              })}
            </div>
          </div>
        ))}
      </nav>
      <div className="px-6 py-4 text-[11px] text-muted">made with ♥ для аудиофилов</div>
    </aside>
      {/* mobile drawer */}
      {open && (
        <div className="fixed inset-0 z-40 md:hidden">
          <div className="absolute inset-0 bg-black/40" onClick={onClose} />
          <aside className="relative w-[280px] max-w-[85vw] h-full bg-bg border-r border-border flex flex-col">
            <div className="px-6 py-6 flex items-center gap-2 border-b border-border">
              <img src="/app-icon.png" alt="KumaFlow" className="w-6 h-6 rounded-full" />
              <div>
                <div className="font-semibold tracking-tight">KumaFlow</div>
                <div className="text-[11px] text-muted">brain · v0.1.0</div>
              </div>
              <button onClick={onClose} className="ml-auto kuma-pill text-xs">✕</button>
            </div>
            <nav className="flex-1 px-3 py-4 space-y-4 overflow-y-auto">
              {sections.map((sec) => (
                <div key={sec.title}>
                  <div className="px-3 py-1 text-[10px] uppercase tracking-wider text-muted">{sec.title}</div>
                  <div className="space-y-1">
                    {sec.items.map((it) => {
                      const active = mounted ? pathname === it.href || (it.href !== "/" && pathname.startsWith(it.href)) : false;
                      const Icon = it.icon;
                      return (
                        <Link
                          key={it.href}
                          href={it.href as any}
                          onClick={onClose}
                          className={clsx(
                            "flex items-center gap-3 px-3 py-3 rounded-lg text-[15px] transition-colors",
                            active ? "bg-surface text-text" : "text-muted hover:text-text hover:bg-surface/60",
                          )}
                        >
                          <Icon className="w-5 h-5" />
                          {it.label}
                        </Link>
                      );
                    })}
                  </div>
                </div>
              ))}
            </nav>
          </aside>
        </div>
      )}
    </>
  );
}
