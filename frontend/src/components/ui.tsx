/** Small shared primitives. Deliberately plain — density over decoration. */

import Link from "next/link";
import type { ReactNode } from "react";

import { benchmarkLabel, benchmarkTone, runStateTone } from "@/lib/format";
import { isTerminal, type BenchmarkStatus, type RunState } from "@/lib/types";

type Tone = "ok" | "bad" | "warn" | "info" | "muted" | "accent";

const TONE_CLASSES: Record<Tone, string> = {
  ok: "text-[var(--color-ok)] bg-[var(--color-ok-soft)] border-[var(--color-ok)]/30",
  bad: "text-[var(--color-bad)] bg-[var(--color-bad-soft)] border-[var(--color-bad)]/30",
  warn: "text-[var(--color-warn)] bg-[var(--color-warn-soft)] border-[var(--color-warn)]/30",
  info: "text-[var(--color-info)] bg-[var(--color-accent-soft)]/40 border-[var(--color-info)]/30",
  muted: "text-[var(--text-dim)] bg-[var(--bg-input)] border-[var(--border)]",
  accent: "text-[var(--color-accent)] bg-[var(--color-accent-soft)]/40 border-[var(--color-accent)]/30",
};

const DOT_CLASSES: Record<Tone, string> = {
  ok: "bg-[var(--color-ok)]",
  bad: "bg-[var(--color-bad)]",
  warn: "bg-[var(--color-warn)]",
  info: "bg-[var(--color-info)]",
  muted: "bg-[var(--text-dim)]",
  accent: "bg-[var(--color-accent)]",
};

