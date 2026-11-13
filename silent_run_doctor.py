"""Attribute a silent, zero-token OpenBot/AG-UI run to its cause, offline.

The upstream issue this tool serves: CopilotKit/OpenBot#883 — a Bot holding more tools
than the provider accepts never answers; the run ends with RUN_FINISHED, zero tokens and
no error anywhere. This tool reads only artifacts you already have (the AG-UI event
stream, the tools manifest that was sent, and optionally the server log) and decides:

  confirmed-tool-overflow   silent run + tools over the model limit + provider rejection seen
  silent-unattributed       silent run, but nothing proves tool overflow -> abstain, do not guess
  tool-limit-only           tools over the limit, run was not silent
  healthy                   none of the above

Exit codes: 0 no error, 1 problems detected, 2 usage/input error.
"""

from __future__ import annotations

import argparse
import json
import re
import sys

MODEL_TOOL_LIMIT = 128  # CopilotKit/OpenBot shared/model-tool-limit.ts, measured against OpenAI
TOOL_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
PROVIDER_REJECTION_PATTERNS = (
    "array_above_max_length",
    "array too long",
    "maximum length 128",
)
TITLE_EMPTY_PATTERN = "Thread name generation returned an empty or invalid title"
OUTPUT_EVENT_TYPES = (
    "TEXT_MESSAGE_CONTENT",
    "TEXT_MESSAGE_CHUNK",
    "TOOL_CALL_START",
    "TOOL_CALL_ARGS",
    "TOOL_CALL_END",
    "TOOL_CALL_RESULT",
)


class InputError(Exception):
    """Raised for malformed input; maps to exit code 2."""


def _event_type(ev):
    if not isinstance(ev, dict):
        raise InputError("event must be a JSON object")
    t = ev.get("type") or ev.get("event")
    if isinstance(t, dict):
        t = t.get("type")
    if not isinstance(t, str) or not t:
        raise InputError("event has no string 'type'")
    return t


def load_events(path):
    """Load AG-UI events from a JSONL file or '-' for stdin."""
    if path == "-":
        text = sys.stdin.read()
    else:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError as exc:
            raise InputError("cannot read events file: %s" % exc)
    events = []
    for lineno, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError as exc:
            raise InputError("events line %d is not JSON: %s" % (lineno, exc))
        _event_type(ev)
        events.append({"line": lineno, "type": _event_type(ev), "raw": ev})
    if not events:
        raise InputError("events file is empty")
    return events


