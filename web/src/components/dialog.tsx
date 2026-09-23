"use client";

import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui";

export type ConfirmOpts = {
  title: string;
  message?: string;
  confirmText?: string;
  cancelText?: string;
  danger?: boolean;
};

export function ConfirmDialog({
  open,
  opts,
  onClose,
}: {
  open: boolean;
  opts: ConfirmOpts;
  onClose: (ok: boolean) => void;
}) {
  useEffect(() => {
    if (!open) return;
    const h = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose(false);
    };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [open, onClose]);
  if (!open) return null;
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      role="dialog"
      aria-modal="true"
      onClick={() => onClose(false)}
    >
      <div className="absolute inset-0 bg-black/40" />
      <div
        className="kuma-card kuma-fade-in relative w-full max-w-sm p-5"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="text-base font-semibold">{opts.title}</div>
        {opts.message && <div className="mt-2 text-sm text-muted">{opts.message}</div>}
        <div className="mt-5 flex justify-end gap-2">
          <Button variant="ghost" onClick={() => onClose(false)}>
            {opts.cancelText ?? "Отмена"}
          </Button>
          <Button
            onClick={() => onClose(true)}
            className={opts.danger ? "!bg-red-600" : ""}
          >
            {opts.confirmText ?? "Подтвердить"}
          </Button>
        </div>
      </div>
    </div>
  );
}

export function useConfirm() {
  const [state, setState] = useState<{ opts: ConfirmOpts; resolve: (v: boolean) => void } | null>(null);
  const confirm = useCallback(
    (opts: ConfirmOpts) => new Promise<boolean>((resolve) => setState({ opts, resolve })),
    [],
  );
  const node = (
    <ConfirmDialog
      open={!!state}
      opts={state?.opts ?? { title: "" }}
      onClose={(ok) => {
        state?.resolve(ok);
        setState(null);
      }}
    />
  );
  return [node, confirm] as const;
}

export function PasswordDialog({
  open,
  title,
  onClose,
}: {
  open: boolean;
  title: string;
  onClose: (pwd: string | null) => void;
}) {
  const [val, setVal] = useState("");
  useEffect(() => {
    if (open) setVal("");
  }, [open ]);
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4" role="dialog" aria-modal="true" onClick={() => onClose(null)}>
      <div className="absolute inset-0 bg-black/40" />
      <div className="kuma-card kuma-fade-in relative w-full max-w-sm p-5" onClick={(e) => e.stopPropagation()}>
        <div className="text-base font-semibold">{title}</div>
        <input
          autoFocus
          type="password"
          className="kuma-input mt-3"
          placeholder="Пароль Navidrome"
          value={val}
          onChange={(e) => setVal(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && val) onClose(val);
            if (e.key === "Escape") onClose(null);
          }}
        />
        <div className="mt-5 flex justify-end gap-2">
          <Button variant="ghost" onClick={() => onClose(null)}>Отмена</Button>
          <Button onClick={() => val && onClose(val)}>Импортировать</Button>
        </div>
      </div>
    </div>
  );
}