export function Badge({
  children,
  tone = "muted",
  dot = false,
  pulse = false,
  className = "",
}: {
  children: ReactNode;
  tone?: Tone;
  dot?: boolean;
  pulse?: boolean;
  className?: string;
}) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-md border px-2 py-0.5 text-[11px] font-medium uppercase tracking-wide ${TONE_CLASSES[tone]} ${className}`}
    >
      {dot && (
        <span
          className={`h-1.5 w-1.5 rounded-full ${DOT_CLASSES[tone]} ${pulse ? "pulse" : ""}`}
        />
      )}
      {children}
    </span>
  );
}

export function RunStateBadge({ state }: { state: RunState }) {
  const tone = runStateTone(state) as Tone;
  const active = !isTerminal(state);
  return (
    <Badge tone={tone} dot pulse={active}>
      {state}
    </Badge>
  );
}

export function BenchmarkStatusBadge({ status }: { status: BenchmarkStatus }) {
  return (
    <Badge tone={benchmarkTone(status) as Tone} dot={status === "RESOLVED"}>
      {benchmarkLabel(status)}
    </Badge>
  );
}

/** Lifecycle status of a whole benchmark batch (not a per-task verdict). */
export function BenchmarkRunStatusBadge({ status }: { status: string }) {
  const tone: Tone =
    status === "COMPLETED"
      ? "ok"
      : status === "FAILED"
        ? "bad"
        : status === "CANCELLED"
          ? "muted"
          : "info";
  const active = ["GENERATING", "EVALUATING", "PENDING"].includes(status);
  return (
    <Badge tone={tone} dot pulse={active}>
      {status.toLowerCase()}
    </Badge>
  );
}

export function Panel({
  children,
  className = "",
  title,
  actions,
}: {
  children: ReactNode;
  className?: string;
  title?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <section className={`surface overflow-hidden ${className}`}>
      {(title || actions) && (
        <header className="flex items-center justify-between gap-3 border-b border-[var(--border)] bg-[var(--bg-raised)] px-4 py-2.5">
          <h2 className="text-[13px] font-semibold text-[var(--text)]">{title}</h2>
          {actions}
        </header>
      )}
      {children}
    </section>
  );
}

export function Stat({
  label,
  value,
  hint,
  tone,
}: {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  tone?: Tone;
}) {
  const colour =
    tone === "ok"
      ? "text-[var(--color-ok)]"
      : tone === "bad"
        ? "text-[var(--color-bad)]"
        : tone === "warn"
          ? "text-[var(--color-warn)]"
          : "text-[var(--text)]";
  return (
    <div className="px-4 py-3">
      <div className="text-[11px] uppercase tracking-wider text-[var(--text-dim)]">
        {label}
      </div>
      <div className={`mono mt-1 text-xl font-semibold tabular-nums ${colour}`}>{value}</div>
      {hint && <div className="mt-0.5 text-[11px] text-[var(--text-dim)]">{hint}</div>}
    </div>
  );
}

export function Button({
  children,
  variant = "secondary",
  size = "md",
  className = "",
  ...props
}: {
  children: ReactNode;
  variant?: "primary" | "secondary" | "ghost" | "danger";
  size?: "sm" | "md";
} & React.ButtonHTMLAttributes<HTMLButtonElement>) {
  const variants = {
    primary:
      "bg-[var(--color-accent)] text-white hover:bg-[var(--color-accent)]/85 border-transparent",
    secondary:
      "bg-[var(--bg-input)] text-[var(--text)] hover:bg-[var(--border)] border-[var(--border-strong)]",
    ghost:
      "bg-transparent text-[var(--text-muted)] hover:text-[var(--text)] hover:bg-[var(--bg-input)] border-transparent",
    danger:
      "bg-transparent text-[var(--color-bad)] hover:bg-[var(--color-bad-soft)] border-[var(--color-bad)]/40",
  };
  const sizes = { sm: "px-2.5 py-1 text-xs", md: "px-3.5 py-1.5 text-[13px]" };
  return (
    <button
      className={`focus-ring inline-flex items-center justify-center gap-1.5 rounded-md border font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-45 ${variants[variant]} ${sizes[size]} ${className}`}
      {...props}
    >
      {children}
    </button>
  );
}

export function Field({
  label,
  hint,
  children,
  required,
}: {
  label: string;
  hint?: ReactNode;
  children: ReactNode;
  required?: boolean;
}) {
  return (
    <label className="block">
      <span className="mb-1.5 flex items-baseline gap-1.5 text-[12px] font-medium text-[var(--text-muted)]">
        {label}
        {required && <span className="text-[var(--color-bad)]">*</span>}
        {hint && <span className="text-[11px] font-normal text-[var(--text-dim)]">{hint}</span>}
      </span>
      {children}
    </label>
  );
}

const CONTROL =
  "focus-ring w-full rounded-md border border-[var(--border-strong)] bg-[var(--bg-input)] px-3 py-2 text-[13px] text-[var(--text)] placeholder:text-[var(--text-dim)]";

export function Input(props: React.InputHTMLAttributes<HTMLInputElement>) {
  return <input {...props} className={`${CONTROL} ${props.className ?? ""}`} />;
}

export function Textarea(props: React.TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return (
    <textarea {...props} className={`${CONTROL} resize-y ${props.className ?? ""}`} />
  );
}

export function Select(props: React.SelectHTMLAttributes<HTMLSelectElement>) {
  return <select {...props} className={`${CONTROL} ${props.className ?? ""}`} />;
}

export function EmptyState({
  title,
  description,
  action,
  icon = "○",
}: {
  title: string;
  description?: ReactNode;
  action?: ReactNode;
  icon?: string;
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 px-6 py-14 text-center">
      <div className="mono text-2xl text-[var(--text-dim)]">{icon}</div>
      <p className="text-[13px] font-medium text-[var(--text)]">{title}</p>
      {description && (
        <p className="max-w-md text-[12px] leading-relaxed text-[var(--text-dim)]">
          {description}
        </p>
      )}
      {action && <div className="mt-2">{action}</div>}
    </div>
  );
}

export function ErrorBanner({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="flex items-start justify-between gap-4 rounded-md border border-[var(--color-bad)]/40 bg-[var(--color-bad-soft)] px-4 py-3">
      <div>
        <p className="text-[13px] font-medium text-[var(--color-bad)]">Something went wrong</p>
        <p className="mt-0.5 text-[12px] text-[var(--text-muted)]">{message}</p>
      </div>
      {onRetry && (
        <Button size="sm" onClick={onRetry}>
          Retry
        </Button>
      )}
    </div>
  );
}

export function WarningBanner({ items }: { items: string[] }) {
  if (items.length === 0) return null;
  return (
    <div className="rounded-md border border-[var(--color-warn)]/40 bg-[var(--color-warn-soft)] px-4 py-3">
      <p className="text-[12px] font-semibold uppercase tracking-wide text-[var(--color-warn)]">
        Environment notices
      </p>
      <ul className="mt-1.5 space-y-1">
        {items.map((item) => (
          <li key={item} className="text-[12px] leading-relaxed text-[var(--text-muted)]">
            • {item}
          </li>
        ))}
      </ul>
    </div>
  );
}

export function Spinner({ label }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 px-4 py-8 text-[12px] text-[var(--text-dim)]">
      <span className="mono pulse">●</span>
      {label ?? "Loading…"}
    </div>
  );
}

export function Tabs<T extends string>({
  tabs,
  active,
  onChange,
}: {
  tabs: { id: T; label: string; count?: number }[];
  active: T;
  onChange: (id: T) => void;
}) {
  return (
    <div
      role="tablist"
      className="flex gap-0.5 overflow-x-auto border-b border-[var(--border)]"
    >
      {tabs.map((tab) => {
        const selected = tab.id === active;
        return (
          <button
            key={tab.id}
            role="tab"
            aria-selected={selected}
            onClick={() => onChange(tab.id)}
            className={`focus-ring -mb-px whitespace-nowrap border-b-2 px-3.5 py-2 text-[12px] font-medium uppercase tracking-wide transition-colors ${
              selected
                ? "border-[var(--color-accent)] text-[var(--text)]"
                : "border-transparent text-[var(--text-dim)] hover:text-[var(--text-muted)]"
            }`}
          >
            {tab.label}
            {tab.count !== undefined && (
              <span className="mono ml-1.5 text-[11px] text-[var(--text-dim)]">
                {tab.count}
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}

export function CodeBlock({ children, className = "" }: { children: string; className?: string }) {
  return (
    <pre
      className={`terminal overflow-x-auto bg-[var(--bg-input)] px-4 py-3 text-[var(--text-muted)] ${className}`}
    >
      {children}
    </pre>
  );
}

export function KeyValue({ rows }: { rows: [string, ReactNode][] }) {
  return (
    <dl className="divide-y divide-[var(--border)]">
      {rows.map(([key, value]) => (
        <div key={key} className="flex items-baseline gap-4 px-4 py-2">
          <dt className="w-40 shrink-0 text-[12px] text-[var(--text-dim)]">{key}</dt>
          <dd className="mono min-w-0 flex-1 break-words text-[12px] text-[var(--text)]">
            {value}
          </dd>
        </div>
      ))}
    </dl>
  );
}

export function NavLink({
  href,
  active,
  children,
}: {
  href: string;
  active: boolean;
  children: ReactNode;
}) {
  return (
    <Link
      href={href}
      className={`focus-ring rounded-md px-2.5 py-1.5 text-[13px] font-medium transition-colors ${
        active
          ? "bg-[var(--bg-input)] text-[var(--text)]"
          : "text-[var(--text-dim)] hover:text-[var(--text)]"
      }`}
    >
      {children}
    </Link>
  );
}
