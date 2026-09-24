"use client";

import { useEffect, useState } from "react";
import useSWR from "swr";
import Link from "next/link";
import { Radio, RefreshCw, Shuffle } from "lucide-react";
import { Card } from "@/components/ui";
import TrackCover from "@/components/TrackCover";
import { api } from "@/lib/api";

/** Виджет «Слушает сейчас + паутина next-5». Polling 10с, без вебсокетов (MVP). */
export default function NowPlaying({ userId: propUserId }: { userId?: string }) {
  const { data: users } = useSWR(propUserId ? null : "/api/users", () => api.listUsers());
  const [selUser, setSelUser] = useState("");
  const [offset, setOffset] = useState(0);
  const [seed, setSeed] = useState("");
  useEffect(() => {
    if (propUserId || selUser) return;
    try {
      const saved = localStorage.getItem("nowplaying_user") ?? "";
      if (saved) setSelUser(saved);
    } catch { /* ignore */ }
  }, [propUserId, selUser]);
  const userId = propUserId ?? selUser;
  useEffect(() => {
    if (!propUserId && selUser) {
      try { localStorage.setItem("nowplaying_user", selUser); } catch { /* ignore */ }
    }
  }, [propUserId, selUser]);
  const { data, mutate } = useSWR(
    `/api/now-playing?u=${userId ?? "-"}&o=${offset}&s=${seed}`,
    () => api.nowPlaying(userId, 5, offset, seed || undefined),
    { refreshInterval: 15000, revalidateOnFocus: true },
  );
  const playing = data?.playing ?? null;
  const next = data?.next ?? [];
  const age = playing?.minutes_ago;
  const stale = typeof age === "number" && age >= 5;

  // Трек сменился — ротацию сначала.
  useEffect(() => {
    setOffset(0);
    setSeed("");
  }, [playing?.track_id]);

  if (!data) return null;
  if (!playing) {
    const idleUser = (data as { idle_for_user?: string }).idle_for_user;
    const lastAge = (data as { last_minutes_ago?: number }).last_minutes_ago;
    const wasStale = (data as { stale_dropped?: boolean }).stale_dropped;
    const selName = (users?.users ?? []).find((u) => u.external_id === idleUser)?.username ?? idleUser;
    return (
      <Card>
        <div className="flex items-center gap-2 text-xs mb-3 flex-wrap">
          <span className="text-muted">Слушает сейчас</span>
          <span className="flex-1" />
          {!propUserId && (users?.users?.length ?? 0) > 0 && (
            <select
              className="kuma-input kuma-input-inline !py-1 !px-2 text-xs w-36"
              value={selUser}
              onChange={(e) => setSelUser(e.target.value)}
              title="Чей эфир смотреть"
            >
              <option value="">все</option>
              {(users?.users ?? []).map((u) => (
                <option key={u.id} value={u.id}>{u.username}</option>
              ))}
            </select>
          )}
        </div>
        <div className="text-sm text-muted flex items-center gap-2">
          <Radio className="w-4 h-4" />
          {selName
            ? `${selName} сейчас ничего не слушает.`
            : "Сейчас ничего не играет в Navidrome."}
          {wasStale && typeof lastAge === "number" && (
            <span className="text-xs">(последнее {lastAge} мин назад — похоже, пауза/залипло, скрыто)</span>
          )}
        </div>
      </Card>
    );
  }

  return (
    <Card>
      <div className="flex items-center gap-2 text-xs mb-3 flex-wrap">
        <Link href={"/wave" as never} className="kuma-pill hover:text-text transition-colors">
          <Radio className="w-3 h-3" /> Моя волна →
        </Link>
        <span className="text-muted">открыть живую очередь мозга</span>
        <span className="flex-1" />
        {!propUserId && (users?.users?.length ?? 0) > 0 && (
          <select
            className="kuma-input kuma-input-inline !py-1 !px-2 text-xs w-36"
            value={selUser}
            onChange={(e) => { setSelUser(e.target.value); setOffset(0); }}
            title="Чей вкус учитывать в next-5"
          >
            <option value="">вкус: общий</option>
            {(users?.users ?? []).map((u) => (
              <option key={u.id} value={u.id}>{u.username}</option>
            ))}
          </select>
        )}
        {next.length > 0 && (
          <>
            <button
              className="kuma-pill"
              title="Следующие 5 из того же ранжирования"
              onClick={() => setOffset((o) => o + 5)}
            >
              Другие 5 →
            </button>
            <button
              className="kuma-pill p-1.5"
              title="Перемешать (новый джиттер)"
              onClick={() => { setSeed(Math.random().toString(36).slice(2, 8)); setOffset(0); mutate(); }}
            >
              <Shuffle className="w-3 h-3" />
            </button>
            <button
              className="kuma-pill p-1.5"
              title="Обновить"
              onClick={() => mutate()}
            >
              <RefreshCw className="w-3 h-3" />
            </button>
          </>
        )}
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
                <span className="text-muted truncate" title={`скор ${t.score}`}>· {t.reason} · {t.score?.toFixed(2)}</span>
              </div>
            ))}
            {next.length === 0 && offset > 0 && (
              <button className="kuma-pill text-xs" onClick={() => setOffset(0)}>
                ← К первым 5
              </button>
            )}
          </div>
        </div>
      )}
    </Card>
  );
}
