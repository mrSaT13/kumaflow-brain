"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { ListMusic, Play, Loader2 } from "lucide-react";
import { Button, Card } from "@/components/ui";
import TrackCover from "@/components/TrackCover";
import { useToast, fmtErr } from "@/components/toasts";
import { api } from "@/lib/api";

type WaveTrack = {
  track_id: string; title: string; artist_name?: string; score: number; reason: string;
  cover_art_id?: string | null;
};

/** Пробник «живой волны клиента»: очередь живёт тут, мозг докладывает next-N. */
export default function LiveWave({ userId }: { userId: string }) {
  const [queue, setQueue] = useState<WaveTrack[]>([]);
  const [seeds, setSeeds] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [playingIdx, setPlayingIdx] = useState(0);
  const toast = useToast();
  const listRef = useRef<HTMLDivElement>(null);
  const itemRefs = useRef<Map<number, HTMLDivElement>>(new Map());
  const pendingEvents = useRef<{ track_id: string; action: string; position_sec?: number }[]>([]);

  // автопрокрутка к играющему — плавно, по центру ленты
  useEffect(() => {
    const el = itemRefs.current.get(playingIdx);
    if (el) {
      el.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
  }, [playingIdx, queue.length]);

  async function more() {
    setBusy(true);
    try {
      const events = pendingEvents.current;
      pendingEvents.current = [];
      const r = await api.waveContinue({
        user_id: userId,
        queue: queue.map((t) => t.track_id),
        current_track_id: queue.length > 0 ? queue[queue.length - 1].track_id : undefined,
        count: 10,
        recent_events: events,
      });
      setSeeds(r.seeds ?? []);
      setQueue((q) => [...q, ...(r.tracks ?? [])].slice(0, 50));
    } catch (e: unknown) {
      toast(fmtErr(e), "err");
    } finally {
      setBusy(false);
    }
  }

  function playAt(i: number) {
    const prev = queue[Math.min(playingIdx, queue.length - 1)];
    if (prev && i !== playingIdx) {
      pendingEvents.current.push({ track_id: prev.track_id, action: "skip", position_sec: 15 });
    }
    const nxt = queue[i];
    if (nxt && i !== playingIdx) {
      pendingEvents.current.push({ track_id: nxt.track_id, action: "play" });
    }
    setPlayingIdx(i);
  }

  // Авто-докрутка хвоста — как в плеере «Моей волны».
  useEffect(() => {
    if (queue.length === 0 || busy) return;
    if (queue.length - 1 - playingIdx <= 3) void more();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [playingIdx, queue.length]);

  function reset() {
    setQueue([]);
    setSeeds([]);
    setPlayingIdx(0);
    pendingEvents.current = [];
  }

  return (
    <Card>
      <div className="flex items-center gap-2 flex-wrap text-sm">
        <span className="text-muted flex items-center gap-2">
          {queue.length > 0 && (
            <span className="kuma-eq" aria-hidden>
              <span /><span /><span />
            </span>
          )}
          {queue.length === 0
            ? "Пусто — нажми «Запустить», мозг подберёт первые 10 от сидов вкуса."
            : `Играет ${Math.min(playingIdx + 1, queue.length)} из ${queue.length}`}
        </span>
        <span className="flex-1" />
        <Button onClick={more} disabled={busy}>
          {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <Play className="w-4 h-4" />}
          {queue.length === 0 ? "Запустить волну" : "Докрутить +10"}
        </Button>
        {queue.length > 0 && (
          <Button variant="ghost" onClick={reset} disabled={busy}>
            Сбросить
          </Button>
        )}
      </div>

      {busy && queue.length === 0 && (
        <div className="mt-4 space-y-2" aria-hidden>
          {[0, 1, 2].map((i) => (
            <div key={i} className="kuma-skeleton h-12" style={{ opacity: 1 - i * 0.25 }} />
          ))}
        </div>
      )}

      {queue.length > 0 && (
        <div ref={listRef} className="mt-4 relative pl-6 max-h-[420px] overflow-y-auto pr-1">
          {/* вертикальная лента как очередь в плеере */}
          <div className="absolute left-[9px] top-2 bottom-2 w-px bg-border" aria-hidden />
          <div className="space-y-2">
            {queue.map((t, i) => {
              const active = i === playingIdx;
              return (
                <div
                  key={`${t.track_id}-${i}`}
                  ref={(el) => {
                    if (el) itemRefs.current.set(i, el);
                    else itemRefs.current.delete(i);
                  }}
                  onClick={() => playAt(i)}
                  className={`kuma-wave-item relative flex items-center gap-3 text-sm rounded-xl px-2 py-1.5 -ml-2 cursor-pointer transition-all hover:bg-border/40 ${active ? "bg-border/50 shadow-sm" : ""}`}
                  style={{ animationDelay: `${Math.min(i, 12) * 45}ms` }}
                >
                  <span
                    className="absolute -left-4 w-[19px] h-[19px] rounded-full text-[10px] flex items-center justify-center border border-border bg-bg tabular-nums"
                    title={`позиция ${i + 1} · скор ${t.score}`}
                  >
                    {i + 1}
                  </span>
                  <TrackCover
                    trackId={t.track_id}
                    coverArtId={t.cover_art_id}
                    size={100}
                    className={`w-9 h-9 rounded-lg transition-transform ${active ? "scale-105 border-muted" : "border-border"}`}
                  />
                  <div className="min-w-0 flex-1">
                    <Link
                      href={`/track/${t.track_id}`}
                      onClick={(e) => e.stopPropagation()}
                      className="kuma-link truncate block"
                    >
                      {t.artist_name ? `${t.artist_name} — ` : ""}{t.title}
                    </Link>
                    <div className="text-[11px] text-muted truncate" title={t.reason}>
                      {t.reason || "—"}
                    </div>
                  </div>
                  {active && (
                    <span className="kuma-eq shrink-0" aria-label="играет">
                      <span /><span /><span />
                    </span>
                  )}
                  <span className="text-[11px] text-muted tabular-nums shrink-0">{t.score}</span>
                </div>
              );
            })}
          </div>
        </div>
      )}

      <div className="mt-3 text-[11px] text-muted flex items-center gap-1.5">
        <ListMusic className="w-3 h-3" />
        Это тот же `POST /api/wave/continue`, что дёргает мобильный клиент. Клик по строке — «сейчас играет» (шлём play/skip), хвост докручивается сам.
      </div>
    </Card>
  );
}
