#!/usr/bin/env python3
"""
tool_doctor.py - diagnose why an LLM agent does not call its tools.

Checks performed:
  1. Tool definitions   : name, description quality, JSON schema validity, complexity,
                          tool count, overlapping descriptions, deferred loading flags
  2. System prompt      : phrases that discourage tool use, tools never mentioned
  3. Wire check         : do the tools actually reach the API request body?
  4. Forced tool_choice : can each tool be called at all, and is the input schema-valid?
  5. Behavioral probes  : realistic user messages -> does the model pick the right tool?
  6. Ablation           : same probes WITHOUT the system prompt (is the prompt suppressing tools?)
  7. Model comparison   : same probes on other models (is the model too weak?)
  8. History scan       : tool errors / orphaned tool calls in a saved conversation

Setup:
    pip install anthropic jsonschema httpx
    export ANTHROPIC_API_KEY=...

Config file (JSON):
    {
      "model": "claude-sonnet-5-5",
      "system": "your agent's system prompt",
      "tools": [ {"name": "...", "description": "...", "input_schema": {...}} ],
      "history": [ ...optional saved messages list... ]
    }

Usage:
    python tool_doctor.py --config agent.json
    python tool_doctor.py --config agent.json --compare-models claude-haiku-5-5 claude-opus-5-5
    python tool_doctor.py --config agent.json --skip-live          # static checks only, no API calls
    python tool_doctor.py --config agent.json --report report.json
"""

import argparse
import json
import re
import sys
from difflib import SequenceMatcher
from itertools import combinations

try:
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import SchemaError
except ImportError:
    sys.exit("Missing dependency: pip install jsonschema")

FINDINGS = []  # (level, check, message)
ICONS = {"FAIL": "[FAIL]", "WARN": "[WARN]", "OK": "[ OK ]", "INFO": "[INFO]"}


def report(level, check, message):
    FINDINGS.append({"level": level, "check": check, "message": message})
    print(f"{ICONS[level]} {check}: {message}")


def section(title):
    print(f"\n=== {title} ===")


# --------------------------------------------------------------------------- #
# 1. Static: tool definitions
# --------------------------------------------------------------------------- #
def schema_depth(node, depth=0):
    if isinstance(node, dict):
        best = depth
        for key, val in node.items():
            inc = 1 if key in ("properties", "items") else 0
            best = max(best, schema_depth(val, depth + inc))
        return best
    if isinstance(node, list):
        return max([schema_depth(x, depth) for x in node] + [depth])
    return depth


def check_definitions(tools):
    section("1. Tool definitions")
    if not tools:
        report("FAIL", "tools", "The tools list is empty. The model cannot call what it never receives.")
        return

    if len(tools) > 20:
        report("WARN", "tool-count", f"{len(tools)} tools. Large toolsets reduce selection accuracy; "
                                     "consider grouping, trimming, or tool search.")
    else:
        report("OK", "tool-count", f"{len(tools)} tools")

    names = [t.get("name", "") for t in tools]
    for dup in {n for n in names if names.count(n) > 1}:
        report("FAIL", "duplicate-name", f"Tool name '{dup}' appears more than once.")

    for t in tools:
        name = t.get("name", "")
        tag = f"tool:{name or '<unnamed>'}"

        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name):
            report("FAIL", tag, "Name must match ^[a-zA-Z0-9_-]{1,64}$")

        desc = (t.get("description") or "").strip()
        if not desc:
            report("FAIL", tag, "Missing description. This is the #1 reason tools are ignored.")
        elif len(desc) < 40:
            report("WARN", tag, f"Description is very short ({len(desc)} chars). Explain what it does, "
                                "when to use it, when NOT to use it, and what it returns.")
        elif not re.search(r"\b(use (this )?(tool )?(when|if|to|for)|when|should|do not|don't|returns?)\b",
                           desc, re.I):
            report("INFO", tag, "Description lacks usage guidance (when to use / when not / what it returns).")
        else:
            report("OK", tag, "description looks reasonable")

        schema = t.get("input_schema")
        if not isinstance(schema, dict):
            report("FAIL", tag, "Missing input_schema (must be a JSON-schema object).")
            continue
        if schema.get("type") != "object":
            report("FAIL", tag, "input_schema.type must be 'object'.")
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as e:
            report("FAIL", tag, f"Invalid JSON schema: {e.message}")
            continue

        props = schema.get("properties", {}) or {}
        required = schema.get("required", []) or []
        if len(required) > 5:
            report("WARN", tag, f"{len(required)} required params. Simplify or add defaults.")
        if schema_depth(schema) > 3:
            report("WARN", tag, "Schema nesting is deep (>3). Flatten it; deeply nested inputs are called less often.")
        undocumented = [p for p, v in props.items() if isinstance(v, dict) and not v.get("description")]
        if undocumented:
            report("WARN", tag, f"Params without description: {', '.join(undocumented)}")
        if t.get("defer_loading"):
            report("WARN", tag, "defer_loading is set: the tool must be loaded (tool search) before the model can call it.")

    for a, b in combinations(tools, 2):
        da, db = a.get("description", ""), b.get("description", "")
        if da and db:
            ratio = SequenceMatcher(None, da.lower(), db.lower()).ratio()
            if ratio > 0.6:
                report("WARN", "overlap", f"'{a.get('name', '<unnamed>')}' and '{b.get('name', '<unnamed>')}' "
                                          f"descriptions are {ratio:.0%} similar. Make the distinction explicit.")


