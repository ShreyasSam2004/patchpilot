"""Fork publication against real git, plus the workflow-rejection diagnosis.

These tests drive the production helper -- :func:`_apply_and_push` -- rather
than a reimplementation of it, using local bare repositories reached over
``file://``. Nothing here talks to GitHub.

The interesting case is reproduced faithfully. GitHub refuses a push when the
ref being pushed would change a workflow file, judged against the destination's
default branch. A branch cut from a base that predates a workflow change carries
the older copy of that file, so it trips the check even though the commit it
introduces never touches ``.github/``. Locally that server-side refusal is a
``pre-receive`` hook emitting GitHub's own wording; everything on this side of
the wire -- the objects git sends, the commit, the diagnosis -- is real.

Fixture patches are produced *by git*, not written by hand, because the approval
digest is byte-exact and a hand-written diff has no ``index`` line.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from patchpilot.domain.enums import PublishErrorCode
from patchpilot.publish.branch import ProtectedBranchError
from patchpilot.publish.github import (
    _apply_and_push,
    _is_workflow_rejection,
    _PushPlan,
    _verify_commit,
    _workflow_drift,
)
from patchpilot.publish.ports import GitHostingError
from patchpilot.publish.verify import changed_paths, workflow_paths

WORKFLOW = ".github/workflows/ci.yml"

#: GitHub's wording, for both token kinds. Used verbatim by the fake hook.
CLASSIC_REFUSAL = (
    "refusing to allow a Personal Access Token to create or update workflow "
    "`.github/workflows/ci.yml` without `workflow` scope"
)
FINE_GRAINED_REFUSAL = (
    "refusing to allow a fine-grained personal access token to create or update "
    "workflow `.github/workflows/ci.yml` without `workflows` permission"
)


# --------------------------------------------------------------- git fixtures


def _run(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )
    return done.stdout


def _write(work: Path, relative: str, text: str) -> None:
    target = work / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")


def _commit(work: Path, message: str) -> str:
    _run(work, "add", "-A")
    _run(work, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
         "commit", "--quiet", "-m", message)
    return _run(work, "rev-parse", "HEAD").strip()


def _serve_any_sha(bare: Path) -> None:
    """Let a bare repo hand out arbitrary SHAs, the way GitHub does."""
    _run(bare, "config", "uploadpack.allowAnySHA1InWant", "true")


@dataclass
class World:
    """Upstream, a fork of it, and patches generated against known bases."""

    tmp: Path
    upstream: Path
    fork: Path
    seed: Path
    base: str
    master: str
    make_patch: Callable[[str, Callable[[Path], None]], str]

    @property
    def patch(self) -> str:
        """The approved patch: ``src.py`` only, based on the review base."""
        return self.make_patch(self.base, lambda w: _write(w, "src.py", _SRC.format(42)))


_SRC = "def value():\n    return {}\n"

#: Long enough that changing one line keeps similarity above git's 50% rename
#: threshold, so a rename is actually reported as a rename rather than as an
#: unrelated delete plus add.
_LIB = "".join(f"def helper_{i}():\n    return {i}\n\n\n" for i in range(12))


@pytest.fixture
def world(tmp_path: Path) -> World:
    """History ``A -- B(base) -- M(master, edits the workflow)``."""
    upstream = tmp_path / "upstream.git"
    _run(tmp_path, "init", "--quiet", "--bare", str(upstream))
    _serve_any_sha(upstream)

    seed = tmp_path / "seed"
    _run(tmp_path, "init", "--quiet", str(seed))
    _run(seed, "config", "core.autocrlf", "false")
    _run(seed, "symbolic-ref", "HEAD", "refs/heads/master")
    _write(seed, "src.py", _SRC.format(1))
    _write(seed, "lib.py", _LIB)
    _write(seed, WORKFLOW, "name: ci\non: [push]\njobs: {}\n")
    _commit(seed, "A")

    _write(seed, "src.py", _SRC.format(2))
    base = _commit(seed, "B")

    # Everything after the review base, including a workflow edit.
    _write(seed, WORKFLOW, "name: ci\non: [push, pull_request]\njobs: {}\n")
    _write(seed, "README.md", "docs\n")
    master = _commit(seed, "M")

    _run(seed, "remote", "add", "origin", upstream.as_uri())
    _run(seed, "push", "--quiet", "origin", "master:refs/heads/master")

    fork = tmp_path / "fork.git"
    _run(tmp_path, "clone", "--quiet", "--bare", upstream.as_uri(), str(fork))
    _serve_any_sha(fork)

    def make_patch(at: str, mutate: Callable[[Path], None]) -> str:
        """A real ``git diff`` for ``mutate`` applied on top of ``at``."""
        _run(seed, "checkout", "--quiet", "--detach", at)
        mutate(seed)
        _run(seed, "add", "-A")
        diff = _run(seed, "diff", "--cached", "--no-color", "--no-ext-diff", "--unified=3")
        _run(seed, "reset", "--hard", "--quiet", at)
        _run(seed, "checkout", "--quiet", "master")
        return diff

    return World(
        tmp=tmp_path,
        upstream=upstream,
        fork=fork,
        seed=seed,
        base=base,
        master=master,
        make_patch=make_patch,
    )


def _plan(world: World, **overrides: object) -> _PushPlan:
    workdir = overrides.pop("workdir", None)
    if not isinstance(workdir, Path):
        workdir = world.tmp / f"wd-{len(list(world.tmp.iterdir()))}"
    workdir.mkdir(parents=True, exist_ok=True)
    fields: dict[str, object] = {
        "workdir": workdir,
        "push_url": world.fork.as_uri(),
        "source_url": world.upstream.as_uri(),
        "source_label": "octo/upstream",
        "branch": "patchpilot/run-abc123",
        "base_commit_sha": world.base,
        "patch": world.patch,
        "commit_message": "Fix value()",
        "author_name": "PatchPilot",
        "author_email": "bot@example.invalid",
        "token": None,
        "default_branch": "master",
    }
    fields.update(overrides)
    return _PushPlan(**fields)  # type: ignore[arg-type]


def _reject_pushes(bare: Path, message: str) -> None:
    """Make the bare repo refuse pushes the way GitHub refuses workflow edits."""
    hook = bare / "hooks" / "pre-receive"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(
        f"#!/bin/sh\necho 'remote: {message}' >&2\nexit 1\n",
        encoding="utf-8",
        newline="\n",
    )
    hook.chmod(0o755)


# --------------------------------------------- the approved-patch path parser


def test_changed_paths_reads_a_simple_modification() -> None:
    diff = "diff --git a/src.py b/src.py\n--- a/src.py\n+++ b/src.py\n@@ -1 +1 @@\n-a\n+b\n"
    assert changed_paths(diff) == {"src.py"}


def test_changed_paths_reads_a_new_file() -> None:
    diff = "diff --git a/new.py b/new.py\n--- /dev/null\n+++ b/new.py\n@@ -0,0 +1 @@\n+a\n"
    assert changed_paths(diff) == {"new.py"}


def test_changed_paths_reads_a_deletion() -> None:
    diff = "diff --git a/old.py b/old.py\n--- a/old.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-a\n"
    assert changed_paths(diff) == {"old.py"}


def test_changed_paths_reports_both_sides_of_a_rename() -> None:
    diff = (
        "diff --git a/old.py b/new.py\nrename from old.py\nrename to new.py\n"
        "--- a/old.py\n+++ b/new.py\n@@ -1 +1 @@\n-a\n+b\n"
    )
    assert changed_paths(diff) == {"old.py", "new.py"}


def test_changed_paths_falls_back_to_the_git_header_when_there_are_no_hunks() -> None:
    diff = "diff --git a/s.sh b/s.sh\nold mode 100644\nnew mode 100755\n"
    assert changed_paths(diff) == {"s.sh"}


def test_changed_paths_ignores_a_trailing_timestamp() -> None:
    diff = "--- a/t.py\t2024-01-01\n+++ b/t.py\t2024-01-02\n@@ -1 +1 @@\n-a\n+b\n"
    assert changed_paths(diff) == {"t.py"}


def test_changed_paths_decodes_a_quoted_path() -> None:
    diff = '--- "a/od\\303\\251.py"\n+++ "b/od\\303\\251.py"\n@@ -1 +1 @@\n-a\n+b\n'
    assert any(path.endswith(".py") for path in changed_paths(diff))


def test_changed_paths_handles_several_files() -> None:
    diff = (
        "diff --git a/one.py b/one.py\n--- a/one.py\n+++ b/one.py\n@@ -1 +1 @@\n-a\n+b\n"
        "diff --git a/two.py b/two.py\n--- a/two.py\n+++ b/two.py\n@@ -1 +1 @@\n-c\n+d\n"
    )
    assert changed_paths(diff) == {"one.py", "two.py"}


def test_workflow_paths_selects_only_workflow_definitions() -> None:
    paths = {"src.py", WORKFLOW, ".github/CODEOWNERS", ".github/workflows/release.yml"}
    assert workflow_paths(paths) == {WORKFLOW, ".github/workflows/release.yml"}


# ------------------------------------------------- recognising GitHub's refusal


def test_a_classic_token_refusal_is_recognised() -> None:
    assert _is_workflow_rejection(f"git push failed: {CLASSIC_REFUSAL}")


def test_a_fine_grained_token_refusal_is_recognised() -> None:
    assert _is_workflow_rejection(f"git push failed: {FINE_GRAINED_REFUSAL}")


def test_an_ordinary_permission_failure_is_not_mistaken_for_a_workflow_refusal() -> None:
    assert not _is_workflow_rejection(
        "git push failed: remote: Permission to octo/repo.git denied to someone"
    )


# ------------------------------------------------------------- the happy paths


def test_a_fork_push_creates_the_branch_in_the_fork(world: World) -> None:
    plan = _plan(world)
    sha = _apply_and_push(plan)
    assert _run(world.fork, "rev-parse", f"refs/heads/{plan.branch}").strip() == sha


def test_the_fork_push_leaves_the_forks_own_branches_alone(world: World) -> None:
    before = _run(world.fork, "rev-parse", "refs/heads/master").strip()
    _apply_and_push(_plan(world))
    assert _run(world.fork, "rev-parse", "refs/heads/master").strip() == before


def test_the_base_is_fetched_from_upstream_for_a_fork_publication(world: World) -> None:
    """The fork is only a push destination; the reviewed base comes from upstream."""
    plan = _plan(world)
    sha = _apply_and_push(plan)

    remotes = _run(plan.workdir, "remote", "-v")
    assert world.upstream.as_uri() in remotes
    assert world.fork.as_uri() in remotes
    # And the commit really was built on upstream's object, not on the fork's tip.
    assert _run(plan.workdir, "rev-parse", f"{sha}^").strip() == world.base
    assert _run(plan.workdir, "rev-parse", f"{sha}^").strip() != world.master


def test_a_destination_without_the_reviewed_base_is_reported_clearly(world: World) -> None:
    """A shallow branch cannot land somewhere lacking its base; say so precisely."""
    empty = world.tmp / "empty-fork.git"
    _run(world.tmp, "init", "--quiet", "--bare", str(empty))
    _serve_any_sha(empty)

    with pytest.raises(GitHostingError) as caught:
        _apply_and_push(_plan(world, push_url=empty.as_uri()))
    assert caught.value.code is PublishErrorCode.FORK_BASE_UNAVAILABLE
    assert "does not contain the reviewed base commit" in str(caught.value)


def test_the_published_commit_diff_equals_the_approved_patch(world: World) -> None:
    plan = _plan(world)
    sha = _apply_and_push(plan)
    produced = _run(plan.workdir, "diff", "--no-color", "--unified=3", world.base, sha)
    assert produced.replace("\r\n", "\n") == plan.patch.replace("\r\n", "\n")


def test_the_published_commit_is_built_on_the_reviewed_base(world: World) -> None:
    plan = _plan(world)
    sha = _apply_and_push(plan)
    assert _run(plan.workdir, "rev-parse", f"{sha}^").strip() == world.base


def test_the_published_commit_touches_no_workflow_file(world: World) -> None:
    """The root-cause assertion: the introduced commit is not a workflow change."""
    plan = _plan(world)
    sha = _apply_and_push(plan)
    names = _run(plan.workdir, "diff", "--name-only", world.base, sha).split()
    assert names == ["src.py"]
    assert not workflow_paths(names)


def test_only_one_commit_becomes_newly_reachable_in_the_destination(world: World) -> None:
    """No workflow-touching ancestry is introduced along with the patch.

    Measured in the destination, which has the full history. The publishing
    workspace is shallow, so its own view cannot answer this question -- it
    cannot see that the base is already an ancestor of the default branch.
    """
    plan = _plan(world)
    sha = _apply_and_push(plan)
    introduced = _run(world.fork, "rev-list", f"master..{plan.branch}").split()
    assert introduced == [sha]


def test_the_push_introduces_no_object_under_dot_github(world: World) -> None:
    """The decisive measurement: no workflow object is transferred at all."""
    plan = _plan(world)
    sha = _apply_and_push(plan)
    listing = _run(world.fork, "rev-list", "--objects", f"master..{plan.branch}")
    # The commit has no path and the root tree an empty one; only named entries
    # are interesting here.
    paths = [
        line.split(" ", 1)[1].strip()
        for line in listing.splitlines()
        if " " in line and line.split(" ", 1)[1].strip()
    ]
    assert paths == ["src.py"]
    assert not any(path.startswith(".github") for path in paths)
    assert sha in listing


def test_the_scratch_patch_file_is_not_committed(world: World) -> None:
    plan = _plan(world)
    sha = _apply_and_push(plan)
    assert ".patchpilot-approved.patch" not in _run(
        plan.workdir, "ls-tree", "-r", "--name-only", sha
    )


# ------------------------------------------------------------ integrity guards


def test_a_commit_touching_an_unapproved_workflow_is_refused(world: World) -> None:
    """The commit edits the workflow; the approved patch does not."""
    def mutate(w: Path) -> None:
        _write(w, "src.py", _SRC.format(42))
        _write(w, WORKFLOW, "name: ci\non: [push, workflow_dispatch]\njobs: {}\n")

    plan = _plan(world, patch=world.make_patch(world.base, mutate))
    sha = _apply_and_push(plan)

    narrowed = _plan(world, workdir=plan.workdir, patch=world.patch)
    with pytest.raises(GitHostingError) as caught:
        _verify_commit(narrowed, base_sha=world.base, commit_sha=sha)
    assert caught.value.code is PublishErrorCode.UNAPPROVED_WORKFLOW_CHANGE
    assert WORKFLOW in str(caught.value)


def test_a_commit_touching_an_unapproved_source_file_is_refused(world: World) -> None:
    def mutate(w: Path) -> None:
        _write(w, "src.py", _SRC.format(42))
        _write(w, "extra.py", "surprise\n")

    plan = _plan(world, patch=world.make_patch(world.base, mutate))
    sha = _apply_and_push(plan)

    narrowed = _plan(world, workdir=plan.workdir, patch=world.patch)
    with pytest.raises(GitHostingError) as caught:
        _verify_commit(narrowed, base_sha=world.base, commit_sha=sha)
    assert caught.value.code is PublishErrorCode.UNAPPROVED_FILE_CHANGE
    assert "extra.py" in str(caught.value)


def test_the_same_files_with_different_content_is_still_refused(world: World) -> None:
    plan = _plan(world)
    sha = _apply_and_push(plan)

    other = world.make_patch(world.base, lambda w: _write(w, "src.py", _SRC.format(99)))
    with pytest.raises(GitHostingError) as caught:
        _verify_commit(
            _plan(world, workdir=plan.workdir, patch=other),
            base_sha=world.base,
            commit_sha=sha,
        )
    assert caught.value.code is PublishErrorCode.PUBLICATION_COMMIT_MISMATCH


def test_a_commit_on_the_wrong_base_is_refused(world: World) -> None:
    plan = _plan(world)
    sha = _apply_and_push(plan)
    with pytest.raises(GitHostingError) as caught:
        _verify_commit(plan, base_sha="0" * 40, commit_sha=sha)
    assert caught.value.code is PublishErrorCode.PUBLICATION_COMMIT_MISMATCH


def test_an_unavailable_base_commit_is_reported_clearly(world: World) -> None:
    with pytest.raises(GitHostingError) as caught:
        _apply_and_push(_plan(world, base_commit_sha="0" * 40))
    assert caught.value.code is PublishErrorCode.FORK_BASE_UNAVAILABLE
    assert "octo/upstream" in str(caught.value)


def test_a_patch_that_does_not_apply_is_reported_and_nothing_is_pushed(
    world: World,
) -> None:
    stale = world.patch.replace("    return 2\n", "    return 7\n")
    plan = _plan(world, patch=stale)
    with pytest.raises(GitHostingError) as caught:
        _apply_and_push(plan)
    assert caught.value.code is PublishErrorCode.PATCH_APPLY_FAILED
    assert plan.branch not in _run(world.fork, "for-each-ref", "--format=%(refname)")


# ------------------------------------------- the workflow rejection, end to end


def test_a_workflow_scope_rejection_is_reported_as_drift(world: World) -> None:
    _reject_pushes(world.fork, CLASSIC_REFUSAL)
    with pytest.raises(GitHostingError) as caught:
        _apply_and_push(_plan(world))
    assert caught.value.code is PublishErrorCode.WORKFLOW_DRIFT


def test_the_drift_message_names_the_workflow_that_differs(world: World) -> None:
    _reject_pushes(world.fork, CLASSIC_REFUSAL)
    with pytest.raises(GitHostingError) as caught:
        _apply_and_push(_plan(world))
    assert WORKFLOW in str(caught.value)


def test_the_drift_message_says_the_patch_touches_no_workflow(world: World) -> None:
    _reject_pushes(world.fork, FINE_GRAINED_REFUSAL)
    with pytest.raises(GitHostingError) as caught:
        _apply_and_push(_plan(world))
    text = str(caught.value)
    assert "touches no workflow" in text
    assert "src.py" in text


def test_the_drift_message_never_suggests_granting_workflow_access(world: World) -> None:
    """The security boundary is the point: the fix must not be 'get permission'."""
    _reject_pushes(world.fork, CLASSIC_REFUSAL)
    with pytest.raises(GitHostingError) as caught:
        _apply_and_push(_plan(world))
    lowered = str(caught.value).lower()
    assert "deliberately does not request workflow permission" in lowered
    for forbidden in ("add the workflow scope", "grant workflow", "enable workflow permission"):
        assert forbidden not in lowered


def test_a_workflow_refusal_wins_over_the_generic_hook_classification(
    world: World,
) -> None:
    """git also says 'pre-receive hook declined'; the specific reason must win."""
    _reject_pushes(world.fork, CLASSIC_REFUSAL)
    with pytest.raises(GitHostingError) as caught:
        _apply_and_push(_plan(world))
    assert caught.value.code is PublishErrorCode.WORKFLOW_DRIFT


def test_a_non_workflow_rejection_still_classifies_normally(world: World) -> None:
    _reject_pushes(world.fork, "Permission to octo/fork.git denied to someone")
    with pytest.raises(GitHostingError) as caught:
        _apply_and_push(_plan(world))
    assert caught.value.code is PublishErrorCode.PERMISSION_DENIED


def test_workflow_drift_finds_the_file_that_differs_from_the_default_branch(
    world: World,
) -> None:
    plan = _plan(world)
    sha = _apply_and_push(plan)
    assert _workflow_drift(plan, commit_sha=sha) == {WORKFLOW}


def test_workflow_drift_is_empty_when_the_base_is_the_default_branch_head(
    world: World,
) -> None:
    """No drift when the review was cut from the current head."""
    patch = world.make_patch(world.master, lambda w: _write(w, "src.py", _SRC.format(42)))
    plan = _plan(world, base_commit_sha=world.master, patch=patch)
    sha = _apply_and_push(plan)
    assert _workflow_drift(plan, commit_sha=sha) == frozenset()


def test_workflow_drift_reports_nothing_when_the_default_branch_is_unknown(
    world: World,
) -> None:
    plan = _plan(world, default_branch=None)
    sha = _apply_and_push(plan)
    assert _workflow_drift(plan, commit_sha=sha) == frozenset()


# ------------------------------------------ an existing fork must not contaminate


def test_a_fork_whose_default_branch_moved_on_does_not_contaminate_publication(
    world: World,
) -> None:
    """The fork's master is at M; the publication must still sit on B."""
    assert _run(world.fork, "rev-parse", "master").strip() == world.master

    plan = _plan(world)
    sha = _apply_and_push(plan)

    assert _run(plan.workdir, "rev-parse", f"{sha}^").strip() == world.base
    assert "README.md" not in _run(plan.workdir, "ls-tree", "-r", "--name-only", sha)


