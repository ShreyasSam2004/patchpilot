r"""Run the official SWE-bench harness with LF line endings.

The harness writes the container's ``eval.sh`` and the model patch file with
``pathlib.Path.write_text`` (``swebench/harness/run_evaluation.py``, lines 291
and 360). On Windows that opens the file in text mode with ``newline=None``, so
every ``\n`` becomes ``\r\n``. Both files are then copied byte-for-byte into a
Linux container, where bash reads the trailing ``\r`` as part of each command::

    + python -m pip install -e $'.\r'
    ERROR: . is not a valid editable requirement.
    + git checkout <sha> $'sympy/core/tests/test_numbers.py\r'
    error: pathspec 'sympy/core/tests/test_numbers.py?' did not match any file(s)
    + bin/test -C --verbose $'sympy/core/tests/test_numbers.py\r'
    ImportError: SymPy now depends on mpmath as an external library.

The dependencies are never installed and the gold test files are never restored,
so every instance dies at import and is graded UNRESOLVED whatever the patch
did. The failure is silent: the harness reports a complete run, and the only
symptom is a 0% resolve rate with zero PASS_TO_PASS successes anywhere. We shipped
one such batch before catching it, which is why this shim exists.

This module forces ``write_text`` to emit LF and then hands control to the
harness's own ``__main__``, so grading remains entirely upstream's -- we change
the line endings of two generated files and nothing else. It is a no-op on
POSIX, where ``write_text`` already writes LF.

Deliberately imports nothing from ``patchpilot``: it is executed by file path
under ``settings.swebench_python``, which may be a bare interpreter that has
swebench installed but not this application.
"""

from __future__ import annotations

import pathlib
import runpy
from typing import Any

HARNESS_MODULE = "swebench.harness.run_evaluation"


def force_lf_write_text() -> None:
    """Make ``Path.write_text`` default to LF instead of the platform newline."""
    original = pathlib.Path.write_text
    if getattr(original, "_patchpilot_lf", False):
        return

    def write_text(
        self: pathlib.Path,
        data: str,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> int:
        if not isinstance(data, str):
            raise TypeError(f"data must be str, not {data.__class__.__name__}")
        with self.open("w", encoding=encoding, errors=errors, newline=newline or "\n") as handle:
            return handle.write(data)

    marked: Any = write_text
    marked._patchpilot_lf = True
    pathlib.Path.write_text = marked  # type: ignore[method-assign]


def main() -> None:
    force_lf_write_text()
    runpy.run_module(HARNESS_MODULE, run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main()
