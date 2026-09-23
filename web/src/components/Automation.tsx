"use client";

import useSWR from "swr";
import { useState } from "react";
import { Play, Save } from "lucide-react";
import { Badge, Button, Card } from "@/components/ui";
import { useToast, fmtErr } from "@/components/toasts";
import { api } from "@/lib/api";
import { fmtDate } from "@/lib/format";

const KIND_RU: Record<string, { title: string; desc: string }> = {
  daily: { title: "Daily каждому", desc: "KumaFlow Daily персонально каждому пользователю по его вкусам (03:00)" },
  refresh_tastes: { title: "Вкусы из Navidrome", desc: "Ночное автообновление лайков/плейлистов по запомненным паролям (04:30)" },
  clap: { title: "CLAP-эмбеддинги", desc: "Аудио-вектора для «Открытий недели» (04:00)" },
  smart: { title: "Умные плейлисты", desc: "Открытия · Забытые любимые · Ночь · Спорт — каждому по его вкусам (06:00)" },
  snapshots: { title: "Слепки вкуса", desc: "Недельные снапшоты для радара дрейфа, тихо (вс 07:00)" },
  weekly: { title: "Открытия недели", desc: "CLAP-открытия персонально каждому (пн 06:00)" },
  covers_gc: { title: "Чистка обложек", desc: "Удаление старого кэша обложек старше 7 дней (вс 05:00)" },
};

/** Вкладка «Автоматизация»: все кроны с тумблерами, расписанием и ручным запуском. */
export default function Automation() {
  const { data, mutate } = useSWR("/api/cron", () => api.listCron(), { refreshInterval: 5000 });
  const [busy, setBusy] = useState(false);
  const [exprs, setExprs] = useState<Record<string, string>>({});
  const toast = useToast();

  async function toggle(id: string, enabled: boolean) {
    setBusy(true);
    try {
      await api.updateCron(id, { enabled: !enabled });
      mutate();
    } catch (e: unknown) {
      toast(fmtErr(e), "err");
    } finally {
      setBusy(false);
    }
  }

  async function saveExpr(id: string, fallback: string) {
    const expr = (exprs[id] ?? fallback).trim();
    if (!expr) return;
    setBusy(true);
    try {
      await api.updateCron(id, { cron_expr: expr });
      toast("Расписание сохранено ✓", "ok");
      mutate();
    } catch (e: unknown) {
      toast(fmtErr(e), "err");
    } finally {
      setBusy(false);
    }
  }

  async function run(id: string, name: string) {
    setBusy(true);
    try {
      const r = await api.runCron(id);
      if (!r.queued) toast(`Не запустилось: ${r.error ?? "неизвестная"}`, "err");
      else toast(`«${name}» запущено в фоне — следите в «Задачи и логи».`, "ok");
      mutate();
    } catch (e: unknown) {
      toast(fmtErr(e), "err");
    } finally {
      setBusy(false);
    }
  }

  const jobs = data?.jobs ?? [];
  const { data: clap } = useSWR("/api/analysis/clap-status", () => api.clapStatus(), { refreshInterval: 30000 });
  const clapN = Object.values(clap?.embeddings ?? {}).reduce((s, n) => s + (n || 0), 0);
  return (
    <>
    <Card>
      <div className="flex items-center gap-2 flex-wrap text-sm">
        <span className="font-medium">CLAP-модель</span>
        <Badge tone={clap?.available ? "ok" : "warn"}>
          {clap ? (clap.available ? "в образе" : "нет в образе") : "…"}
        </Badge>
        {clap && <span className="text-xs text-muted">эмбеддингов в базе: {clapN}</span>}
        <Badge tone="warn">audio — стаб</Badge>
      </div>
      <div className="text-xs text-muted mt-2">
        {clap && !clap.available
          ? "Модели нет — «Открытия недели» считаются как cold-start (не семантика, а вкус+поведение). Причина обычно: сборка без сети (слой закэшировал пропуск) — пересоберите образ с доступом к HuggingFace."
          : "Если модель есть, а эмбеддингов 0 — запустите задачу «CLAP-эмбеддинги» кнопкой «сейчас» ниже."}
      </div>
    </Card>
    <div className="h-4" />
    <Card>
      {!data ? (
        <div className="text-sm text-muted">Загрузка…</div>
      ) : jobs.length === 0 ? (
        <div className="text-sm text-muted">Задач нет.</div>
      ) : (
        <div className="divide-y divide-border">
          {jobs.map((j) => {
            const ru = KIND_RU[j.kind] ?? { title: j.name, desc: `kind: ${j.kind}` };
            return (
              <div key={j.id} className="py-3 flex flex-col md:flex-row md:items-center gap-2">
                <label className="flex items-center gap-3 cursor-pointer min-w-0 flex-1">
                  <input
                    type="checkbox"
                    checked={j.enabled}
                    disabled={busy}
                    onChange={() => toggle(j.id, j.enabled)}
                    className="w-4 h-4 accent-black dark:accent-white shrink-0"
                  />
                  <span className="min-w-0">
                    <span className="font-medium text-sm flex items-center gap-2 flex-wrap">
                      {ru.title}
                      <Badge tone={j.enabled ? "ok" : "default"}>{j.enabled ? "вкл" : "выкл"}</Badge>
                    </span>
                    <span className="text-xs text-muted block">{ru.desc}</span>
                    {j.last_run_at && (
                      <span className="text-[11px] text-muted block">последний запуск: {fmtDate(j.last_run_at)}</span>
                    )}
                  </span>
                </label>
                <div className="flex items-center gap-2 shrink-0">
                  <input
                    className="kuma-input kuma-input-inline w-32 text-xs"
                    value={exprs[j.id] ?? j.cron_expr}
                    onChange={(e) => setExprs((m) => ({ ...m, [j.id]: e.target.value }))}
                    title="cron-выражение (мин час день месяц день-недели)"
                  />
                  <Button variant="ghost" onClick={() => saveExpr(j.id, j.cron_expr)} disabled={busy} title="Сохранить расписание">
                    <Save className="w-3 h-3" />
                  </Button>
                  <Button variant="ghost" onClick={() => run(j.id, ru.title)} disabled={busy || !j.enabled} title="Запустить сейчас в фоне">
                    <Play className="w-3 h-3" /> сейчас
                  </Button>
                </div>
              </div>
            );
          })}
        </div>
      )}
      <div className="text-xs text-muted mt-3">
        Тумблер — вкл/выкл задачу. Расписание — cron «мин час * * день-недели». «Сейчас» ставит задачу в фон (прогресс в «Задачи и логи»). Всё персональное (daily, smart, открытия) считается отдельно под каждого пользователя.
      </div>
    </Card>
    </>
  );
}
