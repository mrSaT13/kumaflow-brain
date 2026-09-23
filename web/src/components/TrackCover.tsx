"use client";

import { useEffect, useState } from "react";
import { Music2 } from "lucide-react";
import { api } from "@/lib/api";

// Прозрачный плейсхолдер бэка (~100 байт) грузится с 200 — onError не стреляет.
// Проверяем размер: мелочь = обложки нет → рисуем иконку, а не пустой квадрат.
const MISS_BYTES = 200;

/** Обложка трека: прямая coverArt (своя/альбома) → track-endpoint → иконка. */
export default function TrackCover({
  trackId,
  coverArtId,
  size = 100,
  className = "w-9 h-9 rounded-lg",
}: {
  trackId: string;
  coverArtId?: string | null;
  size?: number;
  className?: string;
}) {
  const [missing, setMissing] = useState(false);
  const hasTarget = Boolean((coverArtId || "").trim() || (trackId || "").trim());
  const src = coverArtId
    ? api.coverUrl(coverArtId, size)
    : api.trackCoverUrl(trackId, size);

  useEffect(() => {
    if (!hasTarget) return;
    let alive = true;
    setMissing(false);
    fetch(src, { cache: "force-cache" })
      .then(async (r) => {
        if (!r.ok) {
          if (alive) setMissing(true);
          return;
        }
        try {
          const b = await r.blob();
          if (alive && b.size < MISS_BYTES) setMissing(true);
        } catch {
          /* ignore — покажем как есть */
        }
      })
      .catch(() => {
        if (alive) setMissing(true);
      });
    return () => {
      alive = false;
    };
  }, [src, hasTarget]);

  if (!hasTarget || missing) {
    return (
      <div className={`${className} bg-border/50 flex items-center justify-center shrink-0`} aria-hidden>
        <Music2 className="w-4 h-4 text-muted" />
      </div>
    );
  }
  // eslint-disable-next-line @next/next/no-img-element
  return (
    <img
      src={src}
      alt=""
      loading="lazy"
      onError={() => setMissing(true)}
      className={`${className} object-cover border border-border shrink-0`}
    />
  );
}
