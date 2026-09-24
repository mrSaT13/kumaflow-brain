"use client";

import { useEffect, useState } from "react";
import useSWR from "swr";
import { api } from "@/lib/api";

const KEY = "kumaflow_user";
const LEGACY_KEY = "nowplaying_user";

function readSaved(): string {
  try {
    return localStorage.getItem(KEY) ?? localStorage.getItem(LEGACY_KEY) ?? "";
  } catch {
    return "";
  }
}

/** Общий выбранный пользователь веба — один на все меню.
 *
 * Приоритет: ручной выбор (localStorage, переживает переходы и перезагрузки)
 * > владелец токена браузера (whoami — "под которым авторизован")
 * > первый пользователь списка.
 *
 * Все селекторы юзера (волна, главная, cold-start, плейлисты, яндекс)
 * обязаны использовать этот хук, а не свой useState.
 */
export function useCurrentUser() {
  const { data: users } = useSWR("/api/users", () => api.listUsers());
  const { data: me } = useSWR("/api/settings/whoami", () => api.whoami());
  const [sel, setSel] = useState("");
  useEffect(() => {
    setSel(readSaved());
  }, []);
  const list = users?.users ?? [];
  function setUserId(id: string) {
    setSel(id);
    try {
      localStorage.setItem(KEY, id);
    } catch {
      /* ignore */
    }
  }
  let userId = "";
  if (sel && list.some((u) => u.id === sel)) userId = sel;
  else if (me?.owner_user_id && list.some((u) => u.id === me.owner_user_id)) {
    userId = me.owner_user_id;
  } else userId = list[0]?.id ?? "";
  return { users: list, userId, setUserId, isMine: !!me?.owner_user_id && userId === me.owner_user_id };
}
