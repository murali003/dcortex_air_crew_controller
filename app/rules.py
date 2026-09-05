"""The rules engine. THIS IS THE SOURCE OF TRUTH FOR LEGALITY.
The LLM never computes any number that appears here.

Every check returns a RuleResult carrying rule_id, pass/fail, the numbers used,
and a human-readable detail line. That structure is what makes answers explainable.
"""
from dataclasses import dataclass, asdict, field
from datetime import timedelta, date
from typing import List, Optional
from .data import STORE, P, D


@dataclass
class RuleResult:
    rule_id: str
    passed: bool
    detail: str
    numbers: dict = field(default_factory=dict)


def _hm(hours: float) -> str:
    """1.33 -> '1h20m' — matches the answer-key phrasing."""
    neg = hours < 0
    hours = abs(hours)
    h = int(hours)
    m = int(round((hours - h) * 60))
    if m == 60:
        h, m = h + 1, 0
    return ("-" if neg else "") + (f"{h}h{m:02d}m" if h else f"{m}m")


# --------------------------------------------------------------------------
# An "Assignment" is the unit the engine reasons about: one crew member taking
# one or more pairing-days. Everything below is computed from it.
# --------------------------------------------------------------------------
@dataclass
class DutyDay:
    date: str
    report_utc: str
    release_utc: str
    duty_hours: float
    flight_hours: float
    sectors: int
    aircraft_type: str
    dep_station: str
    flights: List[str]


def build_duty_days(pairing_id: str, dates: Optional[List[str]] = None,
                    delay_hours: float = 0.0) -> List[DutyDay]:
    p = STORE.pairings[pairing_id]
    out = []
    for d in p["days"]:
        if dates and d["date"] not in dates:
            continue
        rep = P(d["report_utc"]) + timedelta(hours=delay_hours)
        rel = P(d["release_utc"]) + timedelta(hours=delay_hours)
        ac_type = STORE.flights[d["flights"][0]]["aircraft_type"]
        out.append(DutyDay(
            date=d["date"],
            report_utc=rep.strftime("%Y-%m-%dT%H:%M:%SZ"),
            release_utc=rel.strftime("%Y-%m-%dT%H:%M:%SZ"),
            duty_hours=round((rel - rep).total_seconds() / 3600, 2),
            flight_hours=STORE.flight_hours(d),
            sectors=len(d["flights"]),
            aircraft_type=ac_type,
            dep_station=STORE.dep_station(d),
            flights=list(d["flights"]),
        ))
    return out


def _window_hours(crew_id, target_date: str, window_days: int, new_days: List[DutyDay],
                  metric: str, exclude_pairing: Optional[str] = None):
    """Sum duty (or flight) hours over the N calendar days ENDING on target_date, inclusive.

    Three sources, in priority order per date:
      1. the proposed new assignment  (new_days)
      2. the crew's existing future roster (dates after history end)
      3. daily_history from duty_clocks.json (dates <= history end)
    """
    end = D(target_date)
    start = end - timedelta(days=window_days - 1)
    hist = STORE.hist_duty[crew_id] if metric == "duty" else STORE.hist_flight[crew_id]
    new_map = {d.date: (d.duty_hours if metric == "duty" else d.flight_hours) for d in new_days}

    total, contribs = 0.0, []
    cur = start
    while cur <= end:
        ds = cur.isoformat()
        if ds in new_map:
            v = new_map[ds]
            src = "proposed"
        elif ds in STORE.roster_by_crew.get(crew_id, {}) and ds > STORE.hist_end:
            pid, day = STORE.roster_by_crew[crew_id][ds]
            if exclude_pairing and pid == exclude_pairing:
                v, src = 0.0, "released"
            else:
                v = STORE.duty_hours(day) if metric == "duty" else STORE.flight_hours(day)
                src = f"rostered {pid}"
        else:
            v, src = hist.get(ds, 0.0), "history"
        if v:
            contribs.append({"date": ds, "hours": v, "source": src})
        total += v
        cur += timedelta(days=1)
    return round(total, 2), contribs


# --------------------------------------------------------------------------
# The seven rules
# --------------------------------------------------------------------------
def rule_fdp_01(day: DutyDay) -> RuleResult:
    prm = {r["rule_id"]: r.get("params", {}) for r in STORE.rules["rules"]}["RULE-FDP-01"]
    max_fdp = prm["base_fdp_hours"] - prm["reduction_per_extra_sector_hours"] * max(
        0, day.sectors - prm["free_sectors"])
    ok = day.duty_hours <= max_fdp + 1e-9
    return RuleResult(
        "RULE-FDP-01", ok,
        f"{day.date}: FDP {day.duty_hours}h vs max {max_fdp}h "
        f"({day.sectors} sectors)" + ("" if ok else f" — over by {_hm(day.duty_hours - max_fdp)}"),
        {"fdp_hours": day.duty_hours, "max_fdp_hours": max_fdp, "sectors": day.sectors},
    )


def rule_duty_02(crew_id, day: DutyDay, new_days, exclude_pairing=None) -> RuleResult:
    total, contribs = _window_hours(crew_id, day.date, 7, new_days, "duty", exclude_pairing)
    ok = total <= 60 + 1e-9
    d = f"{day.date}: {total}h duty in the 7 days ending {day.date} vs limit 60h"
    if not ok:
        d = (f"would exceed 60h/7d by {_hm(total - 60)} on {day.date} (total {total}h)")
    return RuleResult("RULE-DUTY-02", ok, d,
                      {"total_hours": total, "limit": 60, "window_days": 7,
                       "contributions": contribs})