def test_unrelated_commits_in_the_fork_do_not_contaminate_publication(
    world: World,
) -> None:
    """A fork carrying its owner's own work publishes the same commit anyway."""
    owner_work = world.tmp / "owner"
    _run(world.tmp, "clone", "--quiet", str(world.fork), str(owner_work))
    _write(owner_work, "OWNER.md", "my own change\n")
    _write(owner_work, WORKFLOW, "name: ci\non: [schedule]\njobs: {}\n")
    _commit(owner_work, "owner's own commit")
    _run(owner_work, "push", "--quiet", "origin", "master:refs/heads/master")
    _run(owner_work, "push", "--quiet", "origin", "master:refs/heads/my-feature")

    plan = _plan(world)
    sha = _apply_and_push(plan)

    assert _run(plan.workdir, "rev-parse", f"{sha}^").strip() == world.base
    tree = _run(plan.workdir, "ls-tree", "-r", "--name-only", sha)
    assert "OWNER.md" not in tree
    produced = _run(plan.workdir, "diff", "--no-color", "--unified=3", world.base, sha)
    assert produced.replace("\r\n", "\n") == plan.patch.replace("\r\n", "\n")


def test_the_publication_tree_equals_the_base_tree_with_the_patch_applied(
    world: World,
) -> None:
    """``published_tree == apply(B, P)``, computed independently of the push."""
    plan = _plan(world)
    sha = _apply_and_push(plan)
    published_tree = _run(plan.workdir, "rev-parse", f"{sha}^{{tree}}").strip()

    # Recompute the expected tree in a separate checkout of B.
    expected = world.tmp / "expected"
    _run(world.tmp, "clone", "--quiet", str(world.upstream), str(expected))
    _run(expected, "checkout", "--quiet", "--detach", world.base)
    patch_file = expected / "p.patch"
    patch_file.write_text(plan.patch, encoding="utf-8", newline="\n")
    _run(expected, "apply", "--whitespace=nowarn", str(patch_file))
    patch_file.unlink()
    _run(expected, "add", "-A")
    assert _run(expected, "write-tree").strip() == published_tree


