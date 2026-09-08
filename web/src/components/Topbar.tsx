"use client";

import { Moon, Sun } from "lucide-react";
import { useEffect, useState } from "react";

export function Topbar() {
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
    // одинаковый HTML на сервере и клиенте до гидратации
    return (
      <div className="h-14 border-b border-border bg-bg/80 backdrop-blur sticky top-0 z-10 flex items-center justify-between px-8">
        <div className="text-sm text-muted">Самохостинг ИИ для вашей музыкальной библиотеки</div>
        <span className="kuma-pill opacity-0" aria-hidden>
          Тёмная
        </span>
      </div>
    );
  }
  return (
    <div className="h-14 border-b border-border bg-bg/80 backdrop-blur sticky top-0 z-10 flex items-center justify-between px-8">
      <div className="text-sm text-muted">
        Самохостинг ИИ для вашей музыкальной библиотеки
      </div>
      <button onClick={toggle} className="kuma-pill" aria-label="Переключить тему" suppressHydrationWarning>
        {dark ? <Sun className="w-3 h-3" /> : <Moon className="w-3 h-3" />}
        {dark ? "Светлая" : "Тёмная"}
      </button>
    </div>
  );
}
