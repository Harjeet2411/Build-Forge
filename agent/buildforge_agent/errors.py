"""Cut a long Jenkins console log down to the part that explains the failure.

The Kiro agent gets this instead of the whole log: the failing stage, the node it ran on, the output of the
step that failed (stack trace included), and Jenkins' own ERROR / Finished lines.
"""
from __future__ import annotations

import re

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")
JENKINS_NOTE = re.compile(r"\x1b\[8mha:[^\x1b]*\x1b\[0m")
TIMESTAMP = re.compile(r"^\[?\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:[.,]\d+)?Z?\]?\s*")


def clean_log(text: str) -> str:
    text = JENKINS_NOTE.sub("", text)
    text = ANSI.sub("", text)
    return "\n".join(TIMESTAMP.sub("", line) for line in text.splitlines())

PIPE = re.compile(r"^\[Pipeline\] ")  # any pipeline marker, including "{" and "}"
STEP = re.compile(r"^\[Pipeline\] (\w+)")
STAGE = re.compile(r"^\[Pipeline\] \{ \((.+)\)\s*$")
EXIT_CODE_ERROR = re.compile(r"^(ERROR: )?script returned exit code \d+|^ERROR: .*exit code \d+", re.I)
JENKINS_ERROR = re.compile(r"^(ERROR: |Finished: |hudson\.AbortException|org\.jenkinsci\.plugins\.workflow\.steps\.FlowInterruptedException)")
SHELL_STEPS = {"sh", "bat", "powershell", "pwsh"}
# Output that is never the cause: package-manager chatter and apt progress.
NOISE = re.compile(r"^(npm warn deprecated|npm warn (using|ERESOLVE|Found|While|Could|peer|Conflicting|node_modules)|"
                   r"Get:\d+ |Hit:\d+ |Ign:\d+ |Fetched |Reading package lists|Building dependency tree|"
                   r"Stage \".*\" skipped due to earlier failure)")


def _tidy(lines: list[str], keep_tail: int) -> list[str]:
    lines = [l for l in lines if not NOISE.match(l)]
    # collapse runs of blank lines
    out: list[str] = []
    for l in lines:
        if not l.strip() and out and not out[-1].strip():
            continue
        out.append(l)
    if len(out) > keep_tail + 15:
        omitted = len(out) - keep_tail - 15
        out = out[:15] + [f"... [{omitted} lines omitted] ..."] + out[-keep_tail:]
    return out


def extract(log: str, max_lines: int = 160, max_chars: int = 24000) -> dict:
    """{"stage", "node", "text"}: the failing step's output plus where it ran (end of the log for non-pipeline logs)."""
    lines = clean_log(log).splitlines()
    if not lines:
        return {"stage": None, "node": None, "text": ""}

    # First Jenkins ERROR line = where the build was declared failed.
    err_at = next((i for i, l in enumerate(lines) if l.startswith("ERROR: ")), None)
    tail_marks = [l for l in lines[(err_at if err_at is not None else len(lines)):] if JENKINS_ERROR.match(l)][:6]

    block: list[str] = []
    start = 0
    if err_at is not None and EXIT_CODE_ERROR.match(lines[err_at]):
        # A shell step failed: its output is the block after the last "[Pipeline] sh" before the error.
        for i in range(err_at - 1, -1, -1):
            m = STEP.match(lines[i])
            if m and m.group(1) in SHELL_STEPS:
                start = i + 1
                j = start
                while j < err_at and not PIPE.match(lines[j]):
                    j += 1
                block = lines[start:j]
                break
    if not block:
        # Another kind of failure (error step, exception, non-pipeline log): the lines just before the error mark.
        end = err_at if err_at is not None else len(lines)
        start = max(0, end - max_lines)
        block = [l for l in lines[start:end] if not PIPE.match(l)]

    stage = node = None
    for i in range(start, -1, -1):
        if stage is None and STAGE.match(lines[i]):
            stage = STAGE.match(lines[i]).group(1)
        if node is None and lines[i].startswith("Running on "):
            node = lines[i]
        if stage and node:
            break

    head = []
    if stage:
        head.append(f"Stage: {stage}")
    if node:
        head.append(node)
    body = _tidy(block, max_lines - len(head) - len(tail_marks))
    out = "\n".join(head + [""] * bool(head) + body + [""] * bool(tail_marks) + tail_marks).strip()
    if len(out) > max_chars:
        out = out[:3000] + "\n... [truncated] ...\n" + out[-(max_chars - 3000):]
    if node:  # "Running on EC2 (Acme) - build (i-0ab) in /home/jenkins/ws/app" -> "EC2 (Acme) - build (i-0ab)"
        node = re.sub(r" in (?:/|[A-Za-z]:\\).*$", "", node[len("Running on "):]).strip()
    return {"stage": stage, "node": node or None, "text": out}


def extract_error(log: str, **kw) -> str:
    return extract(log, **kw)["text"]


SKIPPED = re.compile(r"^Stage \"(.+)\" skipped due to (?:earlier failure|when conditional)")


def stages(log: str, failed: str | None = None) -> list[dict]:
    """The pipeline's stages in order, each {"name", "status"}: passed, failed, skipped or unknown.

    Built from the "[Pipeline] { (name)" markers, so it works without any extra Jenkins plugin. Stages after the
    failing one that Jenkins skipped are marked skipped; with parallel branches the result is a best effort.
    """
    names: list[str] = []
    skipped: set[str] = set()
    for line in clean_log(log).splitlines():
        m = STAGE.match(line)
        if m and m.group(1) not in names:
            names.append(m.group(1))
        s = SKIPPED.match(line)
        if s:
            skipped.add(s.group(1))
    out, seen_failed = [], False
    for name in names:
        if name == failed:
            status, seen_failed = "failed", True
        elif name in skipped:
            status = "skipped"
        else:
            status = "unknown" if seen_failed else "passed"
        out.append({"name": name, "status": status})
    return out


NUMBERS = re.compile(r"\b(?=[0-9a-f]*\d)[0-9a-f]{7,}\b|\d+", re.I)  # hashes and ids, then any digits
KEYWORDS = re.compile(r"error|fail|exception|denied|refused|timed? ?out|not found|cannot|unable|fatal|exit code", re.I)


def fingerprint_lines(text: str, limit: int = 12) -> list[str]:
    """The error lines with numbers, hashes and ids blanked, so the same error in another build looks the same."""
    picked = [NUMBERS.sub("#", l.strip()) for l in text.splitlines()
              if KEYWORDS.search(l) and not l.startswith(("Stage: ", "Running on ", "Finished: "))]
    return picked[-limit:]
