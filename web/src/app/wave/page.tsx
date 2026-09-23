"use client";

import Link from "next/link";
import useSWR from "swr";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ArrowRight, Loader2, Play } from "lucide-react";
import { Button, Card, PageHeader, Section } from "@/components/ui";
import { useToast, fmtErr } from "@/components/toasts";
import { api } from "@/lib/api";
import TrackCover from "@/components/TrackCover";
import { moodLook } from "@/lib/moodStyle";

type WaveComp = {
  audio?: number; genre?: number; artist?: number;
  behavior?: number; collab?: number; novelty?: number;
};

type WaveTrack = {
  track_id: string; title: string; artist_name?: string; score: number; reason: string;
  mood?: string | null; moods?: string[]; energy?: number | null; tempo?: number | null;
  cover_art_id?: string | null; comp?: WaveComp | null;
};

type WaveEvent = { track_id: string; action: string; position_sec?: number };

const REFILL_THRESHOLD = 3;

const COMP_LABELS: { key: keyof WaveComp; label: string }[] = [
  { key: "audio", label: "аудио" },
  { key: "genre", label: "жанр" },
  { key: "artist", label: "артист" },
  { key: "behavior", label: "поведение" },
  { key: "collab", label: "коллаб" },
  { key: "novelty", label: "новизна" },
];

