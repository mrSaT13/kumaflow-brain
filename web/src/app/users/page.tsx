"use client";

import useSWR from "swr";
import { useState } from "react";
import { Shield, Trash2, UserPlus, RefreshCw } from "lucide-react";
import { Badge, Button, Card, EmptyState, Input, PageHeader, Section } from "@/components/ui";
import { api } from "@/lib/api";

export default function UsersPage() {
  const { data, mutate } = useSWR("/api/users", () => api.listUsers());
  const [externalId, setExternalId] = useState("");
  const [username, setUsername] = useState("");
  const [isAdmin, setIsAdmin] = useState(false);
  const [busy, setBusy] = useState(false);

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
        subtitle="Источник данных для коллаборативной фильтрации. Добавьте всех пользователей с Navidrome."
        actions={
          <Button variant="ghost" onClick={sync} disabled={busy}>
            <RefreshCw className={`w-4 h-4 ${busy ? "animate-spin" : ""}`} /> Синхронизировать с Navidrome
          </Button>
        }
      />

      <Section title="Добавить пользователя">
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
            Подсказка: external_id — это ID пользователя в Navidrome. Если Navidrome подключен, используйте «Синхронизировать».
          </div>
        </Card>
      </Section>

      <Section title={`Всего: ${data?.users.length ?? 0}`}>
        {(data?.users ?? []).length === 0 ? (
          <EmptyState message="Пользователей ещё нет." />
        ) : (
          <div className="kuma-card overflow-hidden">
            <table className="kuma-table w-full">
              <thead>
                <tr>
                  <th>Имя</th>
                  <th>external_id</th>
                  <th>Роль</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {(data?.users ?? []).map((u) => (
                  <tr key={u.id}>
                    <td className="font-medium">{u.username}</td>
                    <td className="text-muted text-xs font-mono">{u.external_id}</td>
                    <td>
                      <button className="kuma-pill hover:text-text" onClick={() => toggleAdmin(u.id, u.is_admin)}>
                        <Shield className="w-3 h-3" />
                        {u.is_admin ? "админ" : "обычный"}
                      </button>
                    </td>
                    <td className="text-right">
                      <button className="kuma-pill hover:text-text" onClick={() => del(u.id)}>
                        <Trash2 className="w-3 h-3" /> удалить
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>
    </>
  );
}
