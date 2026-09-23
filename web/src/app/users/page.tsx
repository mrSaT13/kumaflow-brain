"use client";

import useSWR from "swr";
import Link from "next/link";
import { useState } from "react";
import { Shield, Trash2, UserPlus, RefreshCw, Heart } from "lucide-react";
import { Badge, Button, Card, EmptyState, Input, PageHeader, Section } from "@/components/ui";
import { useConfirm, PasswordDialog } from "@/components/dialog";
import { useToast, fmtErr } from "@/components/toasts";
import { api } from "@/lib/api";

function TasteBadge({ id }: { id: string }) {
  const { data } = useSWR(`/api/users/${id}/tastes`, () => api.userTastes(id));
  if (!data) return <span className="text-muted text-xs">…</span>;
  return (
    <span className="text-muted text-xs whitespace-nowrap">
      ♥ {data.favorites} · ▤ {data.playlists}
    </span>
  );
}

const PALETTE = ["#FF3B30", "#007AFF", "#34C759", "#5856D6", "#AF52DE", "#FF9500", "#FF2D55", "#5AC8FA", "#00C7BE", "#FF9F0A"];
function colorFor(s: string) {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
  return PALETTE[h % PALETTE.length];
}

function CompareSection({ ids, users, onClear }: { ids: string[]; users: { id: string; username: string }[]; onClear: () => void }) {
  const { data, isLoading } = useSWR(ids.length >= 2 ? ["compare", ...ids] : null, () => api.collabCompare(ids));
  const nameOf = (uid: string) => users.find((u) => u.id === uid)?.username ?? uid.slice(0, 8);
  return (
    <Section title={`Сравнение вкусов · ${ids.length}`}>
      <Card>
        <div className="flex items-center gap-2 flex-wrap text-sm mb-3">
          {ids.map((id) => (
            <span key={id} className="kuma-pill">{nameOf(id)}</span>
          ))}
          <span className="flex-1" />
          <Button variant="ghost" onClick={onClear}>Сбросить выбор</Button>
        </div>
        {isLoading && <div className="text-sm text-muted">Считаю пересечения…</div>}
        {data && (
          <>
            <div className="flex gap-2 flex-wrap mb-4">
              {data.pairwise.map((p) => (
                <Badge key={`${p.a}-${p.b}`} tone={p.similarity > 0 ? "ok" : undefined}>
                  {p.a_name} ↔ {p.b_name}: {(p.similarity * 100).toFixed(0)}% · общих ♥ {p.shared_likes}
                </Badge>
              ))}
            </div>
            {data.shared_genres.length === 0 && data.shared_artists.length === 0 ? (
              <div className="text-sm text-muted text-center py-4">Общего пока нет — пересекающихся жанров и артистов с весом &gt; 0 не найдено.</div>
            ) : (
              <>
                {data.shared_genres.length > 0 && (
                  <div className="mb-4">
                    <div className="text-xs uppercase tracking-wider text-muted mb-2">Общие жанры · {data.shared_genres.length} — тот же цвет, что в облаках ниже</div>
                    <div className="flex flex-wrap items-center justify-center gap-2.5">
                      {data.shared_genres.slice(0, 24).map((g) => {
                        const t = Math.min(1, g.avg / Math.max(1, data.shared_genres[0].avg));
                        const c = colorFor(g.name);
                        return (
                          <span key={g.name} title={ids.map((id) => `${nameOf(id)}: ${g.weights[id] ?? 0}`).join(" · ")}
                            className="rounded-full text-white font-semibold border border-white/20"
                            style={{ fontSize: 12 + Math.round(t * 16), padding: `${6 + t * 6}px ${12 + t * 10}px`, background: c, opacity: 0.75 + 0.25 * t }}>
                            {g.name}
                          </span>
                        );
                      })}
                    </div>
                  </div>
                )}
                {data.shared_artists.length > 0 && (
                  <div className="mb-4">
                    <div className="text-xs uppercase tracking-wider text-muted mb-2">Общие артисты · {data.shared_artists.length}</div>
                    <div className="flex flex-wrap gap-2 justify-center">
                      {data.shared_artists.slice(0, 24).map((a) => (
                        <span key={a.name} className="kuma-pill" title={ids.map((id) => `${nameOf(id)}: ${a.weights[id] ?? 0}`).join(" · ")}>
                          {a.name}
                        </span>
                      ))}
                    </div>
                  </div>
                )}
              </>
            )}
            {data.shared_tracks.length > 0 && (
              <div className="mb-4">
                <div className="text-xs uppercase tracking-wider text-muted mb-2">Общие лайки · {data.shared_tracks.length}</div>
                <div className="flex flex-wrap gap-2">
                  {data.shared_tracks.slice(0, 12).map((t) => (
                    <span key={t.track_id} className="kuma-pill" title={`Лайкнули: ${t.liked_by.join(", ")}`}>
                      {t.artist_name ? `${t.artist_name} — ` : ""}{t.title} <span className="text-muted">· {t.liked_by.join(", ")}</span>
                    </span>
                  ))}
                </div>
              </div>
            )}
            <div className="grid gap-4" style={{ gridTemplateColumns: `repeat(${Math.min(ids.length, 3)}, minmax(0, 1fr))` }}>
              {data.users.map((u) => {
                const maxW = Math.max(1, ...u.genres_top.map((g) => g.weight));
                return (
                  <div key={u.user_id} className="kuma-card !p-3">
                    <Link href={`/users/${u.user_id}` as never} className="kuma-link font-medium text-sm">{u.username}</Link>
                    <span className="text-xs text-muted"> · ♥ {u.likes}</span>
                    <div className="flex flex-wrap items-center justify-center gap-2 mt-2">
                      {u.genres_top.slice(0, 14).map((g) => {
                        const shared = data.shared_genres.some((s) => s.name === g.name);
                        const t = Math.max(0, g.weight) / maxW;
                        const c = colorFor(g.name);
                        return (
                          <span key={g.name} title={`${g.weight}`}
                            className={`rounded-full text-white font-semibold ${shared ? "border-2 border-white/70" : "border border-white/20"}`}
                            style={{ fontSize: 11 + Math.round(t * 10), padding: `${4 + t * 5}px ${8 + t * 8}px`, background: c, opacity: shared ? 1 : 0.55 + 0.3 * t }}>
                            {g.name}
                          </span>
                        );
                      })}
                    </div>
                    {u.genres_top.length === 0 && <div className="text-xs text-muted mt-2">Нет жанров с весом &gt; 0.</div>}
                  </div>
                );
              })}
            </div>
            <div className="text-[11px] text-muted mt-3 text-center">Одинаковый жанр — одинаковый цвет везде. Белой рамкой подсвечено общее.</div>
          </>
        )}
      </Card>
    </Section>
  );
}