# ------------------------------------------------------- what may never be written


def test_an_abbreviated_base_sha_is_never_used_for_the_publication(
    world: World,
) -> None:
    """Short SHAs must not silently resolve to something else."""
    with pytest.raises(GitHostingError) as caught:
        _apply_and_push(_plan(world, base_commit_sha=world.base[:12]))
    assert caught.value.code in {
        PublishErrorCode.FORK_BASE_UNAVAILABLE,
        PublishErrorCode.PUBLICATION_COMMIT_MISMATCH,
    }


def test_the_upstream_repository_is_never_written_to(world: World) -> None:
    before = _run(world.upstream, "for-each-ref", "--format=%(refname) %(objectname)")
    _apply_and_push(_plan(world))
    assert _run(world.upstream, "for-each-ref", "--format=%(refname) %(objectname)") == before


def test_only_a_patchpilot_branch_may_be_pushed(world: World) -> None:
    before = _run(world.fork, "for-each-ref", "--format=%(refname) %(objectname)")
    for forbidden in ("master", "main", "release/2.0"):
        with pytest.raises(ProtectedBranchError) as caught:
            _apply_and_push(_plan(world, branch=forbidden))
        assert "refusing to push" in str(caught.value)
    assert _run(world.fork, "for-each-ref", "--format=%(refname) %(objectname)") == before


