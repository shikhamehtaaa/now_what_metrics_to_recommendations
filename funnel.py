"""
Weekly funnel analysis for the "one next step" prototype (v2).

What it does:
  1. Builds a session-level funnel (view -> cart -> purchase) for each full week.
  2. Compares the latest full week with the week before.
  3. Splits the change in orders into a TRAFFIC effect (fewer/more visitors) and a
     CONVERSION effect (visitors buying more/less), so a traffic dip is never
     mistaken for a conversion problem.
  4. Builds a list of findings (traffic, each funnel step, average order value),
     each with its numbers, a significance test where one applies, and an
     estimated revenue impact.
  5. Keeps only findings that are material (big enough to matter), and ranks them.
     If nothing is material, it says so: "nothing needs fixing this week" is a valid answer.
  6. For each top finding, shows which brands / price bands drove it.
  7. Writes evidence.json: the only numbers a recommendation is allowed to use.

Expected columns (REES46 e-commerce events schema):
  event_time, event_type (view / cart / remove_from_cart / purchase),
  user_session, price, brand
To use other event data, edit the CONFIG block only.

Usage:
  python funnel.py path/to/events.csv            # full file
  python funnel.py path/to/events.csv 500000     # first 500k rows (quick test)
"""
import json
import math
import sys

import pandas as pd

# ------------------------------ CONFIG ------------------------------
FUNNEL = ["view", "cart", "purchase"]  # ordered steps, counted per session
COLS = {  # internal name -> column name in your file
    "time": "event_time",
    "type": "event_type",
    "session": "user_session",
    "price": "price",
    "brand": "brand",
}
SEGMENT_DIMS = ["brand", "price_band"]  # ways to slice a finding
MIN_SEGMENT_SESSIONS = 300     # ignore tiny segments
TOP_SEGMENTS_TO_CHECK = 50     # only look at the busiest segments (keeps it fast)
ALPHA = 0.05                   # significance threshold for conversion changes
MATERIAL_REVENUE_SHARE = 0.01  # a finding must move >= 1% of last week's revenue
TOP_FINDINGS = 3
OUT_FILE = "evidence.json"
# --------------------------------------------------------------------


def load_events(path, nrows=None):
    df = pd.read_csv(path, usecols=list(COLS.values()), nrows=nrows)
    df = df.rename(columns={v: k for k, v in COLS.items()})
    df["time"] = pd.to_datetime(
        df["time"].astype(str).str.replace(" UTC", "", regex=False)
    )
    df["brand_missing"] = df["brand"].isna()
    df["brand"] = df["brand"].fillna("unknown").astype("category")
    labels = ["price Q1 (lowest)", "price Q2", "price Q3", "price Q4 (highest)"]
    df["price_band"] = pd.qcut(df["price"].rank(method="first"), 4, labels=labels)
    df["week"] = df["time"].dt.to_period("W").dt.start_time  # weeks start on Monday
    df["day"] = df["time"].dt.normalize()
    return df


def complete_weeks(df):
    """Weeks with all 7 days present, so a partial week never looks like a drop."""
    days = df.groupby("week")["day"].nunique()
    return sorted(days[days == 7].index)


def flag_counts(df, by):
    """Sessions reaching each funnel step, nested so a purchase implies a cart
    implies a view. `by` is a list of grouping columns, e.g. ["week"]."""
    ev = df[df["type"].isin(FUNNEL)]
    seen = (
        ev.groupby(by + ["session", "type"], observed=True)
        .size()
        .unstack("type", fill_value=0)
        > 0
    )
    seen = seen.reindex(columns=FUNNEL, fill_value=False)
    for prev_step, step in zip(FUNNEL[:-1], FUNNEL[1:]):
        seen[step] = seen[step] & seen[prev_step]
    return seen.groupby(level=by, observed=True).sum()


