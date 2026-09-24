"use client";
import { useEffect, useState } from "react";
import { Sidebar } from "@/components/Sidebar";
import { Topbar } from "@/components/Topbar";
import PwaInstall from "@/components/PwaInstall";
import { initServerOffset } from "@/lib/serverTime";

export function MobileShell({ children }: { children: React.ReactNode }) {
  const [open, setOpen] = useState(false);
  // Сдвиг часового пояса сервера (APP_TIMEZONE) — чтобы fmtDate/fmtTime
  // показывали локальное время сразу правильно; по готовности перерендер.
  const [, setTzReady] = useState(false);
  useEffect(() => {
    initServerOffset().then(() => setTzReady(true)).catch(() => {});
  }, []);
  return (
    <div suppressHydrationWarning className="min-h-screen flex">
      <Sidebar open={open} onClose={() => setOpen(false)} />
      <main className="flex-1 min-w-0">
        <Topbar onMenu={() => setOpen((v) => !v)} />
        <div className="px-4 md:px-8 py-4 md:py-6 max-w-[1400px] mx-auto">
          <PwaInstall variant="banner" />
          {children}
        </div>
      </main>
    </div>
  );
}