export default function WavePage() {
  const { data: users } = useSWR("/api/users", () => api.listUsers());
  const [userId, setUserId] = useState("");
  const [mood, setMood] = useState("");
  const [queue, setQueue] = useState<WaveTrack[]>([]);
  const [playingIdx, setPlayingIdx] = useState(0);
  const [busy, setBusy] = useState(false);
  const [liveRefill, setLiveRefill] = useState(true);
  const [followNavidrome, setFollowNavidrome] = useState(true);
  const toast = useToast();
  const itemRefs = useRef<Map<number, HTMLDivElement>>(new Map());
  const pendingEvents = useRef<WaveEvent[]>([]);
  const lastSyncedExternal = useRef<string>("");
  const busyRef = useRef(false);
  busyRef.current = busy;

  const ids = useMemo(() => users?.users ?? [], [users]);
  useEffect(() => {
    if (!userId && ids.length > 0) setUserId(ids[0].id);
  }, [ids, userId]);

  const { data: profile } = useSWR(userId ? ["wave-moods", userId] : null, () => api.userProfile(userId));
  const moodOptions = useMemo(() => (profile?.moods ?? []).map((m) => m.name), [profile]);

  const cur = queue[Math.min(playingIdx, queue.length - 1)] ?? null;
  const nxt = queue[Math.min(playingIdx + 1, queue.length - 1)] ?? null;

  useEffect(() => {
    const el = itemRefs.current.get(playingIdx);
    if (el) el.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [playingIdx, queue.length]);

  function drainEvents(): WaveEvent[] {
    const out = pendingEvents.current;
    pendingEvents.current = [];
    return out;
  }

  const more = useCallback(async (reset = false) => {
    if (!userId) {
      toast("Выберите пользователя", "err");
      return;
    }
    if (busyRef.current) return;
    setBusy(true);
    try {
      const q = reset ? [] : queue.map((t) => t.track_id);
      const events = drainEvents().slice(-50);
      const r = await api.waveContinue({
        user_id: userId,
        queue: q,
        current_track_id: q.length > 0 ? q[q.length - 1] : undefined,
        count: 10,
        settings: mood ? { mood } : {},
        recent_events: events,
      });
      const tracks = (r.tracks ?? []) as WaveTrack[];
      setQueue((prev) => (reset ? tracks : [...prev, ...tracks]).slice(0, 100));
      if (reset) {
        setPlayingIdx(0);
        lastSyncedExternal.current = "";
      }
    } catch (e: unknown) {
      toast(fmtErr(e), "err");
    } finally {
      setBusy(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [userId, queue, mood, toast]);

  // Клик по строке = «это сейчас играет во внешнем плеере»: помечаем и копим play-событие.
  function markCurrent(i: number) {
    if (queue.length === 0) return;
    const clamped = Math.max(0, Math.min(i, queue.length - 1));
    const t = queue[clamped];
    if (t && clamped !== playingIdx) {
      pendingEvents.current.push({ track_id: t.track_id, action: "play" });
    }
    setPlayingIdx(clamped);
  }

  // Авто-докрутка хвоста: осталось мало — добрать +10 с накопленными событиями.
  useEffect(() => {
    if (!liveRefill || queue.length === 0 || busy) return;
    if (queue.length - 1 - playingIdx <= REFILL_THRESHOLD) void more(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [playingIdx, queue.length, liveRefill]);

  // Реал-тайм от внешнего плеера: что играет в Navidrome — подсвечиваем как current.
  useEffect(() => {
    if (!followNavidrome || !userId || queue.length === 0) return;
    let stop = false;
    const tick = async () => {
      try {
        const np = await api.nowPlaying(userId, 1);
        const extId = np.playing?.track_id;
        if (stop || !extId || extId === lastSyncedExternal.current) return;
        const idx = queue.findIndex((t) => t.track_id === extId);
        if (idx >= 0) {
          lastSyncedExternal.current = extId;
          if (idx !== playingIdx) {
            pendingEvents.current.push({ track_id: extId, action: "play" });
            setPlayingIdx(idx);
          }
        } else {
          lastSyncedExternal.current = extId;
          const r = await api.waveContinue({
            user_id: userId,
            queue: queue.map((t) => t.track_id),
            current_track_id: extId,
            count: 10,
            settings: mood ? { mood } : {},
            recent_events: [...drainEvents(), { track_id: extId, action: "play" }],
          });
          if (stop) return;
          setQueue((prev) => [...prev, ...((r.tracks ?? []) as WaveTrack[])].slice(0, 100));
        }
      } catch {
        /* внешний плеер молчит — очередь живёт сама */
      }
    };
    const id = setInterval(tick, 15000);
    void tick();
    return () => { stop = true; clearInterval(id); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [followNavidrome, userId, queue.length, mood]);

  const curLook = moodLook(cur?.mood ?? "");
  const nxtLook = moodLook(nxt?.mood ?? "");
  const CurIcon = curLook.icon;
  const NxtIcon = nxtLook.icon;

  return (
    <>
      <PageHeader
        title="Моя волна"
        subtitle="Что мозг поставит дальше и на чём выбор сделан — та же очередь, что уйдёт в плеер клиента"
        actions={
          <div className="flex items-center gap-2 flex-wrap">
            <select
              className="kuma-input kuma-input-inline w-48"
              value={userId}
              onChange={(e) => { setUserId(e.target.value); setQueue([]); setPlayingIdx(0); pendingEvents.current = []; }}
            >
              {ids.map((u) => (
                <option key={u.id} value={u.id}>{u.username}</option>
              ))}
            </select>
            <select
              className="kuma-input kuma-input-inline w-40"
              value={mood}
              onChange={(e) => setMood(e.target.value)}
              title="Настроение волны"
            >
              <option value="">Настроение: всё</option>
              {moodOptions.map((m) => (
                <option key={m} value={m}>{m}</option>
              ))}
            </select>
            <Button onClick={() => more(queue.length === 0)} disabled={busy || !userId}>
              {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <Play className="w-4 h-4" />}
              {queue.length === 0 ? "Запустить волну" : "Докрутить +10"}
            </Button>
          </div>
        }
      />

      {cur && (
        <Section title="Сейчас → дальше">
          <Card>
            <div className="flex items-center gap-3 flex-wrap">
              <span
                className="inline-flex items-center gap-2 rounded-full px-4 py-2 text-sm font-semibold text-white shadow"
                style={{ background: curLook.bg }}
              >
                <CurIcon className="w-4 h-4" />
                {cur.mood ?? "без настроения"}
                {cur.energy != null && <span className="opacity-80">· ⚡{cur.energy.toFixed(2)}</span>}
              </span>
              <ArrowRight className="w-5 h-5 text-muted kuma-wave-item" key={nxt?.track_id ?? "none"} />
              <span
                className="inline-flex items-center gap-2 rounded-full px-4 py-2 text-sm font-semibold text-white shadow"
                style={{ background: nxtLook.bg }}
              >
                <NxtIcon className="w-4 h-4" />
                {nxt?.mood ?? "—"}
                {nxt?.energy != null && <span className="opacity-80">· ⚡{nxt.energy.toFixed(2)}</span>}
              </span>
              <span className="text-xs text-muted">
                {cur.mood && nxt?.mood
                  ? cur.mood === nxt.mood
                    ? "держим вайб"
                    : `переход: ${cur.mood} → ${nxt.mood}`
                  : "мозг подбирает по аудио + вкусу + коллаборативке"}
              </span>
            </div>
            <div className="mt-3 flex items-center gap-4 flex-wrap text-xs text-muted">
              <label className="flex items-center gap-1.5 cursor-pointer">
                <input type="checkbox" checked={liveRefill} onChange={(e) => setLiveRefill(e.target.checked)} />
                Авто-докрутка (осталось ≤{REFILL_THRESHOLD} — добрать +10)
              </label>
              <label className="flex items-center gap-1.5 cursor-pointer" title="Раз в 15 сек смотрим, что играет в Navidrome, и подсвечиваем">
                <input type="checkbox" checked={followNavidrome} onChange={(e) => setFollowNavidrome(e.target.checked)} />
                Подсвечивать, что играет в Navidrome
              </label>
              {busy && <span className="inline-flex items-center gap-1"><Loader2 className="w-3 h-3 animate-spin" /> мозг докладывает…</span>}
            </div>
          </Card>
        </Section>
      )}

      <Section title={queue.length > 0 ? `Что дальше · ${queue.length}` : "Что дальше"}>
        <Card>
          {queue.length === 0 ? (
            <div className="text-sm text-muted text-center py-8">
              Пусто — выбери пользователя и нажми «Запустить волну».
            </div>
          ) : (
            <div className="relative pl-6 max-h-[560px] overflow-y-auto pr-1">
              <div className="absolute left-[9px] top-2 bottom-2 w-px bg-border" aria-hidden />
              <div className="space-y-2">
                {queue.map((t, i) => {
                  const active = i === playingIdx;
                  const look = moodLook(t.mood ?? "");
                  const Icon = look.icon;
                  return (
                    <div
                      key={`${t.track_id}-${i}`}
                      ref={(el) => {
                        if (el) itemRefs.current.set(i, el);
                        else itemRefs.current.delete(i);
                      }}
                      onClick={() => markCurrent(i)}
                      className={`kuma-wave-item relative flex items-center gap-3 rounded-xl px-2 py-2 -ml-2 cursor-pointer transition-all hover:bg-border/40 ${active ? "bg-border/50 shadow-sm" : ""}`}
                      style={{ animationDelay: `${Math.min(i, 12) * 45}ms` }}
                      title="Клик — пометить как «сейчас играет»"
                    >
                      <span className="absolute -left-4 w-[19px] h-[19px] rounded-full text-[10px] flex items-center justify-center border border-border bg-bg tabular-nums">
                        {i + 1}
                      </span>
                      <TrackCover
                        trackId={t.track_id}
                        coverArtId={t.cover_art_id}
                        size={100}
                        className="w-10 h-10 rounded-lg"
                      />
                      <div className="min-w-0 flex-1">
                        <Link
                          href={`/track/${t.track_id}`}
                          onClick={(e) => e.stopPropagation()}
                          className="kuma-link truncate block font-medium"
                        >
                          {t.artist_name ? `${t.artist_name} — ` : ""}{t.title}
                        </Link>
                        {/* На чём выбор сделан: причина + скор + покомпонентный скоринг */}
                        <div className="text-[11px] text-muted truncate" title={t.reason}>
                          {t.reason || "—"} · скор {t.score?.toFixed(2)}
                        </div>
                        {t.comp && (
                          <div className="mt-1 flex flex-wrap gap-1">
                            {COMP_LABELS.map(({ key, label }) => {
                              const v = t.comp?.[key];
                              if (v == null) return null;
                              return (
                                <span
                                  key={key}
                                  className="rounded-full border border-border px-1.5 py-0.5 text-[10px] tabular-nums text-muted"
                                  title={`${label}: ${v.toFixed(2)}`}
                                >
                                  {label} {v.toFixed(2)}
                                </span>
                              );
                            })}
                          </div>
                        )}
                      </div>
                      {t.mood && (
                        <span
                          className="hidden sm:inline-flex items-center gap-1 rounded-full px-2.5 py-1 text-[11px] font-medium text-white shrink-0"
                          style={{ background: look.bg }}
                          title={[...(t.moods ?? [])].join(", ")}
                        >
                          <Icon className="w-3 h-3" />
                          {t.mood}
                        </span>
                      )}
                      {active && (
                        <span className="kuma-eq shrink-0" aria-label="сейчас">
                          <span /><span /><span />
                        </span>
                      )}
                    </div>
                  );
                })}
              </div>
            </div>
          )}
        </Card>
      </Section>
    </>
  );
}