def two_prop_p(k1, n1, k2, n2):
    """Two-sided p-value for 'did the conversion rate change?' (pooled z-test)."""
    if min(n1, n2) == 0:
        return 1.0
    p = (k1 + k2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    if se == 0:
        return 1.0
    z = (k2 / n2 - k1 / n1) / se
    return math.erfc(abs(z) / math.sqrt(2))


def build_findings(counts, revenue, prev, curr):
    top, last = FUNNEL[0], FUNNEL[-1]
    v0, v1 = counts.loc[prev, top], counts.loc[curr, top]
    o0, o1 = counts.loc[prev, last], counts.loc[curr, last]
    c0, c1 = o0 / v0, o1 / v1
    rev0, rev1 = revenue.get(prev, 0.0), revenue.get(curr, 0.0)
    aov0 = rev0 / o0 if o0 else 0.0
    aov1 = rev1 / o1 if o1 else 0.0

    traffic_orders = (v1 - v0) * c0        # orders change explained by visitor count
    conversion_orders = v1 * (c1 - c0)     # orders change explained by buying rate
    aov_revenue = o1 * (aov1 - aov0)       # revenue change explained by order value

    findings = [{
        "id": "traffic",
        "title": f"Sessions with a {top}",
        "prev": float(v0), "curr": float(v1),
        "relative_change": float((v1 - v0) / v0),
        "p_value": None,  # no baseline to test against; judged on size only
        "orders_impact": float(traffic_orders),
        "revenue_impact": float(traffic_orders * aov0),
    }]
    for a, b in zip(FUNNEL[:-1], FUNNEL[1:]):
        n0, k0 = counts.loc[prev, a], counts.loc[prev, b]
        n1, k1 = counts.loc[curr, a], counts.loc[curr, b]
        if n0 == 0 or n1 == 0 or k0 == 0:
            continue
        r0, r1 = k0 / n0, k1 / n1
        # orders change if ONLY this step's rate had changed (others held at last week's)
        step_orders = v1 * c0 * (r1 / r0 - 1)
        findings.append({
            "id": f"{a}_to_{b}",
            "title": f"{a} -> {b} conversion",
            "from": a, "to": b,
            "prev": float(r0), "curr": float(r1),
            "relative_change": float((r1 - r0) / r0),
            "sessions_prev": int(n0), "sessions_curr": int(n1),
            "p_value": float(two_prop_p(k0, n0, k1, n1)),
            "orders_impact": float(step_orders),
            "revenue_impact": float(step_orders * aov0),
        })
    findings.append({
        "id": "average_order_value",
        "title": "Average order value",
        "prev": float(aov0), "curr": float(aov1),
        "relative_change": float((aov1 - aov0) / aov0) if aov0 else 0.0,
        "p_value": None,
        "orders_impact": 0.0,
        "revenue_impact": float(aov_revenue),
    })

    for f in findings:
        tested = f["p_value"] is not None
        f["is_significant"] = bool(f["p_value"] < ALPHA) if tested else None
        big = abs(f["revenue_impact"]) >= MATERIAL_REVENUE_SHARE * rev0
        f["is_material"] = bool(big and (f["is_significant"] is not False))

    headline = {
        "orders_prev": int(o0), "orders_curr": int(o1),
        "orders_change": int(o1 - o0),
        "revenue_prev": float(rev0), "revenue_curr": float(rev1),
        "overall_conversion_prev": float(c0), "overall_conversion_curr": float(c1),
        "orders_change_from_traffic": float(traffic_orders),
        "orders_change_from_conversion": float(conversion_orders),
        "conversion_effect_is_material": bool(
            abs(conversion_orders * aov0) >= MATERIAL_REVENUE_SHARE * rev0
        ),
    }
    return findings, headline


def segment_drivers(df, prev, curr, finding, top=3):
    """For one finding, which segments moved most (in the same direction)?"""
    weekly = df[df["week"].isin([prev, curr])]
    a = finding.get("from", FUNNEL[0])
    b = finding.get("to")
    out = {}
    for dim in SEGMENT_DIMS:
        keep = list(weekly[dim].value_counts().head(TOP_SEGMENTS_TO_CHECK).index)
        sub = weekly[weekly[dim].isin(keep)]
        seg = flag_counts(sub, ["week", dim])
        rows = []
        for s in keep:
            try:
                n_p, n_c = seg.loc[(prev, s), a], seg.loc[(curr, s), a]
                if b:
                    k_p, k_c = seg.loc[(prev, s), b], seg.loc[(curr, s), b]
            except KeyError:
                continue
            if min(n_p, n_c) < MIN_SEGMENT_SESSIONS:
                continue
            if b:
                r_p, r_c = k_p / n_p, k_c / n_c
                rows.append({
                    "segment": str(s), "sessions_curr": int(n_c),
                    "rate_prev": float(r_p), "rate_curr": float(r_c),
                    "effect": float((r_c - r_p) * n_c),  # completions gained/lost
                })
            else:
                rows.append({
                    "segment": str(s), "sessions_prev": int(n_p),
                    "sessions_curr": int(n_c), "effect": float(n_c - n_p),
                })
        rows.sort(key=lambda r: r["effect"], reverse=finding["orders_impact"] > 0)
        out[dim] = rows[:top]
    return out


def analyze(path, nrows=None):
    df = load_events(path, nrows)
    weeks = complete_weeks(df)
    if len(weeks) < 2:
        raise SystemExit("Need at least two complete weeks of data.")
    prev, curr = weeks[-2], weeks[-1]

    counts = flag_counts(df, ["week"])
    revenue = df[df["type"] == FUNNEL[-1]].groupby("week")["price"].sum()
    findings, headline = build_findings(counts, revenue, prev, curr)

    material = [f for f in findings if f["is_material"]]
    material.sort(key=lambda f: abs(f["revenue_impact"]), reverse=True)
    top = material[:TOP_FINDINGS]
    for f in top:
        if f["id"] != "average_order_value":
            f["segments"] = segment_drivers(df, prev, curr, f)

    missing = float(df["brand_missing"].mean())
    notes = [
        f"Brand is missing for {missing:.0%} of events; those are grouped as 'unknown'.",
        "Segments overlap (one session can touch several brands), so segment numbers do not add up to totals.",
        "Revenue is the sum of item prices on purchase events, not order totals.",
        f"Only {len(weeks)} complete weeks of data; week-to-week changes can reflect normal variation.",
        "The dataset has no traffic-source, device, or promotion data, so causes outside these columns cannot be identified.",
    ]
    return {
        "weeks": {"previous": str(prev.date()), "current": str(curr.date())},
        "headline": headline,
        "findings": findings,
        "top_findings": top,
        "nothing_material": len(top) == 0,
        "data_notes": notes,
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else None
    result = analyze(sys.argv[1], limit)
    with open(OUT_FILE, "w") as f:
        json.dump(result, f, indent=2)
    print(json.dumps(result, indent=2))
    print(f"\nSaved to {OUT_FILE}")