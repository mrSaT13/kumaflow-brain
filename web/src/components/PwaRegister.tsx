"use client";

import { useEffect } from "react";

/** Регистрирует /sw.js — без него Chrome/Android не покажет Install. */
export default function PwaRegister() {
  useEffect(() => {
    if (typeof window === "undefined") return;
    if (!("serviceWorker" in navigator)) return;
    // На dev (http) тоже работает, на проде нужен HTTPS.
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  }, []);
  return null;
}
