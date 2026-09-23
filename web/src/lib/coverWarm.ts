"use client";

import { api } from "@/lib/api";

// Умный прогрев обложек: очередь с конкурентностью 8, чтобы не долбить Navidrome 50 запросами сразу.
// Использовать при открытии плейлиста/библиотеки: warmCovers(trackIds.slice(0,40)).
export async function warmCovers(trackIds: string[], size = 100, concurrency = 8): Promise<number> {
  let ok = 0;
  let i = 0;
  async function worker() {
    while (i < trackIds.length) {
      const id = trackIds[i++];
      try {
        const img = new Image();
        img.decoding = "async";
        img.src = api.trackCoverUrl(id, size);
        await (img.decode?.().catch(() => undefined) ?? undefined);
        ok++;
      } catch {
        /* тихо */
      }
    }
  }
  await Promise.all(Array.from({ length: Math.min(concurrency, trackIds.length) }, () => worker()));
  return ok;
}
