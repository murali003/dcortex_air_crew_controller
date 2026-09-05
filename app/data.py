"""Deterministic data layer. Loads the dCortex dataset once, builds indexes.
No LLM touches this file. Everything here is exact."""
import json, os
from datetime import datetime, timedelta, date
from collections import defaultdict

DATA_DIR = os.environ.get("DCORTEX_DATA", "data")
SNAPSHOT = "2026-09-14T18:00:00Z"

P = lambda s: datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")
D = lambda s: datetime.strptime(s, "%Y-%m-%d").date()


def _load(name):
    with open(os.path.join(DATA_DIR, name)) as fh:
        return json.load(fh)


class Store:
    def __init__(self):
        self.flights = {f["flight_id"]: f for f in _load("flights.json")}
        self.crew = {c["crew_id"]: c for c in _load("crew.json")}
        rosters = _load("rosters.json")
        self.pairings = {p["pairing_id"]: p for p in rosters["pairings"]}
        self.flagged = rosters.get("flagged_exceptions", [])
        self.clocks = {c["crew_id"]: c for c in _load("duty_clocks.json")}
        self.reserves = {r["crew_id"]: r for r in _load("reserve_pool.json")}
        self.rules = _load("rules.json")
        self.costs = _load("costs.json")
        self.risk = {r["crew_id"]: r for r in _load("risk_signals.json")}

        self.certs = defaultdict(list)
        for c in _load("certifications.json"):
            self.certs[c["crew_id"]].append(c)

        # crew_id -> {date -> (pairing_id, day_block)}
        self.roster_by_crew = defaultdict(dict)
        # flight_id -> pairing_id
        self.pairing_of_flight = {}
        for p in self.pairings.values():
            for day in p["days"]:
                for m in p["crew"]:
                    self.roster_by_crew[m["crew_id"]][day["date"]] = (p["pairing_id"], day)
                for fid in day["flights"]:
                    self.pairing_of_flight[fid] = p["pairing_id"]

        # crew_id -> {date -> duty_hours} from history
        self.hist_duty = {}
        self.hist_flight = {}
        for cid, c in self.clocks.items():
            self.hist_duty[cid] = {d["date"]: d["duty_hours"] for d in c["daily_history"]}
            self.hist_flight[cid] = {d["date"]: d["flight_hours"] for d in c["daily_history"]}

        self.hist_end = max(next(iter(self.hist_duty.values())).keys())

    # ---------- derived helpers ----------
    def day_block(self, pairing_id, date_str):
        for d in self.pairings[pairing_id]["days"]:
            if d["date"] == date_str:
                return d
        return None

    def duty_hours(self, day):
        return round((P(day["release_utc"]) - P(day["report_utc"])).total_seconds() / 3600, 2)

    def flight_hours(self, day):
        return round(sum(self.flights[f]["block_hours"] for f in day["flights"]), 2)

    def sectors(self, day):
        return len(day["flights"])

    def dep_station(self, day):
        return self.flights[day["flights"][0]]["dep_station"]

    def pax(self, flight_ids):
        return sum(self.flights[f]["seats"] for f in flight_ids)

    def role_of(self, pairing_id, crew_id):
        for m in self.pairings[pairing_id]["crew"]:
            if m["crew_id"] == crew_id:
                return m["role"]
        return None

    def crew_in_role(self, pairing_id, role):
        return [m["crew_id"] for m in self.pairings[pairing_id]["crew"] if m["role"] == role]


STORE = Store()
