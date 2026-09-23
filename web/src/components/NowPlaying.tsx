"use client";

import useSWR from "swr";
import Link from "next/link";
import { Radio } from "lucide-react";
import { Card } from "@/components/ui";
import TrackCover from "@/components/TrackCover";
import { api } from "@/lib/api";

/** Виджет «Слушает сейчас + паутина next-5». Polling 10с, без вебсокетов (MVP). */
export default function NowPlaying({ userId }: { userId?: string }) {
  const { data } = useSWR(
    userId ? `/api/now-playing?u=${userId}` : "/api/now-playing",
    () => api.nowPlaying(userId, 5),
    { refreshInterval: 10000, revalidateOnFocus: true },
  );
  const playing = data?.playing ?? null;
  const next = data?.next ?? [];
  const age = playing?.minutes_ago;
  const stale = typeof age === "number" && age >= 5;

  if (!data) return null;
  if (!playing) {
    return (
      <Card>
        <div className="text-sm text-muted flex items-center gap-2">
          <Radio className="w-4 h-4" /> Сейчас ничего не играет в Navidrome.
        </div>
      </Card>
    );
  }

  return (
    <Card>
      <div className="flex items-center gap-2 text-xs mb-3">
        <Link href={"/wave" as never} className="kuma-pill hover:text-text transition-colors">
          <Radio className="w-3 h-3" /> Моя волна →
        </Link>
        <span className="text-muted">открыть живую очередь мозга</span>
      </div>
      <div className="flex items-center gap-4 flex-wrap">
        {playing.track_id ? (
          <TrackCover
            trackId={playing.track_id}
            coverArtId={playing.cover_art_id}
            size={160}
            className="w-14 h-14 rounded-xl"
          />
        ) : (
          <TrackCover trackId="" coverArtId={null} size={160} className="w-14 h-14 rounded-xl" />
        )}
        <div className="min-w-0 flex-1">
          <div className="text-xs uppercase tracking-wider text-muted flex items-center gap-2">
            <span className="relative flex h-2 w-2">
              <span className={`absolute inline-flex h-full w-full rounded-full opacity-75 ${stale ? "" : "animate-ping bg-green-400"}`} />
              <span className={`relative inline-flex rounded-full h-2 w-2 ${stale ? "bg-amber-500" : "bg-green-500"}`} />
            </span>
            Слушает сейчас{playing.username ? ` · ${playing.username}` : ""}
            {typeof age === "number" && age >= 1 && (
              <span className="normal-case tracking-normal">· {age} мин назад{stale ? " (пауза/залипло?)" : ""}</span>
            )}
          </div>
          <div className="mt-1 font-semibold truncate">
            {playing.artist_name} — {playing.title}
          </div>
          {playing.album_name && (
            <div className="text-xs text-muted truncate">{playing.album_name}</div>
          )}
        </div>
      </div>

      {next.length > 0 && (
        <div className="mt-4">
          {/* Паутина: центр — текущий трек, вокруг — 5 следующих. SVG-линии, без зависимостей. */}
          <div className="relative mx-auto max-w-md h-56">
            <svg className="absolute inset-0 w-full h-full" aria-hidden>
              {next.map((_, i) => {
                const a = (2 * Math.PI * i) / next.length - Math.PI / 2;
                const x2 = 50 + 38 * Math.cos(a);
                const y2 = 50 + 42 * Math.sin(a);
                return (
                  <line
                    key={i}
                    x1="50%"
                    y1="50%"
                    x2={`${x2}%`}
                    y2={`${y2}%`}
                    stroke="currentColor"
                    strokeOpacity={0.25}
                    strokeDasharray="3 4"
                  />
                );
              })}
            </svg>
            <div className="absolute left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 w-20 h-20 rounded-full kuma-card flex items-center justify-center text-center p-2 text-[11px] font-medium">
              сейчас
            </div>
            {next.map((t, i) => {
              const a = (2 * Math.PI * i) / next.length - Math.PI / 2;
              const x = 50 + 38 * Math.cos(a);
              const y = 50 + 42 * Math.sin(a);
              return (
                <Link
                  key={t.track_id}
                  href={`/track/${t.track_id}`}
                  title={`${t.artist_name} — ${t.title}\n${t.reason}`}
                  className="absolute w-24 -translate-x-1/2 -translate-y-1/2 kuma-card p-2 text-center hover:border-muted transition flex flex-col items-center gap-1"
                  style={{ left: `${x}%`, top: `${y}%` }}
                >
                  <TrackCover
                    trackId={t.track_id}
                    coverArtId={t.cover_art_id}
                    size={100}
                    className="w-8 h-8 rounded-lg"
                  />
                  <div>
                    <div className="text-[11px] font-medium truncate">{t.title}</div>
                    <div className="text-[10px] text-muted truncate">{t.artist_name}</div>
                  </div>
                </Link>
              );
            })}
          </div>
          {/* Причины — почему алхимия считает похожим */}
          <div className="mt-3 space-y-1.5">
            {next.map((t) => (
              <div key={t.track_id} className="text-xs flex gap-2 items-baseline">
                <Link href={`/track/${t.track_id}`} className="kuma-link font-medium shrink-0">
                  {t.artist_name} — {t.title}
                </Link>
                <span className="text-muted truncate">· {t.reason}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </Card>
  );
}
