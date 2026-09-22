"use client";

import Link from "next/link";
import useSWR from "swr";
import { useParams } from "next/navigation";
import { useState } from "react";
import { Heart, Play, RefreshCw, ThumbsDown, ThumbsUp, Ban, KeyRound, Users } from "lucide-react";
import { Badge, Button, Card, EmptyState, PageHeader, Section } from "@/components/ui";
import { api } from "@/lib/api";
import { moodLook } from "@/lib/moodStyle";

const PALETTE = ["#FF3B30", "#007AFF", "#34C759", "#5856D6", "#AF52DE", "#FF9500", "#FF2D55", "#5AC8FA", "#00C7BE", "#FF9F0A"];
function colorFor(s: string) {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
  return PALETTE[h % PALETTE.length];
}

const DAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"];

function DriftList({ up, down }: { up: { name: string; old: number; new: number; delta: number }[]; down: { name: string; old: number; new: number; delta: number }[] }) {
  if (up.length === 0 && down.length === 0) return <div className="text-xs text-muted">Без изменений.</div>;
  const row = (m: { name: string; old: number; new: number; delta: number }, tone: "up" | "down") => (
    <div key={m.name} className="flex items-center gap-2 text-sm py-0.5">
      <span className={`tabular-nums font-bold w-12 ${tone === "up" ? "text-green-600" : "text-red-500"}`}>
        {m.delta > 0 ? `+${m.delta}` : m.delta}
      </span>
      <span className="flex-1 truncate" title={`${m.old} → ${m.new}`}>{m.name}</span>
      <span className="text-[11px] text-muted tabular-nums">{m.old} → {m.new}</span>
    </div>
  );
  return (
    <div className="space-y-0.5">
      {up.map((m) => row(m, "up"))}
      {down.map((m) => row(m, "down"))}
    </div>
  );
}

