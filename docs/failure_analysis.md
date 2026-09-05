# Failure analysis

The brief says overstating capability scores badly. This is the case our system handles
poorly, stated plainly.

---

## The failure: station-closure recovery is detection, not planning

**Question (Q35 / scenario S3):** *"BLR closes 08:00–14:00Z on 17 Sep. Outline the
recovery plan across affected pairings."*

**What we get right.** Impact detection is exact. We identify all 13 affected legs —
DX402, DX403, DX404, DX413, DX422, DX423, DX424, DX433, DX434, DX453, DX454, DX462,
DX588 on 17 Sep — the pairings they belong to, and the passengers exposed. This matches
the answer key set for set.

**What we get wrong.** The answer key wants a *recovery plan*, per flight:

```json
{ "flight_id": "DX402-2026-09-17", "pairing_id": "P-2204",
  "min_delay_hours": 5.75, "crew_fdp_after_delay": 17.0, "fdp_limit": 12.0,
  "action": "delay exceeds crew FDP — re-crew tail legs from reserves or cancel" }
```

We produce none of the italicised fields. Specifically we do not compute:

- **`min_delay_hours`** — how far each blocked leg must slip to clear the closure window.
  This requires re-timing against the reopening time *and* the aircraft's inbound position.
- **`crew_fdp_after_delay`** — the FDP the rostered crew would run if they absorbed that
  delay. Our `simulate_delay` computes this for a *given* delay; it cannot derive the
  delay from a closure.
- **The action decision** — delay vs. re-crew the tail legs vs. cancel.

Our eval comparator for Q35 is therefore an honest subset check on flight identification,
not a plan comparison. It is marked as such in `eval/run_eval.py`. **Q35 passes on
detection and would fail on planning.** We are not claiming the planning capability.

### Why it fails

`simulate_station_closure` is a filter. It answers "which legs intersect this window"
by scanning departures and arrivals. It has no model of the aircraft rotation, so it
cannot answer "and then what."

A closure is not one disruption — it is a re-timing problem that cascades down every tail
that touches the station. DX402 slipping 5h45m pushes DX403, which pushes DX404, and each
push changes the crew's FDP, which may or may not still be legal. That is a sequential
constraint propagation, and we built a set filter.

### Why we shipped it anyway

Detection without planning is still the useful half. A controller who is told *"13 legs,
4 pairings, 1,700 passengers, here they are"* within two seconds has the thing that
actually takes ten minutes across five screens. The re-timing is what an experienced
controller does well and fast; the exhaustive impact scan is what they do slowly and
sometimes incompletely.

We would rather ship the half we can prove correct than a plan whose delay arithmetic we
have not validated against the key.

### The fix

About 120 lines, one evening:

1. Build the aircraft rotation graph — `flights` sorted by `(aircraft, dep_utc)`, already
   validated as continuous by `validate.py`.
2. For each blocked leg, `min_delay = closure_end − scheduled_dep`, then propagate:
   each downstream leg on the same tail gets `max(0, predecessor_new_arr − scheduled_dep)`
   plus minimum turn time.
3. Feed the resulting per-day duty extension into the existing `rule_fdp_01`. The rules
   engine already does this correctly — `simulate_delay` proves it on Q20/S4.
4. Where FDP breaks, call the existing `rank_options` for the tail legs.

Every piece except step 1 and 2 already exists and is verified. That is why we are
confident about the estimate and honest about not having done it.

### How the system detects its own limit

`simulate_station_closure` returns detection fields only. Asked for a recovery *plan*,
the router has no tool that produces one, so the answer is scoped to impact and the
narrator says what it covers. We do not synthesise delay figures the engine did not
compute — the grounding check in `agent.py` would suppress the narration if it tried.

---

## Second-order limitations (stated, not demoed)

**No rotation cascade in `simulate_delay`.** A delay shifts one duty uniformly. It does
not propagate to the tail's later pairings or to crew connecting onto that aircraft.
Same root cause as above — no rotation graph.

**Deadhead routes are hardcoded to DEL→BLR.** These are the only positioning legs the
dataset documents. For any other base pair, the candidate is omitted from the ranking with
a "positioning not modelled" reason rather than given an invented connection. Omitting a
legal option is a smaller error than inventing an illegal one.

**Cabin-crew complement is a count, not a rule.** We treat a missing cabin crew member as
requiring a like-for-like replacement. Real operations allow reduced-capacity dispatch —
fly with fewer cabin crew and block seats. We do not model that option, so our cost
comparison for cabin vacancies is missing a cheaper legal alternative.

**`valid_from` is ignored on certifications.** Forced by the dataset (see README finding
1). In production this would be a validation error raised at ingest, not a silent
workaround.

---

## What we would build next, in order

1. Aircraft rotation graph → closure recovery + delay cascade (the failure above).
2. Reduced-complement dispatch as a ranked option for cabin vacancies.
3. Multi-turn what-if stack — "and if C-3310 also goes sick?" applied to a working
   alternate timeline rather than the base snapshot.
4. Proactive monitoring — the morning-briefing panel already computes duty headroom and
   cert expiry; turning it into a push alert is a scheduler, not new reasoning.
