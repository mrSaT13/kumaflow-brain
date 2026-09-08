import clsx from "clsx";
import type { ButtonHTMLAttributes, InputHTMLAttributes, ReactNode } from "react";

export function PageHeader({
  title,
  subtitle,
  actions,
}: {
  title: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div className="flex items-end justify-between gap-4 mb-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
        {subtitle && <p className="text-sm text-muted mt-1">{subtitle}</p>}
      </div>
      {actions && <div className="flex items-center gap-2">{actions}</div>}
    </div>
  );
}

export function Card({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={clsx("kuma-card p-5", className)}>{children}</div>;
}

export function Stat({
  label,
  value,
  hint,
  icon,
}: {
  label: ReactNode;
  value: ReactNode;
  hint?: ReactNode;
  icon?: ReactNode;
}) {
  return (
    <div className="kuma-card p-5">
      <div className="text-xs uppercase tracking-wider text-muted flex items-center gap-2">
        {icon}
        {label}
      </div>
      <div className="mt-2 text-3xl font-semibold tracking-tight tabular-nums">{value}</div>
      {hint && <div className="mt-1 text-xs text-muted">{hint}</div>}
    </div>
  );
}

export function Section({
  title,
  children,
  action,
}: {
  title: ReactNode;
  children: ReactNode;
  action?: ReactNode;
}) {
  return (
    <section className="mb-8">
      <div className="flex items-center justify-between mb-3">
        <h2 className="text-sm font-medium uppercase tracking-wider text-muted">{title}</h2>
        {action}
      </div>
      {children}
    </section>
  );
}

export function EmptyState({ message }: { message: ReactNode }) {
  return (
    <div className="kuma-card flex flex-col items-center justify-center py-16 text-center">
      <div className="text-sm text-muted">{message}</div>
    </div>
  );
}

export function Badge({
  children,
  tone = "default",
}: {
  children: ReactNode;
  tone?: "default" | "ok" | "warn" | "err" | "info";
}) {
  const cls = {
    default: "kuma-pill",
    ok: "kuma-pill !border-emerald-300 !text-emerald-700 dark:!text-emerald-300",
    warn: "kuma-pill !border-amber-300 !text-amber-700 dark:!text-amber-300",
    err: "kuma-pill !border-rose-300 !text-rose-700 dark:!text-rose-300",
    info: "kuma-pill !border-sky-300 !text-sky-700 dark:!text-sky-300",
  }[tone];
  return <span className={cls}>{children}</span>;
}

export function Button({
  variant = "primary",
  className,
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: "primary" | "ghost" }) {
  return (
    <button
      {...rest}
      className={clsx(
        "kuma-btn",
        variant === "ghost" && "kuma-btn-ghost",
        className,
      )}
    />
  );
}

export function Input(props: InputHTMLAttributes<HTMLInputElement>) {
  return <input {...props} className={clsx("kuma-input", props.className)} />;
}
