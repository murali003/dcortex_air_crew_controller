"""Pre-flight check. Run this BEFORE the eval so a bad key fails in 3 seconds
instead of 38 API calls later.

    python -m app.doctor
"""
import os, sys


def main():
    print("=" * 60)
    print("Crew Ops Advisor - pre-flight")
    print("=" * 60)

    # 1. dataset
    try:
        from .data import STORE
        print(f"  data      OK   {len(STORE.flights)} flights, {len(STORE.crew)} crew, "
              f"{len(STORE.pairings)} pairings")
    except Exception as e:
        print(f"  data      FAIL {e}")
        return 1

    # 2. engine
    try:
        from .rules import check_assignment
        r = check_assignment("C-2087", "P-2291")
        ok = not r["legal"] and "1h20m" in str(r["issues"])
        print(f"  engine    {'OK' if ok else 'FAIL'}   C-2087/P-2291 -> {r['issues'][:1]}")
        if not ok:
            return 1
    except Exception as e:
        print(f"  engine    FAIL {e}")
        return 1

    # 3. provider config
    from . import llm
    print(f"  provider  {llm.PROVIDER}   model={llm.MODEL or '(unset)'}")
    keyvar = ("SARVAM_API_KEY" if llm._REQUESTED_PROVIDER == "sarvam"
              else {"anthropic": "ANTHROPIC_API_KEY", "gemini": "GEMINI_API_KEY",
                    "openai": "OPENAI_API_KEY"}.get(llm.PROVIDER, "?"))
    key = os.environ.get(keyvar) or os.environ.get("GOOGLE_API_KEY", "")
    if not key:
        print(f"  key       MISSING  set ${keyvar}")
        return 1
    print(f"  key       set    {keyvar}={key[:6]}...{key[-4:]} ({len(key)} chars)")
    if llm.PROVIDER == "anthropic" and not key.startswith("sk-ant-"):
        print("  key       WARN   Anthropic keys start with 'sk-ant-'. This key looks like")
        print("                   it belongs to another provider — set DCORTEX_PROVIDER.")

    # 4. one live round trip
    try:
        from .tools import TOOLS
        text, calls, _ = llm.complete(
            "You are a tool router. Use a tool.",
            [{"role": "user", "content": "What is C-2210's base and rating?"}],
            tools=TOOLS, max_tokens=400)
        print(f"  llm call  OK   tool={calls[0]['name'] if calls else '(none, text only)'}")
    except Exception as e:
        print(f"  llm call  FAIL {type(e).__name__}: {str(e)[:200]}")
        return 1

    print("-" * 60)
    print("All green. Run:  python -m eval.run_eval --agent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
