"use client";

import Link from "next/link";
import useSWR from "swr";
import { useParams } from "next/navigation";
import { useState } from "react";
import { Send } from "lucide-react";
import { Button, Card, EmptyState, PageHeader, Section, Badge } from "@/components/ui";
import { api } from "@/lib/api";
import { fmtDuration } from "@/lib/format";

export default function PlaylistDetailPage() {
  const params = useParams<{ id: string }>();
  const id = params.id;
  const { data, mutate } = useSWR(["playlist", id], () => api.getPlaylist(id));
  const [exporting, setExporting] = useState(false);

  async function exportToNavidrome() {
    setExporting(true);
    try {
      const r = await api.exportPlaylist(id);
      if (!r.ok) alert(`Ошибка: ${r.error ?? "неизвестная"}`);
      else {
        alert(`В Navidrome: треков ${r.exported ?? 0}${r.skipped ? ` (пропущено файлов с диска: ${r.skipped})` : ""}. Обновите список плейлистов в клиенте.`);
        mutate();
      }
    } catch (e: unknown) {
      alert(String(e));
    } finally {
      setExporting(false);
    }
  }

  if (!data) return <div className="text-sm text-muted">Загрузка…</div>;
  const tracks = data.tracks ?? [];
  const showSim = tracks.some((t) => t.similarity !== null && t.similarity !== undefined);
  const showMood = tracks.some((t) => (t.mood ?? []).length > 0);
  const showKey = tracks.some((t) => t.musical_key);

  return (
    <>
      <PageHeader
        title={data.name}
        subtitle={`${tracks.length} треков${data.in_navidrome ? " · есть в Navidrome ✓" : ""}`}
        actions={
          <div className="flex items-center gap-2">
            {data.in_navidrome && <Badge tone="ok">в Navidrome</Badge>}
            <Button variant="ghost" onClick={exportToNavidrome} disabled={exporting}>
              <Send className="w-4 h-4" /> {exporting ? "Выгружаю…" : data.in_navidrome ? "Обновить в Navidrome" : "В Navidrome"}
            </Button>
            <Link href="/playlists" className="kuma-link text-sm">← Назад</Link>
          </div>
        }
      />
      <Section title="Треки">
        {tracks.length === 0 ? (
          <EmptyState message="Плейлист пуст." />
        ) : (
          <div className="kuma-card overflow-hidden">
            <div className="overflow-x-auto">
            <table className="kuma-table w-full min-w-[960px]">
              <thead>
                <tr>
                  <th>#</th>
                  <th>Название</th>
                  <th>Артист</th>
                  <th>Альбом</th>
                  {showMood && <th>Настроение</th>}
                  {showKey && <th>Тональность</th>}
                  {showSim && <th className="text-right">Схожесть</th>}
                  <th className="text-right">Длительность</th>
                </tr>
              </thead>
              <tbody>
                {tracks.map((t) => (
                  <tr key={t.id}>
                    <td className="tabular-nums text-muted">{t.position + 1}</td>
                    <td>
                      <div className="flex items-center gap-2 min-w-0">
                        {/* eslint-disable-next-line @next/next/no-img-element */}
                        <img
                          src={api.trackCoverUrl(t.id, 100)}
                          alt=""
                          loading="lazy"
                          decoding="async"
                          className="w-8 h-8 rounded object-cover border border-border shrink-0"
                          onError={(e) => ((e.target as HTMLImageElement).style.display = "none")}
                        />
                        <Link href={`/track/${t.id}`} className="kuma-link truncate" title={t.title}>
                          {t.title}
                        </Link>
                      </div>
                    </td>
                    <td className="text-muted truncate" title={t.artist_name ?? ""}>{t.artist_name ?? "—"}</td>
                    <td className="text-muted truncate" title={t.album_name ?? ""}>{t.album_name ?? "—"}</td>
                    {showMood && (
                      <td>
                        {(t.mood ?? []).length > 0 ? (
                          <span className="flex gap-1 flex-wrap">
                            {(t.mood ?? []).slice(0, 2).map((m) => (
                              <span key={m} className="kuma-pill">{m}</span>
                            ))}
                          </span>
                        ) : "—"}
                      </td>
                    )}
                    {showKey && <td className="text-muted tabular-nums">{t.musical_key ?? "—"}</td>}
                    {showSim && (
                      <td className="text-right tabular-nums text-muted">
                        {t.similarity !== null && t.similarity !== undefined ? `${Math.round(t.similarity * 100)}%` : "—"}
                      </td>
                    )}
                    <td className="text-right tabular-nums text-muted">{fmtDuration(t.duration_sec)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            </div>
          </div>
        )}
      </Section>
    </>
  );
}
