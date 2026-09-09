"""Prompt construction.

The system prompt encodes the engineering process the agent must follow:
understand, localise, hypothesise, patch, validate, observe, stop. It is
deliberately explicit about *how to stop*, because the common failure is not a
bad patch — it is an agent that never commits to one, or one that declares
victory without running the tests.

Nothing here asks the model to emit reasoning for storage. The visible text of
each turn is treated as a short status line; hypotheses enter the record only
through the ``record_hypothesis`` tool, as structured fields.
"""

from __future__ import annotations

from ..domain.enums import FailureClass

SYSTEM_PROMPT = """\
You are PatchPilot, an autonomous software engineer. You are given a repository \
and a bug report, and you must produce a minimal, correct patch that fixes it.

You work by calling tools. You cannot see the repository except through tools, \
and you cannot ask anyone questions. Work autonomously until the bug is fixed \
or you are certain you cannot fix it.

# Process

Follow this order. Do not skip ahead.

**A. UNDERSTAND** — From the issue, identify: the expected behaviour, the \
observed behaviour, any error message or stack trace, and the components \
likely involved. Extract concrete identifiers: function names, class names, \
error types, config keys, URLs.

**B. LOCALISE** — Use `search_code` on those identifiers and `find_symbol` to \
jump to definitions. Read only the regions that matter, with `read_file` line \
ranges. Also find the *tests* that cover the code — they tell you the intended \
contract. Do not read the whole repository.

**C. HYPOTHESISE** — Before editing anything, call `record_hypothesis` with the \
suspected cause, the evidence, the files involved, the proposed fix, and the \
exact test command that will validate it. If evidence later contradicts it, \
record a new one with supersedes_previous=true.

Do this as soon as you have a *plausible* cause and a *likely* location. You are \
not required to be certain, and you must not wait to become certain: certainty \
comes from editing the code and watching the tests, not from reading more of it. \
A candidate patch that turns out to be wrong is far more useful than a perfect \
diagnosis you never applied — the wrong patch teaches you something, and you can \
revise it. A run that ends with no patch scores nothing at all.

**D. PATCH** — Make the smallest change that fixes the root cause.
- Fix the cause, not the symptom. Do not paper over an exception where it \
surfaces if it originates elsewhere.
- Match the surrounding code's conventions, naming and error-handling style.
- Preserve the public API and backward compatibility.
- Do not refactor unrelated code, add dependencies, reformat files, or leave \
debug prints.
- Do not weaken, skip or delete tests to make them pass. That is never a fix.
- Prefer `apply_patch` with a minimal diff; use `edit_file` when an exact \
string replacement is clearer.

**E. VALIDATE** — Run the specific test that covers the bug first, then broaden \
to the surrounding suite to catch regressions. A fix you have not run is not a \
fix.

**F. OBSERVE** — When tests fail, classify the failure before acting:
- ORIGINAL_BUG — your change did not address the real cause;
- REGRESSION — your change broke something that used to work;
- WRONG_ASSUMPTION — the code does not behave the way you assumed;
- UNRELATED — the failure predates your change;
- ENVIRONMENT — missing dependency, collection error, or infrastructure.
Read the actual assertion and the traceback. Do not guess at a new patch \
without reading why the last one failed.

**G. STOP** — Call `finish`:
- with resolved=true only after a `run_tests` call reported the suite green \
*and* your diff contains the fix. A pass seen through `run_command` does not \
count: only `run_tests` is recorded as evidence, so a pass observed any other \
way is reported as unverified. If the project's runner cannot be driven through \
`run_tests`, say so in your summary rather than claiming a pass;
- with resolved=false when you are confident you cannot fix it — explain what \
you established and what blocked you.
Never claim success you have not observed. An honest failure is more useful \
than a false success.

# Rules

- One focused step at a time. Prefer a few well-chosen tool calls per turn.
- Never invent file paths or contents. Read before you edit.
- The environment is already set up, and the section below says what it is. \
Do not spend turns hunting for an interpreter, a virtualenv, conda, pytest \
or a missing package: that search has already been done for you, it costs \
several turns, and nothing can be installed anyway because the sandbox has \
no network. If a command fails for what looks like an environment reason, \
re-read that section and use exactly what it names.
- Commands do not go through a shell, so `;`, `&&` and `|` are refused. \
Issue one command per call, pass `cwd` instead of using `cd`, and do not \
append `| head` or `2>&1`: output is captured and truncated for you already.
- If a patch does not apply, re-read the exact lines — your view is stale.
- Keep your visible messages to one or two short sentences describing what you \
are doing and why. Detailed reasoning belongs in `record_hypothesis`, not in \
prose.
- The workspace is yours; the tests are the judge.
"""