def test_an_unapproved_workflow_change_aborts_before_anything_is_pushed(
    world: World,
) -> None:
    def mutate(w: Path) -> None:
        _write(w, "src.py", _SRC.format(42))
        _write(w, WORKFLOW, "name: ci\non: [push, workflow_dispatch]\njobs: {}\n")

    # The approved patch is the one-file one; the commit would carry the workflow.
    plan = _plan(world, patch=world.make_patch(world.base, mutate))
    narrowed = _plan(world, workdir=plan.workdir, patch=world.patch)

    _apply_and_push(plan)  # builds the commit in this workspace
    branch_before = _run(world.fork, "rev-parse", f"refs/heads/{plan.branch}").strip()

    with pytest.raises(GitHostingError) as caught:
        _verify_commit(
            narrowed,
            base_sha=world.base,
            commit_sha=_run(plan.workdir, "rev-parse", "HEAD").strip(),
        )
    assert caught.value.code is PublishErrorCode.UNAPPROVED_WORKFLOW_CHANGE
    # The guard is what stops the push; it does not itself move any ref.
    assert _run(world.fork, "rev-parse", f"refs/heads/{plan.branch}").strip() == branch_before


# ---------------------------------------------------------------- retry behaviour


def test_a_rejected_push_leaves_no_branch_behind(world: World) -> None:
    _reject_pushes(world.fork, CLASSIC_REFUSAL)
    with pytest.raises(GitHostingError):
        _apply_and_push(_plan(world))
    assert "patchpilot" not in _run(world.fork, "for-each-ref", "--format=%(refname)")


