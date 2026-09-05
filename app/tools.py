"""The tool layer — the boundary between the LLM and the deterministic core.

The LLM sees only these signatures. It cannot write a query, so it cannot write a
wrong one. Every function returns plain JSON-serialisable data with the numbers
already computed.
"""
from datetime import timedelta
from .data import STORE, P, D, SNAPSHOT
from .rules import check_assignment as _check, build_duty_days, _window_hours, _hm
from .recommend import rank_options as _rank, _reserve_available, _is_free
from . import simulate as sim

TODAY = "2026-09-14"


# ==========================================================================
# TIER 1 — retrieval
# ==========================================================================
def get_crew(crew_id: str):
    c = STORE.crew.get(crew_id)
    if not c:
        return {"error": f"No crew member {crew_id} in the roster."}
    clk = STORE.clocks.get(crew_id, {})
    return {
        **c,
        "duty_hours_7d": clk.get("duty_hours_7d"),
        "flight_hours_28d": clk.get("flight_hours_28d"),
        "last_rest_ended": clk.get("last_rest_ended"),
        "is_reserve": crew_id in STORE.reserves,
        "oncall_window_utc": STORE.reserves.get(crew_id, {}).get("oncall_window_utc"),
        "certifications": STORE.certs.get(crew_id, []),
        "risk": STORE.risk.get(crew_id),
        "roster": {d: p for d, (p, _) in sorted(STORE.roster_by_crew.get(crew_id, {}).items())},
    }


def find_crew(base=None, rank=None, rating=None, status="active", free_on=None):
    out = []
    for c in STORE.crew.values():
        if base and c["base"] != base:
            continue
        if rank and c["rank"] != rank:
            continue
        if rating and rating not in c["ratings"]:
            continue
        if status and c["status"] != status:
            continue
        if free_on and not _is_free(c["crew_id"], [free_on] if isinstance(free_on, str) else free_on):
            continue
        out.append({k: c[k] for k in ("crew_id", "name", "rank", "base", "ratings",
                                      "seniority", "reachability_minutes")})
    return {"count": len(out), "crew": sorted(out, key=lambda x: x["crew_id"])}


def get_flights(date=None, dep_station=None, arr_station=None, flight_no=None, aircraft=None):
    schedule_dates = [f["date"] for f in STORE.flights.values()]
    date_range = [min(schedule_dates), max(schedule_dates)]
    if date and not date_range[0] <= date <= date_range[1]:
        return {
            "answerable": False,
            "date": date,
            "schedule_date_range": date_range,
            "reason": "The flight schedule does not contain that date.",
            "count": 0,
            "total_seats": 0,
            "flights": [],
        }

    out = []
    for f in STORE.flights.values():
        if date and f["date"] != date:
            continue
        if dep_station and f["dep_station"] != dep_station:
            continue
        if arr_station and f["arr_station"] != arr_station:
            continue
        if flight_no and f["flight_no"] != flight_no:
            continue
        if aircraft and f["aircraft"] != aircraft:
            continue
        out.append({**f, "pairing_id": STORE.pairing_of_flight.get(f["flight_id"])})
    out.sort(key=lambda f: f["dep_utc"])
    return {
        "answerable": True,
        "date": date,
        "schedule_date_range": date_range,
        "count": len(out),
        "total_seats": sum(f["seats"] for f in out),
        "flights": out,
    }


def get_pairing(pairing_id=None, crew_id=None, date=None, aircraft=None):
    pids = set()
    if pairing_id:
        pids.add(pairing_id)
    if crew_id:
        pids |= {p for d, (p, _) in STORE.roster_by_crew.get(crew_id, {}).items()
                 if not date or d == date}
    if aircraft:
        pids |= {p["pairing_id"] for p in STORE.pairings.values()
                 if p["aircraft"] == aircraft and (not date or any(d["date"] == date for d in p["days"]))}
    if not pids:
        return {"error": "No pairing matched. Give a pairing_id, crew_id or aircraft."}
    out = []
    for pid in sorted(pids):
        p = STORE.pairings.get(pid)
        if not p:
            continue
        days = []
        for d in p["days"]:
            if date and d["date"] != date:
                continue
            days.append({**d, "duty_hours": STORE.duty_hours(d),
                         "flight_hours": STORE.flight_hours(d),
                         "sectors": len(d["flights"]),
                         "dep_station": STORE.dep_station(d),
                         "passengers": STORE.pax(d["flights"])})
        out.append({"pairing_id": pid, "aircraft": p["aircraft"],
                    "aircraft_type": STORE.flights[p["days"][0]["flights"][0]]["aircraft_type"],
                    "crew": p["crew"], "days": days})
    return {"pairings": out}


