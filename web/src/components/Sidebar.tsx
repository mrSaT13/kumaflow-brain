"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import clsx from "clsx";
import useSWR from "swr";
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
  Radio,
  ChevronsLeft,
  ChevronsRight,
  LogOut,
} from "lucide-react";
import { setBrowserToken } from "@/lib/api";
import { api } from "@/lib/api";

type NavItem = { href: string; label: string; icon: typeof LayoutDashboard };

const sections: { title: string; items: NavItem[] }[] = [
  { title: "Библиотека", items: [
    { href: "/", label: "Главная", icon: LayoutDashboard },
    { href: "/library", label: "Библиотека", icon: Library },
    { href: "/playlists", label: "Плейлисты", icon: ListMusic },
    { href: "/wave", label: "Моя волна", icon: Radio },
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
  const [collapsed, setCollapsed] = useState(false);
  // Версия — живая с backend (/api/version, без авторизации), а не хардкод:
  // иначе после bump в config.py сайдбар врёт, пока не поправят строку.
  const { data: ver } = useSWR("/api/version", () => api.version().catch(() => null), {
    refreshInterval: 60000,
    shouldRetryOnError: false,
  });
  const verLabel = `brain${ver?.version ? ` · v${ver.version}` : ""}`;
  useEffect(() => {
    setMounted(true);
    try {
      setCollapsed(localStorage.getItem("sidebar-collapsed") === "1");
    } catch { /* ignore */ }
  }, []);
  function toggleCollapsed() {
    setCollapsed((v) => {
      const next = !v;
      try {
        localStorage.setItem("sidebar-collapsed", next ? "1" : "0");
      } catch { /* ignore */ }
      return next;
    });
  }
  // десктопный сайдбар + мобильный дровер
  return (
    <>
      {/* desktop */}
      <aside
        className={clsx(
          "border-r border-border bg-bg sticky top-0 h-screen hidden md:flex md:flex-col shrink-0 transition-[width] duration-200",
          collapsed ? "w-[68px]" : "w-[240px]",
        )}
      >
      <div className={clsx("py-6 flex items-center gap-2", collapsed ? "px-0 justify-center" : "px-6")}>
        <img src="/app-icon.png" alt="KumaFlow" className="w-6 h-6 rounded-full shrink-0" />
        {!collapsed && (
          <div className="min-w-0">
            <div className="font-semibold tracking-tight">KumaFlow</div>
            <div className="text-[11px] text-muted">{verLabel}</div>
          </div>
        )}
        {!collapsed && (
          <button
            onClick={toggleCollapsed}
            className="ml-auto kuma-pill !px-2 py-1"
            title="Свернуть сайдбар"
            aria-label="Свернуть сайдбар"
          >
            <ChevronsLeft className="w-3.5 h-3.5" />
          </button>
        )}
      </div>
      {collapsed && (
        <button
          onClick={toggleCollapsed}
          className="mx-auto mb-2 kuma-pill !px-2 py-1"
          title="Развернуть сайдбар"
          aria-label="Развернуть сайдбар"
        >
          <ChevronsRight className="w-3.5 h-3.5" />
        </button>
      )}
      <nav suppressHydrationWarning className={clsx("flex-1 space-y-3 overflow-y-auto overflow-x-hidden", collapsed ? "px-2" : "px-3")}>
        {sections.map((sec) => (
          <div key={sec.title}>
            {!collapsed ? (
              <div className="px-3 py-1 text-[10px] uppercase tracking-wider text-muted">{sec.title}</div>
            ) : (
              <div className="mx-3 my-1 h-px bg-border" aria-hidden />
            )}
            <div className="space-y-1">
              {sec.items.map((it) => {
                const active = mounted ? pathname === it.href || (it.href !== "/" && pathname.startsWith(it.href)) : false;
                const Icon = it.icon;
                return (
                  <Link
                    key={it.href}
                    href={it.href as any}
                    title={collapsed ? it.label : undefined}
                    className={clsx(
                      "flex items-center gap-3 px-3 py-2 rounded-lg text-sm transition-colors",
                      collapsed && "justify-center px-0",
                      active ? "bg-surface text-text shadow-sm ring-1 ring-border" : "text-muted hover:text-text hover:bg-surface/60",
                    )}
                  >
                    <Icon className="w-4 h-4 shrink-0" />
                    {!collapsed && <span className="truncate">{it.label}</span>}
                  </Link>
                );
              })}
            </div>
          </div>
        ))}
      </nav>
      {!collapsed && <div className="px-6 py-4 text-[11px] text-muted">made with ♥ для аудиофилов</div>}
      <div className={clsx("pb-4", collapsed ? "px-2" : "px-3")}>
        <button
          onClick={() => { setBrowserToken(""); window.location.reload(); }}
          title={collapsed ? "Выйти (убрать токен браузера)" : undefined}
          className={clsx(
            "flex items-center gap-3 px-3 py-2 rounded-lg text-sm transition-colors w-full",
            collapsed && "justify-center px-0",
            "text-muted hover:text-text hover:bg-surface/60",
          )}
        >
          <LogOut className="w-4 h-4 shrink-0" />
          {!collapsed && <span className="truncate">Выйти</span>}
        </button>
      </div>
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
                <div className="text-[11px] text-muted">{verLabel}</div>
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
            <div className="px-3 pb-4">
              <button
                onClick={() => { setBrowserToken(""); window.location.reload(); onClose?.(); }}
                className="flex items-center gap-3 px-3 py-3 rounded-lg text-[15px] transition-colors w-full text-muted hover:text-text hover:bg-surface/60"
              >
                <LogOut className="w-5 h-5" />
                Выйти
              </button>
            </div>
          </aside>
        </div>
      )}
    </>
  );
}
