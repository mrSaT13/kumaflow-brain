"use client";

import Link from "next/link";
import useSWR from "swr";
import { useParams } from "next/navigation";
import { useState } from "react";
import { ArrowLeft, ExternalLink, RefreshCw, Sparkles } from "lucide-react";
import { Badge, Button, Card, EmptyState, PageHeader, Section, Skeleton } from "@/components/ui";
import { api } from "@/lib/api";
import { useIsAdmin } from "@/lib/useIsAdmin";
import { genreIcon } from "@/lib/genreIcon";
import { genreColor } from "@/lib/genreStyle";

const stripHtml = (s: string) => s.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim();
const artistHref = (name: string) => `/artists/${encodeURIComponent(name)}`;

export default function ArtistPage() {
  const params = useParams<{ name: string }>();
  const name = decodeURIComponent(params.name || "");
  const { data, error, isLoading, mutate } = useSWR(
    name ? ["artist-details", name] : null,
    () => api.artistDetails(name),
  );
  const { isAdmin } = useIsAdmin();
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  async function refresh() {
    setBusy(true);
    setNotice(null);
    try {
      const r = await api.bridgeEnrich({ name, kind: "artist", limit: 1 });
      setNotice(r.ok ? "Метаданные обновлены из моста." : `Ошибка: ${r.error ?? "мост недоступен"}`);
      void mutate();
    } catch (e: unknown) {
      setNotice(`Ошибка: ${String(e)}`);
    } finally {
      setBusy(false);
    }
  }

  if (isLoading) return <Skeleton />;
  if (error || !data?.ok) return <EmptyState message="Артист не найден." />;

  const b = data.bridge;
  const cover = b?.image_url || b?.cover_url || (data.entry?.cover_track_id ? api.trackCoverUrl(data.entry.cover_track_id, 400) : "");
  const stats = data.stats ?? {};

  return (
    <>
      <PageHeader
        title={data.name}
        subtitle={`Артист · ${data.track_count} треков в библиотеке`}
        actions={
          <a href="/cold-start" className="kuma-pill inline-flex items-center gap-1">
            <ArrowLeft className="w-3 h-3" /> К артистам
          </a>
        }
      />
      {notice && <div className="mb-4 text-sm p-3 rounded-lg border border-border bg-surface">{notice}</div>}

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <Card className="lg:col-span-1">
          <div className="flex flex-col items-center gap-3 text-center">
            {cover ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img
                src={cover}
                alt={`Обложка ${data.name}`}
                className="w-48 h-48 rounded-2xl object-cover border border-border"
                onError={(e) => ((e.target as HTMLImageElement).style.display = "none")}
              />
            ) : (
              <div className="w-48 h-48 rounded-2xl border border-dashed border-border flex items-center justify-center text-muted text-sm">
                нет обложки
              </div>
            )}
            <div className="flex flex-wrap gap-1 justify-center">
              {data.in_library ? <Badge tone="ok">в библиотеке</Badge> : <Badge tone="warn">только метаданные</Badge>}
              {b && (
                <span title="Данные синхронизированы с KumaFlow Bridge">
                  <Badge tone={b.stale ? "warn" : "ok"}>
                    <Sparkles className="w-3 h-3 inline" /> мост · {b.synced_at?.slice(0, 10) ?? "—"}
                  </Badge>
                </span>
              )}
            </div>
            {isAdmin && (
              <Button variant="ghost" onClick={refresh} disabled={busy}>
                <RefreshCw className="w-4 h-4" /> {busy ? "Обновляю…" : "Обновить из моста"}
              </Button>
            )}
          </div>

          {b && (
            <div className="mt-4 pt-3 border-t border-border text-xs text-muted space-y-1">
              {b.mbid && <div>MusicBrainz ID: <code className="kuma-pill">{b.mbid}</code></div>}
              {b.country && <div>Страна: <b className="text-text">{b.country}</b></div>}
              {b.artist_type && <div>Тип: <b className="text-text">{b.artist_type}</b></div>}
              {!!b.listeners && <div>Слушателей (Last.fm): <b className="text-text tabular-nums">{b.listeners.toLocaleString("ru-RU")}</b></div>}
              {!!b.playcount && <div>Прослушиваний (Last.fm): <b className="text-text tabular-nums">{b.playcount.toLocaleString("ru-RU")}</b></div>}
              {!!b.mood?.label && <div>Муд по тегам: <b className="text-text">{b.mood.label}</b>{(b.mood.valence != null || b.mood.energy != null) && <span className="text-muted"> · valence {b.mood.valence ?? "—"} / energy {b.mood.energy ?? "—"}</span>}</div>}
              {b.lastfm_url && (
                <a href={b.lastfm_url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 hover:text-text">
                  Last.fm <ExternalLink className="w-3 h-3" />
                </a>
              )}
            </div>
          )}

          <div className="grid grid-cols-2 gap-2 mt-4">
            {[
              ["Треков", stats.tracks ?? 0],
              ["Прослушиваний", stats.plays ?? 0],
              ["Лайков", stats.likes ?? 0],
              ["Альбомов", stats.albums ?? 0],
            ].map(([k, v]) => (
              <div key={String(k)} className="kuma-card !p-3">
                <div className="text-[11px] text-muted">{k}</div>
                <div className="text-lg font-semibold tabular-nums">{String(v)}</div>
              </div>
            ))}
          </div>
        </Card>

        <Card className="lg:col-span-2">
          <div className="text-xs uppercase tracking-wider text-muted mb-2">Об артисте</div>
          {b?.bio_summary ? (
            <p className="text-sm leading-relaxed">{stripHtml(b.bio_summary)}</p>
          ) : (
            <p className="text-sm text-muted">Био появится после синхронизации с мостом (Настройки → Мост → Синхронизировать).</p>
          )}

          <div className="mt-4">
            <div className="text-xs text-muted mb-2">
              Теги {b ? <span className="inline-flex items-center gap-1"><Sparkles className="w-3 h-3" /> синхронизировано с мостом {b.synced_at?.slice(0, 10)}</span> : null}
            </div>
            <div className="flex flex-wrap gap-1">
              {(b?.tags ?? []).map((t) => (
                <span key={t} className="kuma-pill !text-[11px]" title="Тег из MusicBrainz/Last.fm через мост">{t}</span>
              ))}
              {(data.genres ?? []).slice(0, 10).map((g) => {
                const Icon = genreIcon(g.name);
                return (
                  <span key={g.name} className="kuma-pill !text-[11px]" style={{ color: genreColor(g.name) }} title={`Локальный жанр · ${g.count} треков`}>
                    <Icon className="w-3 h-3" /> {g.name}
                  </span>
                );
              })}
              {!b?.tags?.length && !data.genres?.length && <span className="text-xs text-muted">Тегов пока нет.</span>}
            </div>
          </div>

          {!!b?.mood_tags?.length && (
            <div className="mt-4">
              <div className="text-xs text-muted mb-2">Настроения (Last.fm, через мост)</div>
              <div className="flex flex-wrap gap-1">
                {b.mood_tags.map((m) => <span key={m} className="kuma-pill !text-[11px]">{m}</span>)}
              </div>
            </div>
          )}
        </Card>
      </div>

      <Section title={`Похожие артисты (${data.similar?.length ?? 0})`}>
        {!data.similar?.length ? (
          <EmptyState message="Похожих не нашли — добавьте теги через мост или включите синхронизацию." />
        ) : (
          <div className="flex flex-wrap gap-2">
            {data.similar.map((s) => (
              <a key={s.name} href={artistHref(s.name)} className="kuma-pill hover:text-text inline-flex items-center gap-1" title={s.from_bridge ? "Из Last.fm через мост" : "Локально по жанрам/кластерам"}>
                {s.from_bridge ? <Sparkles className="w-3 h-3" /> : null}
                {s.name}
                {s.track_count > 0 && <span className="text-muted">· {s.track_count} тр.</span>}
              </a>
            ))}
          </div>
        )}
      </Section>

      <Section title={`Альбомы (${data.albums?.length ?? 0})`}>
        {!data.albums?.length ? (
          <EmptyState message="Альбомов в библиотеке нет." />
        ) : (
          <div className="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-6 gap-3">
            {data.albums.map((a) => (
              <div key={a.external_id} className="kuma-card !p-2">
                {a.cover_art_id ? (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img src={api.coverUrl(a.cover_art_id, 200)} alt={a.title} className="w-full aspect-square rounded-lg object-cover mb-2" />
                ) : null}
                <div className="text-xs font-medium line-clamp-2">{a.title}</div>
                <div className="text-[10px] text-muted">{a.year || "—"} {a.genre ? `· ${a.genre}` : ""}</div>
              </div>
            ))}
          </div>
        )}
      </Section>

      <Section title={`Треки (${data.tracks?.length ?? 0})`}>
        {!data.tracks?.length ? (
          <EmptyState message="Треков нет." />
        ) : (
          <div className="kuma-card divide-y divide-border">
            {data.tracks.map((t) => (
              <Link key={t.track_id} href={`/track/${t.track_id}`} className="flex items-center gap-3 p-2 hover:bg-surface transition-colors">
                {t.cover_art_id ? (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img src={api.trackCoverUrl(t.track_id, 80)} alt="" className="w-10 h-10 rounded object-cover" />
                ) : null}
                <div className="min-w-0 flex-1">
                  <div className="text-sm truncate">{t.title}</div>
                  <div className="text-[11px] text-muted truncate">{t.album_name || "без альбома"} {t.genre ? `· ${t.genre}` : ""}</div>
                </div>
                <div className="text-xs text-muted tabular-nums">{t.plays}</div>
              </Link>
            ))}
          </div>
        )}
      </Section>
    </>
  );
}
