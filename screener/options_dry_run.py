"""Dry-run the options simulator over picks_log.csv.

For each Mon/Thu in the historical picks_log, pick the highest-scored fresh
name (not already open), apply plan_entry + mark_to_market at the 60d
checkpoint (proxy for ~2mo into a 90d option's life). Prints per-trade P&L
and a running equity curve vs the $10k seed.

Caveats:
- Uses a flat IV of 0.38 since picks_log doesn't carry per-ticker realized
  vol. Real sim will estimate IV from the Yahoo window in run.py.
- Closes at 60d using the realized 60d return from picks_log; in the live
  sim we'd either hold to expiry (~90d) or add a stop rule.
- Ignores position overlap limits beyond "don't double up on an open ticker".
"""
from __future__ import annotations
import csv
from datetime import date, datetime
from pathlib import Path
from options_sim import plan_entry, mark_to_market, SEED_CAPITAL

PICKS_LOG = Path(__file__).parent / "picks_log.csv"
import os
DEFAULT_IV = float(os.environ.get("IV", "0.38"))
HOLD_DAYS = 60   # close at 60d checkpoint; option still has ~30d left
TRADE_WEEKDAYS = {0, 3}   # Mon, Thu


def load_picks():
    rows = []
    with PICKS_LOG.open() as f:
        for r in csv.DictReader(f):
            try:
                r["date"] = datetime.strptime(r["date"], "%Y-%m-%d").date()
                r["score"] = float(r["score"])
                r["entry_px"] = float(r["entry_px"])
                r["r60"] = float(r["r60"]) if r["r60"] else None
            except (ValueError, KeyError):
                continue
            if r["r60"] is None:
                continue
            rows.append(r)
    return rows


def main():
    picks = load_picks()
    picks.sort(key=lambda r: (r["date"], -r["score"]))

    cash = SEED_CAPITAL
    open_positions = {}   # ticker -> (trade, open_date, target_close_date_ordinal, r60)
    closed = []

    by_date = {}
    for p in picks:
        by_date.setdefault(p["date"], []).append(p)

    all_dates = sorted(by_date.keys())
    if not all_dates:
        print("no usable picks")
        return

    for d in all_dates:
        # Close any positions that hit their 60d mark
        for tkr in list(open_positions):
            trade, open_d, close_d_ord, r60 = open_positions[tkr]
            if d.toordinal() >= close_d_ord:
                exit_spot = trade.spot * (1 + r60 / 100.0)
                m = mark_to_market(trade, exit_spot, HOLD_DAYS)
                cash += m["proceeds_if_closed"]
                closed.append({"ticker": tkr, "open": open_d, "close": d,
                               "cost": trade.cost, "proceeds": m["proceeds_if_closed"],
                               "pnl": m["pnl"], "pnl_pct": m["pnl_pct"]})
                del open_positions[tkr]

        # Only trade Mon/Thu
        if d.weekday() not in TRADE_WEEKDAYS:
            continue

        # Pick highest-score fresh name for the day
        for cand in by_date[d]:
            if cand["ticker"] in open_positions:
                continue
            trade = plan_entry(cand["ticker"], cand["entry_px"], DEFAULT_IV,
                               cand["date"].isoformat())
            if trade is None or trade.cost > cash:
                continue
            cash -= trade.cost
            open_positions[cand["ticker"]] = (
                trade, d, d.toordinal() + HOLD_DAYS, cand["r60"]
            )
            break  # one new trade per day max

    # Mark any still-open trades at their 60d checkpoint using their r60
    for tkr, (trade, open_d, close_d_ord, r60) in open_positions.items():
        exit_spot = trade.spot * (1 + r60 / 100.0)
        m = mark_to_market(trade, exit_spot, HOLD_DAYS)
        cash += m["proceeds_if_closed"]
        closed.append({"ticker": tkr, "open": open_d, "close": None,
                       "cost": trade.cost, "proceeds": m["proceeds_if_closed"],
                       "pnl": m["pnl"], "pnl_pct": m["pnl_pct"]})

    # Report
    wins = [t for t in closed if t["pnl"] > 0]
    losses = [t for t in closed if t["pnl"] <= 0]
    total_pnl = sum(t["pnl"] for t in closed)
    total_cost = sum(t["cost"] for t in closed)
    print(f"Period: {all_dates[0]} to {all_dates[-1]}")
    print(f"Trades: {len(closed)}  wins: {len(wins)}  losses: {len(losses)}  "
          f"win rate: {len(wins)/len(closed):.1%}" if closed else "no trades")
    print(f"Total capital deployed: ${total_cost:,.0f}")
    print(f"Total P&L: ${total_pnl:,.0f}")
    print(f"Final cash: ${cash:,.0f}  (seed ${SEED_CAPITAL:,.0f})")
    print(f"Return on seed: {(cash/SEED_CAPITAL - 1):+.1%}")
    if wins:
        print(f"Avg winner: {sum(t['pnl_pct'] for t in wins)/len(wins):+.1%}")
    if losses:
        print(f"Avg loser:  {sum(t['pnl_pct'] for t in losses)/len(losses):+.1%}")

    print("\nLast 10 closed trades:")
    for t in closed[-10:]:
        close_str = t["close"].isoformat() if t["close"] else "still open"
        print(f"  {t['open']} -> {close_str}  {t['ticker']:5s}  "
              f"cost=${t['cost']:6.0f}  pnl=${t['pnl']:+7.0f} ({t['pnl_pct']:+.1%})")


if __name__ == "__main__":
    main()
