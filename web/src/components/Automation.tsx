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
  const { data, error, mutate } = useSWR("/api/cron", () => api.listCron(), { refreshInterval: 5000 });
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
  // 401/403 — это НЕ упавший backend, а токен: его нет в этом браузере,
  // он удалён/выключен или выпущен для другого backend. Прямая проверка
  // /api/cron/ в адресной строке всегда даст 401 — браузер не шлёт Bearer.
  const cronErrMsg = error ? fmtErr(error) : "";
  const cronAuthErr = /^40[13]:/.test(cronErrMsg);
  const { data: clap } = useSWR("/api/analysis/clap-status", () => api.clapStatus(), { refreshInterval: 30000 });
  const { data: auto, mutate: mutateAuto } = useSWR("/api/settings/automation", () => api.getAutomation(), { refreshInterval: 10000 });
  const clapN = Object.values(clap?.embeddings ?? {}).reduce((s, n) => s + (n || 0), 0);

  async function toggleLyrics(key: "analysis_fetch_lyrics" | "analysis_ai_mood" | "playlists_push_navidrome" | "clap_enabled" | "clap_audio_enabled") {
    const cur = key === "analysis_fetch_lyrics"
      ? (auto?.flags?.analysis_fetch_lyrics ?? true)
      : key === "analysis_ai_mood"
        ? (auto?.flags?.analysis_ai_mood ?? true)
        : key === "clap_enabled"
          ? (auto?.flags?.clap_enabled ?? true)
          : key === "clap_audio_enabled"
            ? (auto?.flags?.clap_audio_enabled ?? true)
            : (auto?.flags?.playlists_push_navidrome ?? false);
    const label = key === "analysis_fetch_lyrics"
      ? "Тексты"
      : key === "analysis_ai_mood"
        ? "AI-настроение"
        : key === "clap_enabled"
          ? "CLAP-поиск по смыслу"
          : key === "clap_audio_enabled"
            ? "CLAP-аудио-гибрид"
            : "Авто-пуш плейлистов";
    setBusy(true);
    try {
      await api.saveAutomation({ [key]: !cur });
      mutateAuto();
      toast(!cur ? `${label} включены ✓` : `${label} выключены`, !cur ? "ok" : "info");
    } catch (e: unknown) {
      toast(fmtErr(e), "err");
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
    <Card>
      <div className="flex items-center gap-2 flex-wrap text-sm">
        <span className="font-medium">CLAP-модель</span>
        <Badge tone={clap?.available ? "ok" : "warn"}>
          {clap
            ? clap.available
              ? "в образе"
              : (clap.files?.length ?? 0) > 0
                ? "файлы есть, флаг выкл"
                : "нет в образе"
            : "…"}
        </Badge>
        {clap && <span className="text-xs text-muted">эмбеддингов в базе: {clapN}</span>}
        {clap && (
          <Badge tone={clap.audio_available && clap.audio_enabled ? "ok" : "default"}>
            {clap.audio_available && clap.audio_enabled
              ? `audio — гибрид ×${clap.audio_weight ?? 0.7}`
              : clap.audio_available
                ? "audio — модель есть, флаг выкл"
                : "audio — нет модели"}
          </Badge>
        )}
      </div>
      {(clap?.files?.length ?? 0) > 0 && (
        <div className="text-[11px] text-muted mt-1" title={clap?.models_dir || undefined}>
          файлы: {clap!.files.map((f) => `${f.name} ${(f.bytes / 1048576).toFixed(0)}МБ`).join(" · ")}
        </div>
      )}
      <div className="text-xs text-muted mt-2">
        {clap && !clap.available && (clap.files?.length ?? 0) > 0
          ? "Файлы запечены в образ, но тумблер CLAP выключен. Включи его ниже — пересборка и перезапуск контейнеров не нужны."
          : clap && !clap.available
            ? "Текстовой модели нет — обновите образ (CLAP запечён в backend-образ, см. deploy/Dockerfile.server): docker compose pull && docker compose up -d. Модель подтягивается при сборке образа, не на сервере."
            : "Если модель есть, а эмбеддингов 0 — запустите задачу «CLAP-эмбеддинги» кнопкой «сейчас» ниже (текст), аудио-эмбеддинги считаются следом за sonic-анализом и задачей «clap-audio»."}
      </div>
      <div className="mt-3 pt-3 border-t border-border space-y-2">
        <label className="flex items-center gap-3 cursor-pointer">
          <input
            type="checkbox"
            checked={auto?.flags?.clap_enabled ?? true}
            disabled={busy}
            onChange={() => toggleLyrics("clap_enabled")}
            className="w-4 h-4 accent-black dark:accent-white shrink-0"
          />
          <span>
            <span className="font-medium text-sm">CLAP — поиск по смыслу</span>
            <span className="text-xs text-muted block">
              Текстовые эмбеддинги: поиск по описанию, «Открытия недели», подбор похожего по смыслу.
              Выключается здесь же, без правки docker-compose и перезапуска контейнеров.
              Уже посчитанные эмбеддинги в базе сохраняются.
            </span>
          </span>
        </label>
        <label className="flex items-center gap-3 cursor-pointer">
          <input
            type="checkbox"
            checked={auto?.flags?.clap_audio_enabled ?? true}
            disabled={busy}
            onChange={() => toggleLyrics("clap_audio_enabled")}
            className="w-4 h-4 accent-black dark:accent-white shrink-0"
          />
          <span>
            <span className="font-medium text-sm">CLAP — аудио-гибрид</span>
            <span className="text-xs text-muted block">
              Считает вектор по самому звуку при sonic-анализе и подмешивает его в подбор «похожего»
              и в волну. Тяжелее всего по CPU — имеет смысл выключать на слабых машинах.
            </span>
          </span>
        </label>
      </div>
    </Card>
    <div className="h-4" />
    <Card>
      <label className="flex items-center gap-3 cursor-pointer">
        <input
          type="checkbox"
          checked={auto?.flags?.analysis_fetch_lyrics ?? true}
          disabled={busy}
          onChange={() => toggleLyrics("analysis_fetch_lyrics")}
          className="w-4 h-4 accent-black dark:accent-white shrink-0"
        />
        <span>
          <span className="font-medium text-sm">Тексты следом за анализом</span>
          <span className="text-xs text-muted block">
            Каждый проанализированный трек сразу тянет текст (LRCLIB). Быстро, без AI.
          </span>
        </span>
      </label>
      <div className="h-3" />
      <label className="flex items-center gap-3 cursor-pointer">
        <input
          type="checkbox"
          checked={auto?.flags?.analysis_ai_mood ?? true}
          disabled={busy}
          onChange={() => toggleLyrics("analysis_ai_mood")}
          className="w-4 h-4 accent-black dark:accent-white shrink-0"
        />
        <span>
          <span className="font-medium text-sm">AI-настроение текста</span>
          <span className="text-xs text-muted block">
            Прогоняет текст через AI (moods + темы) и добирает в настроение трека. Акустику (energy/valence) не перезаписывает. Без AI-провайдера — no-op. Медленнее: до ~10с на трек.
          </span>
        </span>
      </label>
      <div className="h-3" />
      <label className="flex items-center gap-3 cursor-pointer">
        <input
          type="checkbox"
          checked={auto?.flags?.playlists_push_navidrome ?? false}
          disabled={busy}
          onChange={() => toggleLyrics("playlists_push_navidrome")}
          className="w-4 h-4 accent-black dark:accent-white shrink-0"
        />
        <span>
          <span className="font-medium text-sm">Авто-отправка плейлистов в Navidrome</span>
          <span className="text-xs text-muted block">
            Daily, умные и открытия недели после генерации сразу выгружаются в Navidrome (видны в родном клиенте). Треки с диска пропускаются.
          </span>
        </span>
      </label>
    </Card>
    <div className="h-4" />
    <Card>
      {error && !data ? (
        <div className="text-sm">
          <div className="text-red-500 font-medium">Не смог загрузить задачи: {cronErrMsg}</div>
          <div className="text-xs text-muted mt-1">
            {cronAuthErr ? (
              <>Браузер не передал валидный brain-токен (адресная строка его тоже не шлёт —
                проверка <code className="kuma-pill">/api/cron/health</code> в браузере всегда даст 401).
                Выйдите и войдите заново, или вставьте токен в Настройки → Токены. Проверка токена:{" "}
                <code className="kuma-pill">curl -H &quot;Authorization: Bearer $TOKEN&quot; http://&lt;host&gt;:8000/api/settings/whoami</code></>
            ) : (
              <>Если backend работает (анализ идёт, «Моя волна» открывается) — смотри логи backend:{" "}
                <code className="kuma-pill">docker compose logs backend --tail=50</code></>
            )}
          </div>
          <div className="mt-2">
            <Button variant="ghost" onClick={() => mutate()} disabled={busy}>Повторить</Button>
          </div>
        </div>
      ) : !data ? (
        <div className="text-sm text-muted">Загрузка…</div>
      ) : jobs.length === 0 ? (
        <div className="text-sm text-muted">
          Задач нет. Они создаются автоматически при старте backend и scheduler —
          перезапусти контейнеры:{" "}
          <code className="kuma-pill">docker compose restart backend scheduler</code>
        </div>
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
                      {j.last_status === "error" && <Badge tone="err">ошибка</Badge>}
                      {j.enabled && !j.last_run_at && <Badge tone="warn">ещё не запускалась</Badge>}
                    </span>
                    <span className="text-xs text-muted block">{ru.desc}</span>
                    {j.last_run_at && (
                      <span className="text-[11px] text-muted block">
                        последний запуск: {fmtDate(j.last_run_at)}
                        {typeof j.run_count === "number" ? ` · всего ${j.run_count}` : ""}
                        {j.fail_count ? ` · сорвано ${j.fail_count}` : ""}
                      </span>
                    )}
                    {j.next_run_at && j.enabled && (
                      <span className="text-[11px] text-muted block">следующий: {fmtDate(j.next_run_at)}</span>
                    )}
                    {j.last_status === "error" && j.last_error && (
                      <span className="text-[11px] text-red-500 block">ошибка запуска: {j.last_error}</span>
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