def test_a_publication_can_be_retried_after_the_cause_is_removed(world: World) -> None:
    """The approved artefact is enough; nothing has to be regenerated."""
    hook = world.fork / "hooks" / "pre-receive"
    _reject_pushes(world.fork, CLASSIC_REFUSAL)
    with pytest.raises(GitHostingError):
        _apply_and_push(_plan(world))

    hook.unlink()
    plan = _plan(world)  # a fresh workspace, same approved patch and base
    sha = _apply_and_push(plan)
    assert _run(world.fork, "rev-parse", f"refs/heads/{plan.branch}").strip() == sha


def test_retrying_produces_the_same_commit_rather_than_a_duplicate(
    world: World,
) -> None:
    """Publication is deterministic, so a retry cannot fork the history."""
    first = _plan(world)
    sha_one = _apply_and_push(first)

    second = _plan(world, branch="patchpilot/run-abc123-2")
    sha_two = _apply_and_push(second)

    assert _run(first.workdir, "rev-parse", f"{sha_one}^{{tree}}").strip() == _run(
        second.workdir, "rev-parse", f"{sha_two}^{{tree}}"
    ).strip()
    assert _run(world.fork, "rev-list", "--count", f"master..{first.branch}").strip() == "1"


# ------------------------------------------------------------ credential handling


