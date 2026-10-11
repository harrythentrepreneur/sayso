"""Per-case QA regression set: every check QA ever failed a case on, kept for good.

Ported from the PhonicsMaker loop (Oct 2026). There, QA kept blocking one case on a
new path into the same defect (5, 4 and 7 QA runs on three PRs): each dev round fixed
only the newest FAILING-CHECK and nothing re-ran the earlier ones. With
``[policy] qa_regression_set = true`` (the default; it only adds checks):

* a BLOCKED head's FAILING-CHECK and DEFECT-CLASS are added to the case record, once;
* every later QA brief lists the whole set and a PASS needs one plain
  ``REGRESSION: <n> PASS`` line per entry. A FAIL blocks; a missing report re-runs QA
  once (QA not following its brief is not a code fault and not a dev round);
* every later dev brief carries the set, the defect classes and "fix the class, not
  the instance", and asks dev to run the set before opening the PR.

The set lives in the case record, so it moves with the case and needs no new store.
"""
from __future__ import annotations

import re

MAX_CHECK_CHARS = 600
MAX_CLASS_CHARS = 160
_LINE = re.compile(r"^[ \t]*REGRESSION:[ \t]*(\d+)[ \t]+(PASS|FAIL)\b(.*)$", re.M | re.I)
_CLASS = re.compile(r"^[ \t]*DEFECT-CLASS:[ \t]*(.*?)[ \t]*$", re.M | re.I)


def entries(rec: dict) -> list[dict]:
    raw = rec.get("qa_regressions")
    return [e for e in raw if isinstance(e, dict) and e.get("check")] if isinstance(raw, list) else []


def added(rec: dict, *, check: str, defect_class: str, head: str, pr) -> list[dict] | None:
    """The new list with this check appended, or None when it is empty or already there."""
    check = (check or "").strip()[:MAX_CHECK_CHARS]
    current = entries(rec)
    if not check or any(e["check"] == check for e in current):
        return None
    return current + [{"n": len(current) + 1, "check": check, "head": head, "pr": pr,
                       "defect_class": (defect_class or "").strip()[:MAX_CLASS_CHARS]}]


def defect_class(plain_lines: list[str]) -> str:
    found = _CLASS.findall("\n".join(plain_lines))
    return found[-1][:MAX_CLASS_CHARS] if found and found[-1] else ""


def classes(items: list[dict]) -> list[str]:
    return list(dict.fromkeys(e["defect_class"] for e in items if e.get("defect_class")))


def report_problem(items: list[dict], plain_lines: list[str]) -> tuple[str, str]:
    """('fail'|'missing'|'', reason). A FAIL line anywhere for an entry wins."""
    if not items:
        return "", ""
    seen: dict[int, str] = {}
    for m in _LINE.finditer("\n".join(plain_lines)):
        n, res = int(m.group(1)), m.group(2).upper()
        if seen.get(n) != "FAIL":
            seen[n] = res
    failed = sorted(n for n, r in seen.items() if r == "FAIL")
    if failed:
        return "fail", f"earlier QA check(s) {failed} fail again on this head"
    missing = [int(e["n"]) for e in items if seen.get(int(e["n"])) != "PASS"]
    if missing:
        return "missing", f"QA did not report earlier check(s) {missing} as passing"
    return "", ""


def qa_section(items: list[dict]) -> str:
    if not items:
        return ""
    rows = "\n".join(f"  {e['n']}. (blocked head {str(e.get('head', ''))[:12]}) {e['check']}" for e in items)
    cls = classes(items)
    cls_line = ("\nDefect classes QA named before: " + "; ".join(cls)
                + "\nAlso try one NEW path into each class, not only the listed ones.") if cls else ""
    return (f"\nREGRESSION SET for this case (every check QA failed it on before). Run these FIRST:\n{rows}"
            f"{cls_line}\nEach must pass on THIS head (adapt the head or path, never the assertion). "
            "Write one line per entry:\nREGRESSION: <n> PASS\nREGRESSION: <n> FAIL <what it shows now>\n"
            "A FAIL, or a missing line for any entry, means the head is not a PASS.\n")


def dev_section(items: list[dict]) -> str:
    if not items:
        return ""
    rows = "\n".join(f"- {e['check']}" + (f" (class: {e['defect_class']})" if e.get("defect_class") else "")
                     for e in items)
    cls = ("\nFix the CLASS, not the instance: list every code path that can reach each class, fix them all "
           "or say which you leave and why, and add one test per path.") if classes(items) else ""
    return (f"\n## Regression set (every check QA failed this case on)\n{rows}{cls}\n"
            "Before opening or updating the PR, run EVERY check above on your head and quote each result. "
            "Then try to break your own fix through one path none of them uses, and say what you tried. "
            "QA re-runs the whole set first.\n")
