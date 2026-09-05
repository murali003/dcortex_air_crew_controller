"""The LLM layer. Three jobs, none of them arithmetic:

  1. ROUTE    English  -> typed tool calls
  2. NARRATE  engine JSON -> controller-readable prose
  3. REFUSE   no tool fits -> say so

The narrator is handed the engine's JSON and NOTHING ELSE. It has no access to the
dataset, so it physically cannot name a crew member it wasn't given. A grounding
check after generation enforces that mechanically.
"""
import json, os, re, time
from .tools import TOOLS, call_tool
from .llm import complete, describe

MAX_TOOL_ROUNDS = 4


ROUTER_SYSTEM = """You are the tool router for an airline Crew Control desk (dCortex Air, hub BLR).

Snapshot "now" is 2026-09-14T18:00:00Z. The schedule covers 2026-09-14 to 2026-09-20. All times UTC.
Resolve relative dates before calling: "today" = 2026-09-14, "tomorrow" = 2026-09-15.

Your ONLY job is to convert the controller's message into tool calls.

HARD RULES:
- Never compute duty hours, flight hours, FDP, legality or cost yourself. The tools do that exactly.
- Never state a crew_id, flight, cost or hour figure that a tool has not returned.
- Chain tools when needed. A sick call is usually: simulate_sick -> rank_options.
  "Which flights break" is simulate_sick alone. "What should I do" needs rank_options.
- To rank replacements you need the pairing_id and the vacated role. Use get_pairing
  or get_crew first if you only have a crew_id.
- Always pass the sick/unavailable crew member in rank_options.exclude.
- If nothing in the toolset can answer the question, call cannot_answer. That is a
  correct outcome, not a failure. Do not guess.

When you have enough tool results, stop calling tools and reply with the single word DONE."""

NARRATOR_SYSTEM = """You are explaining a Crew Control decision to an experienced airline controller
who is under time pressure at 06:00 on a bad day.

You are given the controller's question and JSON produced by a deterministic rules engine.

ABSOLUTE RULES:
- Every fact in your answer must appear in that JSON. Do not add crew, flights, pairings,
  costs, hours or rules that are not present.
- Quote numbers EXACTLY as given. Never round, convert or recompute. If the JSON says
  61.33h and 1h20m, write 61.33h and 1h20m.
- If `legal` is false, say so in the first sentence and name the rule ID.
- Never soften a breach. "Would exceed" is not "might be close to".

STYLE:
- Answer first, in one line. Then the reasoning, citing rule IDs.
- Terse. No preamble, no "I'd be happy to". The controller is reading this in five seconds.
- For ranked options, give the top 3 as a compact list: action, cost, and the one-line reason.
- For a `cannot_answer` result, say plainly what you cannot determine and why.

You are not a chatbot. You are the line on the screen a controller acts on."""


# --------------------------------------------------------------------------
# Grounding check — the reason we can claim we do not hallucinate
# --------------------------------------------------------------------------
TOKEN_RE = re.compile(r"(C-\d{3,4}|DX\d{3}|P-\d{4}|VT-[A-Z]{3}|RULE-[A-Z]+-\d{2}|\d+\.\d+|[\d,]{4,})")


def grounding_violations(text, evidence_json):
    """Every identifier and number in the narration must appear in the engine output."""
    blob = json.dumps(evidence_json)
    blob_nocommas = blob.replace(",", "")
    bad = []
    for tok in set(TOKEN_RE.findall(text)):
        t = tok.replace(",", "")
        if t in blob or t in blob_nocommas:
            continue
        # allow rounded forms of a number that IS present (18500 -> 18,500)
        if re.fullmatch(r"[\d.]+", t) and (t.rstrip("0").rstrip(".") in blob_nocommas):
            continue
        bad.append(tok)
    return sorted(bad)


def render_trace(results):
    """Rendered from the engine directly — no LLM. This is the audit trail."""
    lines = []
    for name, args, res in results:
        lines.append(f"→ {name}({json.dumps(args)})")
        if isinstance(res, dict) and "checks" in res:
            for c in res["checks"]:
                lines.append(f"   [{'PASS' if c['passed'] else 'FAIL'}] {c['rule_id']}: {c['detail']}")
        elif isinstance(res, dict) and "options" in res:
            for o in res.get("options", [])[:5]:
                lines.append(f"   #{o.get('rank')} {o['crew_id']} ₹{o['cost_inr']:,} "
                             f"delay {o.get('delay_hours', 0)}h — {o.get('reasoning', '')}")
        elif isinstance(res, dict) and "explanation" in res:
            lines.append(f"   {res['explanation']}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------
def answer(question, history=None, verbose=False):
    t0 = time.time()
    messages = list(history or []) + [{"role": "user", "content": question}]
    results = []

    for _ in range(MAX_TOOL_ROUNDS):
        text, calls, _raw = complete(ROUTER_SYSTEM, messages, tools=TOOLS)
        if not calls:
            break
        messages.append({"role": "assistant", "content": text, "tool_calls": calls})
        for cl in calls:
            out = call_tool(cl["name"], cl["args"])
            results.append((cl["name"], cl["args"], out))
            if verbose:
                print(f"  \u2192 {cl['name']}({cl['args']})")
            messages.append({"role": "tool", "tool_use_id": cl["id"], "name": cl["name"],
                             "content": json.dumps(out, default=str)[:20000]})

    evidence = [{"tool": n, "args": a, "result": r} for n, a, r in results]

    if not evidence:
        return {"answer": "I can't answer that reliably from the crew ops data I have.",
                "evidence": [], "trace": "", "grounded": True,
                "latency_s": round(time.time() - t0, 2)}

    unavailable = next(
        (item["result"] for item in evidence
         if isinstance(item["result"], dict)
         and item["result"].get("answerable") is False),
        None,
    )
    if unavailable:
        date = unavailable.get("date", "that date")
        start, end = unavailable["schedule_date_range"]
        text = (
            f"I can't determine which flights operated on {date}: "
            f"the available flight schedule covers {start} through {end}. "
            "Duty history is available outside that range, but flight-level schedule "
            "data is not."
        )
        return {
            "answer": text,
            "evidence": evidence,
            "trace": render_trace(results),
            "grounded": True,
            "model": describe(),
            "tools_used": [n for n, _, _ in results],
            "latency_s": round(time.time() - t0, 2),
        }

    text, _, _ = complete(NARRATOR_SYSTEM, [{"role": "user", "content":
        f"Controller asked: {question}\n\nEngine output:\n"
        f"{json.dumps(evidence, default=str)[:40000]}"}], max_tokens=1200)

    bad = grounding_violations(text, evidence)
    grounded = not bad
    if not grounded:
        text = ("⚠ The narration referenced values not present in the engine output "
                f"({', '.join(bad)}), so it was suppressed. Verified engine result below.\n\n"
                + render_trace(results))

    return {"answer": text, "evidence": evidence, "trace": render_trace(results),
            "grounded": grounded, "ungrounded_tokens": bad, "model": describe(),
            "tools_used": [n for n, _, _ in results],
            "latency_s": round(time.time() - t0, 2)}


if __name__ == "__main__":
    import sys
    q = " ".join(sys.argv[1:]) or "Captain C-1042 called in sick for tomorrow. What should I do?"
    r = answer(q, verbose=True)
    print("\n" + r["answer"])
    print("\n--- TRACE ---\n" + r["trace"])
    print(f"\n{r['latency_s']}s · {describe()} · grounded={r['grounded']} · {r['tools_used']}")
