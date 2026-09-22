"use client";
import { useState } from "react";
import { Sidebar } from "@/components/Sidebar";
import { Topbar } from "@/components/Topbar";

export function MobileShell({ children }: { children: React.ReactNode }) {
  const [open, setOpen] = useState(false);
  return (
    <div suppressHydrationWarning className="min-h-screen flex">
      <Sidebar open={open} onClose={() => setOpen(false)} />
      <main className="flex-1 min-w-0">
        <Topbar onMenu={() => setOpen((v) => !v)} />
        <div className="px-4 md:px-8 py-4 md:py-6 max-w-[1400px] mx-auto">{children}</div>
      </main>
    </div>
  );
}
