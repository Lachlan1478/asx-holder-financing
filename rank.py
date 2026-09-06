"""Build a director x company panel from the extracted tables, fit a propensity model for bank-financing events, and rank leads."""
import argparse
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from schema import FINANCING_KINDS

T = Path(__file__).parent / "data" / "tables"
TITLES = re.compile(r"\b(mr|mrs|ms|miss|dr|prof|professor|sir|dame|hon|am|ao|ac|oam|obe|cbe|mbe|qc|kc|sc|jnr|jr|snr|sr)\b\.?", re.I)
ORDINARY = re.compile(r"ord|fully paid|share|stapled|cdi|unit", re.I)
SELL, BUY = {"on_market_sell", "off_market_sell"}, {"on_market_buy", "off_market_buy"}
PLAN, PARTICIPATION = {"vesting", "exercise", "issue_or_allotment"}, {"dividend_reinvestment", "entitlement_or_placement"}
YEAR, HORIZON = pd.DateOffset(years=1), pd.DateOffset(months=12)
FEATURES = ["log_shares", "log_value", "runup", "incentive_share", "via_entity", "via_super", "via_nominee", "n_sell_24m", "n_buy_24m", "n_plan_24m",
            "n_participation_24m", "filings_24m", "tenure_years", "n_boards", "holding_rank", "n_prior", "months_since_last", "peer_prior"]


def norm(name):
    w = [x for x in TITLES.sub("", str(name).lower()).replace(",", " ").split() if x.replace("-", "").replace("'", "").isalpha()]
    return f"{w[0]} {w[-1]}" if len(w) > 1 else " ".join(w) or str(name).lower().strip()


def load():
    def read(name):
        p = T / f"{name}.csv"
        if not p.exists():
            sys.exit(f"{p} missing; run extract.py first")
        return pd.read_csv(p, parse_dates=["filing_date"])
    holdings, changes, events = read("holdings"), read("changes"), read("events")
    for df in (holdings, changes):
        df["key"] = df.symbol + "|" + df.director.map(norm)
    seen = pd.concat([holdings[["key", "symbol", "director", "filing_date", "form"]], changes[["key", "symbol", "director", "filing_date", "form"]]], ignore_index=True)
    directors = seen.groupby("key").agg(symbol=("symbol", "first"), director=("director", "first"), first_seen=("filing_date", "min"), last_seen=("filing_date", "max")).reset_index()
    directors = directors.merge(seen[seen.form == "3Z"].groupby("key").filing_date.min().rename("ceased"), on="key", how="left")
    events = events[events.kind.isin(FINANCING_KINDS) & events.confidence.isin(["high", "medium"])].copy()
    events["key"] = link(events, holdings)
    return holdings, changes, events, directors


def link(events, holdings):
    """Attach each event to a director: by director name, else by the holder entity appearing among a director's registered holders."""
    keys = []
    for e in events.itertuples():
        k = f"{e.symbol}|{norm(e.director)}" if isinstance(e.director, str) and e.director.strip() else ""
        if not k:
            h = holdings[holdings.symbol == e.symbol]
            probe = str(e.holder).lower()[:15]
            hit = h[h.director.map(norm) == norm(e.holder)] if isinstance(e.holder, str) else h.iloc[0:0]
            hit = hit if len(hit) else h[h.registered_holder.astype(str).str.lower().str.contains(re.escape(probe), na=False)] if probe else hit
            k = hit.key.iloc[0] if len(hit) else f"{e.symbol}|"
        keys.append(k)
    return keys


