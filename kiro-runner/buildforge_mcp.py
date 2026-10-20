#!/usr/bin/env python3
"""Build Forge MCP server (stdio): the one tool the Kiro agent uses to hand back its analysis.

kiro-cli starts this over stdio (see kiro/agents/buildforge-fixer.json). The tool validates the analysis and writes it,
as JSON, to the file the runner named in BF_RESULT_FILE; the runner returns that file's content to the Jenkins side.
Making the agent answer through a tool call (instead of free text) gives a checked, structured result.
Standard library only; speaks MCP's newline-delimited JSON-RPC.
"""
from __future__ import annotations

import json
import os
import sys

SERVER_INFO = {"name": "buildforge", "version": "2.0.0"}
DEFAULT_PROTOCOL = "2025-03-26"

CATEGORIES = ["application", "infrastructure", "configuration", "dependency", "test_failure", "transient", "unknown"]
SEVERITIES = ["low", "medium", "high"]

TOOL = {
    "name": "submit_analysis",
    "description": ("Record your analysis of the failed Jenkins build. Call this exactly once, after you have read the "
                    "error. The analysis is shown to the developer on the failed build and saved in the incident log."),
    "inputSchema": {
        "type": "object",
        "properties": {
            "error_observed": {"type": "string", "description": "What failed, in one to three plain sentences, quoting the key error text."},
            "proposed_solution": {"type": "string", "description": "Markdown: a numbered list of options to fix it, the most likely first, each with the concrete command, setting or code change."},
            "category": {"type": "string", "enum": CATEGORIES, "description": "The kind of failure."},
            "severity": {"type": "string", "enum": SEVERITIES, "description": "How much this blocks delivery."},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1, "description": "How sure you are of the cause."},
            "rebuild_can_fix": {"type": "boolean", "description": "True only if rebuilding with the same parameters and no change is likely to succeed (a transient problem)."},
            "rebuild_reason": {"type": "string", "description": "One sentence on why a rebuild will or will not help."},
            "signature": {"type": "string", "description": "A short stable label for this kind of error, lower-case with dashes, e.g. npm-e401-codeartifact."},
            "affected_component": {"type": "string", "description": "Optional: the tool, service or file at the centre of the failure."},
        },
        "required": ["error_observed", "proposed_solution", "category", "severity", "confidence",
                     "rebuild_can_fix", "rebuild_reason", "signature"],
    },
}


def result_file() -> str:
    """BF_RESULT_FILE, or (if kiro-cli did not pass it on) the value from the parent kiro-cli process."""
    path = os.environ.get("BF_RESULT_FILE", "")
    if not path:
        try:
            with open(f"/proc/{os.getppid()}/environ", "rb") as fh:
                for kv in fh.read().split(b"\0"):
                    if kv.startswith(b"BF_RESULT_FILE="):
                        return kv.split(b"=", 1)[1].decode()
        except OSError:
            pass
    return path


def validate(args: dict) -> tuple[dict | None, str]:
    """Normalise the tool arguments; returns (analysis, error message)."""
    if not isinstance(args, dict):
        return None, "arguments must be an object"
    out: dict = {}
    for key, limit in (("error_observed", 1500), ("proposed_solution", 6000), ("rebuild_reason", 500),
                       ("signature", 80), ("affected_component", 200)):
        value = args.get(key, "")
        if not isinstance(value, str) or (key != "affected_component" and not value.strip()):
            return None, f"{key} is required and must be a non-empty string"
        out[key] = value.strip()[:limit]
    if args.get("category") not in CATEGORIES:
        return None, f"category must be one of {', '.join(CATEGORIES)}"
    if args.get("severity") not in SEVERITIES:
        return None, f"severity must be one of {', '.join(SEVERITIES)}"
    conf = args.get("confidence")
    if isinstance(conf, bool) or not isinstance(conf, (int, float)) or not 0 <= conf <= 1:
        return None, "confidence must be a number between 0 and 1"
    if not isinstance(args.get("rebuild_can_fix"), bool):
        return None, "rebuild_can_fix must be true or false"
    out.update(category=args["category"], severity=args["severity"], confidence=round(float(conf), 2),
               rebuild_can_fix=args["rebuild_can_fix"])
    out["signature"] = "-".join(out["signature"].lower().split())
    return out, ""


def call_tool(name: str, args: dict) -> dict:
    if name != "submit_analysis":
        return {"content": [{"type": "text", "text": f"unknown tool {name}"}], "isError": True}
    analysis, problem = validate(args)
    if analysis is None:
        return {"content": [{"type": "text", "text": f"Not recorded: {problem}. Fix it and call the tool again."}],
                "isError": True}
    path = result_file()
    if not path:
        return {"content": [{"type": "text", "text": "Not recorded: no result file is set for this run."}], "isError": True}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(analysis, fh)
    os.replace(tmp, path)
    return {"content": [{"type": "text", "text": "Analysis recorded. Reply with the single word: Submitted."}]}


def handle(msg: dict) -> dict | None:
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:           # notifications (e.g. notifications/initialized) need no answer
        return None
    if method == "initialize":
        version = (msg.get("params") or {}).get("protocolVersion") or DEFAULT_PROTOCOL
        result = {"protocolVersion": version, "capabilities": {"tools": {}}, "serverInfo": SERVER_INFO}
    elif method == "tools/list":
        result = {"tools": [TOOL]}
    elif method == "tools/call":
        params = msg.get("params") or {}
        result = call_tool(params.get("name", ""), params.get("arguments") or {})
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            reply = handle(json.loads(line))
        except Exception as exc:  # noqa: BLE001 - a bad request must not kill the server
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": str(exc)}}
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
