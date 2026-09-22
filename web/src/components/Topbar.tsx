"use client";

import { Menu, Moon, Sun } from "lucide-react";
import { useEffect, useState } from "react";

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
      <button onClick={toggle} className="kuma-pill shrink-0" aria-label="Переключить тему" suppressHydrationWarning>
        {dark ? <Sun className="w-3 h-3" /> : <Moon className="w-3 h-3" />}
        <span className="hidden sm:inline">{dark ? "Светлая" : "Тёмная"}</span>
      </button>
    </div>
  );
}
