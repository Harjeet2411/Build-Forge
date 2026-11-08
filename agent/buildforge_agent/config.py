"""Settings, read once from the environment (see agent.env.example)."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a whole number, got {raw!r}") from None


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number, got {raw!r}") from None


def _fraction(name: str, default: float) -> float:
    value = _float(name, default)
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be between 0 and 1, got {value}")
    return value


def _regex(name: str) -> re.Pattern | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return re.compile(raw)
    except re.error as exc:
        raise ValueError(f"{name} is not a valid regular expression: {exc}") from None


def _choice(name: str, default: str, allowed: tuple[str, ...]) -> str:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw not in allowed:
        raise ValueError(f"{name} must be one of {', '.join(allowed)}, got {raw!r}")
    return raw


def _listen(raw: str) -> tuple[str, int]:
    """BF_LISTEN is host:port or :port (all of it after the last colon is the port)."""
    host, sep, port = raw.rpartition(":")
    if not sep or not port.isdigit() or not 0 < int(port) < 65536:
        raise ValueError(f"BF_LISTEN must be host:port, e.g. 127.0.0.1:8791, got {raw!r}")
    return host or "127.0.0.1", int(port)


# Kiro's analysis category enum (kiro-runner/buildforge_mcp.py), plus "all" for every category
CATEGORIES = ("application", "infrastructure", "configuration", "dependency", "test_failure", "transient", "unknown")


def _categories(name: str, default: str) -> set[str]:
    raw = os.environ.get(name, "").strip() or default
    picked = {x.strip().lower() for x in raw.split(",") if x.strip()}
    unknown = sorted(picked - set(CATEGORIES) - {"all"})
    if unknown:
        raise ValueError(f"{name} has unknown categories {', '.join(unknown)}; "
                         f"use {', '.join(CATEGORIES)} or all")
    return picked


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{name} must be on or off (also true/false, yes/no, 1/0), got {raw!r}")


@dataclass
class Settings:
    listen_host: str = "127.0.0.1"
    listen_port: int = 8791
    shared_secret: str = ""                         # the plugin sends it as X-BuildForge-Secret
    data_dir: Path = Path("/var/lib/buildforge")      # SQLite database
    log_dir: Path = Path("/var/log/buildforge")       # incident logs: incidents/<ID>.md and incidents.jsonl

    runner_url: str = ""                            # the Build Forge runner on the Kiro instance, e.g. http://10.0.2.15:8796
    runner_secret: str = ""
    runner_timeout: int = 300                       # keep it above BF_TIMEOUT on the runner
    send: str = "errors"                            # errors = only the failing step | full = the console tail
    max_error_chars: int = 16000

    teams_webhook_url: str = ""
    notify_on: set[str] = field(default_factory=lambda: {"infrastructure"})
    job_include: re.Pattern | None = None
    job_exclude: re.Pattern | None = None
    retention_days: int = 365                       # incidents older than this are pruned (0 = keep forever)
    deck_mode: str = "fallback"                     # off | fallback (deck answers when Kiro can't) | first (deck before Kiro)
    deck_threshold: float = 0.55                    # similarity needed for a deck answer when Kiro is unavailable
    deck_first_threshold: float = 0.85              # ...and for answering before Kiro (approved cards only)
    deck_min_seen: int = 2                          # a draft card is used once its error was seen this many times
    alerts: bool = True                             # the notification bell: pipelines that keep failing
    alert_prod_after: int = 3                       # critical: a production pipeline failed MORE than this many builds in a row
    alert_uat_after: int = 5                        # high: a UAT / staging pipeline failed more than this many in a row
    alert_other_after: int = 5                      # low: any other pipeline failed more than this many in a row
    env_prod: re.Pattern = re.compile(
        r"(?<![a-z0-9])(?<!pre-)(?<!pre_)(?<!pre )(prod|prd|production)(?![a-z0-9])", re.I)
    env_uat: re.Pattern = re.compile(r"(?<![a-z0-9])(uat|staging|stage|stg|preprod|pre-prod|pre_prod)(?![a-z0-9])", re.I)

    @classmethod
    def from_env(cls) -> "Settings":
        host, port = _listen(os.environ.get("BF_LISTEN", "").strip() or "127.0.0.1:8791")
        return cls(
            listen_host=host,
            listen_port=port,
            shared_secret=os.environ.get("BF_SHARED_SECRET", "").strip(),
            data_dir=Path(os.environ.get("BF_DATA_DIR", "/var/lib/buildforge")),
            log_dir=Path(os.environ.get("BF_LOG_DIR", "/var/log/buildforge")),
            runner_url=os.environ.get("BF_RUNNER_URL", "").strip().rstrip("/"),
            runner_secret=os.environ.get("BF_RUNNER_SECRET", "").strip(),
            runner_timeout=_int("BF_RUNNER_TIMEOUT", 300),
            send=_choice("BF_SEND", "errors", ("errors", "full")),
            max_error_chars=_int("BF_MAX_ERROR_CHARS", 16000),
            teams_webhook_url=os.environ.get("TEAMS_WEBHOOK_URL", "").strip(),
            notify_on=_categories("BF_NOTIFY_ON", "infrastructure"),
            job_include=_regex("BF_JOB_INCLUDE"),
            job_exclude=_regex("BF_JOB_EXCLUDE"),
            retention_days=_int("BF_RETENTION_DAYS", 365),
            deck_mode=_choice("BF_DECK_MODE", "fallback", ("off", "fallback", "first")),
            deck_threshold=_fraction("BF_DECK_THRESHOLD", 0.55),
            deck_first_threshold=_fraction("BF_DECK_FIRST_THRESHOLD", 0.85),
            deck_min_seen=_int("BF_DECK_MIN_SEEN", 2),
            alerts=_bool("BF_ALERTS", True),
            alert_prod_after=_int("BF_ALERT_PROD_AFTER", 3),
            alert_uat_after=_int("BF_ALERT_UAT_AFTER", 5),
            alert_other_after=_int("BF_ALERT_OTHER_AFTER", 5),
            **{k: v for k, v in (("env_prod", _regex("BF_ENV_PROD")), ("env_uat", _regex("BF_ENV_UAT"))) if v},
        )

    def job_allowed(self, job: str) -> bool:
        if self.job_include and not self.job_include.search(job):
            return False
        if self.job_exclude and self.job_exclude.search(job):
            return False
        return True