# --------------------------------------------------------------------------- #
# 2. Static: system prompt
# --------------------------------------------------------------------------- #
DISCOURAGING = [
    r"do(n't| not) (use|call) (any )?(the )?tools?",
    r"(answer|respond) (only )?(from|using) (your own|internal) (knowledge|memory)",
    r"without (calling|using) (any )?tools?",
    r"never (call|use|invoke)",
    r"only (call|use) (a |the )?tools? if (the user )?(explicitly|specifically)",
    r"(ask|wait) for (confirmation|approval|permission) before",
    r"avoid (calling|using) tools?",
]


def check_system_prompt(system, tools):
    section("2. System prompt")
    if not system:
        report("WARN", "system", "No system prompt. Add guidance about when and why to use the tools.")
        return
    hits = [p for p in DISCOURAGING if re.search(p, system, re.I)]
    for p in hits:
        report("WARN", "system", f"Possible tool-discouraging instruction matching /{p}/")
    if not hits:
        report("OK", "system", "No obvious tool-discouraging phrases found")

    missing = [t["name"] for t in tools if t.get("name") and t["name"] not in system]
    if tools and len(missing) == len(tools):
        report("INFO", "system", "The system prompt never mentions any tool by name. "
                                 "Consider adding a short 'when to use which tool' section.")
    if len(system) > 6000:
        report("WARN", "system", f"System prompt is long ({len(system)} chars). Tool guidance may get buried.")


# --------------------------------------------------------------------------- #
# Live helpers
# --------------------------------------------------------------------------- #
def api_call(client, model, system, tools, user, tool_choice=None, max_tokens=512):
    kwargs = dict(model=model, max_tokens=max_tokens, tools=tools,
                  messages=[{"role": "user", "content": user}])
    if system:
        kwargs["system"] = system
    if tool_choice:
        kwargs["tool_choice"] = tool_choice
    return client.messages.create(**kwargs)


def called_tools(resp):
    return [b for b in resp.content if b.type == "tool_use"]


# --------------------------------------------------------------------------- #
# 3. Wire check
# --------------------------------------------------------------------------- #
def check_wire(client, captured, model, system, tools):
    section("3. Wire check (do the tools reach the API?)")
    try:
        api_call(client, model, system, tools, "ping", max_tokens=16)
    except Exception as e:  # noqa: BLE001
        report("FAIL", "wire", f"API rejected the request: {e}")
        return False
    body = captured[-1] if captured else {}
    sent = [t.get("name") for t in body.get("tools", [])]
    expected = [t["name"] for t in tools]
    if sorted(sent) != sorted(expected):
        report("FAIL", "wire", f"Request body tools {sent} != config tools {expected}")
        return False
    report("OK", "wire", f"API accepted the request and all {len(sent)} tools were in the request body")
    report("INFO", "wire", "This verifies THIS script. In your own agent, log the raw request body "
                           "(e.g. an httpx request hook) and confirm 'tools' is present and non-empty.")
    return True


