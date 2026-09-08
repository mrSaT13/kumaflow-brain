"use client";

import Link from "next/link";
import useSWR from "swr";
import { useParams } from "next/navigation";
import { Disc3 } from "lucide-react";
import { Card, EmptyState, PageHeader, Section, Badge } from "@/components/ui";
import { api } from "@/lib/api";
import { fmtDuration } from "@/lib/format";

export default function PlaylistDetailPage() {
  const params = useParams<{ id: string }>();
  const id = params.id;
  const { data } = useSWR(["playlist", id], () => api.getPlaylist(id));

  if (!data) return <div className="text-sm text-muted">Загрузка…</div>;
  const tracks = (data as unknown as { tracks: { id: string; title: string; artist_name?: string; album_name?: string; position: number; duration_sec?: number }[] }).tracks ?? [];

  return (
    <>
      <PageHeader
        title={data.name}
        subtitle={`${tracks.length} треков`}
        actions={<Link href="/playlists" className="kuma-link text-sm">← Назад</Link>}
      />
      <Section title="Треки">
        {tracks.length === 0 ? (
          <EmptyState message="Плейлист пуст." />
        ) : (
          <div className="kuma-card overflow-hidden">
            <table className="kuma-table w-full">
              <thead>
                <tr>
                  <th>#</th>
                  <th>Название</th>
                  <th>Артист</th>
                  <th>Альбом</th>
                  <th className="text-right">Длительность</th>
                </tr>
              </thead>
              <tbody>
                {tracks.map((t) => (
                  <tr key={t.id}>
                    <td className="tabular-nums text-muted">{t.position + 1}</td>
                    <td>
                      <Link href={`/track/${t.id}`} className="kuma-link">
                        {t.title}
                      </Link>
                    </td>
                    <td className="text-muted">{t.artist_name ?? "—"}</td>
                    <td className="text-muted">{t.album_name ?? "—"}</td>
                    <td className="text-right tabular-nums text-muted">{fmtDuration(t.duration_sec)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>
    </>
  );
}
