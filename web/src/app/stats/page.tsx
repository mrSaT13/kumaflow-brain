"use client";

import useSWR from "swr";
import Link from "next/link";
import { useState } from "react";
import { TrendingUp } from "lucide-react";
import { Card, PageHeader, Section, Badge } from "@/components/ui";
import { api } from "@/lib/api";
import { useCurrentUser } from "@/lib/useCurrentUser";

const RANGES = [
  { days: 1, label: "сегодня" },
  { days: 7, label: "7 дней" },
  { days: 30, label: "30 дней" },
  { days: 90, label: "90 дней" },
];

const OUTCOME_RU: Record<string, string> = {
  played: "слушали",
  early_skip: "бросили за 30 с",
  skipped: "переключили",
  completed: "дослушали",
  liked: "лайкнули",
  disliked: "задизлайкли",
  abandoned: "бросили трек",
  no_signal: "нет сигнала",
};

function fmtSec(s: number | null | undefined): string {
  if (s == null) return "—";
  const m = Math.floor(s / 60);
  return `${m}:${String(Math.round(s % 60)).padStart(2, "0")}`;
}

function Stat({ label, value, hint, tone }: { label: string; value: string; hint?: string; tone?: "ok" | "warn" | "err" | "default" }) {
  return (
    <div className="min-w-0">
      <div className="text-[11px] uppercase tracking-wider text-muted">{label}</div>
      <div className="mt-1"><Badge tone={tone ?? "default"}>{value}</Badge></div>
      {hint && <div className="text-[11px] text-muted mt-1">{hint}</div>}
    </div>
  );
}

function Bar({ pct, tone }: { pct: number; tone: string }) {
  const w = Math.max(0, Math.min(100, pct));
  const color = tone === "bad" ? "bg-red-500" : tone === "good" ? "bg-green-500" : "bg-sky-500";
  return (
    <div className="h-full rounded-full bg-border/60">
      <div className={`h-full rounded-full ${color} kuma-bar-anim`} style={{ width: `${w}%` }} />
    </div>
  );
}

