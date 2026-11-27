"use client";

import useSWR from "swr";
import { useState } from "react";
import { ArrowLeft, RefreshCw, Sparkles } from "lucide-react";
import { Button, Card, EmptyState, PageHeader, Section, Skeleton } from "@/components/ui";
import { api, type ChartArtist } from "@/lib/api";

const PERIODS = ["7day", "1month", "3month", "6month", "12month", "overall"];

export default function ChartsPage() {
  const [tab, setTab] = useState<"chart" | "geo" | "user">("chart");
  const [country, setCountry] = useState("Russian Federation");
  const [period, setPeriod] = useState("7day");
  const [limit, setLimit] = useState(50);

  const key = [tab, country, period, limit];
  const { data, error, isLoading, mutate } = useSWR(["bridge-charts", ...key], () => {
    if (tab === "chart") return api.bridgeChartArtists(limit);
    if (tab === "geo") return api.bridgeGeoArtists(country, limit);
    return api.bridgeTopArtists(period, limit);
  });
  const artists: ChartArtist[] = data?.artists ?? [];
  const chartUser = (data as { user?: string } | undefined)?.user;

  return (
    <>
      <PageHeader
        title="Чарты Last.fm"
        subtitle="Только чтение: чарты не подмешиваются в рекомендации"
        actions={
          <a href="/cold-start" className="kuma-pill inline-flex items-center gap-1">
            <ArrowLeft className="w-3 h-3" /> К артистам
          </a>
        }
      />
      <Card>
        <div className="flex flex-wrap items-center gap-2">
          <button className="kuma-pill" onClick={() => setTab("chart")} disabled={tab === "chart"}>Глобальный чарт</button>
          <button className="kuma-pill" onClick={() => setTab("geo")} disabled={tab === "geo"}>Топ страны</button>
          <button className="kuma-pill" onClick={() => setTab("user")} disabled={tab === "user"}>Мой топ</button>
          {tab === "geo" && (
            <input className="kuma-input !w-56" value={country} onChange={(e) => setCountry(e.target.value)} placeholder="Russian Federation" />
          )}
          {tab === "user" && (
            <select className="kuma-input !w-auto" value={period} onChange={(e) => setPeriod(e.target.value)}>
              {PERIODS.map((p) => <option key={p} value={p}>{p}</option>)}
            </select>
          )}
          <select className="kuma-input !w-auto" value={limit} onChange={(e) => setLimit(Number(e.target.value))}>
            {[20, 50, 100].map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
          <Button variant="ghost" onClick={() => void mutate()} disabled={isLoading}>
            <RefreshCw className={`w-4 h-4 ${isLoading ? "animate-spin" : ""}`} /> Обновить
          </Button>
        </div>
        {tab === "user" && !chartUser && (
          <div className="text-xs text-muted mt-2">
            Укажи логин Last.fm в Настройки → Мост → «Last.fm пользователь», иначе личный топ недоступен.
          </div>
        )}
        {data?.error && <div className="text-sm text-rose-600 mt-2">{data.error}</div>}
      </Card>

      <Section title={`Артисты (${artists.length})`}>
        {isLoading ? <Skeleton /> : error || artists.length === 0 ? (
          <EmptyState message="Чарт пуст или мост недоступен." />
        ) : (
          <div className="kuma-card divide-y divide-border">
            {artists.map((a, i) => (
              <a
                key={`${a.name}-${i}`}
                href={`/artists/${encodeURIComponent(a.name)}`}
                className="flex items-center gap-3 p-2 hover:bg-surface transition-colors"
                title={a.url}
              >
                <span className="text-xs text-muted tabular-nums w-6">{i + 1}</span>
                {a.image_url ? (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img src={a.image_url} alt="" className="w-10 h-10 rounded object-cover" loading="lazy" />
                ) : (
                  <span className="w-10 h-10 rounded border border-border flex items-center justify-center"><Sparkles className="w-3 h-3 text-muted" /></span>
                )}
                <span className="text-sm flex-1 truncate">{a.name}</span>
                {!!a.listeners && <span className="text-[11px] text-muted tabular-nums">{Number(a.listeners).toLocaleString("ru-RU")} слуш.</span>}
                {!!a.playcount && <span className="text-[11px] text-muted tabular-nums hidden sm:inline">{Number(a.playcount).toLocaleString("ru-RU")} plays</span>}
              </a>
            ))}
          </div>
        )}
      </Section>
    </>
  );
}