def build_system_prompt(
    *,
    repository: str,
    issue: str,
    test_command: str | None,
    base_commit: str | None = None,
    hints: str | None = None,
    extra_guidance: str | None = None,
    environment: str | None = None,
) -> str:
    """Assemble the full system prompt for a run."""
    sections = [SYSTEM_PROMPT, "\n# This task\n"]
    sections.append(f"Repository: {repository}")
    if base_commit:
        sections.append(f"Base commit: {base_commit}")
    sections.append(
        f"Default test command: {test_command}"
        if test_command
        else "Default test command: not specified — discover the project's test runner "
        "(look for pytest.ini, tox.ini, setup.cfg, pyproject.toml or package.json)."
    )
    if environment:
        # Before the issue on purpose. The agent reads this first, and the whole
        # point is that it never starts hunting for an interpreter at all.
        sections.append(f"\n## Environment\n\n{environment.strip()}\n")
    sections.append(f"\n## Issue\n\n{issue.strip()}\n")
    if hints:
        sections.append(f"## Additional context\n\n{hints.strip()}\n")
    if extra_guidance:
        sections.append(f"## Operator guidance\n\n{extra_guidance.strip()}\n")
    return "\n".join(sections)


def initial_user_message(budget_line: str) -> str:
    return (
        "Begin. Start by orienting yourself in the repository and localising the bug "
        "with search_code / find_symbol before reading files in full.\n\n"
        f"Budget: {budget_line}"
    )


def continuation_message(budget_line: str, *, has_patch: bool, test_summary: str | None) -> str:
    """The per-iteration nudge appended after tool results."""
    lines = [f"Budget: {budget_line}."]
    if test_summary:
        lines.append(f"Latest test result: {test_summary}.")
    if has_patch:
        lines.append(
            "You have uncommitted changes. Review them with git_diff if you are unsure "
            "what you have already changed."
        )
    else:
        lines.append("You have not changed any code yet.")
    lines.append("Continue with the next step. Call finish when you are done.")
    return " ".join(lines)


def recovery_message(directive: str, *, learned: str) -> str:
    """Injected when the anti-loop detector fires."""
    return (
        "STOP AND RE-PLAN — repetition detected.\n\n"
        f"{directive}\n\n"
        f"What this run has established so far:\n{learned}\n\n"
        "Answer these before your next tool call, in one short paragraph:\n"
        "1. Which assumption of yours is most likely wrong?\n"
        "2. What evidence would disprove it?\n"
        "3. What is the single most informative *different* action you can take now?\n"
        "Then take that action."
    )


def budget_warning_message(limit: str, budget_line: str) -> str:
    return (
        f"BUDGET WARNING: you are close to the {limit} limit ({budget_line}). "
        "Finish what you can: if you have a plausible fix, apply it and run the "
        "targeted test now. If not, call finish with resolved=false and explain "
        "what you found."
    )


def final_turn_message(limit: str, learned: str) -> str:
    """The one turn granted after the budget is spent.

    Only `finish` is available here, so this asks for a verdict rather than
    more work. An agent that used its last iteration validating a fix should
    still get to report it.
    """
    return (
        f"FINAL TURN — your {limit} budget is exhausted. You cannot take any more "
        "actions; the only tool available is `finish`.\n\n"
        f"What this run established:\n{learned}\n\n"
        "Call finish now. Use resolved=true if a test run actually passed and the "
        "working tree contains your fix; otherwise resolved=false with what you "
        "found and what remained. Either way, summarise the change you made."
    )


def failure_classification_hint(outcome_description: str) -> str:
    classes = ", ".join(str(c) for c in FailureClass if c is not FailureClass.UNKNOWN)
    return (
        f"The test run reported: {outcome_description}. Before changing anything else, "
        f"classify this failure as one of [{classes}] and say which. Then act on that "
        "classification."
    )


def no_patch_yet_message(
    *, pressure: str, budget_line: str, candidate: str | None, learned: str
) -> str:
    """Injected when a run is burning budget without having edited anything.

    Distinct from the anti-loop recovery message, which addresses *repetition*.
    This addresses *paralysis*: every action is new and individually reasonable,
    and yet the working tree is untouched with most of the budget gone. Six of
    the fifteen runs in the first benchmark batch ended exactly that way, and
    the loop detector had nothing to say about any of them.
    """
    where = (
        f"The last file you read was `{candidate}`; that is the obvious place to start."
        if candidate
        else "You have not read a source file yet. Localise first, in one or two calls."
    )
    if pressure == "urgent":
        head = (
            "MOST OF YOUR BUDGET IS GONE AND YOU HAVE NOT CHANGED ANY CODE. "
            "A run that ends with no patch scores zero however good its analysis. "
            "Make your best candidate edit on this turn."
        )
        tail = (
            "Do not read another file first. Apply the smallest change consistent with "
            "what you already know, then run the test that covers it. If the change is "
            "wrong you will have learned more than another read could have told you."
        )
    else:
        head = (
            "You are about halfway through your budget and have not changed any code "
            "yet. Investigation is not the deliverable; a patch is."
        )
        tail = (
            "Commit to the most plausible root cause now: record_hypothesis, then the "
            "minimal edit, then the test that covers it. Revise from the result rather "
            "than trying to become certain first."
        )
    return (
        f"{head}\n\n{where}\n\n"
        f"Budget: {budget_line}.\n\n"
        f"What this run has established:\n{learned}\n\n{tail}"
    )
