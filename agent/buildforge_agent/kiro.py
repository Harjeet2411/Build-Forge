"""Client for the Build Forge runner on the Kiro instance (POST /analyze)."""
from __future__ import annotations

import json
import urllib.error
import urllib.request


class RunnerError(RuntimeError):
    pass


def analyze(url: str, secret: str, request: dict, timeout: int) -> dict:
    """Returns {"analysis": {...}, "seconds": ..., "agent": ...} or raises RunnerError with a readable reason."""
    if not url:
        raise RunnerError("BF_RUNNER_URL is not set")
    req = urllib.request.Request(f"{url}/analyze", data=json.dumps(request).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "X-BuildForge-Secret": secret,
                                          "User-Agent": "buildforge/2"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8", "replace"))
        except ValueError:
            detail = {}
        msg = detail.get("error") or f"HTTP {exc.code}"
        if detail.get("stderr_tail"):
            msg += f" ({detail['stderr_tail'].strip()[-200:]})"
        raise RunnerError(f"Build Forge runner answered HTTP {exc.code}: {msg}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RunnerError(f"Build Forge runner is unreachable at {url}: {getattr(exc, 'reason', exc)}") from exc
    except ValueError as exc:
        raise RunnerError("Build Forge runner sent something that is not JSON") from exc
    if not isinstance(body.get("analysis"), dict):
        raise RunnerError("Build Forge runner answered without an analysis")
    return body


def healthz(url: str, timeout: int = 10) -> dict:
    with urllib.request.urlopen(f"{url}/healthz", timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))