def test_the_token_never_reaches_the_repository_config(world: World) -> None:
    secret = "ghp_" + "S3cretTokenValue0123456789"
    plan = _plan(world, token=secret)
    _apply_and_push(plan)

    config = (plan.workdir / ".git" / "config").read_text(encoding="utf-8")
    assert secret not in config
    assert "extraheader" not in config.lower()
    for remote_url in (plan.push_url, plan.source_url):
        assert secret not in remote_url


def test_a_token_is_scrubbed_out_of_a_failure_message(world: World) -> None:
    secret = "ghp_" + "S3cretTokenValue0123456789"
    _reject_pushes(world.fork, "Permission to octo/fork.git denied to someone")
    with pytest.raises(GitHostingError) as caught:
        _apply_and_push(_plan(world, token=secret))
    assert secret not in str(caught.value)


def test_a_rename_patch_publishes_without_a_false_file_set_mismatch(
    world: World,
) -> None:
    """Both sides of a rename are named by the patch, so both must be counted.

    With git's rename detection on, ``--name-only`` reports only the
    destination, which would look like the commit changed a different set of
    files than the approved patch.
    """
    def mutate(w: Path) -> None:
        (w / "lib.py").unlink()
        _write(w, "lib_renamed.py", _LIB.replace("return 7\n", "return 77\n"))

    plan = _plan(world, patch=world.make_patch(world.base, mutate))
    # The test is only meaningful if git actually recorded this as a rename.
    assert "rename from lib.py" in plan.patch
    assert "rename to lib_renamed.py" in plan.patch

    sha = _apply_and_push(plan)  # must not raise UNAPPROVED_FILE_CHANGE
    tracked = _run(plan.workdir, "ls-tree", "-r", "--name-only", sha)
    assert "lib_renamed.py" in tracked
    assert "\nlib.py" not in f"\n{tracked}"
