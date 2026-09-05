"""Provider adapter.

Hackathon venues hand out whatever key they have. This isolates the three things that
differ between providers — tool schema shape, response parsing, tool-result encoding —
so `agent.py` never has to care.

    DCORTEX_PROVIDER = anthropic | gemini | openai      (default: anthropic)
    DCORTEX_MODEL    = provider-specific model string
    <PROVIDER>_API_KEY in the environment

Nothing about the deterministic core changes. The rules engine is provider-independent
by construction — which is the point of keeping the LLM at the edge.
"""
import json, os

_MODEL_SETTING = os.environ.get("DCORTEX_MODEL", "").strip()
_REQUESTED_PROVIDER = os.environ.get("DCORTEX_PROVIDER", "").strip().lower()
PROVIDER = _REQUESTED_PROVIDER

# Accept the common provider/model notation directly, e.g.
# DCORTEX_MODEL=openai/gpt-oss-120b.
if not PROVIDER:
    PROVIDER = _MODEL_SETTING.split("/", 1)[0].lower() if "/" in _MODEL_SETTING else "anthropic"

if PROVIDER == "sarvam":
    PROVIDER = "openai"

_DEFAULT_MODEL = {
    "anthropic": "claude-sonnet-4-6",
    "gemini": "gemini-3.1-pro-preview",
    "openai": "gpt-4o",
    "sarvam": "sarvam-105b-conversations",
}
MODEL = _MODEL_SETTING or (
    _DEFAULT_MODEL["sarvam"] if _REQUESTED_PROVIDER == "sarvam"
    else _DEFAULT_MODEL.get(PROVIDER, "")
)
for prefix in ("openai/", "sarvam/"):
    if MODEL.lower().startswith(prefix):
        MODEL = MODEL[len(prefix):]

_client = None


def _key(*names):
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    raise RuntimeError(
        f"No API key found for provider '{PROVIDER}'. "
        f"Set {names[0]} before starting the app.")


def client():
    global _client
    if _client is not None:
        return _client
    if PROVIDER == "anthropic":
        import anthropic
        _client = anthropic.Anthropic(api_key=_key("ANTHROPIC_API_KEY"))
    elif PROVIDER == "gemini":
        from google import genai
        _client = genai.Client(api_key=_key("GEMINI_API_KEY", "GOOGLE_API_KEY"))
    elif PROVIDER == "openai":
        from openai import OpenAI
        base_url = os.environ.get("OPENAI_BASE_URL")
        if _REQUESTED_PROVIDER == "sarvam" and not base_url:
            base_url = "https://api.sarvam.ai/v1"
        _client = OpenAI(api_key=_key(
            "SARVAM_API_KEY" if _REQUESTED_PROVIDER == "sarvam" else "OPENAI_API_KEY",
            "OPENAI_API_KEY" if _REQUESTED_PROVIDER == "sarvam" else "OPENAI_API_KEY"),
                         base_url=base_url)
    else:
        raise RuntimeError(f"Unknown DCORTEX_PROVIDER '{PROVIDER}'")
    return _client


# --------------------------------------------------------------------------
# Tool schema translation.  TOOLS is the Anthropic shape (tools.py) — the others
# are derived from it, so there is one source of truth for the tool contract.
# --------------------------------------------------------------------------
def _clean(schema):
    """Gemini rejects empty `properties` objects and unknown keys."""
    props = schema.get("properties") or {}
    out = {"type": "OBJECT", "properties": {}}
    for k, v in props.items():
        t = (v.get("type") or "string").upper()
        if t == "ARRAY":
            out["properties"][k] = {"type": "ARRAY", "items": {"type": "STRING"}}
        elif t == "OBJECT":
            out["properties"][k] = {"type": "OBJECT",
                                    "properties": {kk: {"type": "STRING"}
                                                   for kk in (v.get("properties") or {})}}
        else:
            out["properties"][k] = {"type": t}
    if schema.get("required"):
        out["required"] = list(schema["required"])
    return out if out["properties"] else None


def translate_tools(tools):
    if PROVIDER == "anthropic":
        return tools
    if PROVIDER == "openai":
        return [{"type": "function",
                 "function": {"name": t["name"], "description": t["description"],
                              "parameters": t["input_schema"]}} for t in tools]
    if PROVIDER == "gemini":
        decls = []
        for t in tools:
            d = {"name": t["name"], "description": t["description"][:1000]}
            params = _clean(t["input_schema"])
            if params:
                d["parameters"] = params
            decls.append(d)
        return [{"function_declarations": decls}]
    raise RuntimeError(PROVIDER)


