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
  Users,
  Settings as Cog,
  Activity,
} from "lucide-react";

const items = [
  { href: "/", label: "Главная", icon: LayoutDashboard },
  { href: "/library", label: "Библиотека", icon: Library },
  { href: "/playlists", label: "Плейлисты", icon: ListMusic },
  { href: "/scans", label: "Задачи и логи", icon: Activity },
  { href: "/users", label: "Пользователи", icon: Users },
  { href: "/settings", label: "Настройки", icon: Cog },
  { href: "/history", label: "История", icon: History },
];

export function Sidebar() {
  // usePathname() может вернуть null на первом клиентском рендере —
  // приводим к строке, чтобы серверный и клиентский HTML совпадали,
  // а подсветка активного пункта включается только после монтирования.
  const pathname = usePathname() ?? "";
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);
  return (
    <aside className="w-[240px] border-r border-border bg-bg sticky top-0 h-screen hidden md:flex md:flex-col">
      <div className="px-6 py-6 flex items-center gap-2">
        <img src="/app-icon.png" alt="KumaFlow" className="w-6 h-6 rounded-full" />
        <div>
          <div className="font-semibold tracking-tight">KumaFlow</div>
          <div className="text-[11px] text-muted">brain · v0.1.0</div>
        </div>
      </div>
      <nav suppressHydrationWarning className="flex-1 px-3 space-y-1">
        {items.map((it) => {
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
      </nav>
      <div className="px-6 py-4 text-[11px] text-muted">made with ♥ для аудиофилов</div>
    </aside>
  );
}
