"use client";

import useSWR from "swr";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";
import { ExternalLink, FileText, Sparkles } from "lucide-react";
import { Button, Card, EmptyState, PageHeader, Section, Skeleton } from "@/components/ui";
import { api, type Track } from "@/lib/api";
import { useIsAdmin } from "@/lib/useIsAdmin";
import { fmtDuration } from "@/lib/format";
import { moodLook } from "@/lib/moodStyle";

export default function TrackPage() {
  const params = useParams<{ id: string }>();
  const id = params.id;
  const { data, error, isLoading, mutate } = useSWR(["track", id], () => api.getTrack(id));
  const bridgeTrack = data?.metadata?.bridge_track as {
    mood_tags?: string[]; mood_codes?: string[]; tags?: string[];
    synced_at?: string; stale?: boolean; source?: string;
    listeners?: number | null; playcount?: number | null; image_url?: string;
    mood?: { valence?: number | null; energy?: number | null; label?: string };
  } | undefined;
  const { data: recs } = useSWR(data ? ["rec", id] : null, () => api.recommendByTrack(id));
  const [busy, setBusy] = useState<"sonic" | "lyrics" | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  // POST /tracks/{id}/analyze — require_admin; /lyrics — открыт всем.
  const { isAdmin } = useIsAdmin();

  async function analyzeNow() {
    setBusy("sonic");
    setNotice(null);
    try {
      const r = await api.analyzeTrack(id);
      setNotice(`Sonic-анализ запущен (задача ${r.run_id.slice(0, 8)}). Обновите страницу через минуту.`);
    } catch (e: unknown) {
      setNotice(`Ошибка: ${String(e)}`);
    } finally {
      setBusy(null);
    }
  }

  async function lyricsNow() {
    setBusy("lyrics");
    setNotice(null);
    try {
      const r = await api.fetchTrackLyrics(id);
      if (!r.ok) setNotice(`Текст не найден: ${r.error ?? "LRCLIB ничего не вернул"}`);
      else {
        setNotice("Текст загружен.");
        mutate();
      }
    } catch (e: unknown) {
      setNotice(`Ошибка: ${String(e)}`);
    } finally {
      setBusy(null);
    }
  }

  if (isLoading) return (
    <div className="space-y-4">
      <Skeleton className="h-16" />
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <Skeleton className="h-64" />
        <Skeleton className="h-64" />
        <Skeleton className="h-64" />
      </div>
    </div>
  );
  if (error || !data) return <EmptyState message="Трек не найден или бэкенд недоступен." />;

  const f = data.features ?? {};

  return (
    <>
      <PageHeader
        title={data.title}
        subtitle={[data.artist_name, data.album_name, data.year].filter(Boolean).join(" · ")}
        actions={
          <div className="flex items-center gap-2 flex-wrap">
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src={api.trackCoverUrl(id, 80)}
              alt="Обложка"
              loading="lazy"
              decoding="async"
              className="w-10 h-10 rounded-lg object-cover border border-border"
              onError={(e) => ((e.target as HTMLImageElement).style.display = "none")}
            />
            {isAdmin && (
            <Button variant="ghost" onClick={analyzeNow} disabled={busy !== null}>
              <Sparkles className="w-4 h-4" /> {busy === "sonic" ? "Анализ…" : "Проанализировать трек"}
            </Button>
            )}
            {!data.lyrics && (
              <Button variant="ghost" onClick={lyricsNow} disabled={busy !== null}>
                <FileText className="w-4 h-4" /> {busy === "lyrics" ? "Ищу…" : "Загрузить текст"}
              </Button>
            )}
          </div>
        }
      />
      {notice && (
        <div className="mb-4 text-sm p-3 rounded-lg border border-border bg-surface">{notice}</div>
      )}
      <YandexCorrection id={id} />

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <Card className="lg:col-span-1">
          <div className="flex items-start gap-3 mb-3">
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src={api.trackCoverUrl(id, 300)}
              alt="Обложка трека"
              loading="lazy"
              decoding="async"
              className="w-24 h-24 rounded-xl object-cover border border-border shrink-0"
              onError={(e) => ((e.target as HTMLImageElement).style.display = "none")}
            />
            <div className="text-xs uppercase tracking-wider text-muted pt-1">Метаданные</div>
          </div>
          <dl className="text-sm space-y-2">
            <Row k="Артист" v={data.artist_name} />
            <Row k="Альбом" v={data.album_name} />
            <Row k="Жанр" v={data.genre} />
            <Row k="Год" v={data.year} />
            <Row k="Длительность" v={fmtDuration(data.duration_sec)} />
            <Row k="Прослушиваний" v={data.play_count} />
            <Row k="Оценка" v={data.rating} />
            <Row k="Кластер" v={data.cluster ? `#${data.cluster.id} (${data.cluster.algorithm})` : "—"} />
          </dl>
          {data.artist_name && (
            <a
              href={`/artists/${encodeURIComponent(data.artist_name)}`}
              className="kuma-pill inline-flex items-center gap-1 mt-3 hover:text-text"
            >
              Страница артиста <ExternalLink className="w-3 h-3" />
            </a>
          )}

          {data.artist_metadata && (
            <div className="mt-4 pt-3 border-t border-border">
              <div className="text-[11px] text-muted flex items-center gap-1 flex-wrap">
                <Sparkles className="w-3 h-3" />
                <span>Теги артиста · синхронизировано с мостом</span>
                {data.artist_metadata.synced_at && (
                  <span className="tabular-nums">({data.artist_metadata.synced_at.slice(0, 10)})</span>
                )}
                {data.artist_metadata.stale && (
                  <span title="Последняя синхронизация не удалась — показаны прошлые данные">⚠ outdated</span>
                )}
              </div>
              <div className="flex flex-wrap gap-1 mt-2">
                {(data.artist_metadata.tags ?? []).length ? (
                  data.artist_metadata.tags.map((tag) => (
                    <span
                      key={tag}
                      className="kuma-pill !text-[11px]"
                      title={`${data.artist_metadata?.source ?? "bridge"} · ${data.artist_metadata?.mbid || "MusicBrainz ID неизвестен"}`}
                    >
                      {tag}
                    </span>
                  ))
                ) : (
                  <span className="text-xs text-muted">Тегов пока нет — запустите синхронизацию с мостом в Настройках.</span>
                )}
              </div>
              {!!data.artist_metadata.bio_summary && (
                <p className="text-xs text-muted mt-2 line-clamp-3">{data.artist_metadata.bio_summary}</p>
              )}
              {!!data.artist_metadata.similar_artists?.length && (
                <div className="text-[11px] text-muted mt-2">
                  Похожие (Last.fm): {data.artist_metadata.similar_artists.slice(0, 6).join(", ")}
                </div>
              )}
            </div>
          )}
        </Card>

        <Card className="lg:col-span-2">
          <div className="text-xs uppercase tracking-wider text-muted mb-2">Sonic-фичи</div>
          {Object.keys(f).length === 0 ? (
            <EmptyState message="Трек ещё не проанализирован. Нажмите «Проанализировать трек» выше или запустите sonic-анализ на странице «Задачи и логи»." />
          ) : (
            <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
              {Object.entries(f).map(([k, v]) => (
                <div key={k} className="kuma-card !p-3">
                  <div className="text-[11px] text-muted">{k}</div>
                  <div className="text-sm font-medium tabular-nums mt-1">
                    {typeof v === "number" ? v.toFixed(3) : String(v)}
                  </div>
                </div>
              ))}
            </div>
          )}
        </Card>

        <Card className="lg:col-span-1">
          <div className="text-xs uppercase tracking-wider text-muted mb-3">Настроение</div>
          {data.moods && data.moods.length ? (
            <>
              <div className="flex flex-wrap gap-2 kuma-fade-in">
                {data.moods.map((m) => {
                  const look = moodLook(m);
                  const MIcon = look.icon;
                  return (
                    <span
                      key={m}
                      className="inline-flex items-center gap-1.5 rounded-full px-3 py-1.5 text-sm font-medium text-white shadow-sm"
                      style={{ background: look.bg }}
                    >
                      <MIcon className="w-4 h-4" />
                      {m}
                    </span>
                  );
                })}
              </div>
              <MoodMeters features={f} />
            </>
          ) : (
            <EmptyState message="Настроение появится после AI-анализа текста." />
          )}
          {bridgeTrack && (bridgeTrack.mood_tags?.length || bridgeTrack.tags?.length) ? (
            <div className="mt-3 pt-3 border-t border-border">
              <div className="text-[11px] text-muted flex items-center gap-1 flex-wrap">
                <Sparkles className="w-3 h-3" />
                <span>Настроение по тегам Last.fm · синхронизировано с мостом</span>
                {bridgeTrack.synced_at && <span className="tabular-nums">({bridgeTrack.synced_at.slice(0, 10)})</span>}
                {bridgeTrack.stale && <span title="Последняя синхронизация не удалась">⚠ outdated</span>}
              </div>
              <div className="flex flex-wrap gap-1 mt-2">
                {(bridgeTrack.mood_tags ?? []).map((m) => {
                  const look = moodLook(m);
                  const MIcon = look.icon;
                  return (
                    <span key={m} className="kuma-pill !text-[11px]" title={`${bridgeTrack.source ?? "bridge"} · ${(bridgeTrack.mood_codes ?? []).join(", ")}`}>
                      <MIcon className="w-3 h-3" /> {m}
                    </span>
                  );
                })}
                {!bridgeTrack.mood_tags?.length && (bridgeTrack.tags ?? []).slice(0, 6).map((t) => (
                  <span key={t} className="kuma-pill !text-[11px]" title="Тег Last.fm (не является пилюлей настроения)">{t}</span>
                ))}
              </div>
              {(!!bridgeTrack.listeners || !!bridgeTrack.playcount || !!bridgeTrack.mood?.label) && (
                <div className="text-[10px] text-muted mt-1 flex flex-wrap gap-2">
                  {!!bridgeTrack.listeners && <span>Слушателей: {bridgeTrack.listeners.toLocaleString("ru-RU")}</span>}
                  {!!bridgeTrack.playcount && <span>Прослушиваний: {bridgeTrack.playcount.toLocaleString("ru-RU")}</span>}
                  {!!bridgeTrack.mood?.label && <span>Муд по тегам: {bridgeTrack.mood.label} (valence {bridgeTrack.mood.valence ?? "—"} / energy {bridgeTrack.mood.energy ?? "—"})</span>}
                </div>
              )}
              <div className="text-[10px] text-muted mt-1">
                Аудио-анализ имеет приоритет; мост используется только для треков без анализа.
              </div>
            </div>
          ) : null}
        </Card>

        <Card className="lg:col-span-2">
          <div className="text-xs uppercase tracking-wider text-muted mb-2">Текст песни</div>
          {data.lyrics ? (
            <>
              <pre className="whitespace-pre-wrap text-sm leading-relaxed font-sans max-h-96 overflow-auto">
                {data.lyrics.text}
              </pre>
              <div className="text-[11px] text-muted mt-2">
                Источник: {data.lyrics.provider} {data.lyrics.source_url ? `· ${data.lyrics.source_url}` : ""}
              </div>
            </>
          ) : (
            <EmptyState message="Текст ещё не загружен. Нажмите «Загрузить текст» выше или запустите загрузку текстов на странице «Задачи и логи»." />
          )}
        </Card>

        <Card className="lg:col-span-3">
          <div className="text-xs uppercase tracking-wider text-muted mb-3">Похожие треки (content-based, косинус по фичам)</div>
          {recs?.items && recs.items.length > 0 ? (
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-2">
              {recs.items.slice(0, 9).map((t: Track & { score?: number; track_id?: string }) => {
                const tid = t.id ?? t.track_id;
                if (!tid) return null;
                return (
                <Link
                  key={tid}
                  href={`/track/${tid}`}
                  className="kuma-card !p-3 hover:bg-surface flex items-center gap-3"
                >
                  <div className="flex-1 min-w-0">
                    <div className="text-sm font-medium truncate">{t.title}</div>
                    <div className="text-xs text-muted truncate">{t.artist_name ?? "—"}</div>
                  </div>
                  {typeof t.score === "number" && (
                    <div className="text-xs tabular-nums text-muted">{(t.score * 100).toFixed(0)}%</div>
                  )}
                </Link>
                );
              })}
            </div>
          ) : (
            <div className="text-sm text-muted">Похожих пока нет — нужен sonic-анализ большего числа треков.</div>
          )}
        </Card>
      </div>
    </>
  );
}

