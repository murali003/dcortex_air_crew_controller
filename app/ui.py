"""Streamlit chat UI.

Design rule: the ANSWER comes from the LLM narrator, the TRACE comes from the engine.
If the two ever disagree, the trace wins and the UI says so. A controller must be able
to challenge any line on this screen.

Run:  streamlit run app/ui.py
"""
import json, os, sys
import streamlit as st

# Streamlit puts the SCRIPT's folder on sys.path, not the repo root — add the root
# so `from app...` resolves however the app is launched.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agent import answer
from app.tools import (crew_above_duty_threshold, list_expiring_certs, get_reserves,
                       get_flights)

st.set_page_config(page_title="Crew Ops Advisor", page_icon="✈", layout="wide")

TODAY = "2026-09-15"

st.markdown("""
<style>
.block-container{padding-top:2rem}
.stChatMessage{font-size:0.95rem}
code{font-size:0.82rem}
</style>""", unsafe_allow_html=True)

# ---------------------------------------------------------------- sidebar
with st.sidebar:
    st.markdown("### ✈ dCortex Air — Crew Control")
    st.caption("Snapshot 2026-09-14 18:00Z · all times UTC")

    st.markdown("#### Morning briefing")
    tight = crew_above_duty_threshold(TODAY, 50)
    st.metric("Crew within 10h of the 60h/7d wall", tight["count"])
    for c in tight["crew"][:5]:
        st.caption(f"`{c['crew_id']}` {c['rank']} — {c['duty_hours_7d']}h "
                   f"({c['headroom_hours']}h left)")

    certs = list_expiring_certs(TODAY, 30)
    st.metric("Certifications expiring in 30 days", certs["count"])
    for c in certs["certifications"][:5]:
        st.caption(f"`{c['crew_id']}` {c['cert_type']} → {c['valid_to']} ({c['days_left']}d)")

    res = get_reserves(base="BLR", date=TODAY)
    st.metric(f"Reserves on call at BLR, {TODAY}", res["count"])
    st.metric(f"Flights {TODAY}", get_flights(date=TODAY)["count"])

    st.divider()
    st.caption("**Boundary** — the LLM routes and narrates. All legality, cost and "
               "ranking arithmetic is deterministic Python. Open any Reasoning trace "
               "to see the rule-by-rule working.")
    if st.button("Clear conversation"):
        st.session_state.msgs = []
        st.rerun()

# ---------------------------------------------------------------- main
st.title("Crew Ops Advisor")

if "msgs" not in st.session_state:
    st.session_state.msgs = []

if not st.session_state.msgs:
    st.caption("Try one of these:")
    cols = st.columns(3)
    seeds = ["Who's on reserve at BLR tomorrow?",
             "Captain C-1042 called in sick for 15 Sep — which flights are now uncrewed?",
             "C-1042 is out for P-2291. What should I do?"]
    for col, s in zip(cols, seeds):
        if col.button(s, use_container_width=True):
            st.session_state.pending = s

for m in st.session_state.msgs:
    with st.chat_message(m["role"]):
        st.markdown(m["content"])
        if m.get("trace"):
            with st.expander(f"Reasoning trace · {m['latency']}s · "
                             f"{'grounded ✓' if m['grounded'] else 'GROUNDING FAILED ⚠'}"):
                st.code(m["trace"], language="text")
                st.caption("Rendered directly from the rules engine — no LLM in this path.")
                with st.popover("Raw engine JSON"):
                    st.json(m["evidence"])

prompt = st.chat_input("Ask Crew Control…") or st.session_state.pop("pending", None)

if prompt:
    st.session_state.msgs.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        with st.spinner("Checking the rulebook…"):
            hist = [{"role": m["role"], "content": m["content"]}
                    for m in st.session_state.msgs[:-1]][-6:]
            try:
                r = answer(prompt, history=hist)
            except Exception as e:
                r = {"answer": f"Engine error: {e}", "trace": "", "evidence": [],
                     "grounded": True, "latency_s": 0}
        st.markdown(r["answer"])
        if not r["grounded"]:
            st.error("The narration was suppressed — it referenced values the engine "
                     "did not produce. The verified engine result is shown above.")
        if r.get("trace"):
            with st.expander(f"Reasoning trace · {r['latency_s']}s · "
                             f"{'grounded ✓' if r['grounded'] else 'GROUNDING FAILED ⚠'}"):
                st.code(r["trace"], language="text")
                st.caption("Rendered directly from the rules engine — no LLM in this path.")
                with st.popover("Raw engine JSON"):
                    st.json(r["evidence"])

    st.session_state.msgs.append({
        "role": "assistant", "content": r["answer"], "trace": r.get("trace", ""),
        "evidence": r.get("evidence", []), "grounded": r["grounded"],
        "latency": r["latency_s"]})