# --------------------------------------------------------------------------
# One call.  Returns (text, tool_calls, raw) where tool_calls is a list of
# {"id":..., "name":..., "args": {...}} regardless of provider.
# --------------------------------------------------------------------------
def _with_retry(fn, tries=4, base=2.0):
    """Free-tier quota and transient 503s are the norm at a hackathon venue.
    Back off rather than failing the whole eval run on one blip."""
    import random, time as _t
    last = None
    for i in range(tries):
        try:
            return fn()
        except Exception as e:
            msg = str(e)
            last = e
            if not any(c in msg for c in ("429", "RESOURCE_EXHAUSTED", "503",
                                          "overloaded", "UNAVAILABLE", "rate_limit")):
                raise
            if i == tries - 1:
                break
            wait = base * (2 ** i) + random.random()
            print(f"    [retry {i+1}/{tries-1} in {wait:.1f}s — {msg[:70]}]")
            _t.sleep(wait)
    raise last


def complete(system, messages, tools=None, max_tokens=1500):
    """`messages` is a provider-neutral list of
       {"role": "user"|"assistant", "content": str}
       or {"role": "tool", "tool_use_id": ..., "name": ..., "content": str}."""
    c = client()

    if PROVIDER == "anthropic":
        msgs = []
        for m in messages:
            if m["role"] == "tool":
                blk = {"type": "tool_result", "tool_use_id": m["tool_use_id"],
                       "content": m["content"]}
                if msgs and msgs[-1]["role"] == "user" and isinstance(msgs[-1]["content"], list):
                    msgs[-1]["content"].append(blk)
                else:
                    msgs.append({"role": "user", "content": [blk]})
            elif m["role"] == "assistant" and m.get("tool_calls"):
                msgs.append({"role": "assistant", "content":
                             [{"type": "tool_use", "id": t["id"], "name": t["name"],
                               "input": t["args"]} for t in m["tool_calls"]]
                             + ([{"type": "text", "text": m["content"]}] if m.get("content") else [])})
            else:
                msgs.append({"role": m["role"], "content": m["content"]})
        kw = {"model": MODEL, "max_tokens": max_tokens, "system": system, "messages": msgs}
        if tools:
            kw["tools"] = translate_tools(tools)
        r = _with_retry(lambda: c.messages.create(**kw))
        text = "".join(b.text for b in r.content if b.type == "text")
        calls = [{"id": b.id, "name": b.name, "args": b.input}
                 for b in r.content if b.type == "tool_use"]
        return text, calls, r

    if PROVIDER == "openai":
        msgs = [{"role": "system", "content": system}]
        for m in messages:
            if m["role"] == "tool":
                msgs.append({"role": "tool", "tool_call_id": m["tool_use_id"],
                             "content": m["content"]})
            elif m["role"] == "assistant" and m.get("tool_calls"):
                msgs.append({"role": "assistant", "content": m.get("content") or None,
                             "tool_calls": [{"id": t["id"], "type": "function",
                                             "function": {"name": t["name"],
                                                          "arguments": json.dumps(t["args"])}}
                                            for t in m["tool_calls"]]})
            else:
                msgs.append({"role": m["role"], "content": m["content"]})
        kw = {"model": MODEL, "messages": msgs, "max_tokens": max_tokens}
        if tools:
            kw["tools"] = translate_tools(tools)
        r = _with_retry(lambda: c.chat.completions.create(**kw))
        msg = r.choices[0].message
        calls = [{"id": t.id, "name": t.function.name,
                  "args": json.loads(t.function.arguments or "{}")}
                 for t in (msg.tool_calls or [])]
        return msg.content or "", calls, r

    if PROVIDER == "gemini":
        from google.genai import types
        contents = []
        for m in messages:
            if m["role"] == "tool":
                contents.append(types.Content(role="user", parts=[types.Part.from_function_response(
                    name=m["name"], response={"result": m["content"]})]))
            elif m["role"] == "assistant" and m.get("tool_calls"):
                contents.append(types.Content(role="model", parts=[
                    types.Part.from_function_call(name=t["name"], args=t["args"])
                    for t in m["tool_calls"]]))
            else:
                contents.append(types.Content(
                    role="user" if m["role"] == "user" else "model",
                    parts=[types.Part.from_text(text=m["content"])]))
        cfg = types.GenerateContentConfig(
            system_instruction=system, max_output_tokens=max_tokens,
            tools=[types.Tool(function_declarations=translate_tools(tools)[0]["function_declarations"])]
            if tools else None)
        r = _with_retry(lambda: c.models.generate_content(
            model=MODEL, contents=contents, config=cfg))
        text, calls = "", []
        for i, p in enumerate(r.candidates[0].content.parts or []):
            if getattr(p, "function_call", None):
                calls.append({"id": f"call_{i}", "name": p.function_call.name,
                              "args": dict(p.function_call.args or {})})
            elif getattr(p, "text", None):
                text += p.text
        return text, calls, r

    raise RuntimeError(PROVIDER)


def describe():
    return f"{PROVIDER}/{MODEL}"
