"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";

import { api } from "@/lib/api";
import { NavLink } from "./ui";

const LINKS = [
  { href: "/runs", label: "Runs" },
  { href: "/benchmarks", label: "Benchmarks" },
  { href: "/settings", label: "Settings" },
];

export function Nav() {
  const pathname = usePathname() ?? "/";
  const [online, setOnline] = useState<boolean | null>(null);
  const [version, setVersion] = useState<string>("");

  useEffect(() => {
    let cancelled = false;
    const ping = async () => {
      try {
        const health = await api.health();
        if (!cancelled) {
          setOnline(true);
          setVersion(health.version);
        }
      } catch {
        if (!cancelled) setOnline(false);
      }
    };
    void ping();
    const timer = setInterval(ping, 15_000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, []);

  return (
    <header className="sticky top-0 z-20 border-b border-[var(--border)] bg-[var(--bg)]/85 backdrop-blur">
      <div className="mx-auto flex w-full max-w-[1400px] items-center gap-5 px-5 py-3">
        <Link href="/" className="focus-ring flex items-center gap-2 rounded-md">
          <span className="mono text-[15px] font-bold tracking-tight text-[var(--text)]">
            Patch<span className="text-[var(--color-accent)]">Pilot</span>
          </span>
        </Link>

        <nav className="flex items-center gap-0.5">
          {LINKS.map((link) => (
            <NavLink
              key={link.href}
              href={link.href}
              active={pathname === link.href || pathname.startsWith(`${link.href}/`)}
            >
              {link.label}
            </NavLink>
          ))}
        </nav>

        <div className="ml-auto flex items-center gap-3">
          <Link
            href="/runs/new"
            className="focus-ring rounded-md bg-[var(--color-accent)] px-3 py-1.5 text-[13px] font-medium text-white transition-colors hover:bg-[var(--color-accent)]/85"
          >
            New run
          </Link>
          <span
            className="flex items-center gap-1.5 text-[11px] text-[var(--text-dim)]"
            title={
              online === false
                ? "The backend is not reachable on port 8000"
                : `PatchPilot ${version}`
            }
          >
            <span
              className={`h-1.5 w-1.5 rounded-full ${
                online === null
                  ? "bg-[var(--text-dim)]"
                  : online
                    ? "bg-[var(--color-ok)]"
                    : "bg-[var(--color-bad)]"
              }`}
            />
            <span className="mono hidden sm:inline">
              {online === null ? "…" : online ? `api ${version}` : "api offline"}
            </span>
          </span>
        </div>
      </div>
    </header>
  );
}
