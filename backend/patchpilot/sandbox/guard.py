"""Command guard.

Defence in depth for the ``run_command`` / ``run_tests`` tools. Even when the
sandbox itself is isolated, we refuse commands that are obviously destructive,
exfiltrating, or an attempt to escape the workspace. The guard is a *deny*
filter over an argv list — never over a shell string, because we never invoke a
shell.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Executables that are never allowed, regardless of arguments.
BLOCKED_EXECUTABLES: frozenset[str] = frozenset(
    {
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

#: Argument patterns that indicate an escape or a destructive wipe.
DANGEROUS_ARG_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^(/|[A-Za-z]:[\\/])$"), "targets a filesystem root"),
    (re.compile(r"^~($|[\\/])"), "targets the host home directory"),
    (re.compile(r"^\$\{?HOME"), "targets the host home directory"),
    (re.compile(r"(^|[\\/])\.ssh([\\/]|$)"), "touches SSH credentials"),
    (re.compile(r"(^|[\\/])\.aws([\\/]|$)"), "touches cloud credentials"),
    (re.compile(r"(^|[\\/])\.env($|[\\/.])"), "touches an environment secrets file"),
    (re.compile(r"(^|[\\/])\.git-credentials$"), "touches git credentials"),
)

#: Shell metacharacters. We never run a shell, so their presence in an argument
#: means the model is trying to smuggle a second command.
SHELL_METACHARACTERS = re.compile(r"[;&|`$<>]|\|\||&&|\$\(")


@dataclass(slots=True)
class GuardVerdict:
    allowed: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.allowed


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

    for arg in argv[1:]:
        if SHELL_METACHARACTERS.search(arg):
            return GuardVerdict(
                False,
                f"argument {arg!r} contains shell metacharacters; commands run without a shell",
            )
        for pattern, reason in DANGEROUS_ARG_PATTERNS:
            if pattern.search(arg):
                return GuardVerdict(False, f"argument {arg!r} {reason}")

    return GuardVerdict(True)
