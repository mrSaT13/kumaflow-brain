"use client";

import useSWR from "swr";
import { useEffect, useState } from "react";
import { Check, Download, Heart, History, ListMusic } from "lucide-react";
import { Button, Card } from "@/components/ui";
import { useToast, fmtErr } from "@/components/toasts";
import { api } from "@/lib/api";

/** Яндекс Музыка (Marshal): персонифицированный токен, импорт вкуса/истории/чартов. */
export default function YandexImport() {
  const { data: users } = useSWR("/api/users", () => api.listUsers());
  const [userId, setUserId] = useState("");
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [report, setReport] = useState<string | null>(null);
  const toast = useToast();

  useEffect(() => {
    const list = users?.users ?? [];
    if (!userId && list.length > 0) setUserId(list[0].id);
  }, [users, userId]);

  const { data: tokStatus, mutate: mutateTok } = useSWR(
    userId ? `/api/yandex/tok-${userId}` : null,
    () => api.yandexUserTokenStatus(userId),
  );
  const { data: impSettings, mutate: mutateImp } = useSWR(
    "/api/yandex/import-settings",
    () => api.yandexImportSettings(),
  );
  const { data: corrections, mutate: mutateCorr } = useSWR(
    "/api/yandex/corrections",
    () => api.yandexCorrections(50, 0),
  );
  const { data: charts, mutate: mutateCharts } = useSWR(
    "/api/yandex/charts",
    () => api.yandexCharts(),
  );

  async function run(key: string, fn: () => Promise<unknown>, okMsg: (r: never) => string) {
    setBusy(key);
    setReport(null);
    try {
      const r = (await fn()) as { ok?: boolean; error?: string } & Record<string, unknown>;
      if (r && (r as { ok?: boolean }).ok === false) {
        const msg = `✗ ${(r as { error?: string }).error ?? "ошибка"}`;
        setReport(msg);
        toast(msg, "err");
      } else {
        const msg = okMsg(r as never);
        setReport(`✓ ${msg}`);
        toast(msg, "ok");
      }
    } catch (e: unknown) {
      const msg = fmtErr(e);
      setReport(`✗ ${msg}`);
      toast(msg, "err");
    } finally {
      setBusy(null);
    }
  }

  const settings = impSettings?.settings ?? { history_enabled: true, charts_enabled: true, corrections_enabled: true };

  async function toggle(key: "history_enabled" | "charts_enabled" | "corrections_enabled", v: boolean) {
    try {
      await api.saveYandexImportSettings({ [key]: v });
      mutateImp();
    } catch (e: unknown) {
      toast(fmtErr(e), "err");
    }
  }

  return (
    <Card>
      <div className="text-sm text-muted mb-3">
        Лайки, история и чарты из Яндекс Музыки → во вкус мозга. Токен — OAuth из{" "}
        <a className="kuma-link" href="https://ym.marshal.dev/token/" target="_blank" rel="noreferrer">
          ym.marshal.dev/token
        </a>
        , хранится шифром в базе. Вкус импортируется всегда, история и чарты — за тумблерами.
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <label className="block">
          <div className="text-xs text-muted mb-1">Пользователь мозга</div>
          <select className="kuma-input" value={userId} onChange={(e) => setUserId(e.target.value)}>
            {(users?.users ?? []).map((u) => (
              <option key={u.id} value={u.id}>{u.username}</option>
            ))}
          </select>
        </label>
        <label className="block">
          <div className="text-xs text-muted mb-1">
            OAuth-токен Яндекса {tokStatus?.stored ? "· сохранён ✓" : "· нет"}
          </div>
          <div className="flex gap-2">
            <input
              type="password"
              className="kuma-input flex-1"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              placeholder="y0_Ag…"
            />
            <Button
              disabled={!userId || !token.trim() || busy !== null}
              onClick={() => run("token", () => api.yandexUserTokenSave(userId, token.trim()).then((r) => { setToken(""); mutateTok(); return r; }), (r) => `Токен сохранён${(r as { login?: string }).login ? ` (${(r as { login?: string }).login})` : ""}`)}
            >
              <Check className="w-4 h-4" /> Сохранить
            </Button>
            {tokStatus?.stored && (
              <Button
                variant="ghost"
                disabled={busy !== null}
                onClick={() => run("forget", () => api.yandexUserTokenForget(userId).then((r) => { mutateTok(); return r; }), () => "Токен забыт")}
              >
                Забыть
              </Button>
            )}
          </div>
        </label>
      </div>

      <div className="mt-4 flex gap-2 flex-wrap">
        <Button
          disabled={!userId || busy !== null}
          onClick={() => run("taste", () => api.yandexImportTaste(userId), (r) => {
            const x = r as unknown as Record<string, number>;
            return `Вкус: ♥ ${x.fav_added ?? "?"} · 👎 ${x.dis_added ?? "?"} · совпало ${x.matched ?? "?"} из ${x.liked_total ?? "?"}`;
          })}
        >
          <Heart className="w-4 h-4" /> {busy === "taste" ? "Импортирую…" : "Импорт вкуса"}
        </Button>
        <Button
          variant="ghost"
          disabled={!userId || busy !== null || !settings.history_enabled}
          title={settings.history_enabled ? "Тянем историю прослушиваний" : "Включи тумблер истории ниже"}
          onClick={() => run("history", () => api.yandexImportHistory(userId), (r) => {
            const x = r as unknown as Record<string, number>;
            return `История: событий ${x.events_stored ?? "?"} · в памяти ${x.history_added ?? "?"}`;
          })}
        >
          <History className="w-4 h-4" /> {busy === "history" ? "Тяну…" : "Импорт истории"}
        </Button>
        <Button
          variant="ghost"
          disabled={busy !== null || !settings.charts_enabled}
          onClick={() => run("charts", () => api.yandexChartsRefresh().then((r) => { mutateCharts(); return r; }), (r) => {
            const x = r as unknown as Record<string, number>;
            return `Чарты обновлены: треков ${x.tracks ?? "?"}`;
          })}
        >
          <ListMusic className="w-4 h-4" /> {busy === "charts" ? "Обновляю…" : "Обновить чарты"}
        </Button>
      </div>

      <div className="mt-4 flex gap-4 flex-wrap text-sm">
        <label className="flex items-center gap-1.5 cursor-pointer">
          <input
            type="checkbox"
            checked={settings.history_enabled}
            onChange={(e) => toggle("history_enabled", e.target.checked)}
          />
          Импорт истории
        </label>
        <label className="flex items-center gap-1.5 cursor-pointer">
          <input
            type="checkbox"
            checked={settings.charts_enabled}
            onChange={(e) => { toggle("charts_enabled", e.target.checked); mutateCharts(); }}
          />
          Чарты/новинки
        </label>
        <label className="flex items-center gap-1.5 cursor-pointer" title="Фоновая задача enrich правит год/жанр/альбом по данным Яндекса">
          <input
            type="checkbox"
            checked={(settings as { corrections_enabled?: boolean }).corrections_enabled ?? true}
            onChange={(e) => toggle("corrections_enabled", e.target.checked)}
          />
          Автокоррекция метаданных
        </label>
      </div>

      {(corrections?.items?.length ?? 0) > 0 && (
        <div className="mt-4">
          <div className="text-xs uppercase tracking-wider text-muted mb-2">
            Правки Яндекса · {corrections?.total} — было → стало, можно откатить
          </div>
          <div className="space-y-1.5 max-h-64 overflow-auto">
            {(corrections?.items ?? []).map((c) => (
              <div key={c.track_id} className="text-xs flex gap-2 items-baseline flex-wrap">
                <a href={`/track/${c.track_id}`} className="kuma-link font-medium shrink-0">
                  {c.artist_name ? `${c.artist_name} — ` : ""}{c.title}
                </a>
                {Object.entries(c.corrected).map(([f, [oldV, newV]]) => (
                  <span key={f} className="rounded-full border border-border px-1.5 py-0.5 tabular-nums text-muted">
                    {f}: {String(oldV ?? "—")} → {String(newV ?? "—")}
                  </span>
                ))}
                <button
                  className="kuma-pill !py-0.5 !px-2 text-[11px]"
                  disabled={busy !== null}
                  onClick={() => run(`revert-${c.track_id}`, () => api.yandexRevertCorrection(c.track_id).then((r) => { mutateCorr(); return r; }), () => `Откачено: ${c.title}`)}
                >
                  {busy === `revert-${c.track_id}` ? "…" : "Откатить"}
                </button>
              </div>
            ))}
          </div>
        </div>
      )}

      {charts && (charts.tracks?.length ?? 0) > 0 && (
        <div className="mt-4">
          <div className="text-xs uppercase tracking-wider text-muted mb-2">
            Чарт Яндекса · {charts.in_library} из {charts.total} уже в библиотеке
            {charts.fetched_at && <span className="normal-case"> · {new Date(charts.fetched_at).toLocaleString("ru-RU")}</span>}
          </div>
          <div className="space-y-1.5 max-h-64 overflow-auto">
            {charts.tracks.slice(0, 30).map((t, i) => (
              <div key={`${t.title}-${i}`} className="text-xs flex gap-2 items-baseline">
                <span className="text-muted tabular-nums w-6 shrink-0">{i + 1}</span>
                <span className="font-medium truncate">{t.artist ? `${t.artist} — ` : ""}{t.title}</span>
                {t.track_id ? (
                  <span className="text-emerald-600 shrink-0">· в библиотеке</span>
                ) : (
                  <span className="text-muted shrink-0">· {t.origin === "chart" ? "чарт" : "новинка"}</span>
                )}
              </div>
            ))}
          </div>
          <Button
            variant="ghost"
            className="mt-3"
            disabled={!userId || busy !== null || charts.in_library === 0}
            onClick={() => run("seed", async () => {
              const ids = charts.tracks.filter((t) => t.track_id).map((t) => t.track_id as string).slice(0, 30);
              const artists = [...new Set(charts.tracks.filter((t) => t.track_id).map((t) => t.artist).filter(Boolean))].slice(0, 10) as string[];
              return api.seedTaste(userId, { genres: [], artists, track_ids: ids });
            }, (r) => {
              const x = r as unknown as Record<string, number>;
              return `Засеяно из чарта: +${x.favorites_added ?? "?"} в избранное`;
            })}
          >
            <Download className="w-4 h-4" /> {busy === "seed" ? "Сею…" : "Засеять вкус из чарта (cold start)"}
          </Button>
        </div>
      )}

      {report && (
        <div className="mt-3 text-sm p-3 rounded-lg border border-border bg-surface whitespace-pre-wrap">{report}</div>
      )}
    </Card>
  );
}
