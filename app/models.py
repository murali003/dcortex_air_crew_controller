"""List the models this key can actually reach.

    python -m app.models
"""
import os
from . import llm


def main():
    if llm.PROVIDER == "gemini":
        c = llm.client()
        print(f"Models visible to this key ({llm.PROVIDER}):\n")
        for m in c.models.list():
            actions = getattr(m, "supported_actions", None) or []
            if not actions or "generateContent" in actions:
                print(f"  {m.name}")
    elif llm.PROVIDER == "openai":
        for m in llm.client().models.list().data:
            print(f"  {m.id}")
    else:
        print("Anthropic: claude-opus-5, claude-sonnet-5, claude-sonnet-4-6, "
              "claude-haiku-4-5-20251001")


if __name__ == "__main__":
    main()