export default function UserProfilePage() {
  const params = useParams<{ id: string }>();
  const id = params.id;
  const { data, mutate, isLoading } = useSWR(["profile", id], () => api.userProfile(id));
  const { data: vault, mutate: mutateVault } = useSWR(["vault", id], () => api.vaultStatus(id));
  const { data: collab } = useSWR(["collab", id], () => api.collabSimilar(id));
  const { data: collabRec } = useSWR(["collabRec", id], () => api.collabRecommend(id, 12));
  const { data: drift, mutate: mutateDrift } = useSWR(["drift", id], () => api.drift(id));
  const [busy, setBusy] = useState(false);
  const [waveMood, setWaveMood] = useState("");

  async function act(fn: () => Promise<unknown>, okMsg?: string) {
    setBusy(true);
    try {
      const r = (await fn()) as { ok?: boolean; error?: string } | undefined;
      if (r && "ok" in r && !r.ok) alert(`Ошибка: ${r.error ?? "неизвестная"}`);
      else if (okMsg) alert(okMsg);
      mutate();
    } catch (e: unknown) {
      alert(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function myWave() {
    setBusy(true);
    try {
      const r = await api.myWave(id, 30, undefined, waveMood || undefined);
      location.href = `/playlists/${r.playlist_id}`;
    } catch (e: unknown) {
      alert(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function rememberPwd() {
    const pwd = window.prompt("Пароль этого пользователя в Navidrome (сохранится шифром для автообновления):", "");
    if (!pwd) return;
    await act(async () => {
      const r = await api.vaultStore(id, pwd);
      mutateVault();
      return r;
    }, "Запомнено ✓ — вкусы будут обновляться ночью сами");
  }

  if (isLoading) return <div className="text-sm text-muted">Загрузка профиля…</div>;
  if (!data?.ok) return <EmptyState message="Пользователь не найден." />;

  const genres = data.genres ?? [];
  const positives = genres.filter((g) => g.weight > 0 && g.name !== "—");
  const unknownCount = genres.filter((g) => g.name === "—").reduce((s, g) => s + g.likes + g.plays, 0);
  const sumW = positives.reduce((s, g) => s + g.weight, 0) || 1;
  const maxW = Math.max(1, ...positives.map((g) => g.weight));
  const maxH = Math.max(1, ...(data.hours ?? []));
  const maxA = Math.max(1, ...(data.artists ?? []).slice(0, 10).map((a) => Math.max(a.weight, 0.1)));

  return (
    <>
      <PageHeader
        title={data.username}
        subtitle={`♥ ${data.counts.likes} · 👎 ${data.counts.dislikes} · ⛔ ${data.counts.banned} · ▶ ${data.counts.plays} · событий ${data.counts.events}`}
        actions={
          <div className="flex items-center gap-2 flex-wrap">
            <select className="kuma-input kuma-input-inline w-40" value={waveMood} onChange={(e) => setWaveMood(e.target.value)} title="Настроение волны">
              <option value="">Волна: всё</option>
              {(data.moods ?? []).map((m) => (
                <option key={m.name} value={m.name}>Волна: {m.name}</option>
              ))}
            </select>
            <Button onClick={myWave} disabled={busy}>
              <Play className="w-4 h-4" /> Моя волна
            </Button>
            <Link href={`/users/${id}/wrapped` as never} className="kuma-link text-sm">Итоги месяца →</Link>
            <Link href="/users" className="kuma-link text-sm">← Все</Link>
          </div>
        }
      />

      <Section title={`Облако жанров · ${positives.length}`}>
        <Card>
          {positives.length === 0 ? (
            <div className="text-sm text-muted text-center py-8">Пока пусто — пройдите визард или импортируйте вкусы.</div>
          ) : (
            <>
              <div className="text-xs text-muted text-center mb-3">
                Топ: <span className="font-semibold text-text">{positives[0].name}</span>
                {" · "}{((positives[0].weight / sumW) * 100).toFixed(0)}% веса вкуса
                {" · "}{positives[0].likes} ♥ · {positives[0].plays} ▶
              </div>
              <div className="flex flex-wrap items-center justify-center gap-2.5 py-2">
                {positives.slice(0, 30).map((g, i) => {
                  const t = Math.max(0, g.weight) / maxW;
                  const share = ((g.weight / sumW) * 100).toFixed(0);
                  const fs = 12 + Math.round(t * 18);
                  const c = colorFor(g.name);
                  return (
                    <span
                      key={g.name}
                      title={`${g.name}: ${share}% веса · ${g.likes} ♥ · ${g.plays} ▶ · вес ${g.weight}`}
                      className="rounded-full text-white font-semibold transition-transform hover:scale-105 cursor-default border border-white/20"
                      style={{ fontSize: fs, padding: `${6 + t * 8}px ${12 + t * 12}px`, background: c, opacity: 0.6 + 0.4 * t, boxShadow: i < 3 ? `0 4px 16px ${c}66` : `0 2px 8px ${c}44` }}
                    >
                      {i < 3 ? `${i + 1}. ` : ""}{g.name}
                      <span className="opacity-80 font-normal"> {share}%</span>
                    </span>
                  );
                })}
              </div>
              {unknownCount > 0 && (
                <div className="text-[11px] text-muted text-center mt-2">Без жанра: {unknownCount} сигналов — проставьте теги для точности облака.</div>
              )}
            </>
          )}
          {(data.moods ?? []).length > 0 && (
            <div className="mt-3 flex gap-2 flex-wrap justify-center">
              {(data.moods ?? []).map((m) => {
                const look = moodLook(m.name);
                const MIcon = look.icon;
                return (
                  <span
                    key={m.name}
                    className="inline-flex items-center gap-1.5 rounded-full px-3 py-1 text-sm font-medium text-white shadow-sm"
                    style={{ background: look.bg }}
                  >
                    <MIcon className="w-3.5 h-3.5" />
                    {m.name} · {m.count}
                  </span>
                );
              })}
            </div>
          )}
        </Card>
      </Section>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <Section title="Топ артистов">
          <Card>
            {(data.artists ?? []).slice(0, 10).length === 0 ? (
              <div className="text-sm text-muted">Нет данных.</div>
            ) : (
              <div className="space-y-2">
                {(data.artists ?? []).slice(0, 10).map((a) => (
                  <div key={a.name} className="flex items-center gap-2 text-sm">
                    <span className="w-40 truncate" title={a.name}>{a.banned ? "⛔ " : ""}{a.name}</span>
                    <div className="flex-1 h-2 rounded-full bg-border overflow-hidden">
                      <div className="h-full rounded-full" style={{ width: `${Math.max(2, (Math.max(a.weight, 0) / maxA) * 100)}%`, background: colorFor(a.name) }} />
                    </div>
                    <span className="text-xs text-muted tabular-nums w-16 text-right">{a.likes}♥ {a.plays}▶</span>
                    {!a.banned ? (
                      <button className="kuma-pill hover:text-text" title="Забанить" onClick={() => act(() => api.banArtist(id, a.name))} disabled={busy}>ban</button>
                    ) : (
                      <button className="kuma-pill hover:text-text" title="Разбанить" onClick={() => act(() => api.unbanArtist(id, a.name))} disabled={busy}>unban</button>
                    )}
                  </div>
                ))}
              </div>
            )}
          </Card>
        </Section>

        <Section title="Когда слушает">
          <Card>
            <div className="text-xs text-muted mb-2">Часы</div>
            <div className="flex items-end gap-[3px] h-20">
              {(data.hours ?? []).map((h, i) => (
                <div key={i} className="flex-1 rounded-t" title={`${i}:00 — ${h}`}
                  style={{ height: `${Math.max(3, (h / maxH) * 100)}%`, background: "var(--accent)", opacity: 0.35 + 0.65 * (h / maxH) }} />
              ))}
            </div>
            <div className="flex gap-[3px] text-[9px] text-muted mt-1">
              {[0, 6, 12, 18, 23].map((h) => (
                <span key={h} className="flex-1 text-center">{h}</span>
              ))}
            </div>
            <div className="text-xs text-muted mt-4 mb-2">Дни недели</div>
            <div className="flex items-end gap-2 h-16">
              {(data.days ?? []).map((d, i) => {
                const mx = Math.max(1, ...(data.days ?? []));
                return (
                  <div key={i} className="flex-1 flex flex-col items-center gap-1">
                    <div className="w-full rounded-t" title={`${DAYS[i]} — ${d}`}
                      style={{ height: 48, position: "relative" }}>
                      <div className="absolute bottom-0 w-full rounded-t" style={{ height: `${Math.max(4, (d / mx) * 48)}px`, background: colorFor(DAYS[i]) }} />
                    </div>
                    <span className="text-[10px] text-muted">{DAYS[i]}</span>
                  </div>
                );
              })}
            </div>
          </Card>
        </Section>
      </div>

      <Section title="Дрейф вкуса" action={
        <Button variant="ghost" onClick={() => act(async () => { await api.takeDriftSnapshot(id); mutateDrift(); return { ok: true }; }, "Слепок снят")} disabled={busy}>
          Снять слепок
        </Button>
      }>
        <Card>
          {!drift ? (
            <div className="text-sm text-muted">Считаю…</div>
          ) : (drift.weeks ?? []).length === 0 ? (
            <div className="text-sm text-muted text-center py-4">
              {drift.hint ?? "Снапшотов пока нет."} Нажмите «Снять слепок» — через неделю будет с чем сравнить.
            </div>
          ) : (
            <>
              {drift.summary && <div className="text-base font-medium text-center mb-3">{drift.summary} <span className="text-xs text-muted font-normal">(с недели {drift.snapshot_week})</span></div>}
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <div>
                  <div className="text-xs uppercase tracking-wider text-muted mb-2">Жанры вверх / вниз</div>
                  <DriftList up={drift.genres_up ?? []} down={drift.genres_down ?? []} />
                </div>
                <div>
                  <div className="text-xs uppercase tracking-wider text-muted mb-2">Артисты вверх / вниз</div>
                  <DriftList up={drift.artists_up ?? []} down={drift.artists_down ?? []} />
                </div>
              </div>
            </>
          )}
        </Card>
      </Section>

      <Section title={`Топ треков по скору · формула мобильного`}>
        <Card>
          <div className="overflow-x-auto">
          <table className="kuma-table w-full min-w-[760px]">
            <thead><tr><th>#</th><th>Трек</th><th className="text-right">Скор</th><th className="text-right">▶/↷/⟳</th><th></th></tr></thead>
            <tbody>
              {(data.top_tracks ?? []).slice(0, 25).map((t, i) => (
                <tr key={t.track_id}>
                  <td className="text-muted tabular-nums">{i + 1}</td>
                  <td>
                    <div className="flex items-center gap-2 min-w-0">
                      {/* eslint-disable-next-line @next/next/no-img-element */}
                      <img src={api.trackCoverUrl(t.track_id, 100)} alt="" loading="lazy" className="w-8 h-8 rounded object-cover border border-border shrink-0"
                        onError={(e) => ((e.target as HTMLImageElement).style.display = "none")} />
                      <div className="min-w-0">
                        <Link href={`/track/${t.track_id}`} className="kuma-link truncate block" title={t.title}>{t.title}</Link>
                        <div className="text-xs text-muted truncate">{t.artist_name ?? ""}</div>
                      </div>
                    </div>
                  </td>
                  <td className="text-right tabular-nums font-medium">{t.score}</td>
                  <td className="text-right tabular-nums text-muted text-xs">{t.plays}/{t.skips}/{t.replays}</td>
                  <td className="text-right whitespace-nowrap">
                    <button className="kuma-pill hover:text-text" title="Лайк" onClick={() => act(() => api.rateTrack(id, t.track_id, true))} disabled={busy}>
                      <ThumbsUp className="w-3 h-3" />
                    </button>{" "}
                    <button className="kuma-pill hover:text-text" title="Дизлайк" onClick={() => act(() => api.rateTrack(id, t.track_id, false))} disabled={busy}>
                      <ThumbsDown className="w-3 h-3" />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>
          <div className="text-xs text-muted mt-2">Скор = ♥±100 + ▶×10 + complete×15 + ⟳×50 + ⏪×30 − abandon×8 − скипы (ранний −15/−5, &gt;1 −n×10). Как в мобильном.</div>
        </Card>
      </Section>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <Section title={`Дизлайки · ${data.dislikedSongs.length}`}>
          <Card>
            {data.dislikedSongs.length === 0 ? (
              <div className="text-sm text-muted">Нет дизлайков. <Heart className="w-3 h-3 inline" /></div>
            ) : (
              <div className="flex flex-wrap gap-2">
                {data.top_tracks.filter((t) => t.like === false).slice(0, 20).map((t) => (
                  <span key={t.track_id} className="kuma-pill">
                    {t.artist_name ? `${t.artist_name} — ` : ""}{t.title}
                    <button className="hover:text-text ml-1" title="Снять дизлайк" onClick={() => act(() => api.rateTrack(id, t.track_id, null))} disabled={busy}>✕</button>
                  </span>
                ))}
              </div>
            )}
            {data.bannedArtists.length > 0 && (
              <div className="mt-3">
                <div className="text-xs text-muted mb-2">Забаненные артисты:</div>
                <div className="flex flex-wrap gap-2">
                  {data.bannedArtists.map((b) => (
                    <span key={b} className="kuma-pill">
                      <Ban className="w-3 h-3" /> {b}
                      <button className="hover:text-text ml-1" title="Разбанить" onClick={() => act(() => api.unbanArtist(id, b))} disabled={busy}>✕</button>
                    </span>
                  ))}
                </div>
              </div>
            )}
          </Card>
        </Section>

        <Section title="Похожие слушатели (коллаборативка)">
          <Card>
            {(collab?.users ?? []).length === 0 ? (
              <div className="text-sm text-muted">Пока не с кем сравнить — нужно 2+ пользователя с пересекающимися лайками.</div>
            ) : (
              <div className="space-y-2">
                {(collab?.users ?? []).map((u) => (
                  <div key={u.user_id} className="flex items-center gap-2 text-sm">
                    <Users className="w-4 h-4 text-muted" />
                    <Link href={`/users/${u.user_id}` as never} className="kuma-link font-medium">{u.username}</Link>
                    <span className="ml-auto text-xs text-muted tabular-nums">
                      {(u.similarity * 100).toFixed(0)}% · общих ♥ {u.shared_likes}
                    </span>
                  </div>
                ))}
              </div>
            )}
            {(collabRec?.items ?? []).length > 0 && (
              <div className="mt-3">
                <div className="text-xs text-muted mb-2">От них стоит послушать:</div>
                <div className="space-y-1.5">
                  {(collabRec?.items ?? []).slice(0, 6).map((t) => (
                    <div key={t.track_id} className="text-sm flex items-center gap-2">
                      <Link href={`/track/${t.track_id}`} className="kuma-link truncate">{t.artist_name ? `${t.artist_name} — ` : ""}{t.title}</Link>
                      <span className="ml-auto text-[10px] text-muted whitespace-nowrap">via {t.because_of.join(", ")}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </Card>
        </Section>
      </div>

      <Section title="Автообновление вкусов">
        <Card>
          <div className="flex flex-col md:flex-row gap-3 md:items-center text-sm">
            <span className="flex items-center gap-2">
              <KeyRound className="w-4 h-4 text-muted" />
              {vault?.stored ? "Пароль запомнен (шифр, ключ в compose)" : "Пароль не запомнен"}
              {!vault?.available && <Badge tone="warn">TASTE_VAULT_KEY не задан</Badge>}
            </span>
            {data.mobile?.synced_at && (
              <Badge tone="ok">мобила: {new Date(data.mobile.synced_at).toLocaleString("ru-RU")}</Badge>
            )}
            <span className="flex-1" />
            {!vault?.stored ? (
              <Button variant="ghost" onClick={rememberPwd} disabled={busy || !vault?.available} title={!vault?.available ? "Сначала задайте TASTE_VAULT_KEY" : ""}>
                Запомнить пароль
              </Button>
            ) : (
              <Button variant="ghost" onClick={() => act(async () => { await api.vaultForget(id); mutateVault(); return { ok: true }; }, "Забыто — автообновление выключено")} disabled={busy}>
                Забыть пароль
              </Button>
            )}
            <Button
              variant="ghost"
              onClick={() => act(async () => {
                const r = await api.refreshNow(id);
                return r.ok ? { ok: true } : r;
              }, "Вкусы обновлены из Navidrome")}
              disabled={busy || !vault?.stored}
            >
              <RefreshCw className="w-4 h-4" /> Обновить сейчас
            </Button>
          </div>
          <div className="text-xs text-muted mt-2">Ночью (04:30) вкусы подтянутся сами. Пароль хранится только шифротекстом, ключ — в секретах compose, не в базе.</div>
        </Card>
      </Section>
    </>
  );
}