function MoodMeters({ features }: { features: Record<string, number | string | null> }) {
  const meters = [
    { key: "energy", label: "Энергия", bar: "linear-gradient(90deg,#f59e0b,#ef4444)" },
    { key: "valence", label: "Позитив", bar: "linear-gradient(90deg,#34d399,#fbbf24)" },
    { key: "danceability", label: "Танцевальность", bar: "linear-gradient(90deg,#ec4899,#8b5cf6)" },
  ].map((m) => {
    const raw = features[m.key];
    return { ...m, value: typeof raw === "number" ? Math.min(1, Math.max(0, raw)) : null };
  }).filter((m) => m.value !== null);
  if (meters.length === 0) return null;
  return (
    <div className="mt-4 space-y-2.5">
      <div className="text-xs uppercase tracking-wider text-muted">Характер трека</div>
      {meters.map((m) => (
        <div key={m.key}>
          <div className="flex items-center justify-between text-xs mb-1">
            <span className="text-muted">{m.label}</span>
            <span className="tabular-nums font-medium">{Math.round((m.value ?? 0) * 100)}%</span>
          </div>
          <div className="h-2 rounded-full bg-border overflow-hidden">
            <div className="h-full rounded-full transition-all" style={{ width: `${Math.round((m.value ?? 0) * 100)}%`, background: m.bar }} />
          </div>
        </div>
      ))}
    </div>
  );
}