def get_duty_clock(crew_id: str, as_of_date: str = TODAY):
    if crew_id not in STORE.clocks:
        return {"error": f"No duty clock for {crew_id}."}
    duty, dc = _window_hours(crew_id, as_of_date, 7, [], "duty")
    flt, _ = _window_hours(crew_id, as_of_date, 28, [], "flight")
    return {
        "crew_id": crew_id, "as_of_date": as_of_date,
        "duty_hours_7d": duty, "duty_limit": 60, "duty_headroom_hours": round(60 - duty, 2),
        "flight_hours_28d": flt, "flight_limit": 100, "flight_headroom_hours": round(100 - flt, 2),
        "last_rest_ended": STORE.clocks[crew_id]["last_rest_ended"],
        "daily_contributions_7d": dc,
    }


def get_reserves(base=None, date=None, rank=None, report_utc=None):
    out = []
    for cid, r in STORE.reserves.items():
        c = STORE.crew[cid]
        if base and r["base"] != base:
            continue
        if rank and c["rank"] != rank:
            continue
        if date and date not in r["dates"]:
            continue
        row = {"crew_id": cid, "rank": c["rank"], "base": r["base"],
               "ratings": c["ratings"], "window": r["oncall_window_utc"],
               "reachability_minutes": c["reachability_minutes"]}
        if report_utc:
            row["covers_report"] = _reserve_available(cid, report_utc[:10], report_utc)
        out.append(row)
    return {"count": len(out), "reserves": sorted(out, key=lambda x: x["crew_id"])}


def list_expiring_certs(as_of_date: str = TODAY, within_days: int = 30):
    lo, hi = D(as_of_date), D(as_of_date) + timedelta(days=within_days)
    out = [{"crew_id": c["crew_id"], "cert_type": c["cert_type"], "valid_to": c["valid_to"],
            "days_left": (D(c["valid_to"]) - lo).days,
            "rank": STORE.crew[c["crew_id"]]["rank"], "base": STORE.crew[c["crew_id"]]["base"]}
           for lst in STORE.certs.values() for c in lst if lo <= D(c["valid_to"]) <= hi]
    return {"count": len(out), "certifications": sorted(out, key=lambda x: x["valid_to"])}


def get_risk_signal(crew_id: str):
    return STORE.risk.get(crew_id, {"error": f"No risk signal for {crew_id}."})


def network_summary():
    stations, nonstop = set(), {}
    for f in STORE.flights.values():
        stations |= {f["dep_station"], f["arr_station"]}
        nonstop.setdefault(f["dep_station"], set()).add(f["arr_station"])
    longest = max(f["block_hours"] for f in STORE.flights.values())
    return {
        "stations": sorted(stations),
        "nonstop_from": {k: sorted(v) for k, v in sorted(nonstop.items())},
        "fleet": sorted({(f["aircraft"], f["aircraft_type"], f["seats"])
                         for f in STORE.flights.values()}),
        "total_flights": len(STORE.flights),
        "longest_block_hours": longest,
        "longest_block_flights": sorted({f["flight_no"] for f in STORE.flights.values()
                                         if f["block_hours"] == longest}),
        "date_range": [min(f["date"] for f in STORE.flights.values()),
                       max(f["date"] for f in STORE.flights.values())],
    }


# ==========================================================================
# TIER 2 — consequence
# ==========================================================================
def check_assignment(crew_id: str, pairing_id: str, dates=None, deadhead: bool = False):
    return _check(crew_id, pairing_id, dates, deadhead)


