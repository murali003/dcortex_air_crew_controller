"""Tier-2: apply a disruption event to the snapshot and compute its consequences.

Each simulate_* function returns the same shape: what broke, why, who is at risk
downstream, and how many passengers are exposed. No LLM in this path.
"""
from datetime import timedelta
from .data import STORE, P, D
from .rules import check_assignment, build_duty_days, rule_fdp_01, _hm


def simulate_sick(crew_id, dates=None, reported_utc=None):
    """A crew member is unavailable. Which flights lose their complement?"""
    assigns = STORE.roster_by_crew.get(crew_id, {})
    dates = dates or sorted(assigns)
    affected = {d: assigns[d] for d in dates if d in assigns}
    uncovered, pairings = [], set()
    for d, (pid, day) in affected.items():
        uncovered += day["flights"]
        pairings.add(pid)
    role = next((STORE.role_of(p, crew_id) for p in pairings), None)
    return {
        "event": "SICK_CREW", "crew_id": crew_id, "reported_utc": reported_utc,
        "role": role, "pairings_broken": sorted(pairings),
        "uncovered_flights": sorted(uncovered),
        "passengers_affected": STORE.pax(uncovered),
        "explanation": (
            f"{role} {crew_id} operates {'/'.join(sorted(pairings))} on {', '.join(dates)}. "
            f"Without a {role} the complement is incomplete, so all "
            f"{len(uncovered)} legs are uncrewed: {', '.join(sorted(uncovered))}."),
    }


def simulate_station_closure(station, date, start_utc, end_utc):
    """Station shut for a window. Flights touching it in the window cannot operate."""
    lo = P(f"{date}T{start_utc}:00Z")
    hi = P(f"{date}T{end_utc}:00Z")
    hit = []
    for f in STORE.flights.values():
        if f["date"] != date:
            continue
        if f["dep_station"] == station and lo <= P(f["dep_utc"]) <= hi:
            hit.append((f["flight_id"], "departure blocked"))
        elif f["arr_station"] == station and lo <= P(f["arr_utc"]) <= hi:
            hit.append((f["flight_id"], "arrival blocked"))
    pairings = sorted({STORE.pairing_of_flight[fid] for fid, _ in hit
                       if fid in STORE.pairing_of_flight})
    return {
        "event": "STATION_CLOSURE", "station": station, "date": date,
        "window_utc": f"{start_utc}-{end_utc}",
        "affected_flights": [{"flight_id": f, "reason": r} for f, r in sorted(hit)],
        "pairings_affected": pairings,
        "passengers_affected": STORE.pax([f for f, _ in hit]),
        "explanation": (
            f"{station} is closed {start_utc}–{end_utc}Z on {date}. "
            f"{len(hit)} legs touch {station} inside that window across "
            f"{len(pairings)} pairings. Downstream legs on the same aircraft rotation "
            f"inherit the delay."),
    }


def simulate_delay(pairing_id, date, delay_hours):
    """A delay pushes report/release. Does the rostered crew now bust FDP?"""
    base = build_duty_days(pairing_id, [date])[0]
    delayed = build_duty_days(pairing_id, [date], delay_hours=0)[0]
    # a delay extends the duty (release moves, report does not)
    delayed.duty_hours = round(base.duty_hours + delay_hours, 2)
    delayed.release_utc = (P(base.release_utc) + timedelta(hours=delay_hours)
                           ).strftime("%Y-%m-%dT%H:%M:%SZ")
    fdp = rule_fdp_01(delayed)
    crew = STORE.pairings[pairing_id]["crew"]
    return {
        "event": "DELAY", "pairing_id": pairing_id, "date": date,
        "delay_hours": delay_hours,
        "fdp_after_delay": delayed.duty_hours,
        "fdp_limit": fdp.numbers["max_fdp_hours"],
        "breach": not fdp.passed,
        "crew_affected": [m["crew_id"] for m in crew] if not fdp.passed else [],
        "explanation": (
            f"A {delay_hours}h delay extends the {date} duty on {pairing_id} to "
            f"{delayed.duty_hours}h against a {fdp.numbers['max_fdp_hours']}h limit "
            f"({delayed.sectors} sectors). "
            + ("The whole rostered complement busts RULE-FDP-01 — the last sector must be "
               "re-crewed, cancelled, or the duty split."
               if not fdp.passed else "Still inside the FDP limit.")),
    }


def simulate_cert_lapse(crew_id):
    """Which of this crew's future assignments are illegal on cert grounds?"""
    bad = []
    for ds, (pid, day) in sorted(STORE.roster_by_crew.get(crew_id, {}).items()):
        chk = check_assignment(crew_id, pid, [ds])
        cert = [i for i in chk["issues"] if i.startswith("RULE-CERT-06")]
        if cert:
            bad.append({"date": ds, "pairing_id": pid, "flights": day["flights"],
                        "issues": cert})
    return {
        "event": "CERT_EXPIRY", "crew_id": crew_id,
        "illegal_assignments": bad,
        "passengers_affected": STORE.pax([f for b in bad for f in b["flights"]]),
        "explanation": (f"{crew_id} holds {len(bad)} assignment(s) that fail RULE-CERT-06."
                        if bad else f"{crew_id} has no certification conflicts on roster."),
    }
