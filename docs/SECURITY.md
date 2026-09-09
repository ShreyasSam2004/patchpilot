# Security

PatchPilot clones arbitrary repositories and executes their test suites. The
threat model is straightforward: **repository code is untrusted, and the model
choosing which code to run is not a security boundary.**

## Threat model

| Threat | Mitigation |
|---|---|
| Repository test suite runs `rm -rf ~`, exfiltrates data, or mines crypto | Docker sandbox: `--network none`, cpu/memory/pid limits, `--cap-drop ALL`, no host mounts except the workspace |
| Model is prompt-injected by repository content into running a destructive command | Command guard (deny-list), no shell, path jail, default-deny tool policy |
| Model reads `~/.ssh/id_rsa` or `.env` and puts it in a patch | Path jail confines every read to the workspace; dangerous argument patterns refused |
| API keys leak into a subprocess environment | Environment rebuilt from an allow-list, plus a secret-name filter |
| API keys leak through the API or UI | `/api/config` reduces credentials to `"set"` / `null`; asserted by test |
| Agent pushes to a real remote | `git push`/`commit`/`remote`/`config` absent from the registry, denied by policy, *and* refused by the command guard |
| Runaway test consumes all memory or never exits | Wall-clock timeout with process-tree kill; output read under a hard byte cap |
| A hostile MCP server injects instructions via tool descriptions | Descriptions prefixed and truncated; discovery never implies permission |

## Execution

Commands are **always** an argv vector; a shell is never invoked. When the model
supplies a string it is tokenised with `shlex` (in non-POSIX mode on Windows so
backslash paths survive) and then validated.

`sandbox/guard.py` refuses:

- **blocked executables** — `sudo`, `su`, `ssh`, `scp`, `docker`, `kubectl`,
  `aws`, `gcloud`, `systemctl`, `reg`, `powershell`, `cmd`, `crontab`, …
- **network fetchers** — `curl`, `wget`, `rsync`, `svn`, `hg` (unless network is
  explicitly enabled)
- **mutating git subcommands** — `push`, `remote`, `config`, `clone`,
  `submodule`, `fetch`, `pull`, `credential`
- **recursive workspace deletes** — `rm -rf .`, `rm -rf *`
- **shells** — `sh`, `bash`, `zsh`, `dash`, `powershell`, `cmd`, … Blocking
  these is what makes the rest of the guard sound: metacharacters in arguments
  are inert precisely because no shell ever interprets them.
- **standalone shell operators** — an argument that is exactly `;`, `&&`, `|`,
  `>` … means the model tried to chain commands and shlex split it into an
  inert token that would only confuse the target program. Refused with a note
  to issue separate calls.
- **credential paths, for every command** — `.ssh`, `.aws`, `.env`,
  `.git-credentials`, `.netrc`
- **root and home paths, for destructive commands only** — `rm -rf /` is
  refused; `ls /` is not.

A metacharacter *inside* a larger argument is deliberately allowed.
`python -c "import numpy; print(x)"` is Python syntax, not shell syntax, and is
completely inert without a shell. An earlier version refused it and cost a real
run about a third of its tool budget on safe, useful commands — a guard that
blocks legitimate work for no security gain is a bug, not caution.

The guard applies to model-supplied commands (`run_tests`, `run_command`).
PatchPilot's own git plumbing calls the sandbox directly, which is why the
agent cannot run `git config` but `prepare_workspace` can.

## Isolation

### DockerSandbox — for untrusted repositories

One container per run:

```
docker run --detach
  --workdir /workspace  --volume <workspace>:/workspace
  --network none
  --memory 2g --memory-swap 2g --cpus 2 --pids-limit 512
  --cap-drop ALL --security-opt no-new-privileges
  --tmpfs /tmp:rw,noexec,nosuid,size=512m
  <image> sleep infinity
```

Commands are delivered with `docker exec` as an argv vector — no shell
interpolation on either side of the boundary. Host-specific environment
variables are stripped rather than passed through. A timeout kills the
in-container process, not just the `docker exec` client.

`--network none` has a consequence worth stating plainly: **nothing can be
installed at run time.** The image must already contain git, a test runner, and
the repository's dependencies. `backend/docker/runner.Dockerfile` builds a
384 MB image with git and a standard pytest stack (`make docker-image`); derive
from it for a project with extra dependencies. A missing image is caught before
the container starts and reported with the build command, rather than
`docker run` attempting a pull that cannot succeed.

If the daemon is unreachable, construction **raises** rather than silently
degrading; the caller decides whether to fall back, and the run record stores
which sandbox was actually used.

### LocalSandbox — for fixtures and development

Working-directory jail, environment allow-list, wall-clock timeout with
process-tree kill (`taskkill /F /T` on Windows, `killpg` elsewhere), and hard
output byte caps.

It provides **no kernel-level isolation**, and says so — in `sandbox.info()`,
in `/api/status`, on the landing page, and on the Settings page. It is not a
security boundary; it is a blast-radius reducer for code you already trust.

## Filesystem

Every model-supplied path goes through `Workspace.resolve()`:

- symlinks are resolved before the containment check, so a symlink out of the
  tree is caught;
- a path is treated as *rooted* if it looks absolute under **either** POSIX or
  Windows conventions. Without this, `/etc/passwd` is not `Path.is_absolute()`
  on Windows and would be quietly rewritten to `<root>/etc/passwd` — no escape,
  but the same input behaving differently per platform;
- anything resolving outside the root raises `PathEscapeError`;
- binary files are refused for text reads (by extension and by NUL sniffing);
- reads are capped at `max_file_read_bytes`.

## Secrets

Child processes receive an environment built from an allow-list (`PATH`,
`HOME`, locale, temp dirs, and the platform variables Windows needs). Anything
whose name contains `KEY`, `TOKEN`, `SECRET`, `PASSWORD`, `PASSWD`,
`CREDENTIAL`, `SESSION`, `COOKIE` or `AUTH` is dropped even if it somehow
reaches the allow-list — defence in depth.

Tests assert that a planted `ANTHROPIC_API_KEY` and `MY_DB_PASSWORD` do not
appear in a child process's environment, and that `extra` cannot smuggle one in.

`Settings.redacted()` replaces every credential with `"set"` or `null`, and
`test_config_never_exposes_a_key` plants a key and asserts it is absent from the
`/api/config` response.

## Agent constraints

- **Default-deny tool policy.** Unlisted tools are unreachable and are not even
  described to the model.
- **No chain-of-thought storage.** `thinking` blocks are dropped at the provider
  boundary. Only structured actions, observations, explicit hypotheses and the
  agent's short visible status line are persisted.
- **Guarded success.** `finish(resolved=true)` is refused without a patch and a
  green test run; a `RESOLVED` run with an empty diff is downgraded at
  finalisation.
- **Bounded.** Iterations, tool calls, wall-clock time and estimated spend are
  all capped, and the agent is told how much remains.

## Deployment notes

PatchPilot ships **no authentication**. It is a local developer tool. Before
exposing it beyond localhost you must add authentication and authorisation at
the API layer, and you should:

- set `PATCHPILOT_SANDBOX=docker`;
- run the worker as an unprivileged user with its own Docker context;
- put the workspace root on a volume with a quota;
- move to PostgreSQL and set `PATCHPILOT_CORS_ORIGINS` to your real origin;
- supply model credentials through a secret manager, never a committed `.env`.

## Reporting

This is a portfolio project, not a maintained product. If you find a
vulnerability, open an issue describing the impact rather than a working
exploit.
