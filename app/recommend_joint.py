"""Joint assignment across simultaneous vacancies.

WHY THIS FILE EXISTS
--------------------
`recommend.rank_options` optimises ONE vacancy at a time. When two vacancies open at
once (scenario S6: both A320 captains sick at 00:30Z on 18 Sep) the greedy result names
the same rank-1 reserve twice. That plan is infeasible — one person cannot fly two
aircraft.

This is a min-cost bipartite matching problem, not a ranking problem.

Candidate pools here are ~20 crew over ~3 vacancies, so exhaustive assignment is both
optimal and instant. No solver dependency, and — importantly for a Crew Control desk —
the result stays auditable: you can point at every rejected combination.
"""
from itertools import permutations
from .recommend import rank_options


def solve_joint(vacancies, top_n=8):
    """vacancies: [{"pairing_id":..., "role":..., "dates":[...]?, "exclude":[...]?}, ...]

    Returns the cheapest feasible set of assignments with no crew member used twice,
    plus the greedy plan for comparison so a controller can see what changed and why.
    """
    pools = []
    for v in vacancies:
        opts = rank_options(v["pairing_id"], v["role"], v.get("dates"),
                            tuple(v.get("exclude") or ()), max_options=top_n)["options"]
        if not opts:
            return {"feasible": False,
                    "reason": f"No legal candidate for {v['role']} on {v['pairing_id']}",
                    "vacancy": v}
        pools.append(opts)

    greedy = [p[0] for p in pools]
    greedy_conflict = len({o["crew_id"] for o in greedy}) < len(greedy)

    best, best_cost = None, float("inf")
    # exhaustive over the cross product, pruned by the no-double-booking constraint
    def search(i, chosen, used, cost):
        nonlocal best, best_cost
        if cost >= best_cost:
            return
        if i == len(pools):
            best, best_cost = list(chosen), cost
            return
        for o in pools[i]:
            if o["crew_id"] in used:
                continue
            chosen.append(o)
            search(i + 1, chosen, used | {o["crew_id"]}, cost + o["cost_inr"])
            chosen.pop()

    search(0, [], set(), 0)

    if best is None:
        return {"feasible": False,
                "reason": "No conflict-free combination inside the candidate pools",
                "greedy_plan": greedy}

    return {
        "feasible": True,
        "total_cost_inr": best_cost,
        "assignments": [
            {"pairing_id": v["pairing_id"], "role": v["role"], **o}
            for v, o in zip(vacancies, best)
        ],
        "greedy_would_have": [o["crew_id"] for o in greedy],
        "greedy_was_infeasible": greedy_conflict,
        "explanation": (
            ("Greedy per-vacancy ranking named "
             f"{greedy[0]['crew_id']} for both pairings — one crew member cannot cover two "
             "aircraft. Joint matching reassigns to the cheapest conflict-free pair."
             if greedy_conflict else
             "No candidate collision; the joint optimum equals the per-vacancy optimum.")
            + f" Total ₹{best_cost:,}."),
    }


if __name__ == "__main__":
    import json
    from .data import STORE
    ev = [{"crew_id": "C-3940", "pairing_id": "P-2205"},
          {"crew_id": "C-1938", "pairing_id": "P-2212"}]
    vac = [{"pairing_id": e["pairing_id"],
            "role": STORE.role_of(e["pairing_id"], e["crew_id"]),
            "exclude": [e["crew_id"]]} for e in ev]
    print(json.dumps(solve_joint(vac), indent=1))
