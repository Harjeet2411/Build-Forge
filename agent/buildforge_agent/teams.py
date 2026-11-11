"""Teams alert through a Workflows webhook ("Send webhook alerts to a channel"): one Adaptive Card."""
from __future__ import annotations

import json
import urllib.error
import urllib.request

ICONS = {"infrastructure": "🔥", "transient": "🔁", "dependency": "📦", "configuration": "⚙️"}


def incident_card(inc: dict, admin_url: str = "") -> dict:
    a = inc.get("analysis") or {}
    cat = a.get("category", "unknown")
    label = cat.replace("_", " ").capitalize()
    if not label.endswith(" failure"):  # "test_failure" already says it
        label += " failure"
    title = f"{ICONS.get(cat, '⚠️')} {label}: {inc['job']} #{inc['build']}"
    facts = [
        {"title": "Entry", "value": inc["id"]},
        {"title": "Stage", "value": inc.get("stage") or "-"},
        {"title": "Severity", "value": a.get("severity", "-")},
        {"title": "Rebuild can fix it", "value": "yes" if a.get("rebuild_can_fix") else "no"},
    ]
    body = [
        {"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Medium", "wrap": True, "color": "Attention"},
        {"type": "FactSet", "facts": facts},
        {"type": "TextBlock", "text": "**Error observed**", "wrap": True, "spacing": "Medium"},
        {"type": "TextBlock", "text": (a.get("error_observed") or inc.get("error") or "-")[:800], "wrap": True},
        {"type": "TextBlock", "text": "**Proposed solution**", "wrap": True, "spacing": "Medium"},
        {"type": "TextBlock", "text": (a.get("proposed_solution") or "-")[:1500], "wrap": True},
    ]
    actions = []
    if inc.get("build_url"):
        actions.append({"type": "Action.OpenUrl", "title": "Open build", "url": inc["build_url"]})
    if admin_url:
        actions.append({"type": "Action.OpenUrl", "title": "Open in Build Forge", "url": admin_url})
    card = {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard",
            "version": "1.4", "msteams": {"width": "Full"}, "body": body}
    if actions:
        card["actions"] = actions
    return {"type": "message", "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive",
                                                "contentUrl": None, "content": card}]}


def post(webhook_url: str, payload: dict, timeout: int = 30) -> None:
    if not webhook_url:
        raise RuntimeError("TEAMS_WEBHOOK_URL is not set")
    req = urllib.request.Request(webhook_url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status >= 300:
                raise RuntimeError(f"Teams webhook answered HTTP {resp.status}")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Teams webhook answered HTTP {exc.code}: {exc.read()[:300]!r}") from exc
