"""Command guard.

Defence in depth for the ``run_command`` / ``run_tests`` tools. Even when the
sandbox itself is isolated, we refuse commands that are obviously destructive,
exfiltrating, or an attempt to escape the workspace. The guard is a *deny*
filter over an argv list — never over a shell string, because we never invoke a
shell.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Executables that are never allowed, regardless of arguments.
BLOCKED_EXECUTABLES: frozenset[str] = frozenset(
    {
        # Shells. Blocking these is what makes the rest of this module sound:
        # PatchPilot never invokes a shell, so metacharacters in arguments are
        # inert -- unless the model launches a shell itself.
        "sh", "bash", "zsh", "dash", "ksh", "fish", "csh", "tcsh",
        "sudo",
        "su",
        "doas",
        "shutdown",
        "reboot",
        "halt",
        "poweroff",
        "mkfs",
        "fdisk",
        "diskpart",
        "dd",
        "chown",
        "chmod",
        "useradd",
        "usermod",
        "passwd",
        "ssh",
        "scp",
        "sftp",
        "ssh-keygen",
        "ssh-add",
        "nc",
        "ncat",
        "netcat",
        "telnet",
        "ftp",
        "systemctl",
        "service",
        "docker",
        "podman",
        "kubectl",
        "aws",
        "gcloud",
        "az",
        "gh",
        "crontab",
        "at",
        "reg",
        "regedit",
        "wmic",
        "powershell",
        "pwsh",
        "cmd",
        "taskkill",
        "sc",
        "net",
    }
)

#: Git subcommands that mutate remotes or history in ways a bug-fix run must not.
BLOCKED_GIT_SUBCOMMANDS: frozenset[str] = frozenset(
    {"push", "remote", "config", "clone", "submodule", "fetch", "pull", "credential"}
)

#: Network fetchers. Blocked so a run cannot exfiltrate the workspace even when
#: the sandbox cannot enforce ``--network none`` (i.e. the local sandbox).
NETWORK_EXECUTABLES: frozenset[str] = frozenset(
    {"curl", "wget", "httpie", "http", "rsync", "svn", "hg"}
)

#: Commands that act destructively on whatever path they are given. Only for
#: these does a bare `/` or `~` argument mean something dangerous.
DESTRUCTIVE_EXECUTABLES: frozenset[str] = frozenset(
    {"rm", "rmdir", "del", "erase", "mv", "move", "cp", "copy", "chmod", "chown",
     "dd", "shred", "truncate", "mkfs", "rsync"}
)

#: Paths that are sensitive to *read*, not just to write. Narrow on purpose:
#: these are credential stores, and there is no legitimate reason for a
#: bug-fixing agent to touch them.
CREDENTIAL_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(^|[\\/])\.ssh([\\/]|$)"), "touches SSH credentials"),
    (re.compile(r"(^|[\\/])\.aws([\\/]|$)"), "touches cloud credentials"),
    (re.compile(r"(^|[\\/])\.env($|[\\/.])"), "touches an environment secrets file"),
    (re.compile(r"(^|[\\/])\.git-credentials$"), "touches git credentials"),
    (re.compile(r"(^|[\\/])\.netrc$"), "touches network credentials"),
)

#: Paths that only matter as the *target* of a destructive command.
DESTRUCTIVE_TARGETS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^(/|[A-Za-z]:[\\/])$"), "targets a filesystem root"),
    (re.compile(r"^~($|[\\/])"), "targets the host home directory"),
    (re.compile(r"^\$\{?HOME"), "targets the host home directory"),
)

#: Tokens that only make sense to a shell. We never run one, so shlex has split
#: them into inert arguments that will merely confuse the target program —
#: refusing with an explanation is more useful than letting `pytest` receive a
#: literal `&&`.
#:
#: NB: this deliberately matches a *whole argument*, not a substring. A
#: semicolon inside `python -c "import numpy; print(x)"` is Python syntax and is
#: completely inert without a shell; an earlier version rejected it and wasted
#: a third of the agent's budget on refused, entirely safe commands.
SHELL_OPERATOR_TOKENS: frozenset[str] = frozenset(
    {";", "&&", "||", "|", "&", ">", ">>", "<", "<<", "2>", "2>&1"}
)


#: Trailing shell noise that exists only to shape output, and that the sandbox
#: already does: stderr is captured either way, and stdout is truncated to
#: ``max_output_bytes``. Dropping these tokens changes nothing about what runs.
_REDIRECT_NOISE: frozenset[str] = frozenset({"2>&1", "2>/dev/null", ">/dev/null", "1>/dev/null"})

#: Pagers whose only effect is to shorten output we already cap.
_PAGERS: frozenset[str] = frozenset({"head", "tail", "cat"})


@dataclass(slots=True)
class GuardVerdict:
    allowed: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.allowed


@dataclass(slots=True)
class Sanitized:
    """An argv with inert shell decoration removed."""

    argv: list[str]
    cwd: str | None = None
    """Directory extracted from a leading ``cd <dir> &&``, if there was one."""
    notes: list[str] = field(default_factory=list)
    """Human-readable description of what was dropped, shown to the agent."""


def sanitize_command(argv: list[str]) -> Sanitized:
    """Strip shell decoration that has no effect here, keeping the real command.

    Commands never reach a shell, so `cmd 2>&1 | head -20` arrives as literal
    trailing arguments that would confuse the program being run, and the guard
    refuses it. That refusal is correct but expensive: in the first SWE-bench
    batch 33 of 45 tool failures were refusals of this kind, each costing a
    whole iteration to discover and another to redo.

    Three of those forms carry no meaning without a shell and are simply
    removed:

    * ``cd <dir> && <command>``  -> the command, with ``cwd`` set to ``<dir>``
    * ``... 2>&1`` / ``... >/dev/null``  -> stderr and stdout are captured anyway
    * ``... | head -20``  -> output is already truncated to a byte cap

    Anything else -- a real pipeline into `grep`, a chain of two commands with
    ``;`` -- is left untouched for :func:`check_command` to refuse, because
    honouring it would mean running a shell.
    """
    argv = list(argv)
    notes: list[str] = []
    cwd: str | None = None

    if len(argv) >= 4 and argv[0] == "cd" and argv[2] == "&&":
        cwd, argv = argv[1], argv[3:]
        notes.append(f"ran it in {cwd} instead of using `cd`")

    # Drop trailing pager pipes, innermost last: `x | head -5 | tail -2`.
    while True:
        if len(argv) >= 2 and argv[-1] in _REDIRECT_NOISE:
            argv.pop()
            notes.append("dropped a redirect (both streams are captured for you)")
            continue
        cut = _pager_start(argv)
        if cut is not None:
            argv = argv[:cut]
            notes.append("dropped a pager (output is truncated for you already)")
            continue
        break

    return Sanitized(argv=argv, cwd=cwd, notes=notes)


def _pager_start(argv: list[str]) -> int | None:
    """Index of a trailing ``| head -n`` group, or None if there is not one."""
    for index in range(len(argv) - 1, 0, -1):
        if argv[index] != "|":
            continue
        tail = argv[index + 1 :]
        if not tail or _basename(tail[0]) not in _PAGERS:
            return None
        # Only a pager and its numeric flags may follow, else it is a real pipeline.
        for token in tail[1:]:
            if not re.fullmatch(r"-\d+|-[nc]|\d+", token):
                return None
        return index if index > 0 else None
    return None


def _basename(executable: str) -> str:
    name = executable.replace("\\", "/").rsplit("/", 1)[-1].lower()
    for suffix in (".exe", ".cmd", ".bat", ".com", ".ps1"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return name


def check_command(argv: list[str], *, allow_network: bool = False) -> GuardVerdict:
    """Decide whether ``argv`` may run inside the sandbox."""
    if not argv:
        return GuardVerdict(False, "empty command")

    executable = _basename(argv[0])

    if executable in BLOCKED_EXECUTABLES:
        return GuardVerdict(False, f"{executable!r} is a blocked host command")

    if executable in NETWORK_EXECUTABLES and not allow_network:
        return GuardVerdict(False, f"{executable!r} performs network access, which is disabled")

    if executable == "git":
        subcommand = next((a for a in argv[1:] if not a.startswith("-")), "")
        if subcommand.lower() in BLOCKED_GIT_SUBCOMMANDS:
            return GuardVerdict(
                False, f"'git {subcommand}' is not permitted inside a run"
            )

    if executable in {"rm", "rmdir", "del", "erase"}:
        recursive = any(a in {"-r", "-rf", "-fr", "-R", "/s", "/q"} for a in argv[1:])
        targets = [a for a in argv[1:] if not a.startswith(("-", "/"))]
        if recursive and any(t in {".", "..", "*"} for t in targets):
            return GuardVerdict(False, "recursive delete of the whole workspace is refused")

    destructive = executable in DESTRUCTIVE_EXECUTABLES

    for arg in argv[1:]:
        if arg in SHELL_OPERATOR_TOKENS:
            return GuardVerdict(
                False,
                f"{arg!r} is a shell operator, but commands run without a shell. "
                "Issue each command as a separate call.",
            )
        for pattern, reason in CREDENTIAL_PATTERNS:
            if pattern.search(arg):
                return GuardVerdict(False, f"argument {arg!r} {reason}")
        if destructive:
            for pattern, reason in DESTRUCTIVE_TARGETS:
                if pattern.search(arg):
                    return GuardVerdict(False, f"argument {arg!r} {reason}")

    return GuardVerdict(True)
