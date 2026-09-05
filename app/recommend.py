"""Tier-3: enumerate every candidate, check each with the rules engine, cost it, rank it.

This is exhaustive search over ~150 crew, not an LLM guess. At this dataset size
exhaustive is both cheap and provably complete — that is the whole argument for
putting it in deterministic code.
"""
from datetime import timedelta
from .data import STORE, P, D
from .rules import check_assignment, build_duty_days

PILOT_ROLES = {"Captain", "First Officer"}

# Documented positioning legs (starter README). Anything else is NOT modelled —
# the engine says so rather than inventing a connection.
DEADHEAD_LEGS = {
    ("DEL", "BLR"): [
        {"flight_no": "DX402", "arr": "08:45", "when": "odd"},
        {"flight_no": "DX589", "arr": "07:45", "when": "even"},
    ]
}


def _cost_key(role):
    return "pilot" if role in PILOT_ROLES else "cabin"


def _deadhead(from_base, to_base, first_date, orig_report_utc):
    legs = DEADHEAD_LEGS.get((from_base, to_base))
    if not legs:
        return None
    day = D(first_date).day
    want = "odd" if day % 2 else "even"
    leg = next((l for l in legs if l["when"] == want), legs[0])
    arr = P(f"{first_date}T{leg['arr']}:00Z")
    new_report = arr + timedelta(minutes=15)
    delay = max(0.0, (new_report - P(orig_report_utc)).total_seconds() / 3600)
    return {"flight_no": leg["flight_no"], "new_report_utc": new_report.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "delay_hours": round(delay, 2)}


def _is_free(crew_id, dates):
    r = STORE.roster_by_crew.get(crew_id, {})
    return all(d not in r for d in dates)


def _reserve_available(crew_id, first_date, report_utc):
    r = STORE.reserves.get(crew_id)
    if not r or first_date not in r["dates"]:
        return False
    w = r["oncall_window_utc"]
    t = P(report_utc).strftime("%H:%M")
    return w["start"] <= t <= w["end"]


def rank_options(pairing_id, vacated_role, dates=None, exclude=(), max_options=10):
    p = STORE.pairings[pairing_id]
    days = build_duty_days(pairing_id, dates)
    dates = [d.date for d in days]
    first_date, report_utc = days[0].date, days[0].report_utc
    origin, ac_type = days[0].dep_station, days[0].aircraft_type
    c = STORE.costs
    ck = _cost_key(vacated_role)

    options = []
    for cid, cr in STORE.crew.items():
        if cid in exclude or cr["rank"] != vacated_role or cr["status"] != "active":
            continue
        if ac_type not in cr["ratings"]:
            continue
        if not _is_free(cid, dates):
            continue

        is_reserve = _reserve_available(cid, first_date, report_utc)
        same_base = cr["base"] == origin
        dh = None
        if not same_base:
            dh = _deadhead(cr["base"], origin, first_date, report_utc)
            if dh is None:
                continue  # no modelled positioning route — omitted, not invented

        chk = check_assignment(cid, pairing_id, dates, deadhead=not same_base)
        if not chk["legal"]:
            options.append({"crew_id": cid, "legal": False, "issues": chk["issues"],
                            "action": f"{vacated_role} {cid} — NOT legal", "cost_inr": None,
                            "rules_checked": chk["rules_checked"]})
            continue

        if is_reserve:
            cost = c[f"reserve_callout_{ck}"]
            kind, why = "reserve callout", f"{origin}-based reserve, on-call {STORE.reserves[cid]['oncall_window_utc']['start']}–{STORE.reserves[cid]['oncall_window_utc']['end']}Z, reachable in {cr['reachability_minutes']} min"
        else:
            cost = c[f"dayoff_callout_{ck}"]
            kind, why = "day-off callout", f"{cr['base']}-based, {'/'.join(cr['ratings'])}-rated, off-roster on {dates[0]}, reachable in {cr['reachability_minutes']} min"
        delay = 0.0
        if dh:
            cost += c["deadhead_positioning"] + round(dh["delay_hours"] * c["delay_cost_per_duty_hour"])
            delay = dh["delay_hours"]
            kind = f"deadhead from {cr['base']}"
            why = f"positions on {dh['flight_no']}, new report {dh['new_report_utc']}; delays first departure by {delay}h"

        options.append({
            "crew_id": cid, "legal": True,
            "action": f"Assign {vacated_role} {cid} ({kind})",
            "cost_inr": cost, "delay_hours": delay,
            "coverage": f"all {sum(len(d.flights) for d in days)} flights",
            "rules_checked": chk["rules_checked"],
            "reachability_minutes": cr["reachability_minutes"],
            "risk_score": STORE.risk.get(cid, {}).get("disruption_risk_score"),
            "reasoning": why,
        })

    legal = [o for o in options if o["legal"]]
    legal.sort(key=lambda o: (o["cost_inr"], o["delay_hours"], o["reachability_minutes"]))
    for i, o in enumerate(legal, 1):
        o["rank"] = i

    cancel_cost = len([f for d in days for f in d.flights]) * c["cancellation_per_flight"]
    return {
        "pairing_id": pairing_id, "role": vacated_role, "dates": dates,
        "options": legal[:max_options],
        "rejected": [o for o in options if not o["legal"]][:20],
        "fallback": {"action": "Cancel the pairing", "cost_inr": cancel_cost,
                     "passengers_affected": STORE.pax([f for d in days for f in d.flights])},
    }