def snapshot(t, holdings, changes, events, directors):
    """Feature rows for every director x company active at cutoff t, using only filings on or before t."""
    d = directors[(directors.first_seen <= t) & (directors.last_seen >= t - 4 * YEAR) & ~(directors.ceased <= t)].copy()
    d["t"] = t
    h = holdings[holdings.filing_date <= t]
    h = h[h.filing_date == h.groupby("key").filing_date.transform("max")].copy()
    h["ord"] = h.security_class.astype(str).str.contains(ORDINARY)
    h["n"] = h.number_after.fillna(0)
    g = h.groupby("key")
    d = d.merge(pd.DataFrame({"shares": g.apply(lambda x: x.n[x.ord].sum()), "incentives": g.apply(lambda x: x.n[~x.ord].sum()),
                              "via_entity": g.vehicle.apply(lambda v: int(v.isin(["company", "trust"]).any())),
                              "via_super": g.vehicle.apply(lambda v: int((v == "superannuation").any())),
                              "via_nominee": g.vehicle.apply(lambda v: int((v == "nominee_or_custodian").any()))}), left_on="key", right_index=True, how="left")
    px = changes[(changes.price > 0) & changes.category.isin(SELL | BUY)].sort_values("filing_date")
    now = px[px.filing_date <= t].groupby("symbol").price.last()
    prev = px[px.filing_date <= t - YEAR].groupby("symbol").price.last()
    d["price"] = d.symbol.map(now)
    d["runup"] = (d.symbol.map(now) / d.symbol.map(prev) - 1).fillna(0).clip(-0.9, 5)
    d["value"] = d.shares * d.price
    c = changes[(changes.filing_date > t - 2 * YEAR) & (changes.filing_date <= t)]
    cg = c.groupby("key")
    d["n_sell_24m"] = d.key.map(cg.category.apply(lambda s: s.isin(SELL).sum()))
    d["n_buy_24m"] = d.key.map(cg.category.apply(lambda s: s.isin(BUY).sum()))
    d["n_plan_24m"] = d.key.map(cg.category.apply(lambda s: s.isin(PLAN).sum()))
    d["n_participation_24m"] = d.key.map(cg.category.apply(lambda s: s.isin(PARTICIPATION).sum()))
    d["filings_24m"] = d.key.map(cg.document_key.nunique())
    d["tenure_years"] = (t - d.first_seen).dt.days / 365.25
    d["n_boards"] = d.director.map(norm).map(d.assign(n=d.director.map(norm)).groupby("n").symbol.nunique())
    d["holding_rank"] = d.groupby("symbol").shares.rank(ascending=False, method="min")
    e = events[events.filing_date <= t]
    d["n_prior"] = d.key.map(e.groupby("key").size())
    last_event = e.groupby("key").filing_date.max().reindex(d.key).to_numpy()
    d["months_since_last"] = pd.Series((t.to_datetime64() - last_event) / np.timedelta64(30, "D"), index=d.index).fillna(240).clip(upper=240)
    d["peer_prior"] = d.symbol.map(e.groupby("symbol").size()).fillna(0) - d.n_prior.fillna(0)
    d = d.fillna({"shares": 0, "incentives": 0, "via_entity": 0, "via_super": 0, "via_nominee": 0, "n_sell_24m": 0, "n_buy_24m": 0, "n_plan_24m": 0,
                  "n_participation_24m": 0, "filings_24m": 0, "n_boards": 1, "n_prior": 0, "value": 0})
    d["log_shares"], d["log_value"] = np.log1p(d.shares), np.log1p(d.value)
    d["incentive_share"] = d.incentives / (d.incentives + d.shares).replace(0, np.nan)
    d["incentive_share"] = d.incentive_share.fillna(0)
    fut = events[(events.filing_date > t) & (events.filing_date <= t + HORIZON)]
    d["label"] = d.key.isin(fut.key).astype(int)
    return d


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cutoff", help="ISO date to score leads at (default today)")
    p.add_argument("--top", type=int, default=200)
    args = p.parse_args()
    holdings, changes, events, directors = load()
    last = max(holdings.filing_date.max(), changes.filing_date.max())
    cutoffs = pd.date_range(directors.first_seen.min() + YEAR, last, freq="QE")
    panel = pd.concat([snapshot(t, holdings, changes, events, directors) for t in cutoffs], ignore_index=True)
    obs = panel[panel.t + HORIZON <= last]
    split = last - HORIZON - 2 * YEAR
    train, test = obs[obs.t < split], obs[obs.t >= split]
    print(f"panel {len(panel)} rows over {len(cutoffs)} quarterly cutoffs; {len(events)} linked financing events; "
          f"train {len(train)} rows / {train.label.sum()} positives, test {len(test)} rows / {test.label.sum()} positives")
    if train.label.sum() < 5:
        sys.exit("too few financing events before the split date to fit a model; extract more filings or more history")
    model = make_pipeline(StandardScaler(), LogisticRegression(C=0.3, class_weight="balanced", max_iter=2000))
    model.fit(train[FEATURES], train.label)
    lines = [f"{f:22} {c:+.3f}" for f, c in sorted(zip(FEATURES, model[-1].coef_[0]), key=lambda x: -abs(x[1]))]
    if test.label.sum():
        s = model.predict_proba(test[FEATURES])[:, 1]
        ranked = test.assign(score=s).sort_values("score", ascending=False)
        lines = [f"test AUC {roc_auc_score(test.label, s):.3f}  average precision {average_precision_score(test.label, s):.3f}  base rate {test.label.mean():.4f}",
                 *[f"precision@{k} {ranked.label.head(k).mean():.3f}" for k in (25, 50, 100) if k <= len(ranked)], ""] + lines
    (T / "model.txt").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    t = pd.Timestamp(args.cutoff) if args.cutoff else pd.Timestamp.today().normalize()
    leads = snapshot(t, holdings, changes, events, directors)
    leads["score"] = model.predict_proba(leads[FEATURES])[:, 1]
    cols = ["symbol", "director", "score", "shares", "value", "runup", "holding_rank", "n_prior", "months_since_last", "peer_prior", "n_sell_24m", "n_buy_24m",
            "tenure_years", "n_boards", "via_entity", "via_super", "via_nominee", "incentive_share"]
    leads.sort_values("score", ascending=False).head(args.top)[cols].round(3).to_csv(T / "leads.csv", index=False)
    panel.drop(columns=["first_seen", "last_seen", "ceased"]).to_csv(T / "panel.csv", index=False)
    print(f"top {args.top} leads at {t.date()} -> data/tables/leads.csv; full panel -> data/tables/panel.csv; metrics -> data/tables/model.txt")


if __name__ == "__main__":
    main()
