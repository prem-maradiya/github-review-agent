"""Unified-diff helpers.

GitHub's PR files API returns a `patch` per file (hunks only, no file header).
Review comments may only target lines that appear in the diff, so we compute the
set of new-file line numbers that are commentable, and render a line-numbered
version of the patch for the LLM so it can cite exact line numbers.
"""
import re

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def commentable_lines(patch: str) -> set[int]:
    """New-file line numbers for added (+) and context ( ) lines in the patch."""
    lines: set[int] = set()
    new_ln = 0
    for raw in patch.splitlines():
        m = HUNK_RE.match(raw)
        if m:
            new_ln = int(m.group(3))
            continue
        if raw.startswith("\\"):  # "\ No newline at end of file"
            continue
        if raw.startswith("-"):
            continue
        if raw.startswith("+") or raw.startswith(" ") or raw == "":
            lines.add(new_ln)
            new_ln += 1
    return lines


def added_lines(patch: str) -> set[int]:
    """New-file line numbers that were added or modified (+ lines only)."""
    lines: set[int] = set()
    new_ln = 0
    for raw in patch.splitlines():
        m = HUNK_RE.match(raw)
        if m:
            new_ln = int(m.group(3))
            continue
        if raw.startswith("\\") or raw.startswith("-"):
            continue
        if raw.startswith("+"):
            lines.add(new_ln)
        new_ln += 1
    return lines


def number_patch(patch: str) -> str:
    """Prefix each new-side line with its line number, e.g. `  42 +foo()`.

    Removed lines get a blank number column since they can't be commented on.
    """
    out: list[str] = []
    new_ln = 0
    for raw in patch.splitlines():
        m = HUNK_RE.match(raw)
        if m:
            new_ln = int(m.group(3))
            out.append(raw)
            continue
        if raw.startswith("\\"):
            out.append(raw)
        elif raw.startswith("-"):
            out.append(f"{'':>5} {raw}")
        else:
            out.append(f"{new_ln:>5} {raw}")
            new_ln += 1
    return "\n".join(out)