def simulate_sick(crew_id: str, dates=None, reported_utc=None):
    return sim.simulate_sick(crew_id, dates, reported_utc)


def simulate_station_closure(station: str, date: str, start_utc: str, end_utc: str):
    return sim.simulate_station_closure(station, date, start_utc, end_utc)


def simulate_delay(pairing_id: str, date: str, delay_hours: float):
    return sim.simulate_delay(pairing_id, date, delay_hours)


def simulate_cert_lapse(crew_id: str):
    return sim.simulate_cert_lapse(crew_id)


def earliest_next_report(release_utc: str):
    t = P(release_utc) + timedelta(hours=12)
    return {"released_utc": release_utc, "min_rest_hours": 12,
            "earliest_report_utc": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "rule": "RULE-REST-04"}


def cancellation_impact(flight_ids):
    if isinstance(flight_ids, str):
        flight_ids = [flight_ids]
    known = [f for f in flight_ids if f in STORE.flights]
    missing = [f for f in flight_ids if f not in STORE.flights]
    return {"flights": known, "unknown_flights": missing,
            "passengers": STORE.pax(known),
            "cancellation_cost_inr": len(known) * STORE.costs["cancellation_per_flight"],
            "rate_per_flight_inr": STORE.costs["cancellation_per_flight"]}


def crew_above_duty_threshold(date: str, threshold_hours: float = 45.0):
    """Who is close to the 60h/7d wall on a given date, including that day's roster?"""
    out = []
    for cid in STORE.clocks:
        planned = []
        if date in STORE.roster_by_crew.get(cid, {}):
            pid, _ = STORE.roster_by_crew[cid][date]
            planned = build_duty_days(pid, [date])
        total, _ = _window_hours(cid, date, 7, planned, "duty")
        if total >= threshold_hours:
            out.append({"crew_id": cid, "rank": STORE.crew[cid]["rank"],
                        "base": STORE.crew[cid]["base"],
                        "duty_hours_7d": total, "headroom_hours": round(60 - total, 2)})
    return {"date": date, "threshold_hours": threshold_hours, "count": len(out),
            "crew": sorted(out, key=lambda x: -x["duty_hours_7d"])}


def find_reserve_candidates(pairing_id: str, role: str, date=None):
    """Reserve-only screen with an explicit reason for every exclusion.
    Answers 'why not X?' — the most common controller follow-up."""
    days = build_duty_days(pairing_id, [date] if date else None)
    report, ac_type = days[0].report_utc, days[0].aircraft_type
    origin = days[0].dep_station
    eligible, excluded = [], []
    for cid, r in STORE.reserves.items():
        c = STORE.crew[cid]
        if c["rank"] != role:
            continue
        if ac_type not in c["ratings"]:
            excluded.append({"crew_id": cid, "reason": f"RULE-QUAL-05: no {ac_type} rating"})
            continue
        if not _reserve_available(cid, days[0].date, report):
            w = r["oncall_window_utc"]
            excluded.append({"crew_id": cid,
                             "reason": f"reserve on-call window {w['start']}-{w['end']}Z does not "
                                       f"cover required report {P(report).strftime('%H:%M')}Z"})
            continue
        chk = _check(cid, pairing_id, [d.date for d in days], deadhead=c["base"] != origin)
        (eligible if chk["legal"] else excluded).append(
            {"crew_id": cid} if chk["legal"]
            else {"crew_id": cid, "reason": "; ".join(chk["issues"])})
    return {"pairing_id": pairing_id, "role": role, "required_report_utc": report,
            "aircraft_type": ac_type,
            "eligible": [e["crew_id"] for e in eligible], "excluded": excluded}


# ==========================================================================
# TIER 3 — recommendation
# ==========================================================================
def rank_options(pairing_id: str, role: str, dates=None, exclude=None, max_options: int = 10):
    return _rank(pairing_id, role, dates, tuple(exclude or ()), max_options)


