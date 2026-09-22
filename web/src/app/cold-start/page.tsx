"use client";

import Link from "next/link";
import useSWR from "swr";
import { useEffect, useMemo, useState } from "react";
import { ArrowLeft, ArrowRight, Check, Heart, Loader2, Play, Sparkles } from "lucide-react";
import { Button, Card, Input, PageHeader, Section } from "@/components/ui";
import { api, type ArtistEntry } from "@/lib/api";

// Палитра как в мобильном (fallback по хэшу жанра)
const PALETTE = ["#FF3B30", "#007AFF", "#34C759", "#5856D6", "#AF52DE", "#FF9500", "#FF2D55", "#5AC8FA"];
function genreColor(g: string) {
  let h = 0;
  for (let i = 0; i < g.length; i++) h = (h * 31 + g.charCodeAt(i)) >>> 0;
  return PALETTE[h % PALETTE.length];
}

function coverUrl(a: ArtistEntry): string | null {
  if (a.cover_track_id) return api.trackCoverUrl(a.cover_track_id, 300);
  if (a.cover_art_id) return api.coverUrl(a.cover_art_id, 300);
  return null;
}

function coverUrlSmall(a: ArtistEntry): string | null {
  if (a.cover_track_id) return api.trackCoverUrl(a.cover_track_id, 100);
  if (a.cover_art_id) return api.coverUrl(a.cover_art_id, 100);
  return null;
}

function StepDots({ page }: { page: number }) {
  return (
    <div className="flex items-center gap-1.5">
      {[0, 1, 2].map((i) => (
        <div
          key={i}
          className="h-2 rounded-full transition-all"
          style={{ width: i === page ? 22 : 8, background: i === page ? "var(--accent)" : "var(--border)" }}
        />
      ))}
    </div>
  );
}

