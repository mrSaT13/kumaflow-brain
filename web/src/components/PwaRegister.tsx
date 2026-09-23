"use client";

import { useEffect } from "react";

/** Регистрирует /sw.js — без него Chrome/Android не покажет Install.
 *  На http://IP (не secure context) SW невозможен — пропускаем молча,
 *  причину показывает PwaInstall. */
export default function PwaRegister() {
  useEffect(() => {
    if (typeof window === "undefined") return;
    if (!("serviceWorker" in navigator)) return;
    if (!window.isSecureContext) {
      console.info("[pwa] skip SW: нужен HTTPS (http://IP — insecure context)");
      return;
    }
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  }, []);
  return null;
}
