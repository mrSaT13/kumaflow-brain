"use client";

import useSWR from "swr";
import { api } from "@/lib/api";

/** Админ ли текущий браузер. Бэк уже отдаёт 403, это только для скрытия кнопок. */
export function useIsAdmin() {
  const { data } = useSWR("/api/settings/whoami", () => api.whoami(), {
    refreshInterval: 60000,
    shouldRetryOnError: false,
    revalidateOnFocus: false,
  });
  return { isAdmin: data?.is_admin === true, locked: data?.locked === true };
}