# --------------------------------------------------------------------------- #
# 4. Forced tool_choice
# --------------------------------------------------------------------------- #
def check_forced_choice(client, model, system, tools):
    section("4. Forced tool_choice (can each tool be called with valid input?)")
    forced_supported = True
    for t in tools:
        tag = f"force:{t['name']}"
        nudge = f"Call the {t['name']} tool with plausible example arguments."
        try:
            if forced_supported:
                resp = api_call(client, model, system, tools, nudge,
                                tool_choice={"type": "tool", "name": t["name"]})
            else:
                resp = api_call(client, model, system, tools, nudge, tool_choice={"type": "auto"})
        except Exception as e:  # noqa: BLE001
            # Some models reject tool_choice type "tool"/"any"; fall back to a prompt nudge.
            if forced_supported and re.search(r"tool_choice.*not supported", str(e), re.I | re.S):
                forced_supported = False
                report("INFO", "tool_choice", f"{model} does not support tool_choice type 'tool'/'any'; "
                                              "falling back to 'auto' plus an explicit prompt nudge.")
                try:
                    resp = api_call(client, model, system, tools, nudge, tool_choice={"type": "auto"})
                except Exception as e2:  # noqa: BLE001
                    report("FAIL", tag, f"Nudged call failed: {e2}")
                    continue
            else:
                report("FAIL", tag, f"{'Forced' if forced_supported else 'Nudged'} call failed: {e}")
                continue
        calls = called_tools(resp)
        if not calls:
            report("FAIL", tag, "Even with a forced tool_choice no tool_use block came back."
                                if forced_supported else
                                "No tool_use block came back even when the prompt asked for this tool by name.")
            continue
        errors = list(Draft202012Validator(t["input_schema"]).iter_errors(calls[0].input))
        if errors:
            report("WARN", tag, f"Model's input violates the schema: {errors[0].message}. "
                                "The schema may be confusing.")
        else:
            report("OK", tag, f"callable, input valid: {json.dumps(calls[0].input)[:100]}")

    # tool_choice=auto sanity: just make sure the setting is not 'none'
    report("INFO", "tool_choice", "Make sure your agent does not send tool_choice={'type':'none'} "
                                  "and that it uses 'auto' (or 'any' for debugging).")


# --------------------------------------------------------------------------- #
# 5-7. Behavioral probes, ablation, model comparison
# --------------------------------------------------------------------------- #
def generate_probes(client, model, tool, n):
    prompt = (
        f"Write {n} different, realistic user messages that can only be answered well by calling the tool below. "
        "Do NOT mention the tool name. Return ONLY a JSON array of strings.\n\n"
        f"{json.dumps(tool)}"
    )
    try:
        resp = client.messages.create(model=model, max_tokens=800,
                                      messages=[{"role": "user", "content": prompt}])
        text = "".join(b.text for b in resp.content if b.type == "text")
        arr = json.loads(re.search(r"\[.*\]", text, re.S).group(0))
        return [str(x) for x in arr][:n]
    except Exception as e:  # noqa: BLE001
        report("WARN", "probe-gen", f"Could not generate probes for {tool['name']}: {e}")
        return []


def run_probes(client, model, system, tools, probes):
    rows = []
    for expected, text in probes:
        try:
            resp = api_call(client, model, system, tools, text, tool_choice={"type": "auto"})
            names = [b.name for b in called_tools(resp)]
        except Exception as e:  # noqa: BLE001
            names = [f"ERROR:{e}"]
        rows.append({"expected": expected, "called": names, "text": text})
    return rows


def summarize(rows):
    per_tool = {}
    for r in rows:
        d = per_tool.setdefault(r["expected"], {"hit": 0, "wrong": 0, "none": 0, "n": 0})
        d["n"] += 1
        if r["expected"] in r["called"]:
            d["hit"] += 1
        elif r["called"]:
            d["wrong"] += 1
        else:
            d["none"] += 1
    total_hit = sum(d["hit"] for d in per_tool.values())
    total = sum(d["n"] for d in per_tool.values())
    return per_tool, (total_hit / total if total else 0.0)


def print_summary(label, rows):
    per_tool, rate = summarize(rows)
    print(f"  -- {label}: overall hit rate {rate:.0%}")
    for name, d in per_tool.items():
        print(f"     {name:<30} hit {d['hit']}/{d['n']}  wrong-tool {d['wrong']}  no-call {d['none']}")
    return rate


