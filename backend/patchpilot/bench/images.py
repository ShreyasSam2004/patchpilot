"""Official SWE-bench per-instance Docker images.

A generic sandbox image cannot run a real repository's tests: astropy needs
numpy, qutip needs scipy, Django needs its own test harness. The official
benchmark solves this by publishing one image per instance with the repository
and its dependencies already installed — and the evaluator runs in exactly
those images.

Generating patches in the same image the evaluator will use is the right
default: the agent's `run_tests` observations then mean the same thing the
grader's do. Without it the agent can explore and patch but cannot validate,
which is the single biggest quality ceiling on a benchmark run.

Naming is taken from ``swebench.image_builder.image_spec.ImageSpec.name``
(verified against swebench 5.0.2)::

    {namespace}/sweb.eval.{arch}.{instance_id}:{tag}

with ``__`` replaced by ``_1776_`` (Docker Hub rejects dunders) and the whole
string lowercased.
"""

from __future__ import annotations

import platform

DEFAULT_NAMESPACE = "swebench"
DEFAULT_TAG = "latest"


def docker_arch(machine: str | None = None) -> str:
    """The architecture segment the published images use."""
    value = (machine or platform.machine()).lower()
    if value in {"arm64", "aarch64"}:
        return "arm64"
    # Everything else is published under the historical x86_64 tag.
    return "x86_64"


def instance_image_name(
    instance_id: str,
    *,
    namespace: str | None = DEFAULT_NAMESPACE,
    tag: str = DEFAULT_TAG,
    arch: str | None = None,
) -> str:
    """Build the official image name for a SWE-bench instance.

    >>> instance_image_name("astropy__astropy-12907", arch="x86_64")
    'swebench/sweb.eval.x86_64.astropy_1776_astropy-12907:latest'
    """
    key = f"sweb.eval.{arch or docker_arch()}.{instance_id}:{tag}"
    if namespace:
        key = f"{namespace}/{key}".replace("__", "_1776_")
    return key.lower()