export default function StatsPage() {
  const { users } = useCurrentUser();
  // "" = все пользователи. По умолчанию показываем сводку по всем, а не по
  // неявному выбору из шапки: иначе непонятно, чьи это метрики.
  const [who, setWho] = useState<string>("");
  const [days, setDays] = useState(7);
  const [source, setSource] = useState<string>("");
  const { data, error, isLoading } = useSWR(
    ["/api/wave/feedback", who, days, source],
    () => api.waveFeedback({ user_id: who || undefined, days, source: source || undefined }),
    { refreshInterval: 60000 },
  );

  const has = (data?.decided ?? 0) > 0;

  return (
    <>
      <PageHeader
        title="Статистика волны"
        subtitle="Что мозг предлагал и что из этого вышло. Нужно, чтобы видеть, стали ли рекомендации точнее, а не гадать."
        actions={
          <div className="flex items-center gap-2 flex-wrap">
            {RANGES.map((r) => (
              <button
                key={r.days}
                onClick={() => setDays(r.days)}
                className={`kuma-pill ${days === r.days ? "bg-accent text-bg border-accent" : "hover:text-text"}`}
              >
                {r.label}
              </button>
            ))}
            {/* Метрики считаются per-user: у каждого своя волна, свои скипы и
                свои лайки. Раньше фильтр был неявным — брался глобальный выбор
                пользователя из шапки, и «посмотреть всех» было невозможно,
                поэтому непонятно было, чьи это цифры. */}
            <select
              value={who}
              onChange={(e) => setWho(e.target.value)}
              className="kuma-input kuma-input-inline text-xs"
              title="Чьи показы считать"
            >
              <option value="">все пользователи</option>
              {users.map((u) => (
                <option key={u.id} value={u.id}>{u.username || u.id.slice(0, 8)}</option>
              ))}
            </select>
            <select
              value={source}
              onChange={(e) => setSource(e.target.value)}
              className="kuma-input kuma-input-inline text-xs"
              title="Источник рекомендаций"
            >
              <option value="">все источники</option>
              {(data?.by_source ?? []).map((s) => (
                <option key={s.source} value={s.source}>{s.source}</option>
              ))}
              <option value="wave">wave</option>
              <option value="daily">daily</option>
              <option value="smart">smart</option>
              <option value="weekly">weekly</option>
            </select>
          </div>
        }
      />

      {error ? (
        <Section title="">
          <Card>
            <div className="text-sm text-red-500">Не смог загрузить метрики: {String((error as Error)?.message ?? error)}</div>
            <div className="text-xs text-muted mt-1">
              Нужна пересборка образа: таблица recommendation_feedback и endpoint
              <code className="kuma-pill">/api/wave/feedback</code> появились в 0.2.5.
            </div>
          </Card>
        </Section>
      ) : isLoading ? (
        <Card><div className="text-sm text-muted">Загрузка…</div></Card>
      ) : !has ? (
        <Card>
          <div className="text-sm">Пока нет данных, по которым можно судить.</div>
          <div className="text-xs text-muted mt-1">
            Метрика появляется, когда мозг что-то предлагает <b>и</b> клиент сообщает, что с этим
            треком произошло. Покажется после событий от плеера — позиция приходит из{" "}
            <code className="kuma-pill">position_sec</code>.
            {" "}Показано показов: {data?.served ?? 0}, из них с сигналом: {data?.decided ?? 0}.
          </div>
          {(data?.served ?? 0) > 0 && (data?.decided ?? 0) === 0 && (
            <div className="text-xs text-amber-500 mt-2">
              Показы идут, а сигналов нет — значит клиент не присылает события прослушивания
              (<code className="kuma-pill">POST /api/users/{"{id}"}/events</code> с{" "}
              <code className="kuma-pill">position_sec</code>). Сама механика подсказок при этом
              работает, но оценивать её точность пока не по чему.
            </div>
          )}
        </Card>
      ) : (
        <>
          <Section title="Главное">
            <Card>
              <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
                <Stat
                  label="Скип < 30 с"
                  value={`${data!.early_skip_rate}%`}
                  tone={data!.early_skip_rate > 40 ? "err" : data!.early_skip_rate > 25 ? "warn" : "ok"}
                  hint="Главный признак, что трек не зашёл"
                />
                <Stat
                  label="Дослушали"
                  value={`${data!.completion_rate}%`}
                  tone={data!.completion_rate > 40 ? "ok" : data!.completion_rate > 20 ? "warn" : "err"}
                  hint="Трек доигран целиком"
                />
                <Stat
                  label="Лайк с волны"
                  value={`${data!.like_rate}%`}
                  tone={data!.like_rate > 3 ? "ok" : "default"}
                  hint="Самый ценный сигнал попадания"
                />
                <Stat
                  label="Средняя позиция скипа"
                  value={fmtSec(data!.avg_early_skip_sec)}
                  hint="Насколько глубоко бросали"
                />
              </div>
              <div className="text-[11px] text-muted mt-3">
                Показов: {data!.served} · с сигналом: {data!.decided} · без сигнала: {data!.pending}
              </div>
            </Card>
          </Section>

          <div className="h-4" />
          <Section title="Что происходило">
            <Card>
              <div className="space-y-2">
                {Object.entries(data!.counts)
                  .sort((a, b) => b[1] - a[1])
                  .map(([k, v]) => (
                    <div key={k} className="flex items-center gap-3">
                      <span className="text-xs w-40 shrink-0 truncate">{OUTCOME_RU[k] ?? k}</span>
                      <div className="flex-1 h-2">
                        <Bar pct={data!.decided ? (v / data!.decided) * 100 : 0}
                          tone={k === "liked" || k === "completed" ? "good" : k === "early_skip" ? "bad" : "other"} />
                      </div>
                      <span className="text-xs text-muted w-16 text-right tabular-nums">
                        {v} · {data!.decided ? Math.round((v / data!.decided) * 100) : 0}%
                      </span>
                    </div>
                  ))}
              </div>
            </Card>
          </Section>

          <div className="h-4" />
          <Section title="Работает ли ранжир (позиция в выдаче)">
            <Card>
              {data!.by_score.length === 0 ? (
                <div className="text-sm text-muted">
                  Пока нет строк с позицией rank — пополни волну, и здесь появится разбивка.
                </div>
              ) : (
                <table className="kuma-table w-full text-xs">
                  <thead>
                    <tr>
                      <th>Позиция</th>
                      <th>Показов</th>
                      <th>Скип &lt; 30 с</th>
                      <th>Дослушали</th>
                      <th>Лайки</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data!.by_score.map((b) => (
                      <tr key={b.bucket}>
                        <td>{b.bucket}</td>
                        <td className="tabular-nums">{b.served}</td>
                        <td className="tabular-nums">{b.early_skip_rate}%</td>
                        <td className="tabular-nums">{b.completion_rate}%</td>
                        <td className="tabular-nums">{b.like_rate}%</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
              <div className="text-[11px] text-muted mt-2">
                Смысл: топ-3 должен скипаться реже хвоста (11+). Если разницы нет —
                ранжирующая часть скоринга в <code className="kuma-pill">wave.py</code> не работает,
                и крутить её бессмысленно. Раньше здесь были бакеты по score, но волна пишет
                растянутый скор окна, а плейлисты — сырой: 1.0 одного окна ≠ 1.0 другого.
              </div>
            </Card>
          </Section>

          <div className="h-4" />
          <Section title="Какие причины работают">
            <Card>
              {data!.by_reason.length === 0 ? (
                <div className="text-sm text-muted">Причины появятся, когда мозг начнёт объяснять выбор (поле reason).</div>
              ) : (
                <div className="space-y-2">
                  {data!.by_reason.map((r) => (
                    <div key={r.reason} className="flex items-center gap-3">
                      <span className="text-xs w-56 shrink-0 truncate" title={r.reason}>{r.reason}</span>
                      <div className="flex-1 h-2">
                        <Bar pct={r.served ? ((r.early_skip + r.completed + r.liked) / r.served) * 100 : 0} tone="good" />
                      </div>
                      <span className="text-xs text-muted w-32 text-right tabular-nums">
                        {r.served} · скип {r.served ? Math.round((r.early_skip / r.served) * 100) : 0}%
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </Card>
          </Section>

          <div className="h-4" />
          <Section title="По источникам">
            <Card>
              <table className="kuma-table w-full text-xs">
                <thead>
                  <tr>
                    <th>Источник</th>
                    <th>Показов</th>
                    <th>Скип &lt; 30 с</th>
                    <th>Дослушали</th>
                    <th>Лайки</th>
                  </tr>
                </thead>
                <tbody>
                  {data!.by_source.map((s) => (
                    <tr key={s.source}>
                      <td>{s.source}</td>
                      <td className="tabular-nums">{s.served}</td>
                      <td className="tabular-nums">{s.served ? Math.round((s.early_skip / s.served) * 100) : 0}%</td>
                      <td className="tabular-nums">{s.served ? Math.round((s.completed / s.served) * 100) : 0}%</td>
                      <td className="tabular-nums">{s.served ? Math.round((s.liked / s.served) * 100) : 0}%</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Card>
          </Section>

          <div className="text-xs text-muted mt-4 flex items-center gap-2">
            <TrendingUp className="w-3 h-3" />
            Данные накопительные: первые дни проценты нестабильны. Сравнивать имеет смысл
            периоды одинаковой длины — <Link href="/wave" className="kuma-link underline">Моя волна</Link>.
          </div>
        </>
      )}
    </>
  );
}