def check_behavior(client, model, system, tools, n, compare_models):
    section("5. Behavioral probes (natural user requests, tool_choice=auto)")
    probes = []
    for t in tools:
        for text in generate_probes(client, model, t, n):
            probes.append((t["name"], text))
    if not probes:
        report("WARN", "probes", "No probes available; skipping behavioral checks.")
        return

    rows = run_probes(client, model, system, tools, probes)
    rate = print_summary(f"{model} + system prompt", rows)
    if rate >= 0.8:
        report("OK", "behavior", f"Model calls the right tool {rate:.0%} of the time")
    elif rate > 0:
        report("WARN", "behavior", f"Only {rate:.0%} correct tool calls. See sections 6 and 7 for likely causes.")
    else:
        report("FAIL", "behavior", "The model never called the right tool for natural requests.")

    # confusion: wrong tool vs no call
    wrong = sum(1 for r in rows if r["called"] and r["expected"] not in r["called"])
    none = sum(1 for r in rows if not r["called"])
    if none > wrong:
        report("INFO", "behavior", "Most misses are NO CALL -> look at the system prompt, descriptions, model strength.")
    elif wrong > none:
        report("INFO", "behavior", "Most misses are the WRONG tool -> descriptions overlap; sharpen the distinctions.")

    # 6. Ablation
    section("6. Ablation (same probes without the system prompt)")
    if system:
        rows_nosys = run_probes(client, model, None, tools, probes)
        rate_nosys = print_summary(f"{model}, no system prompt", rows_nosys)
        if rate_nosys - rate >= 0.2:
            report("FAIL", "ablation", f"Hit rate jumps {rate:.0%} -> {rate_nosys:.0%} without the system prompt. "
                                       "Your system prompt is suppressing tool use.")
        else:
            report("OK", "ablation", "System prompt does not appear to suppress tool use")
    else:
        report("INFO", "ablation", "No system prompt to ablate.")

    # 7. Model comparison
    section("7. Model comparison")
    if not compare_models:
        report("INFO", "models", "Pass --compare-models <model> ... to test whether the model is the bottleneck.")
        return
    for m in compare_models:
        rows_m = run_probes(client, m, system, tools, probes)
        rate_m = print_summary(f"{m} + system prompt", rows_m)
        if rate_m - rate >= 0.2:
            report("WARN", "models", f"{m} scores {rate_m:.0%} vs {rate:.0%} on {model}. Consider a stronger model.")


# --------------------------------------------------------------------------- #
# 8. Conversation history
# --------------------------------------------------------------------------- #
def check_history(history):
    section("8. Conversation history")
    if not history:
        report("INFO", "history", "No history provided (add a 'history' list to the config to scan for tool errors).")
        return
    use_ids, result_ids, errors = set(), set(), []
    for msg in history:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                use_ids.add(block.get("id"))
            elif block.get("type") == "tool_result":
                result_ids.add(block.get("tool_use_id"))
                text = json.dumps(block.get("content", ""))
                if block.get("is_error") or re.search(r"error|traceback|exception|timeout|not found", text, re.I):
                    errors.append(text[:160])
    if not use_ids:
        report("WARN", "history", "No tool_use blocks in the history: the model never called any tool.")
    if use_ids - result_ids:
        report("FAIL", "history", f"{len(use_ids - result_ids)} tool_use block(s) have no tool_result.")
    if errors:
        report("WARN", "history", f"{len(errors)} tool result(s) look like errors. After failures the model "
                                  f"may stop trying. First: {errors[0]}")
    if use_ids and not errors and not (use_ids - result_ids):
        report("OK", "history", f"{len(use_ids)} tool calls, all with clean results")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Diagnose why an agent does not call its tools.")
    ap.add_argument("--config", required=True, help="Path to JSON config (system, tools, model, history)")
    ap.add_argument("--model", help="Override model from config")
    ap.add_argument("--compare-models", nargs="*", default=[], help="Other models to run the probes on")
    ap.add_argument("--probes-per-tool", type=int, default=2)
    ap.add_argument("--skip-live", action="store_true", help="Static checks only (no API calls)")
    ap.add_argument("--report", help="Write findings as JSON to this path")
    args = ap.parse_args()

    with open(args.config, encoding="utf-8") as f:
        cfg = json.load(f)
    tools = cfg.get("tools", [])
    system = cfg.get("system", "")
    model = args.model or cfg.get("model") or "claude-sonnet-5-5"

    check_definitions(tools)
    check_system_prompt(system, tools)
    check_history(cfg.get("history"))

    if not args.skip_live and tools:
        import anthropic
        import httpx

        captured = []

        def _hook(request):
            try:
                captured.append(json.loads(request.content))
            except Exception:  # noqa: BLE001
                pass

        client = anthropic.Anthropic(http_client=httpx.Client(event_hooks={"request": [_hook]}))
        if check_wire(client, captured, model, system, tools):
            check_forced_choice(client, model, system, tools)
            check_behavior(client, model, system, tools, args.probes_per_tool, args.compare_models)

    section("Summary")
    counts = {lvl: sum(1 for f in FINDINGS if f["level"] == lvl) for lvl in ICONS}
    print(f"FAIL: {counts['FAIL']}   WARN: {counts['WARN']}   OK: {counts['OK']}   INFO: {counts['INFO']}")
    if counts["FAIL"]:
        print("Fix the FAIL items first; they usually fully explain why tools are not called.")

    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump(FINDINGS, f, indent=2, ensure_ascii=False)
        print(f"Report written to {args.report}")

    sys.exit(1 if counts["FAIL"] else 0)


if __name__ == "__main__":
    main()