def load_tools(path):
    """Load an OpenAI tools array (list, or {"tools": [...]})."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except OSError as exc:
        raise InputError("cannot read tools file: %s" % exc)
    except json.JSONDecodeError as exc:
        raise InputError("tools file is not JSON: %s" % exc)
    if isinstance(data, dict):
        data = data.get("tools")
    if not isinstance(data, list):
        raise InputError("tools file must be a list or an object with a 'tools' list")
    names = []
    for i, tool in enumerate(data):
        if not isinstance(tool, dict):
            raise InputError("tool %d is not an object" % i)
        fn = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        name = fn.get("name")
        if not isinstance(name, str) or not name:
            raise InputError("tool %d has no name" % i)
        names.append(name)
    return names


def load_log(path):
    if path is None:
        return None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError as exc:
        raise InputError("cannot read log file: %s" % exc)


def _usage_zero(ev):
    raw = ev.get("usage") or (ev.get("result") or {}).get("usage") or {}
    values = []
    for key in ("inputTokens", "outputTokens", "input_tokens", "output_tokens"):
        if key in raw and isinstance(raw[key], (int, float)):
            values.append(raw[key])
    if not values:
        return True  # absent usage on a terminated run is treated as no measured output
    return sum(values) == 0


def analyse_run(events):
    """Return per-run facts: silent runs, unterminated runs, output evidence."""
    runs = []
    open_run = None
    for ev in events:
        t = ev["type"]
        if t == "RUN_STARTED":
            open_run = {"start_line": ev["line"], "output_events": [], "finish": None}
            runs.append(open_run)
        elif t == "RUN_FINISHED":
            if open_run is None:
                continue
            open_run["finish"] = ev
            open_run = None
        elif t in OUTPUT_EVENT_TYPES and open_run is not None:
            open_run["output_events"].append(ev["line"])
    facts = []
    for run in runs:
        fin = run["finish"]
        finish_reason = None
        usage_zero = None
        if fin is not None:
            finish_reason = fin["raw"].get("finishReason") or fin["raw"].get("finish_reason")
            usage_zero = _usage_zero(fin["raw"])
        silent = (
            fin is not None
            and not run["output_events"]
            and usage_zero
            and (finish_reason in (None, "length", "stop"))
        )
        facts.append(
            {
                "start_line": run["start_line"],
                "finish_line": fin["line"] if fin else None,
                "finish_reason": finish_reason,
                "usage_zero": usage_zero,
                "output_event_lines": list(run["output_events"]),
                "silent": silent,
            }
        )
    return facts


def _finding(rule, severity, message, line=None, **extra):
    out = {"rule": rule, "severity": severity, "message": message}
    if line is not None:
        out["line"] = line
    out.update(extra)
    return out


def diagnose(events, tool_names, log_text, limit=MODEL_TOOL_LIMIT):
    findings = []
    runs = analyse_run(events)
    silent_runs = [r for r in runs if r["silent"]]
    unterminated = [r for r in runs if r["finish_line"] is None]
    for run in unterminated:
        findings.append(
            _finding(
                "RUN_UNTERMINATED",
                "error",
                "RUN_STARTED at line %d never reached RUN_FINISHED" % run["start_line"],
                line=run["start_line"],
            )
        )
    for run in silent_runs:
        findings.append(
            _finding(
                "RUN_FINISHED_WITHOUT_OUTPUT",
                "error",
                "run ended at line %d with no text/tool events and zero measured tokens"
                % run["finish_line"],
                line=run["finish_line"],
                finish_reason=run["finish_reason"],
            )
        )

    count = len(tool_names)
    over = count > limit
    if over:
        findings.append(
            _finding(
                "TOOL_ARRAY_OVER_MODEL_LIMIT",
                "error",
                "%d tools sent, model limit is %d (over by %d)" % (count, limit, count - limit),
                count=count,
                limit=limit,
                over_by=count - limit,
            )
        )
    seen = {}
    for i, name in enumerate(tool_names):
        seen[name] = seen.get(name, 0) + 1
        if not TOOL_NAME_RE.match(name):
            findings.append(
                _finding(
                    "TOOL_NAME_INVALID",
                    "error",
                    "tool %d name %r does not match ^[a-zA-Z0-9_-]{1,64}$" % (i, name),
                    tool_index=i,
                )
            )
    for name, n in sorted(seen.items()):
        if n > 1:
            findings.append(
                _finding(
                    "TOOL_NAME_DUPLICATE",
                    "error",
                    "tool name %r appears %d times" % (name, n),
                    tool_name=name,
                    occurrences=n,
                )
            )

    provider_lines = []
    title_empty_lines = []
    if log_text is not None:
        for lineno, line in enumerate(log_text.splitlines(), 1):
            low = line.lower()
            if any(p.lower() in low for p in PROVIDER_REJECTION_PATTERNS):
                provider_lines.append({"line": lineno, "text": line.strip()[:300]})
            if TITLE_EMPTY_PATTERN in line:
                title_empty_lines.append({"line": lineno, "text": line.strip()[:300]})
    if provider_lines:
        findings.append(
            _finding(
                "PROVIDER_REJECTION_IN_LOG",
                "error",
                "server log carries the provider's tool-array rejection",
                log_lines=[p["line"] for p in provider_lines],
                excerpts=provider_lines[:3],
            )
        )
    if title_empty_lines and silent_runs:
        findings.append(
            _finding(
                "TITLE_GENERATION_EMPTY",
                "info",
                "thread-title generation also got an empty answer — corroborates a provider-side refusal",
                log_lines=[p["line"] for p in title_empty_lines],
            )
        )

    if silent_runs and over and provider_lines:
        verdict = "confirmed-tool-overflow"
    elif silent_runs and over:
        verdict = "tool-overflow-unconfirmed"
    elif silent_runs:
        verdict = "silent-unattributed"
    elif over:
        verdict = "tool-limit-only"
    else:
        verdict = "healthy"

    if verdict == "silent-unattributed":
        findings.append(
            _finding(
                "ATTRIBUTION_ABSTAIN",
                "warn",
                "the run was silent but the tools manifest is within the limit; refusing to guess a cause",
            )
        )
    errors = sum(1 for f in findings if f["severity"] == "error")
    warnings = sum(1 for f in findings if f["severity"] == "warn")
    return {
        "tool": "openbot-silent-run-doctor",
        "verdict": verdict,
        "model_tool_limit": limit,
        "tools": {"count": count, "over_limit": over},
        "runs": runs,
        "findings": findings,
        "summary": {"errors": errors, "warnings": warnings, "infos": len(findings) - errors - warnings},
    }


def render_text(report):
    lines = []
    lines.append("openbot-silent-run-doctor")
    lines.append("verdict: %s" % report["verdict"])
    lines.append("tools: %d (limit %d)" % (report["tools"]["count"], report["model_tool_limit"]))
    lines.append("runs: %d" % len(report["runs"]))
    for f in report["findings"]:
        loc = " line %s" % f["line"] if "line" in f else ""
        lines.append("[%s] %s%s: %s" % (f["severity"].upper(), f["rule"], loc, f["message"]))
    s = report["summary"]
    lines.append("summary: %d error(s), %d warning(s), %d info" % (s["errors"], s["warnings"], s["infos"]))
    return "\n".join(lines)


def build_parser():
    p = argparse.ArgumentParser(
        prog="openbot-silent-run-doctor",
        description="Attribute a silent, zero-token OpenBot/AG-UI run to tool-array overflow (offline).",
    )
    p.add_argument("--events", required=True, help="AG-UI event stream as JSONL, or '-' for stdin")
    p.add_argument("--tools", required=True, help="tools manifest (OpenAI tools array or {\"tools\": [...]})")
    p.add_argument("--log", default=None, help="optional OpenBot/server log to look for the provider rejection")
    p.add_argument("--limit", type=int, default=MODEL_TOOL_LIMIT, help="model tool limit (default: 128)")
    p.add_argument("--json", action="store_true", help="emit a machine-readable report on stdout")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.limit <= 0:
        print("error: --limit must be positive", file=sys.stderr)
        return 2
    try:
        events = load_events(args.events)
        names = load_tools(args.tools)
        log_text = load_log(args.log)
    except InputError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    report = diagnose(events, names, log_text, limit=args.limit)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render_text(report))
    return 1 if report["summary"]["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
