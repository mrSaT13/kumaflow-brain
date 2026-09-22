"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";
import useSWR from "swr";
import { CalendarDays, Disc3, Heart, MoonStar, Sparkles } from "lucide-react";
import { Badge, Card, EmptyState, PageHeader, Section, Stat } from "@/components/ui";
import { api } from "@/lib/api";

const MONTHS_RU = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"];

function monthLabel(m: string) {
  const [y, mm] = m.split("-").map(Number);
  return `${MONTHS_RU[(mm || 1) - 1]} ${y}`;
}

function TopBars({ items, max }: { items: { name: string; plays: number }[]; max: number }) {
  return (
    <div className="space-y-2">
      {items.map((t) => (
        <div key={t.name} className="flex items-center gap-2 text-sm">
          <span className="w-36 truncate" title={t.name}>{t.name}</span>
          <div className="flex-1 h-2 rounded-full bg-border overflow-hidden">
            <div className="h-full rounded-full bg-accent" style={{ width: `${Math.max(3, (t.plays / Math.max(1, max)) * 100)}%` }} />
          </div>
          <span className="text-xs text-muted tabular-nums w-12 text-right">{t.plays} ▶</span>
        </div>
      ))}
    </div>
  );
}

export default function WrappedPage() {
  const params = useParams<{ id: string }>();
  const id = params.id;
  const [sel, setSel] = useState<string | null>(null);
  const y = sel ? Number(sel.split("-")[0]) : undefined;
  const m = sel ? Number(sel.split("-")[1]) : undefined;
  const { data, isLoading } = useSWR(["wrapped", id, sel], () => api.wrapped(id, y, m));

  if (isLoading) return <div className="text-sm text-muted">Считаю итоги…</div>;
  if (!data?.ok) return <EmptyState message="Нет данных." />;

  const cur = sel ?? data.month;
  const maxP = Math.max(1, ...(data.top_tracks ?? []).map((t) => t.plays));

  return (
    <>
      <PageHeader
        title={`Итоги · ${monthLabel(cur)}`}
        subtitle={<Link href={`/users/${id}` as never} className="kuma-link text-sm">← к профилю</Link>}
        actions={
          <select className="kuma-input kuma-input-inline w-44" value={cur} onChange={(e) => setSel(e.target.value)}>
            {(data.months ?? []).map((mm) => (
              <option key={mm} value={mm}>{monthLabel(mm)}</option>
            ))}
          </select>
        }
      />

      {data.plays === 0 ? (
        <EmptyState message="В этом месяце прослушиваний нет." />
      ) : (
        <>
          <Section title="Цифры месяца">
            <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
              <Stat label="Прослушиваний" value={data.plays} icon={<Disc3 className="w-3 h-3" />} hint={`${data.minutes} мин музыки`} />
              <Stat label="Активных дней" value={data.active_days} icon={<CalendarDays className="w-3 h-3" />} hint={`пик в ${data.peak_hour}:00`} />
              <Stat label="Открытий" value={data.discoveries} icon={<Sparkles className="w-3 h-3" />} hint="треков впервые" />
              <Stat label="Новые ♥" value={data.new_likes} icon={<Heart className="w-3 h-3" />} hint={`👎 ${data.new_dislikes} · ⛔ ${data.new_bans} · ↷ ${data.replays} · скипы ${data.skips}`} />
            </div>
          </Section>

          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            <Section title="Топ треков">
              <Card>
                <div className="space-y-2">
                  {(data.top_tracks ?? []).map((t, i) => (
                    <div key={t.track_id} className="flex items-center gap-2 text-sm">
                      <span className="text-muted tabular-nums w-5">{i + 1}</span>
                      <div className="flex-1 min-w-0">
                        <Link href={`/track/${t.track_id}` as never} className="kuma-link truncate block">{t.title}</Link>
                        <div className="text-xs text-muted truncate">{t.artist_name ?? ""}</div>
                      </div>
                      <div className="flex-1 h-2 rounded-full bg-border overflow-hidden hidden sm:block">
                        <div className="h-full rounded-full bg-accent" style={{ width: `${Math.max(3, (t.plays / maxP) * 100)}%` }} />
                      </div>
                      <span className="text-xs text-muted tabular-nums w-12 text-right">{t.plays} ▶</span>
                    </div>
                  ))}
                </div>
              </Card>
            </Section>

            <div className="space-y-4">
              <Section title="Топ артистов">
                <Card>
                  <TopBars items={data.top_artists ?? []} max={Math.max(1, ...(data.top_artists ?? []).map((a) => a.plays))} />
                </Card>
              </Section>
              <Section title="Топ жанров">
                <Card>
                  <TopBars items={data.top_genres ?? []} max={Math.max(1, ...(data.top_genres ?? []).map((g) => g.plays))} />
                </Card>
              </Section>
            </div>
          </div>

          {(data.discoveries_sample ?? []).length > 0 && (
            <Section title="Первые открытия месяца">
              <Card>
                <div className="flex flex-wrap gap-2">
                  {(data.discoveries_sample ?? []).map((t) => (
                    <span key={t.track_id} className="kuma-pill">
                      <MoonStar className="w-3 h-3" /> {t.artist_name ? `${t.artist_name} — ` : ""}{t.title}
                    </span>
                  ))}
                </div>
              </Card>
            </Section>
          )}

          <div className="flex gap-2 flex-wrap">
            <Badge tone="info">completes {data.completes}</Badge>
            <Badge>дослушано {data.plays > 0 ? Math.round(((data.completes ?? 0) / data.plays) * 100) : 0}%</Badge>
          </div>
        </>
      )}
    </>
  );
}