export default function ColdStartPage() {
  const [page, setPage] = useState(0);
  const [userId, setUserId] = useState("");
  const [selectedGenres, setSelectedGenres] = useState<string[]>([]);
  const [selectedArtists, setSelectedArtists] = useState<string[]>([]);
  const [genreQ, setGenreQ] = useState("");
  const [artistQ, setArtistQ] = useState("");
  const [artistQDeb, setArtistQDeb] = useState("");
  const [expanded, setExpanded] = useState<string | null>(null);
  const [similarCache, setSimilarCache] = useState<Record<string, ArtistEntry[]>>({});
  const [similarServer, setSimilarServer] = useState<Record<string, boolean>>({});
  const [similarBusy, setSimilarBusy] = useState(false);
  const [saving, setSaving] = useState(false);
  const [seedResult, setSeedResult] = useState<{ favorites_added: number; history_added: number; favorites_total: number } | null>(null);
  const [playlistId, setPlaylistId] = useState<string | null>(null);

  const { data: usersData } = useSWR("/api/users", () => api.listUsers());
  const { data: genresData } = useSWR("/api/library/genres", () => api.genres());

  useEffect(() => {
    const t = setTimeout(() => setArtistQDeb(artistQ), 450);
    return () => clearTimeout(t);
  }, [artistQ]);

  const genresKey = useMemo(() => [...selectedGenres].sort().join("|"), [selectedGenres]);
  const { data: artistsData, isLoading: artistsLoading } = useSWR(
    ["coldstart-artists", artistQDeb, genresKey],
    () =>
      api.artists({
        q: artistQDeb || undefined,
        genre: selectedGenres.length ? selectedGenres : undefined,
        limit: 300,
      }),
  );

  const filteredGenres = useMemo(() => {
    const all = genresData?.genres ?? [];
    const q = genreQ.trim().toLowerCase();
    if (!q) return all;
    return all.filter((g) => g.toLowerCase().includes(q));
  }, [genresData, genreQ]);

  function toggleGenre(g: string) {
    setSelectedGenres((prev) => (prev.includes(g) ? prev.filter((x) => x !== g) : [...prev, g]));
  }

  async function toggleArtist(a: ArtistEntry) {
    const was = selectedArtists.includes(a.name);
    setSelectedArtists((prev) => (was ? prev.filter((x) => x !== a.name) : [...prev, a.name]));
    if (!was) {
      setExpanded(a.name);
      if (!similarCache[a.name]) {
        setSimilarBusy(true);
        try {
          const r = await api.similarArtists(a.name, 6);
          setSimilarCache((prev) => ({ ...prev, [a.name]: r.items }));
          setSimilarServer((prev) => ({ ...prev, [a.name]: r.server_used }));
        } catch {
          /* тихо */
        } finally {
          setSimilarBusy(false);
        }
      }
    } else if (expanded === a.name) {
      setExpanded(null);
    }
  }

  async function save() {
    if (!userId) return alert("Выберите пользователя — вкусы сохраняются персонально");
    setSaving(true);
    try {
      const r = await api.seedTaste(userId, { genres: selectedGenres, artists: selectedArtists });
      if (!r.ok) return alert(`Ошибка: ${r.error ?? "неизвестная"}`);
      setSeedResult(r);
      setPage(2);
    } catch (e: unknown) {
      alert(String(e));
    } finally {
      setSaving(false);
    }
  }

  async function makePlaylist() {
    if (!userId) return;
    setSaving(true);
    try {
      const r = await api.generateDailyPlaylist(30, userId);
      setPlaylistId(r.playlist_id);
    } catch (e: unknown) {
      alert(String(e));
    } finally {
      setSaving(false);
    }
  }

  const canNext = page === 0 ? selectedGenres.length > 0 : page === 1 ? selectedArtists.length > 0 : true;
  const titles = ["Шаг 1 из 3 — Жанры", "Шаг 2 из 3 — Артисты", "Шаг 3 из 3 — Готово"];

  return (
    <>
      <PageHeader
        title="Настрой свой вкус"
        subtitle={titles[page]}
        actions={<StepDots page={page} />}
      />

      <Section title="Пользователь">
        <Card>
          <div className="flex flex-col md:flex-row gap-3 md:items-center">
            <select className="kuma-input kuma-input-inline md:w-72" value={userId} onChange={(e) => { setUserId(e.target.value); setSeedResult(null); setPlaylistId(null); }}>
              <option value="">— выберите пользователя —</option>
              {(usersData?.users ?? []).map((u) => (
                <option key={u.id} value={u.id}>{u.username}</option>
              ))}
            </select>
            <div className="text-xs text-muted">
              Как в мобильном: вкусы (жанры + артисты) записываются в профиль пользователя, и миксы становятся персональными. Нет пользователя — создайте на странице «Пользователи».
            </div>
          </div>
        </Card>
      </Section>

      {page === 0 && (
        <Section title={`Выбрано: ${selectedGenres.length} · коснитесь кружков с любимыми жанрами`}>
          <div className="mb-3">
            <Input placeholder="Поиск жанра…" value={genreQ} onChange={(e) => setGenreQ(e.target.value)} />
          </div>
          {(filteredGenres ?? []).length === 0 ? (
            <Card><div className="text-sm text-muted text-center py-8">Жанры не найдены — сначала просканируйте библиотеку.</div></Card>
          ) : (
            <div className="grid grid-cols-3 sm:grid-cols-4 md:grid-cols-6 gap-4">
              {filteredGenres.map((g) => {
                const sel = selectedGenres.includes(g);
                const color = genreColor(g);
                return (
                  <button key={g} onClick={() => toggleGenre(g)} className="flex flex-col items-center gap-2 group">
                    <span
                      className="w-20 h-20 rounded-full flex items-center justify-center text-white text-xl font-bold transition-all"
                      style={{
                        background: color,
                        outline: sel ? "3px solid var(--accent)" : "3px solid transparent",
                        outlineOffset: 2,
                        boxShadow: sel ? `0 0 16px ${color}88` : "0 4px 10px #0002",
                        opacity: sel ? 1 : 0.92,
                      }}
                    >
                      {sel ? <Check className="w-7 h-7" /> : g.slice(0, 1).toUpperCase()}
                    </span>
                    <span className="text-xs text-center leading-tight line-clamp-2" style={{ fontWeight: sel ? 700 : 500 }}>
                      {g}
                    </span>
                  </button>
                );
              })}
            </div>
          )}
        </Section>
      )}

      {page === 1 && (
        <Section title={`Выбрано артистов: ${selectedArtists.length} · нажмите на кружок, чтобы лайкнуть ♥`}>
          <div className="mb-3">
            <Input placeholder="Поиск артиста…" value={artistQ} onChange={(e) => setArtistQ(e.target.value)} />
          </div>
          {artistsLoading ? (
            <Card><div className="text-sm text-muted text-center py-8 flex items-center justify-center gap-2"><Loader2 className="w-4 h-4 animate-spin" /> Загружаем артистов…</div></Card>
          ) : (artistsData?.items ?? []).length === 0 ? (
            <Card><div className="text-sm text-muted text-center py-8">Артисты не найдены. Попробуйте другой запрос или уберите фильтр жанров.</div></Card>
          ) : (
            <div className="grid grid-cols-3 sm:grid-cols-4 md:grid-cols-6 gap-4">
              {(artistsData?.items ?? []).map((a) => {
                const sel = selectedArtists.includes(a.name);
                const img = coverUrl(a);
                const isExp = expanded === a.name;
                const similars = similarCache[a.name] ?? [];
                return (
                  <div key={a.name} className="flex flex-col items-center gap-1.5">
                    <button onClick={() => toggleArtist(a)} className="relative">
                      <span
                        className="w-20 h-20 rounded-full overflow-hidden flex items-center justify-center transition-all bg-[#FFE066]"
                        style={{
                          outline: sel ? "3px solid var(--accent)" : "3px solid transparent",
                          outlineOffset: 2,
                          boxShadow: "0 4px 10px #0002",
                        }}
                      >
                        {img ? (
                          // eslint-disable-next-line @next/next/no-img-element
                          <img src={img} alt="" loading="lazy" className="w-full h-full object-cover"
                            onError={(e) => ((e.target as HTMLImageElement).style.display = "none")} />
                        ) : (
                          <Heart className="w-7 h-7 text-[#FF3B30]" fill="currentColor" />
                        )}
                      </span>
                      {sel && (
                        <span className="absolute -top-1 -right-1 w-6 h-6 rounded-full flex items-center justify-center text-white"
                          style={{ background: "var(--accent)" }}>
                          <Check className="w-3.5 h-3.5" />
                        </span>
                      )}
                    </button>
                    <span className="text-xs text-center leading-tight line-clamp-2" style={{ fontWeight: sel ? 700 : 500 }}>
                      {a.name}
                    </span>
                    <span className="text-[10px] text-muted">{a.track_count} тр.</span>
                    {isExp && sel && (
                      <div className="w-full rounded-xl border border-border p-2 mt-1">
                        <div className="text-[10px] text-muted mb-1.5 text-center">
                          Похожие{similarServer[a.name] ? " · с сервера" : ""}:
                        </div>
                        {similarBusy && similars.length === 0 ? (
                          <div className="text-[10px] text-muted text-center">ищем…</div>
                        ) : (
                          <div className="flex justify-center gap-1.5">
                            {similars.map((s) => {
                              const sSel = selectedArtists.includes(s.name);
                              const sImg = coverUrlSmall(s);
                              return (
                                <button key={s.name} title={s.name} onClick={() => toggleArtist(s)} className="flex flex-col items-center gap-0.5 w-12">
                                  <span
                                    className="w-10 h-10 rounded-full overflow-hidden flex items-center justify-center bg-surface text-xs font-bold"
                                    style={{ outline: sSel ? "2px solid var(--accent)" : "1px solid var(--border)" }}
                                  >
                                    {sImg ? (
                                      // eslint-disable-next-line @next/next/no-img-element
                                      <img src={sImg} alt="" loading="lazy" className="w-full h-full object-cover"
                                        onError={(e) => ((e.target as HTMLImageElement).style.display = "none")} />
                                    ) : s.name.slice(0, 1)}
                                  </span>
                                  <span className="text-[8px] leading-tight text-center line-clamp-1 w-full">{s.name}</span>
                                  {!sSel && <span className="text-[10px] leading-none text-[#FF3B30]">＋</span>}
                                </button>
                              );
                            })}
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </Section>
      )}

      {page === 2 && (
        <Section title="Профиль обучен!">
          <Card>
            <div className="flex flex-col items-center text-center py-6 gap-3">
              <span className="w-20 h-20 rounded-full flex items-center justify-center"
                style={{ background: "color-mix(in srgb, var(--accent) 15%, transparent)", border: "3px solid var(--accent)" }}>
                <Sparkles className="w-9 h-9" />
              </span>
              <div className="text-lg font-semibold">Вкусы сохранены!</div>
              <div className="text-sm text-muted max-w-md">
                Жанров: {selectedGenres.length} · Артистов: {selectedArtists.length}
                {seedResult && <> · Лайков всего: {seedResult.favorites_total} (+{seedResult.favorites_added} новых)</>}
                . Миксы станут персональными с каждым прослушиванием.
              </div>
              {!playlistId ? (
                <Button onClick={makePlaylist} disabled={saving || !userId}>
                  <Play className="w-4 h-4" /> {saving ? "Создаю…" : "Начать слушать — создать плейлист"}
                </Button>
              ) : (
                <Link href={`/playlists/${playlistId}`} className="kuma-btn">
                  <Play className="w-4 h-4" /> Открыть мой плейлист
                </Link>
              )}
            </div>
          </Card>
        </Section>
      )}

      {page < 2 && (
        <div className="sticky bottom-0 py-3 flex items-center gap-3"
          style={{ background: "color-mix(in srgb, var(--bg) 92%, transparent)", borderTop: "1px solid var(--border)" }}>
          {page > 0 ? (
            <Button variant="ghost" onClick={() => setPage((p) => p - 1)}>
              <ArrowLeft className="w-4 h-4" /> Назад
            </Button>
          ) : <span />}
          <span className="flex-1" />
          {page === 0 && (
            <Button onClick={() => setPage(1)} disabled={!canNext}>
              Далее <ArrowRight className="w-4 h-4" />
            </Button>
          )}
          {page === 1 && (
            <Button onClick={save} disabled={!canNext || saving || !userId} title={!userId ? "Выберите пользователя выше" : ""}>
              {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Sparkles className="w-4 h-4" />}
              {saving ? "Сохраняю…" : "Обучить"}
            </Button>
          )}
        </div>
      )}
    </>
  );
}