function Row({ k, v }: { k: string; v: unknown }) {
  return (
    <div className="flex items-center justify-between">
      <dt className="text-muted">{k}</dt>
      <dd>{v === undefined || v === null || v === "" ? "—" : String(v)}</dd>
    </div>
  );
}

function YandexCorrection({ id }: { id: string }) {
  const { data, mutate } = useSWR(["yandex-meta", id], () => api.yandexTrackMeta(id));
  const corr = (data?.items ?? [])
    .map((it) => (it.data as Record<string, unknown>).corrected as Record<string, [unknown, unknown]> | undefined)
    .find(Boolean);
  if (!corr || Object.keys(corr).length === 0) return null;
  return (
    <div className="mb-4 text-sm p-3 rounded-lg border border-amber-300 bg-amber-50 dark:bg-amber-950 flex items-center gap-2 flex-wrap">
      <Sparkles className="w-4 h-4 shrink-0" />
      <span>Яндекс поправил:</span>
      {Object.entries(corr).map(([f, [oldV, newV]]) => (
        <span key={f} className="rounded-full border border-border px-2 py-0.5 text-xs tabular-nums bg-bg">
          {f}: {String(oldV ?? "—")} → {String(newV ?? "—")}
        </span>
      ))}
      <RevertButton id={id} onDone={mutate} />
    </div>
  );
}

function RevertButton({ id, onDone }: { id: string; onDone: () => void }) {
  // POST /yandex/corrections/revert — require_admin.
  const { isAdmin } = useIsAdmin();
  const [busy, setBusy] = useState(false);
  if (!isAdmin) return null;
  return (
      <button
        className="kuma-pill text-xs"
        disabled={busy}
        onClick={async () => {
          setBusy(true);
          try {
            await api.yandexRevertCorrection(id);
            onDone();
          } finally {
            setBusy(false);
          }
        }}
      >
        {busy ? "…" : "Откатить"}
      </button>
  );
}