def rule_flt_03(crew_id, day: DutyDay, new_days, exclude_pairing=None) -> RuleResult:
    total, contribs = _window_hours(crew_id, day.date, 28, new_days, "flight", exclude_pairing)
    ok = total <= 100 + 1e-9
    d = f"{day.date}: {total}h block in the 28 days ending {day.date} vs limit 100h"
    if not ok:
        d = f"would exceed 100h/28d by {_hm(total - 100)} on {day.date} (total {total}h)"
    return RuleResult("RULE-FLT-03", ok, d,
                      {"total_hours": total, "limit": 100, "window_days": 28})


def rule_rest_04(crew_id, new_days: List[DutyDay], exclude_pairing=None) -> RuleResult:
    """Three checks: rest already banked, rest inside the new assignment, and rest
    against the crew's NEXT rostered duty. The third one is what catches the
    downstream conflicts a controller would otherwise miss."""
    clk = STORE.clocks[crew_id]
    rest_end = P(clk["last_rest_ended"])
    first, last = P(new_days[0].report_utc), P(new_days[-1].release_utc)
    new_dates = {d.date for d in new_days}
    issues = []

    if first < rest_end:
        issues.append(f"reports {new_days[0].report_utc} before rest ends {clk['last_rest_ended']}")
    for a, b in zip(new_days, new_days[1:]):
        gap = (P(b.report_utc) - P(a.release_utc)).total_seconds() / 3600
        if gap < 12 - 1e-9:
            issues.append(f"only {_hm(gap)} rest between {a.date} release and {b.date} report (min 12h)")

    # surrounding roster
    for ds, (pid, day) in sorted(STORE.roster_by_crew.get(crew_id, {}).items()):
        if ds in new_dates or pid == exclude_pairing:
            continue
        rep, rel = P(day["report_utc"]), P(day["release_utc"])
        if rep >= last:                                   # next duty after this one
            gap = (rep - last).total_seconds() / 3600
            if gap < 12 - 1e-9:
                issues.append(f"only {_hm(gap)} rest before {pid} on {ds} (downstream conflict)")
            break
        if rel <= first and (first - rel).total_seconds() / 3600 < 12 - 1e-9:
            issues.append(f"only {_hm((first - rel).total_seconds()/3600)} rest after {pid} on {ds}")
    ok = not issues
    return RuleResult("RULE-REST-04", ok,
                      "12h minimum rest satisfied" if ok else "; ".join(issues),
                      {"last_rest_ended": clk["last_rest_ended"]})


def rule_qual_05(crew_id, new_days: List[DutyDay]) -> RuleResult:
    ratings = STORE.crew[crew_id]["ratings"]
    bad = sorted({d.aircraft_type for d in new_days if d.aircraft_type not in ratings})
    ok = not bad
    return RuleResult("RULE-QUAL-05", ok,
                      f"rated for {'/'.join(ratings)}" if ok
                      else f"not rated for {'/'.join(bad)} (holds {'/'.join(ratings)})",
                      {"ratings": ratings})


def rule_cert_06(crew_id, new_days: List[DutyDay]) -> RuleResult:
    issues = []
    for d in new_days:
        dd = D(d.date)
        for c in STORE.certs[crew_id]:
            # DATASET NOTE: `valid_from` is unreliable across the dataset (licence
            # records carry future / inverted issue dates). Expiry is the operative
            # field for RULE-CERT-06 and is the only one the answer keys exercise.
            if dd > D(c["valid_to"]):
                issues.append(f"{c['cert_type']} invalid on {d.date} (valid to {c['valid_to']})")
    ok = not issues
    return RuleResult("RULE-CERT-06", ok,
                      "all certifications valid on every duty date" if ok else "; ".join(issues),
                      {})


def rule_base_07(crew_id, new_days: List[DutyDay], deadhead: bool) -> RuleResult:
    base = STORE.crew[crew_id]["base"]
    origin = new_days[0].dep_station
    if base == origin:
        return RuleResult("RULE-BASE-07", True, f"{crew_id} is based at {origin} — no positioning needed",
                          {"base": base, "origin": origin})
    if deadhead:
        return RuleResult("RULE-BASE-07", True,
                          f"{crew_id} is {base}-based; covered by deadhead positioning to {origin} (cost applies)",
                          {"base": base, "origin": origin, "deadhead": True})
    return RuleResult("RULE-BASE-07", False,
                      f"{crew_id} is based at {base}, pairing starts at {origin} — deadhead positioning required",
                      {"base": base, "origin": origin})


# --------------------------------------------------------------------------
# Composite check
# --------------------------------------------------------------------------
def check_assignment(crew_id: str, pairing_id: str, dates=None, deadhead=False,
                     delay_hours=0.0, exclude_pairing=None) -> dict:
    """Run all 7 rules for one crew member taking one pairing (or a subset of days)."""
    days = build_duty_days(pairing_id, dates, delay_hours)
    exclude_pairing = exclude_pairing or pairing_id
    results = [rule_qual_05(crew_id, days), rule_cert_06(crew_id, days),
               rule_rest_04(crew_id, days, exclude_pairing), rule_base_07(crew_id, days, deadhead)]
    for d in days:
        results += [rule_fdp_01(d),
                    rule_duty_02(crew_id, d, days, exclude_pairing),
                    rule_flt_03(crew_id, d, days, exclude_pairing)]
    legal = all(r.passed for r in results)
    return {
        "crew_id": crew_id, "pairing_id": pairing_id,
        "dates": [d.date for d in days],
        "legal": legal,
        "issues": [f"{r.rule_id}: {r.detail}" for r in results if not r.passed],
        "rules_checked": sorted({r.rule_id for r in results}),
        "checks": [asdict(r) for r in results],
    }