export default function UsersPage() {
  const [confirmNode, confirm] = useConfirm();
  const toast = useToast();
  const { data, error, mutate } = useSWR("/api/users", () => api.listUsers());
  const [externalId, setExternalId] = useState("");
  const [username, setUsername] = useState("");
  const [isAdmin, setIsAdmin] = useState(false);
  const [busy, setBusy] = useState(false);
  const [navLogin, setNavLogin] = useState("");
  const [navPassword, setNavPassword] = useState("");
  const [rememberPwd, setRememberPwd] = useState(true);
  const [importBusy, setImportBusy] = useState<string | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [pwdOpen, setPwdOpen] = useState<{ id: string; name: string } | null>(null);

  function toggleSelect(id: string) {
    setSelected((prev) => prev.includes(id) ? prev.filter((x) => x !== id) : [...prev.slice(0, 9), id]);
  }

  async function create() {
    if (!externalId.trim() || !username.trim()) { toast("Заполните ID и имя", "info"); return; }
    setBusy(true);
    try {
      await api.createUser({ external_id: externalId.trim(), username: username.trim(), is_admin: isAdmin });
      setExternalId("");
      setUsername("");
      setIsAdmin(false);
      mutate();
      toast("Пользователь добавлен.", "ok");
    } catch (e: unknown) {
      toast(fmtErr(e), "err");
    } finally {
      setBusy(false);
    }
  }

  async function createWithImport() {
    if (!navLogin.trim() || !navPassword) { toast("Укажите логин и пароль пользователя Navidrome", "info"); return; }
    setBusy(true);
    try {
      const r = await api.createUserByCredentials({ username: navLogin.trim(), password: navPassword, remember: rememberPwd });
      if (!r.ok) { toast(`Ошибка: ${r.error ?? "неизвестная"}`, "err"); return; }
      const im = (r.import ?? {}) as Record<string, unknown>;
      // пароль запоминаем только в sessionStorage браузера (на сервер не сохраняется) —
      // повторный «импорт» уже не спросит его заново
      try {
        if (r.user?.id) sessionStorage.setItem(`navpwd:${r.user.id}`, navPassword);
      } catch { /* приватный режим */ }
      setNavLogin("");
      setNavPassword("");
      mutate();
      toast(
        `Готово: «${r.user?.username}» — лайков: ${im.favorites_total ?? 0} (+${im.favorites_added ?? 0} новых), плейлистов: ${im.playlists ?? 0}.`,
        "ok",
      );
    } catch (e: unknown) {
      toast(fmtErr(e), "err");
    } finally {
      setBusy(false);
    }
  }

  async function importFor(id: string, fallbackName: string) {
    // если пароль уже вводился в этой вкладке — берём из памяти, не спрашиваем
    let pwd: string | null = null;
    try {
      pwd = sessionStorage.getItem(`navpwd:${id}`);
    } catch { pwd = null; }
    if (!pwd) {
      setPwdOpen({ id, name: fallbackName });
      return;
    }
    await doImport(id, pwd);
  }

  async function doImport(id: string, pwd: string) {
    setImportBusy(id);
    try {
      const r = await api.importTastes(id, pwd);
      if (!r.ok) toast(`Ошибка: ${r.error ?? "неизвестная"}`, "err");
      else {
        try {
          sessionStorage.setItem(`navpwd:${id}`, pwd);
        } catch { /* приватный режим */ }
        toast(`Готово: лайков всего ${r.favorites_total ?? 0} (+${r.favorites_added ?? 0} новых), плейлистов ${r.playlists ?? 0}.`, "ok");
      }
      mutate();
    } catch (e: unknown) {
      toast(fmtErr(e), "err");
    } finally {
      setImportBusy(null);
    }
  }

  async function toggleAdmin(id: string, cur: boolean) {
    await api.updateUser(id, { is_admin: !cur });
    mutate();
  }

  async function del(id: string) {
    if (!(await confirm({ title: "Удалить пользователя?", confirmText: "Удалить", danger: true }))) return;
    await api.deleteUser(id);
    mutate();
  }

  async function sync() {
    setBusy(true);
    try {
      const r = await api.syncUsers();
      if (!r.ok) toast(`Ошибка: ${r.error ?? "неизвестная"}`, "err");
      else toast(`Готово: +${r.added ?? 0} новых, обновлено ${r.updated ?? 0}, всего ${r.total ?? 0}`, "ok");
      mutate();
    } catch (e: unknown) {
      toast(fmtErr(e), "err");
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      {confirmNode}
      <PasswordDialog
        open={!!pwdOpen}
        title={pwdOpen ? `Пароль «${pwdOpen.name}» в Navidrome` : ""}
        onClose={(pwd) => {
          const cur = pwdOpen;
          setPwdOpen(null);
          if (pwd && cur) void doImport(cur.id, pwd);
        }}
      />
      <PageHeader
        title="Пользователи"
        subtitle="Источник данных для коллаборативной фильтрации. Лайки/плейлисты тянутся из Navidrome под логином самого пользователя."
        actions={
          <Button variant="ghost" onClick={sync} disabled={busy}>
            <RefreshCw className={`w-4 h-4 ${busy ? "animate-spin" : ""}`} /> Синхронизировать с Navidrome
          </Button>
        }
      />

      <Section title="Добавить по логину и паролю (с автоимпортом вкусов)">
        <Card>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
            <Input placeholder="Логин в Navidrome" value={navLogin} onChange={(e) => setNavLogin(e.target.value)} />
            <Input placeholder="Пароль в Navidrome" type="password" value={navPassword} onChange={(e) => setNavPassword(e.target.value)} />
            <Button onClick={createWithImport} disabled={busy}>
              <Heart className="w-4 h-4" /> Добавить + импорт вкусов
            </Button>
          </div>
          <label className="flex items-center gap-2 text-sm mt-3">
            <input type="checkbox" checked={rememberPwd} onChange={(e) => setRememberPwd(e.target.checked)} />
            запомнить пароль для автообновления (шифр Fernet, ключ в compose)
          </label>
          <div className="text-xs text-muted mt-2">
            Тянет лайки (★), оценки и плейлисты пользователя прямо из Navidrome. Без галочки пароль используется один раз и забывается. Треки, которых нет в локальной библиотеке, пропускаются (сначала «Синхронизировать» в Библиотеке).
          </div>
        </Card>
      </Section>

      <Section title="Добавить вручную (без импорта)">
        <Card>
          <div className="grid grid-cols-1 md:grid-cols-4 gap-3">
            <Input placeholder="external_id (из Navidrome)" value={externalId} onChange={(e) => setExternalId(e.target.value)} />
            <Input placeholder="Имя" value={username} onChange={(e) => setUsername(e.target.value)} />
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={isAdmin} onChange={(e) => setIsAdmin(e.target.checked)} />
              админ
            </label>
            <Button onClick={create} disabled={busy}>
              <UserPlus className="w-4 h-4" /> Добавить
            </Button>
          </div>
          <div className="text-xs text-muted mt-2">
            Подсказка: external_id — это логин пользователя в Navidrome. После ручного добавления нажмите «Импорт» в таблице — понадобится его пароль.
          </div>
        </Card>
      </Section>

      <Section title={`Всего: ${data?.users.length ?? 0}`}>
        {error ? (
          <EmptyState message={`Не смог загрузить: ${error instanceof Error ? error.message : String(error)} — если там 401, вставьте токен в Настройки → Токены → «Использовать».`} />
        ) : (data?.users ?? []).length === 0 ? (
          <EmptyState message="Пользователей ещё нет." />
        ) : (
          <>
          {selected.length >= 2 && (
            <div className="mb-4">
              <CompareSection ids={selected} users={data?.users ?? []} onClear={() => setSelected([])} />
            </div>
          )}
          {selected.length > 0 && selected.length < 2 && (
            <div className="text-xs text-muted mb-2">Отметьте ещё хотя бы одного пользователя галочкой — покажу общие жанры, артисты и лайки.</div>
          )}
          <div className="kuma-card overflow-hidden">
            <div className="overflow-x-auto">
            <table className="kuma-table w-full min-w-[720px]">
              <thead>
                <tr>
                  <th></th>
                  <th>Имя</th>
                  <th>external_id</th>
                  <th>Вкусы</th>
                  <th>Роль</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {(data?.users ?? []).map((u) => (
                  <tr key={u.id}>
                    <td><input type="checkbox" checked={selected.includes(u.id)} onChange={() => toggleSelect(u.id)} title="Выбрать для сравнения" /></td>
                    <td className="font-medium"><Link href={`/users/${u.id}` as never} className="kuma-link">{u.username}</Link></td>
                    <td className="text-muted text-xs font-mono">{u.external_id}</td>
                    <td><TasteBadge id={u.id} /></td>
                    <td>
                      <button className="kuma-pill hover:text-text" onClick={() => toggleAdmin(u.id, u.is_admin)}>
                        <Shield className="w-3 h-3" />
                        {u.is_admin ? "админ" : "обычный"}
                      </button>
                    </td>
                    <td className="text-right whitespace-nowrap">
                      <button
                        className="kuma-pill hover:text-text"
                        onClick={() => importFor(u.id, u.username)}
                        disabled={importBusy === u.id}
                      >
                        <Heart className="w-3 h-3" /> {importBusy === u.id ? "импорт…" : "импорт"}
                      </button>{" "}
                      <button className="kuma-pill hover:text-text" onClick={() => del(u.id)}>
                        <Trash2 className="w-3 h-3" /> удалить
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            </div>
          </div>
          </>
        )}
      </Section>
    </>
  );
}