def draft_notification(crew_id: str, pairing_id: str, dates=None, ack_deadline_minutes: int = 30):
    """Engine fills every fact. The LLM only polishes tone — it cannot change a time."""
    days = build_duty_days(pairing_id, dates)
    c = STORE.crew[crew_id]
    legs = []
    for i, d in enumerate(days, 1):
        first, last = STORE.flights[d.flights[0]], STORE.flights[d.flights[-1]]
        legs.append({
            "day": i, "date": d.date, "report_utc": d.report_utc,
            "report_place": f"{d.dep_station} crew room",
            "flights": [STORE.flights[f]["flight_no"] for f in d.flights],
            "release_utc": d.release_utc, "ends_at": last["arr_station"],
            "overnight": last["arr_station"] if i < len(days) else None,
        })
    return {
        "to": {"crew_id": crew_id, "name": c["name"], "rank": c["rank"],
               "reachability_minutes": c["reachability_minutes"]},
        "pairing_id": pairing_id, "days": legs,
        "hotel": {"provided": len(days) > 1, "rate_inr": STORE.costs["hotel_overnight"]},
        "ack_deadline_minutes": ack_deadline_minutes,
        "contact": "Crew Control desk, BLR — ext 4412",
        "must_include": ["crew_id", "pairing_id", "report time and place", "all flight numbers",
                         "overnight arrangement", "acknowledgement deadline", "contact"],
    }


def solve_joint_assignment(vacancies):
    """Simultaneous vacancies. Per-vacancy ranking double-books crew; this does
    min-cost matching so no one is assigned twice."""
    from .recommend_joint import solve_joint
    norm = []
    for v in vacancies:
        pid = v["pairing_id"]
        role = v.get("role") or (STORE.role_of(pid, v["crew_id"]) if v.get("crew_id") else None)
        norm.append({"pairing_id": pid, "role": role, "dates": v.get("dates"),
                     "exclude": v.get("exclude") or ([v["crew_id"]] if v.get("crew_id") else [])})
    return solve_joint(norm)


def cannot_answer(reason: str, missing: str = ""):
    """The honest exit. Preferred over a plausible guess."""
    return {"answerable": False, "reason": reason, "missing_data": missing}


# ==========================================================================
# Schemas for the Anthropic tool-use API
# ==========================================================================
_S = lambda **p: {"type": "object", "properties": p, "required": []}
STR = {"type": "string"}
NUM = {"type": "number"}
BOOL = {"type": "boolean"}
ARR = {"type": "array", "items": {"type": "string"}}

