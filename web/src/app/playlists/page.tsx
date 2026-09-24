"use client";

import useSWR from "swr";
import Link from "next/link";
import { useEffect, useState } from "react";
import { ListMusic, Play, RefreshCw, Send, Sparkles, Trash2, Wand2, Eye, EyeOff, Search } from "lucide-react";
import { Badge, Button, Card, EmptyState, Input, PageHeader, Section } from "@/components/ui";
import { useConfirm } from "@/components/dialog";
import { useToast, fmtErr } from "@/components/toasts";
import { api } from "@/lib/api";
import { useCurrentUser } from "@/lib/useCurrentUser";
import { fmtDate } from "@/lib/format";

function usePersisted<T>(key: string, initial: T): [T, (v: T) => void] {
  const [val, setVal] = useState<T>(initial);
  useEffect(() => {
    try {
      const raw = localStorage.getItem(key);
      if (raw !== null) setVal(JSON.parse(raw) as T);
    } catch { /* ignore */ }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  const set = (v: T) => {
    setVal(v);
    try {
      localStorage.setItem(key, JSON.stringify(v));
    } catch { /* приватный режим */ }
  };
  return [val, set];
}

export default function PlaylistsPage() {
  const [confirmNode, confirm] = useConfirm();
  const toast = useToast();
  const [showHidden, setShowHidden] = usePersisted<boolean>("kf:pl:showHidden", false);
  const { data, mutate } = useSWR(["/api/playlists", showHidden], () => api.listPlaylists(showHidden), { refreshInterval: 4000 });
  const { users: usersList, userId: coldUser, setUserId: setColdUser } = useCurrentUser();
  const usersData = { users: usersList };
  const [busy, setBusy] = useState(false);
  const [coldN, setColdN] = useState(30);
  const [aiQuery, setAiQuery] = useState("");
  const [plQuery, setPlQuery] = usePersisted<string>("kf:pl:q", "");
  const [plFilter, setPlFilter] = usePersisted<"all" | "auto" | "manual" | "hidden">("kf:pl:filter", "all");
  const [plOwner, setPlOwner] = usePersisted<string>("kf:pl:owner", "");
  const [lastSteps, setLastSteps] = useState<{ step: number; name: string; items: number }[] | null>(null);
  const [weekly, setWeekly] = useState<{ track_id: string; score: number; because_of_title?: string | null; text: string }[]>([]);

  const visiblePlaylists = (data?.playlists ?? []).filter((p) => {
    if (plFilter === "auto" && !p.is_auto_generated) return false;
    if (plFilter === "manual" && p.is_auto_generated) return false;
    if (plFilter === "hidden" && !p.is_hidden) return false;
    if (plFilter !== "hidden" && !showHidden && p.is_hidden) return false;
    if (plOwner && (p.owner_user_id ?? "") !== plOwner && (p.owner_username ?? "") !== plOwner) return false;
    const q = plQuery.trim().toLowerCase();
    if (q && !p.name.toLowerCase().includes(q)) return false;
    return true;
  });

  async function generate() {
    setBusy(true);
    try {
      const r = await api.generateDailyPlaylist(coldN || 30, coldUser || undefined);
      setLastSteps(r.steps ?? null);
      mutate();
      toast("Daily готов — откройте плейлист ниже.", "ok");
    } catch (e: unknown) {
      toast(fmtErr(e), "err");
    } finally {
      setBusy(false);
    }
  }

  async function coldStartOnly() {
    setBusy(true);
    try {
      const r = await api.coldStart(coldN || 30);
      setLastSteps(r.steps ?? null);
      toast(`Cold-start: кандидатов ${r.tracks.length}. Чтобы сохранить — «Пройти холодный старт».`, "info");
    } catch (e: unknown) {
      toast(fmtErr(e), "err");
    } finally {
      setBusy(false);
    }
  }

  async function aiMix() {
    if (!aiQuery.trim()) { toast("Опишите настроение/жанр — например «вечерний лоуфай для работы»", "info"); return; }
    setBusy(true);
    try {
      const r = await api.aiGenerate(aiQuery.trim(), coldN || 30, coldUser || undefined);
      mutate();
      toast(`Готово: «${r.name}» — треков ${r.tracks}${r.from_fallback ? " (без AI, keyword-подбор)" : ""}.`, "ok");
    } catch (e: unknown) {
      toast(fmtErr(e), "err");
    } finally {
      setBusy(false);
    }
  }

  async function myWave() {
    const uid = coldUser || (usersData?.users ?? [])[0]?.id;
    if (!uid) { toast("Сначала добавьте пользователя на странице «Пользователи»", "info"); return; }
    setBusy(true);
    try {
      const r = await api.myWave(uid, coldN || 30);
      mutate();
      toast(`Моя волна готова: треков ${r.tracks} (убрано дизлайков ${r.excluded_disliked}, банов ${r.excluded_banned}).`, "ok");
    } catch (e: unknown) {
      toast(fmtErr(e), "err");
    } finally {
      setBusy(false);
    }
  }

  async function del(id: string) {
    if (!(await confirm({ title: "Удалить плейлист?", message: "Копия в Navidrome тоже будет удалена, если выгружалась.", confirmText: "Удалить", danger: true }))) return;
    await api.deletePlaylist(id);
    mutate();
  }

  async function hide(id: string) {
    await api.hidePlaylist(id);
    mutate();
  }

  async function unhide(id: string) {
    await api.unhidePlaylist(id);
    mutate();
  }

  async function exportOne(id: string, name: string) {
    setBusy(true);
    try {
      const r = await api.exportPlaylist(id);
      if (!r.ok) toast(`Ошибка: ${r.error ?? "неизвестная"}`, "err");
      else {
        toast(`«${name}» теперь в Navidrome: треков ${r.exported ?? 0}${r.skipped ? ` (пропущено файлов с диска: ${r.skipped})` : ""}.`, "ok");
        mutate();
      }
    } catch (e: unknown) {
      toast(fmtErr(e), "err");
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      {confirmNode}
      <PageHeader
        title="Плейлисты"
        subtitle="Ежедневный плейлист пересоздаётся автоматически (если уже есть — удаляется и делается заново). Cold-start в 3 шага."
        actions={
          <div className="flex items-center gap-2 flex-wrap">
            <Button variant="ghost" onClick={myWave} disabled={busy}>
              <Play className="w-4 h-4" /> Моя волна
            </Button>
            <Button onClick={generate} disabled={busy}>
              <RefreshCw className={`w-4 h-4 ${busy ? "animate-spin" : ""}`} />
              {busy ? "Генерирую…" : "Пройти холодный старт"}
            </Button>
          </div>
        }
      />

      <Section title="Алгоритм">
        <Card>
          <ol className="list-decimal list-inside text-sm space-y-1 text-muted">
            <li>
              <span className="text-text font-medium">Сигнал</span> — собираем жанры/артистов по прослушиваниям + избранному + оценкам.
            </li>
            <li>
              <span className="text-text font-medium">Кандидаты</span> — расширяем похожими артистами (кластера/жанры).
            </li>
            <li>
              <span className="text-text font-medium">Диверсификация</span> — MMR-баланс чтобы не зацикливаться на одном, 30 треков.
            </li>
          </ol>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-3 mt-4">
            <label className="block">
              <div className="text-xs text-muted mb-1">Пользователь (персонально)</div>
              <select className="kuma-input" value={coldUser} onChange={(e) => setColdUser(e.target.value)}>
                <option value="">Общий (вся библиотека)</option>
                {(usersData?.users ?? []).map((u) => (
                  <option key={u.id} value={u.id}>{u.username}</option>
                ))}
              </select>
            </label>
            <label className="block">
              <div className="text-xs text-muted mb-1">Треков</div>
              <select className="kuma-input" value={String(coldN)} onChange={(e) => setColdN(Number(e.target.value))}>
                {[15, 30, 50].map((n) => (
                  <option key={n} value={n}>{n}</option>
                ))}
              </select>
            </label>
            <div className="flex items-end">
              <div className="text-xs text-muted">Без пользователя — по глобальным счётчикам. С пользователем — по его лайкам (импорт на странице «Пользователи»).</div>
            </div>
          </div>
          {lastSteps && (
            <div className="mt-3 flex gap-2 text-xs flex-wrap">
              {lastSteps.map((s) => (
                <Badge key={s.step}>
                  шаг {s.step}: {s.name} — {s.items}
                </Badge>
              ))}
            </div>
          )}
          <div className="mt-3 flex gap-2 flex-wrap">
            <Button
              variant="ghost"
              onClick={coldStartOnly}
              disabled={busy}
            >
              <Sparkles className="w-4 h-4" /> Тест cold-start
            </Button>
            <Button
              variant="ghost"
              onClick={async () => {
                setBusy(true);
                try {
                  const r = await api.buildClusters();
                  toast(`Кластеризация запущена (задача ${r.run_id.slice(0, 8)}). Следите в «Задачи и логи».`, "ok");
                } catch (e: unknown) {
                  toast(fmtErr(e), "err");
                } finally {
                  setBusy(false);
                }
              }}
              disabled={busy}
            >
              Пересобрать кластеры
            </Button>
          </div>
        </Card>
      </Section>

      <Section title="AI-микс по описанию">
        <Card>
          <div className="flex flex-col md:flex-row gap-2">
            <Input
              className="flex-1"
              value={aiQuery}
              onChange={(e) => setAiQuery(e.target.value)}
              placeholder="Например: вечерний лоуфай для работы, без вокала"
            />
            <Button onClick={aiMix} disabled={busy}>
              <Wand2 className="w-4 h-4" /> Создать AI-микс
            </Button>
          </div>
          <div className="text-xs text-muted mt-2">Использует настроенного AI-провайдера (Настройки → AI). Без AI — подберёт по ключевым словам.</div>
        </Card>
      </Section>

      <Section title="Открытия недели · CLAP">
        <Card>
          <div className="text-sm text-muted mb-3">
            Средний вектор твоих лайков → ближайшие неслушанные по косинусу. Без CLAP-модели или при &lt;3 лайках — fallback на cold-start.
          </div>
          <div className="flex flex-col md:flex-row gap-2">
            <select className="kuma-input" value={coldUser} onChange={(e) => setColdUser(e.target.value)}>
              <option value="">Выбери пользователя…</option>
              {(usersData?.users ?? []).map((u) => (
                <option key={u.id} value={u.id}>{u.username}</option>
              ))}
            </select>
            <Button
              onClick={async () => {
                if (!coldUser) { toast("Выбери пользователя для Открытий недели", "info"); return; }
                setBusy(true);
                try {
                  const r = await api.weeklyDiscovery(coldUser, coldN || 30);
                  mutate();
                  setLastSteps(r.steps ?? null);
                  setWeekly(r.explanations ?? []);
                  toast(`Открытия недели готовы: ${r.tracks} треков (${r.mode}).`, "ok");
                } catch (e: unknown) {
                  toast(fmtErr(e), "err");
                } finally {
                  setBusy(false);
                }
              }}
              disabled={busy}
            >
              <Sparkles className="w-4 h-4" /> Собрать открытия недели
            </Button>
          </div>
          {weekly.length > 0 && (
            <div className="mt-3 space-y-1.5">
              {weekly.slice(0, 10).map((w) => (
                <div key={w.track_id} className="text-xs text-muted">
                  <span className="text-text font-medium">≈{w.score}</span> · {w.text}
                </div>
              ))}
              {weekly.length > 10 && <div className="text-xs text-muted">…и ещё {weekly.length - 10}</div>}
            </div>
          )}
        </Card>
      </Section>

      <Section title={`Всего плейлистов: ${data?.playlists.length ?? 0}${(data?.hidden_count ?? 0) > 0 ? ` · скрыто ${data?.hidden_count}` : ""}`}>
        <div className="flex flex-col sm:flex-row gap-2 mb-3">
          <div className="relative flex-1">
            <Search className="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 text-muted pointer-events-none" />
            <Input className="pl-9" placeholder="Поиск плейлиста…" value={plQuery} onChange={(e) => setPlQuery(e.target.value)} />
          </div>
          <select className="kuma-input kuma-input-inline sm:w-44" value={plFilter} onChange={(e) => setPlFilter(e.target.value as typeof plFilter)}>
            <option value="all">Все типы</option>
            <option value="auto">Только auto</option>
            <option value="manual">Только manual</option>
            <option value="hidden">Только скрытые</option>
          </select>
          <select className="kuma-input kuma-input-inline sm:w-44" value={plOwner} onChange={(e) => setPlOwner(e.target.value)} title="Владелец">
            <option value="">Все пользователи</option>
            {(usersData?.users ?? []).map((u) => (
              <option key={u.id} value={u.id}>{u.username}</option>
            ))}
          </select>
          <label className="flex items-center gap-2 text-sm text-muted whitespace-nowrap">
            <input type="checkbox" checked={showHidden} onChange={(e) => setShowHidden(e.target.checked)} />
            показать скрытые
          </label>
        </div>
        {visiblePlaylists.length === 0 ? (
          <EmptyState message={(data?.playlists ?? []).length === 0 ? "Плейлистов ещё нет. Нажмите «Пройти холодный старт»." : "Ничего не найдено — поменяйте поиск или фильтр."} />
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            {visiblePlaylists.map((p) => (
              <Card key={p.id} className={`flex flex-col gap-3 ${p.is_hidden ? "opacity-60" : ""}`}>
                <div className="flex items-start justify-between gap-2">
                  <div>
                    <div className="font-medium flex items-center gap-2">
                      <ListMusic className="w-4 h-4 text-muted" />
                      {p.name}
                    </div>
                    <div className="text-xs text-muted mt-1">
                      {p.is_auto_generated ? "ежедневный" : "обычный"} · {p.track_count} треков · {fmtDate(p.created_at)}
                      {p.owner_username && (
                        <span> · для <span className="font-medium text-text">{p.owner_username}</span></span>
                      )}
                    </div>
                  </div>
                  <div className="flex gap-1">
                    <Badge tone={p.is_auto_generated ? "info" : "default"}>
                      {p.is_auto_generated ? "auto" : "manual"}
                    </Badge>
                    {p.in_navidrome && <Badge tone="ok">в Navidrome</Badge>}
                    {p.is_hidden && <Badge tone="warn">скрыт</Badge>}
                  </div>
                </div>
                <div className="flex gap-2 flex-wrap">
                  <Link href={`/playlists/${p.id}`} className="kuma-btn kuma-btn-ghost text-sm">
                    Открыть
                  </Link>
                  <button className="kuma-pill hover:text-text" onClick={() => exportOne(p.id, p.name)} disabled={busy}>
                    <Send className="w-3 h-3" /> {p.in_navidrome ? "обновить в Navidrome" : "в Navidrome"}
                  </button>
                  {p.is_hidden ? (
                    <button className="kuma-pill hover:text-text" onClick={() => unhide(p.id)}>
                      <Eye className="w-3 h-3" /> показать
                    </button>
                  ) : (
                    <button className="kuma-pill hover:text-text" onClick={() => hide(p.id)}>
                      <EyeOff className="w-3 h-3" /> скрыть
                    </button>
                  )}
                  <button className="kuma-pill hover:text-text" onClick={() => del(p.id)}>
                    <Trash2 className="w-3 h-3" /> удалить
                  </button>
                </div>
              </Card>
            ))}
          </div>
        )}
      </Section>
    </>
  );
}
