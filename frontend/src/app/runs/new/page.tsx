"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import {
  Badge,
  Button,
  ErrorBanner,
  Field,
  Input,
  Panel,
  Select,
  Textarea,
  WarningBanner,
} from "@/components/ui";
import { ApiError, api, type CreateRunInput } from "@/lib/api";
import type { Fixture, SystemStatus } from "@/lib/types";

type Source = "fixture" | "url" | "local";

export default function NewRunPage() {
  const router = useRouter();
  const [fixtures, setFixtures] = useState<Fixture[]>([]);
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [source, setSource] = useState<Source>("fixture");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showAdvanced, setShowAdvanced] = useState(false);

  const [form, setForm] = useState({
    fixture: "",
    repo_url: "",
    local_path: "",
    issue: "",
    base_commit: "",
    issue_url: "",
    test_command: "",
    provider: "",
    model: "",
    sandbox: "",
    max_iterations: "",
    max_runtime_s: "",
  });

  useEffect(() => {
    (async () => {
      try {
        const [f, s] = await Promise.all([api.fixtures(), api.status()]);
        setFixtures(f);
        setStatus(s);
        if (f.length > 0) {
          setForm((prev) => ({ ...prev, fixture: f[0].id, issue: f[0].issue }));
        }
      } catch {
        /* the banner below covers an unreachable API */
      }
    })();
  }, []);

  const set = (key: keyof typeof form, value: string) =>
    setForm((prev) => ({ ...prev, [key]: value }));

  const chooseFixture = (id: string) => {
    const fixture = fixtures.find((f) => f.id === id);
    setForm((prev) => ({
      ...prev,
      fixture: id,
      issue: fixture?.issue ?? prev.issue,
      test_command: fixture?.test_command ?? "",
    }));
  };

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setError(null);
    setSubmitting(true);

    const payload: CreateRunInput = {
      issue: form.issue,
      base_commit: form.base_commit || null,
      issue_url: form.issue_url || null,
      test_command: form.test_command || null,
      provider: form.provider || null,
      model: form.model || null,
      sandbox: form.sandbox || null,
      max_iterations: form.max_iterations ? Number(form.max_iterations) : null,
      max_runtime_s: form.max_runtime_s ? Number(form.max_runtime_s) : null,
    };
    if (source === "fixture") payload.fixture = form.fixture;
    if (source === "url") payload.repo_url = form.repo_url;
    if (source === "local") payload.local_path = form.local_path;

    try {
      const run = await api.createRun(payload);
      router.push(`/runs/${run.id}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
      setSubmitting(false);
    }
  };

  const providerNotReady = status && !status.provider.ready;
  const isTestDouble = status?.provider.is_test_double;

  return (
    <div className="mx-auto max-w-3xl space-y-5">
      <div>
        <h1 className="text-xl font-semibold tracking-tight">Start an autonomous fix</h1>
        <p className="mt-1 text-[13px] text-[var(--text-muted)]">
          The agent explores the repository, forms a hypothesis, patches the code and runs
          the tests until they pass or a budget is exhausted.
        </p>
      </div>

      {providerNotReady && (
        <WarningBanner
          items={[
            `${status.provider.detail} The run will be queued but cannot call a model until a key is configured.`,
          ]}
        />
      )}
      {isTestDouble && (
        <WarningBanner
          items={[
            "The provider is set to the scripted test double. Runs will not use a real model.",
          ]}
        />
      )}
      {error && <ErrorBanner message={error} />}

      <form onSubmit={submit} className="space-y-5">
        <Panel title="Repository">
          <div className="space-y-4 p-4">
            <div className="flex gap-1 rounded-md border border-[var(--border)] bg-[var(--bg-input)] p-1">
              {(
                [
                  ["fixture", "Built-in fixture"],
                  ["url", "Repository URL"],
                  ["local", "Local path"],
                ] as [Source, string][]
              ).map(([id, label]) => (
                <button
                  key={id}
                  type="button"
                  onClick={() => setSource(id)}
                  className={`focus-ring flex-1 rounded px-3 py-1.5 text-[12px] font-medium transition-colors ${
                    source === id
                      ? "bg-[var(--bg-panel)] text-[var(--text)] shadow-sm"
                      : "text-[var(--text-dim)] hover:text-[var(--text-muted)]"
                  }`}
                >
                  {label}
                </button>
              ))}
            </div>

            {source === "fixture" && (
              <Field
                label="Fixture"
                hint="tiny repositories with known bugs — the cheapest way to see the loop work"
              >
                <div className="space-y-2">
                  {fixtures.map((fixture) => (
                    <label
                      key={fixture.id}
                      className={`flex cursor-pointer items-start gap-3 rounded-md border p-3 transition-colors ${
                        form.fixture === fixture.id
                          ? "border-[var(--color-accent)] bg-[var(--color-accent-soft)]/25"
                          : "border-[var(--border)] hover:border-[var(--border-strong)]"
                      }`}
                    >
                      <input
                        type="radio"
                        name="fixture"
                        checked={form.fixture === fixture.id}
                        onChange={() => chooseFixture(fixture.id)}
                        className="mt-1"
                      />
                      <div className="min-w-0">
                        <div className="flex items-center gap-2">
                          <span className="mono text-[12px] font-medium text-[var(--text)]">
                            {fixture.id}
                          </span>
                          <Badge tone={fixture.difficulty === "easy" ? "ok" : "warn"}>
                            {fixture.difficulty}
                          </Badge>
                        </div>
                        <p className="mt-0.5 text-[12px] text-[var(--text-muted)]">
                          {fixture.title}
                        </p>
                        <p className="mt-1 text-[11px] text-[var(--text-dim)]">
                          {fixture.expected_failing_tests.length} failing tests ·{" "}
                          {fixture.expected_files.join(", ")}
                        </p>
                      </div>
                    </label>
                  ))}
                  {fixtures.length === 0 && (
                    <p className="text-[12px] text-[var(--text-dim)]">
                      No fixtures available — is the backend running?
                    </p>
                  )}
                </div>
              </Field>
            )}

            {source === "url" && (
              <>
                <Field label="Repository URL" required>
                  <Input
                    value={form.repo_url}
                    onChange={(e) => set("repo_url", e.target.value)}
                    placeholder="https://github.com/example/project"
                    required
                  />
                </Field>
                <Field label="Base commit" hint="optional — defaults to the default branch">
                  <Input
                    value={form.base_commit}
                    onChange={(e) => set("base_commit", e.target.value)}
                    placeholder="d26b2424437dabeeca94d7900b37d2df4410da0c"
                    className="mono"
                  />
                </Field>
              </>
            )}

            {source === "local" && (
              <Field
                label="Local repository path"
                hint="an absolute path on the machine running the backend"
                required
              >
                <Input
                  value={form.local_path}
                  onChange={(e) => set("local_path", e.target.value)}
                  placeholder="/home/you/code/project"
                  className="mono"
                  required
                />
              </Field>
            )}
          </div>
        </Panel>

        <Panel title="Issue">
          <div className="space-y-4 p-4">
            <Field
              label="Description"
              hint="expected behaviour, observed behaviour, error messages, reproduction"
              required
            >
              <Textarea
                value={form.issue}
                onChange={(e) => set("issue", e.target.value)}
                rows={9}
                required
                minLength={10}
                placeholder={
                  "Login endpoint returns 500 when the refresh token has expired.\n\n" +
                  "Expected: 401, like every other rejected token.\n" +
                  "Observed: 500 internal_error.\n\n" +
                  "Reproduce: issue a token with a past TTL, then call refresh_session()."
                }
              />
            </Field>
            <div className="grid gap-4 sm:grid-cols-2">
              <Field label="GitHub issue URL" hint="optional">
                <Input
                  value={form.issue_url}
                  onChange={(e) => set("issue_url", e.target.value)}
                  placeholder="https://github.com/example/project/issues/142"
                />
              </Field>
              <Field label="Test command" hint="optional — discovered if omitted">
                <Input
                  value={form.test_command}
                  onChange={(e) => set("test_command", e.target.value)}
                  placeholder="python -m pytest tests/ -q"
                  className="mono"
                />
              </Field>
            </div>
          </div>
        </Panel>

        <Panel
          title="Model & budgets"
          actions={
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={() => setShowAdvanced((v) => !v)}
            >
              {showAdvanced ? "Hide" : "Configure"}
            </Button>
          }
        >
          {showAdvanced ? (
            <div className="grid gap-4 p-4 sm:grid-cols-2">
              <Field label="Provider" hint={`default: ${status?.provider.provider ?? "—"}`}>
                <Select value={form.provider} onChange={(e) => set("provider", e.target.value)}>
                  <option value="">Use configured default</option>
                  <option value="anthropic">anthropic</option>
                  <option value="openai">openai</option>
                  <option value="scripted">scripted (test double)</option>
                </Select>
              </Field>
              <Field label="Model" hint={`default: ${status?.provider.model ?? "—"}`}>
                <Input
                  value={form.model}
                  onChange={(e) => set("model", e.target.value)}
                  placeholder={status?.provider.model ?? "claude-sonnet-5"}
                  className="mono"
                />
              </Field>
              <Field
                label="Sandbox"
                hint={
                  status?.sandbox.docker_available
                    ? "docker available"
                    : "docker unavailable on this machine"
                }
              >
                <Select value={form.sandbox} onChange={(e) => set("sandbox", e.target.value)}>
                  <option value="">Use configured default</option>
                  <option value="local">local (process isolation)</option>
                  <option value="docker">docker (container isolation)</option>
                </Select>
              </Field>
              <Field label="Max iterations" hint={`default: ${status?.budgets.max_iterations ?? 30}`}>
                <Input
                  type="number"
                  min={1}
                  max={200}
                  value={form.max_iterations}
                  onChange={(e) => set("max_iterations", e.target.value)}
                  placeholder={String(status?.budgets.max_iterations ?? 30)}
                />
              </Field>
              <Field label="Max runtime (seconds)" hint={`default: ${status?.budgets.max_runtime_s ?? 1800}`}>
                <Input
                  type="number"
                  min={30}
                  max={21600}
                  value={form.max_runtime_s}
                  onChange={(e) => set("max_runtime_s", e.target.value)}
                  placeholder={String(status?.budgets.max_runtime_s ?? 1800)}
                />
              </Field>
            </div>
          ) : (
            <p className="px-4 py-3 text-[12px] text-[var(--text-dim)]">
              Using configured defaults: {status?.provider.provider ?? "—"} /{" "}
              <span className="mono">{status?.provider.model ?? "—"}</span>,{" "}
              {status?.sandbox.configured ?? "—"} sandbox,{" "}
              {status?.budgets.max_iterations ?? 30} iterations max.
            </p>
          )}
        </Panel>

        <div className="flex items-center gap-3">
          <Button type="submit" variant="primary" disabled={submitting}>
            {submitting ? "Queuing…" : "Start autonomous fix"}
          </Button>
          <span className="text-[11px] text-[var(--text-dim)]">
            The run executes in the background; you will be taken to its live workspace.
          </span>
        </div>
      </form>
    </div>
  );
}
