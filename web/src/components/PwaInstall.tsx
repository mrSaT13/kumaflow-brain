"use client";

import { useEffect, useState } from "react";
import { Download, X } from "lucide-react";

type BIPEvent = Event & {
  prompt: () => Promise<void>;
  userChoice: Promise<{ outcome: string }>;
};

function isIos(): boolean {
  if (typeof navigator === "undefined") return false;
  return /iphone|ipad|ipod/i.test(navigator.userAgent);
}

function isStandalone(): boolean {
  if (typeof window === "undefined") return false;
  const mql = window.matchMedia?.("(display-mode: standalone)").matches;
  // iOS Safari
  const iosStandalone =
    typeof navigator !== "undefined" &&
    "standalone" in navigator &&
    (navigator as unknown as { standalone?: boolean }).standalone === true;
  return Boolean(mql || iosStandalone);
}

/** Кнопка/баннер «Установить как приложение». Chrome/Android — native prompt, iOS — инструкция. */
export default function PwaInstall({ variant = "button" }: { variant?: "button" | "banner" }) {
  const [deferred, setDeferred] = useState<BIPEvent | null>(null);
  const [dismissed, setDismissed] = useState(false);
  const [installed, setInstalled] = useState(false);
  const [ios, setIos] = useState(false);

  useEffect(() => {
    setIos(isIos());
    if (isStandalone()) setInstalled(true);
    const onBip = (e: Event) => {
      e.preventDefault();
      setDeferred(e as BIPEvent);
    };
    const onInstalled = () => {
      setInstalled(true);
      setDeferred(null);
    };
    window.addEventListener("beforeinstallprompt", onBip);
    window.addEventListener("appinstalled", onInstalled);
    return () => {
      window.removeEventListener("beforeinstallprompt", onBip);
      window.removeEventListener("appinstalled", onInstalled);
    };
  }, []);

  useEffect(() => {
    try {
      if (localStorage.getItem("pwa-dismissed") === "1") setDismissed(true);
    } catch {
      /* ignore */
    }
  }, []);

  if (installed || dismissed) return null;

  function dismiss() {
    setDismissed(true);
    try {
      localStorage.setItem("pwa-dismissed", "1");
    } catch {
      /* ignore */
    }
  }

  async function install() {
    if (deferred) {
      await deferred.prompt();
      await deferred.userChoice.catch(() => {});
      setDeferred(null);
      return;
    }
    // Нет native prompt (iOS / уже в standalone) — показываем инструкцию ниже.
    setIos(true);
  }

  // iOS без prompt: только инструкция, кнопку показа не прячем.
  if (!deferred && !ios) {
    // Десктоп/андроид до beforeinstallprompt — ничего не показываем, чтобы не шуметь.
    return null;
  }

  if (variant === "banner") {
    return (
      <div className="mb-4 flex items-start gap-3 rounded-xl border border-border bg-surface p-3 text-sm">
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img src="/icon-192.png" alt="KumaFlow" className="h-10 w-10 rounded-xl shrink-0" />
        <div className="min-w-0 flex-1">
          <div className="font-medium">Установить KumaFlow как приложение</div>
          <div className="text-xs text-muted mt-0.5">
            {deferred
              ? "Быстрый запуск с экрана телефона, отдельный плеер волны."
              : "На iPhone: Поделиться → «На экран Домой»."}
          </div>
          {deferred && (
            <button onClick={install} className="kuma-btn mt-2 !py-1.5 !px-3 text-xs">
              <Download className="h-3.5 w-3.5" /> Установить
            </button>
          )}
        </div>
        <button onClick={dismiss} className="kuma-pill p-1.5" aria-label="Закрыть">
          <X className="h-3.5 w-3.5" />
        </button>
      </div>
    );
  }

  return (
    <button
      onClick={install}
      className="kuma-pill shrink-0"
      title={deferred ? "Установить как приложение (PWA)" : "Как установить на iPhone: Поделиться → На экран Домой"}
    >
      <Download className="h-3 w-3" />
      <span className="hidden sm:inline">Установить</span>
    </button>
  );
}
