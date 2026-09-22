"use client";

import useSWR from "swr";
import { useState } from "react";
import { Shield, Trash2, UserPlus, RefreshCw, Heart } from "lucide-react";
import { Badge, Button, Card, EmptyState, Input, PageHeader, Section } from "@/components/ui";
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

export default function UsersPage() {
  const { data, mutate } = useSWR("/api/users", () => api.listUsers());
  const [externalId, setExternalId] = useState("");
  const [username, setUsername] = useState("");
  const [isAdmin, setIsAdmin] = useState(false);
  const [busy, setBusy] = useState(false);
  const [navLogin, setNavLogin] = useState("");
  const [navPassword, setNavPassword] = useState("");
  const [importBusy, setImportBusy] = useState<string | null>(null);

  async function create() {
    if (!externalId.trim() || !username.trim()) return alert("Заполните ID и имя");
    setBusy(true);
    try {
      await api.createUser({ external_id: externalId.trim(), username: username.trim(), is_admin: isAdmin });
      setExternalId("");
      setUsername("");
      setIsAdmin(false);
      mutate();
    } catch (e: unknown) {
      alert(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function createWithImport() {
    if (!navLogin.trim() || !navPassword) return alert("Укажите логин и пароль пользователя Navidrome");
    setBusy(true);
    try {
      const r = await api.createUserByCredentials({ username: navLogin.trim(), password: navPassword });
      if (!r.ok) return alert(`Ошибка: ${r.error ?? "неизвестная"}`);
      const im = (r.import ?? {}) as Record<string, unknown>;
      setNavLogin("");
      setNavPassword("");
      mutate();
      alert(
        `Готово: пользователь «${r.user?.username}» — лайков: ${im.favorites_total ?? 0} (+${im.favorites_added ?? 0} новых), дизлайков: ${im.disliked ?? 0}, плейлистов: ${im.playlists ?? 0} (треков: ${im.playlist_tracks ?? 0}).`,
      );
    } catch (e: unknown) {
      alert(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function importFor(id: string, fallbackName: string) {
    const pwd = window.prompt(`Пароль пользователя «${fallbackName}» в Navidrome (нужен для чтения его лайков):`, "");
    if (pwd === null) return;
    if (!pwd) return alert("Без пароля Navidrome не отдаст чужие лайки — введите пароль.");
    setImportBusy(id);
    try {
      const r = await api.importTastes(id, pwd);
      if (!r.ok) alert(`Ошибка: ${r.error ?? "неизвестная"}`);
      else alert(`Готово: лайков всего ${r.favorites_total ?? 0} (+${r.favorites_added ?? 0} новых), плейлистов ${r.playlists ?? 0}. Теперь cold-start для этого пользователя будет персональным.`);
      mutate();
    } catch (e: unknown) {
      alert(String(e));
    } finally {
      setImportBusy(null);
    }
  }

  async function toggleAdmin(id: string, cur: boolean) {
    await api.updateUser(id, { is_admin: !cur });
    mutate();
  }

  async function del(id: string) {
    if (!confirm("Удалить пользователя?")) return;
    await api.deleteUser(id);
    mutate();
  }

  async function sync() {
    setBusy(true);
    try {
      const r = await api.syncUsers();
      if (!r.ok) alert(`Ошибка: ${r.error ?? "неизвестная"}`);
      else alert(`Готово: +${r.added ?? 0} новых, обновлено ${r.updated ?? 0}, всего ${r.total ?? 0}`);
      mutate();
    } catch (e: unknown) {
      alert(String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
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
          <div className="text-xs text-muted mt-2">
            Тянет лайки (★), оценки и плейлисты пользователя прямо из Navidrome. Пароль не хранится — используется только для одного запроса. Треки, которых нет в локальной библиотеке, пропускаются (сначала «Синхронизировать» в Библиотеке).
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
        {(data?.users ?? []).length === 0 ? (
          <EmptyState message="Пользователей ещё нет." />
        ) : (
          <div className="kuma-card overflow-hidden">
            <div className="overflow-x-auto">
            <table className="kuma-table w-full min-w-[720px]">
              <thead>
                <tr>
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
                    <td className="font-medium">{u.username}</td>
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
        )}
      </Section>
    </>
  );
}
