"use client";

/**
 * Settings is read-only by design.
 *
 * Configuration comes from the environment, so a UI that wrote it back would
 * create a second source of truth and a way to inject secrets over HTTP. This
 * page shows the effective configuration, what is actually available on this
 * machine, and exactly which tools the policy permits.
 */

import { useCallback, useEffect, useState } from "react";

import {
  Badge,
  Button,
  ErrorBanner,
  KeyValue,
  Panel,
  Spinner,
  WarningBanner,
} from "@/components/ui";
import { ApiError, api } from "@/lib/api";
import type { SystemStatus } from "@/lib/types";

export default function SettingsPage() {
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [probe, setProbe] = useState<Record<string, unknown>[] | null>(null);
  const [probing, setProbing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setStatus(await api.status());
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  if (error && !status) return <ErrorBanner message={error} onRetry={() => void load()} />;
  if (!status) return <Spinner label="Loading configuration…" />;

  const nativeTools = status.tools.filter((t) => t.origin === "native");
  const mcpTools = status.tools.filter((t) => t.origin !== "native");

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-semibold tracking-tight">Settings</h1>
        <p className="mt-0.5 max-w-2xl text-[12px] text-[var(--text-muted)]">
          Read-only. Configuration is supplied through environment variables so there is
          one source of truth and no path for a secret to arrive over HTTP. Edit{" "}
          <code className="mono text-[var(--text)]">.env</code> and restart.
        </p>
      </div>

      {status.warnings.length > 0 && <WarningBanner items={status.warnings} />}

      <div className="grid gap-4 lg:grid-cols-2">
        <Panel title="Model provider">
          <KeyValue
            rows={[
              ["Provider", status.provider.provider],
              ["Model", status.provider.model],
              [
                "Credentials",
                status.provider.ready ? (
                  <Badge tone="ok">configured</Badge>
                ) : (
                  <Badge tone="bad">missing</Badge>
                ),
              ],
              ["Detail", status.provider.detail],
              [
                "Test double",
                status.provider.is_test_double ? (
                  <Badge tone="warn">scripted — not a real model</Badge>
                ) : (
                  "no"
                ),
              ],
            ]}
          />
        </Panel>

        <Panel title="Execution sandbox">
          <KeyValue
            rows={[
              ["Configured", status.sandbox.configured],
              ["Effective", status.sandbox.effective],
              [
                "Docker",
                status.sandbox.docker_available ? (
                  <Badge tone="ok">available</Badge>
                ) : (
                  <Badge tone="warn">unavailable</Badge>
                ),
              ],
              ["Docker detail", status.sandbox.docker_detail],
            ]}
          />
          {status.sandbox.configured === "local" && (
            <p className="border-t border-[var(--border)] px-4 py-2.5 text-[11px] leading-relaxed text-[var(--color-warn)]">
              The local sandbox has no kernel-level isolation: repository code runs as your
              user account under a cwd jail, a scrubbed environment, timeouts and output
              caps. Use <code className="mono">PATCHPILOT_SANDBOX=docker</code> for
              untrusted repositories.
            </p>
          )}
        </Panel>

        <Panel title="Budgets">
          <KeyValue
            rows={Object.entries(status.budgets).map(([key, value]) => [
              key.replace(/_/g, " "),
              String(value),
            ])}
          />
        </Panel>

        <Panel title="SWE-bench evaluation">
          <KeyValue
            rows={[
              ["Dataset", status.swebench.dataset ?? "—"],
              ["Split", status.swebench.split ?? "—"],
              [
                "Dataset package",
                status.swebench.dataset_available ? (
                  <Badge tone="ok">installed</Badge>
                ) : (
                  <Badge tone="warn">missing</Badge>
                ),
              ],
              [
                "Official harness",
                status.swebench.harness_installed ? (
                  <Badge tone="ok">installed</Badge>
                ) : (
                  <Badge tone="warn">missing</Badge>
                ),
              ],
              [
                "Docker",
                status.swebench.docker_available ? (
                  <Badge tone="ok">running</Badge>
                ) : (
                  <Badge tone="warn">unavailable</Badge>
                ),
              ],
              [
                "Free disk",
                status.swebench.free_disk_gb == null
                  ? "unknown"
                  : `${status.swebench.free_disk_gb} GB (≈120 GB recommended)`,
              ],
            ]}
          />
          {status.swebench.problems.length > 0 && (
            <ul className="space-y-1 border-t border-[var(--border)] px-4 py-2.5">
              {status.swebench.problems.map((problem) => (
                <li key={problem} className="text-[11px] text-[var(--color-warn)]">
                  • {problem}
                </li>
              ))}
            </ul>
          )}
        </Panel>
      </div>

      {/* --------------------------------------------------------- policy */}
      <Panel
        title="Tool permission policy"
        actions={
          <span className="text-[11px] text-[var(--text-dim)]">
            default:{" "}
            <span
              className={
                status.policy.default === "deny"
                  ? "text-[var(--color-ok)]"
                  : "text-[var(--color-warn)]"
              }
            >
              {status.policy.default}
            </span>
          </span>
        }
      >
        <p className="border-b border-[var(--border)] px-4 py-2.5 text-[12px] leading-relaxed text-[var(--text-muted)]">
          The MCP server decides <em>what tools exist</em>. PatchPilot decides{" "}
          <em>what this agent is allowed to call</em>. The model only decides{" "}
          <em>which allowed tool to call now</em>. Deny always beats allow, and anything
          not on the allow-list is refused.
        </p>
        <div className="grid gap-4 p-4 sm:grid-cols-2">
          <div>
            <p className="text-[11px] uppercase tracking-wider text-[var(--color-ok)]">
              Allow
            </p>
            <div className="mt-1.5 flex flex-wrap gap-1">
              {status.policy.allow.map((rule) => (
                <span
                  key={rule}
                  className="mono rounded border border-[var(--color-ok)]/30 bg-[var(--color-ok-soft)] px-1.5 py-0.5 text-[11px] text-[var(--color-ok)]"
                >
                  {rule}
                </span>
              ))}
            </div>
          </div>
          <div>
            <p className="text-[11px] uppercase tracking-wider text-[var(--color-bad)]">
              Deny
            </p>
            <div className="mt-1.5 flex flex-wrap gap-1">
              {status.policy.deny.map((rule) => (
                <span
                  key={rule}
                  className="mono rounded border border-[var(--color-bad)]/30 bg-[var(--color-bad-soft)] px-1.5 py-0.5 text-[11px] text-[var(--color-bad)]"
                >
                  {rule}
                </span>
              ))}
            </div>
          </div>
        </div>
      </Panel>

      {/* ---------------------------------------------------------- tools */}
      <Panel
        title={`Tool registry (${status.tools.length})`}
        actions={
          <span className="text-[11px] text-[var(--text-dim)]">
            {nativeTools.length} native · {mcpTools.length} MCP
          </span>
        }
      >
        <div className="max-h-96 overflow-y-auto">
          <table className="w-full border-collapse text-[12px]">
            <thead className="sticky top-0 bg-[var(--bg-raised)]">
              <tr className="border-b border-[var(--border)] text-left">
                {["Tool", "Origin", "Permission", "Description"].map((heading) => (
                  <th
                    key={heading}
                    className="px-3 py-2 text-[11px] font-semibold uppercase tracking-wider text-[var(--text-dim)]"
                  >
                    {heading}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {status.tools.map((tool) => (
                <tr
                  key={tool.name}
                  className="border-b border-[var(--border)] align-top last:border-0"
                >
                  <td className="mono px-3 py-2 text-[var(--text)]">
                    {tool.name}
                    {tool.mutating && (
                      <span className="ml-1.5 text-[10px] text-[var(--color-warn)]">
                        mutates
                      </span>
                    )}
                  </td>
                  <td className="px-3 py-2">
                    <Badge tone={tool.origin === "native" ? "muted" : "accent"}>
                      {tool.origin}
                      {tool.server ? `:${tool.server}` : ""}
                    </Badge>
                  </td>
                  <td className="px-3 py-2">
                    <Badge tone={tool.allowed ? "ok" : "bad"}>
                      {tool.allowed ? "allowed" : "denied"}
                    </Badge>
                  </td>
                  <td className="px-3 py-2 text-[11px] text-[var(--text-dim)]">
                    {tool.description.slice(0, 150)}
                    {tool.description.length > 150 ? "…" : ""}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>

      {/* ------------------------------------------------------------ mcp */}
      <Panel
        title="MCP servers"
        actions={
          <div className="flex items-center gap-2">
            <Badge tone={status.mcp.enabled ? "ok" : "muted"}>
              {status.mcp.enabled ? "enabled" : "disabled"}
            </Badge>
            <Button
              size="sm"
              disabled={probing || !status.mcp.config_exists}
              onClick={async () => {
                setProbing(true);
                try {
                  setProbe(await api.mcpProbe());
                } catch (err) {
                  setError(err instanceof ApiError ? err.message : String(err));
                } finally {
                  setProbing(false);
                }
              }}
            >
              {probing ? "Probing…" : "Probe servers"}
            </Button>
          </div>
        }
      >
        <KeyValue
          rows={[
            ["Config path", status.mcp.config_path],
            [
              "Config file",
              status.mcp.config_exists ? (
                <Badge tone="ok">found</Badge>
              ) : (
                <Badge tone="muted">not present</Badge>
              ),
            ],
            [
              "mcp package",
              status.mcp.package_installed ? (
                <Badge tone="ok">installed</Badge>
              ) : (
                <Badge tone="warn">not installed</Badge>
              ),
            ],
            ["Servers configured", String(status.mcp.servers.length)],
          ]}
        />

        {status.mcp.servers.length > 0 && (
          <div className="divide-y divide-[var(--border)] border-t border-[var(--border)]">
            {status.mcp.servers.map((server) => (
              <div key={server.name} className="px-4 py-2.5">
                <div className="flex items-center gap-2">
                  <span className="mono text-[12px] font-medium text-[var(--text)]">
                    {server.name}
                  </span>
                  <Badge tone={server.enabled ? "ok" : "muted"}>
                    {server.enabled ? "enabled" : "disabled"}
                  </Badge>
                  <Badge tone="muted">{server.transport}</Badge>
                </div>
                <p className="mono mt-1 text-[11px] text-[var(--text-dim)]">
                  {server.command} {server.args.join(" ")}
                </p>
                {server.env_keys.length > 0 && (
                  <p className="mt-0.5 text-[11px] text-[var(--text-dim)]">
                    env: {server.env_keys.join(", ")}{" "}
                    <span className="text-[10px]">(values never displayed)</span>
                  </p>
                )}
              </div>
            ))}
          </div>
        )}

        {probe && (
          <pre className="terminal max-h-72 overflow-auto border-t border-[var(--border)] px-4 py-3 text-[var(--text-muted)]">
            {JSON.stringify(probe, null, 2)}
          </pre>
        )}

        <p className="border-t border-[var(--border)] px-4 py-2.5 text-[11px] leading-relaxed text-[var(--text-dim)]">
          {status.mcp.note}
        </p>
      </Panel>

      <Panel title="Runtime">
        <KeyValue
          rows={[
            ["Version", status.version],
            ["Environment", status.environment],
            ["Database", status.database],
            ["Queue depth", String(status.queue_depth)],
          ]}
        />
      </Panel>
    </div>
  );
}