TOOL_SCHEMAS = [
    ("get_crew", "Full profile for one crew member: rank, base, ratings, duty clocks, certifications, risk score and current roster.",
     {"crew_id": STR}, ["crew_id"]),
    ("find_crew", "Filter the crew list by base, rank, aircraft rating, status, or availability on a date.",
     {"base": STR, "rank": STR, "rating": STR, "status": STR, "free_on": STR}, []),
    ("get_flights", "Schedule slice. Filter by date (YYYY-MM-DD), departure/arrival station, flight number or aircraft registration.",
     {"date": STR, "dep_station": STR, "arr_station": STR, "flight_no": STR, "aircraft": STR}, []),
    ("get_pairing", "Pairing detail: crew complement, per-day flights, report/release, duty hours, sectors, passengers. Look up by pairing_id, crew_id or aircraft.",
     {"pairing_id": STR, "crew_id": STR, "date": STR, "aircraft": STR}, []),
    ("get_duty_clock", "Duty and flight hour totals for the 7/28 calendar-day windows ENDING on as_of_date, with headroom and the per-day contributions.",
     {"crew_id": STR, "as_of_date": STR}, ["crew_id"]),
    ("get_reserves", "Reserve pool with on-call windows. Optionally test whether each window covers a required report time.",
     {"base": STR, "date": STR, "rank": STR, "report_utc": STR}, []),
    ("list_expiring_certs", "Certifications expiring within N days of a date.",
     {"as_of_date": STR, "within_days": NUM}, []),
    ("get_risk_signal", "Pre-computed disruption-risk score and its drivers for one crew member.",
     {"crew_id": STR}, ["crew_id"]),
    ("network_summary", "Stations served, nonstop city pairs, fleet, total flights, longest block time, schedule date range.",
     {}, []),
    ("check_assignment", "Run all 7 legality rules for one crew member taking one pairing. Returns legal true/false, the failing rules with exact margins, and the full per-rule trace.",
     {"crew_id": STR, "pairing_id": STR, "dates": ARR, "deadhead": BOOL}, ["crew_id", "pairing_id"]),
    ("simulate_sick", "A crew member becomes unavailable. Returns the uncovered flights, broken pairings and passengers affected.",
     {"crew_id": STR, "dates": ARR, "reported_utc": STR}, ["crew_id"]),
    ("simulate_station_closure", "A station is closed for a window. Returns every leg blocked and the pairings hit. Times are HH:MM UTC.",
     {"station": STR, "date": STR, "start_utc": STR, "end_utc": STR},
     ["station", "date", "start_utc", "end_utc"]),
    ("simulate_delay", "A delay extends a duty. Returns the FDP after delay against the sector-adjusted limit and whether it breaches.",
     {"pairing_id": STR, "date": STR, "delay_hours": NUM}, ["pairing_id", "date", "delay_hours"]),
    ("simulate_cert_lapse", "Which of a crew member's rostered assignments fail RULE-CERT-06.",
     {"crew_id": STR}, ["crew_id"]),
    ("earliest_next_report", "Given a release time, the earliest legal next report under RULE-REST-04.",
     {"release_utc": STR}, ["release_utc"]),
    ("cancellation_impact", "Passengers and direct cancellation cost for one or more flight legs.",
     {"flight_ids": ARR}, ["flight_ids"]),
    ("crew_above_duty_threshold", "Crew at or above a 7-day duty-hour threshold on a date, including that day's rostered duty.",
     {"date": STR, "threshold_hours": NUM}, ["date"]),
    ("find_reserve_candidates", "Screen the reserve pool for one role on one pairing. Returns who is eligible AND an explicit reason for every exclusion.",
     {"pairing_id": STR, "role": STR, "date": STR}, ["pairing_id", "role"]),
    ("rank_options", "Exhaustively enumerate every crew member who could fill a vacated role, check all 7 rules, cost each option, and rank legal ones by cost then delay. Also returns rejected candidates and the cancellation fallback.",
     {"pairing_id": STR, "role": STR, "dates": ARR, "exclude": ARR, "max_options": NUM},
     ["pairing_id", "role"]),
    ("draft_notification", "Structured callout facts for a crew notification: report time and place, all legs, overnight, acknowledgement deadline, contact.",
     {"crew_id": STR, "pairing_id": STR, "dates": ARR, "ack_deadline_minutes": NUM},
     ["crew_id", "pairing_id"]),
    ("solve_joint_assignment", "TWO OR MORE simultaneous vacancies. Min-cost matching so no crew member is assigned twice. Use this instead of calling rank_options repeatedly when more than one crew member is unavailable at the same time. Each vacancy needs pairing_id plus either role or the crew_id being replaced.",
     {"vacancies": {"type": "array", "items": {"type": "object", "properties": {
        "pairing_id": STR, "role": STR, "crew_id": STR, "dates": ARR}}}}, ["vacancies"]),
    ("cannot_answer", "Use when no tool can answer the question from the available data. Preferred over guessing.",
     {"reason": STR, "missing": STR}, ["reason"]),
]

TOOLS = [{"name": n, "description": d,
          "input_schema": {"type": "object", "properties": p, "required": r}}
         for n, d, p, r in TOOL_SCHEMAS]

DISPATCH = {n: globals()[n] for n, _, _, _ in TOOL_SCHEMAS}


def call_tool(name, args):
    fn = DISPATCH.get(name)
    if not fn:
        return {"error": f"Unknown tool {name}"}
    try:
        return fn(**(args or {}))
    except Exception as e:                                    # never crash the desk
        return {"error": f"{type(e).__name__}: {e}", "tool": name, "args": args}
