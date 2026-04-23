"""Paper-trading portfolio state — nightly update logic.

Called from run.py after top5 is chosen. Maintains a single `portfolio`
blob in Edge Config alongside the screener results.

State shape (stored under Edge Config key `portfolio`):
{
  "seed": 10000,
  "cash": 8234.56,
  "equity": 10453.21,           # cash + sum(position.mark)
  "last_updated": "2026-04-23",
  "open_positions": [ ... ],    # see position_record()
  "closed_trades": [ ... ],     # capped at 200
  "equity_curve": [ [date, equity], ... ],  # capped at 400 points (~18 months daily)
  "stats": { total_trades, wins, losses, total_pnl }
}

Trading rules (v1):
- One new position on Mondays and Thursdays.
- Highest-scored top5 name we don't already hold.
- 90 DTE call, strike = nearest $2.50 >= spot*1.02.
- Size: floor($500 / (fill_premium*100 + 0.65)), skip if <1 or cash insufficient.
- Close at expiry (DTE=0) or when mark >= 2x cost (take profit).
"""
from __future__ import annotations
from datetime import date, datetime, timedelta

from options_sim import (
    plan_entry, mark_to_market, estimate_iv, bsm_call,
    SEED_CAPITAL, SLIPPAGE_PCT, COMMISSION_PER_CONTRACT, DEFAULT_DTE_DAYS
)

TRADE_WEEKDAYS = {0, 3}         # Mon, Thu
TAKE_PROFIT_MULT = 2.0          # close when mark >= 2x cost
MAX_CLOSED_TRADES = 200
MAX_EQUITY_POINTS = 400


def _empty_portfolio(today_str: str) -> dict:
    return {
        "seed": SEED_CAPITAL,
        "cash": SEED_CAPITAL,
        "equity": SEED_CAPITAL,
        "last_updated": today_str,
        "open_positions": [],
        "closed_trades": [],
        "equity_curve": [[today_str, SEED_CAPITAL]],
        "stats": {"total_trades": 0, "wins": 0, "losses": 0, "total_pnl": 0.0},
    }


def _days_between(d1_str: str, d2_str: str) -> int:
    d1 = datetime.strptime(d1_str, "%Y-%m-%d").date()
    d2 = datetime.strptime(d2_str, "%Y-%m-%d").date()
    return (d2 - d1).days


def _current_spot(frames: dict, ticker: str):
    df = frames.get(ticker)
    if df is None:
        return None
    try:
        c = df["Close"].squeeze()
        return float(c.iloc[-1])
    except Exception:
        return None


def _mark_position(pos: dict, frames: dict, today_str: str) -> dict:
    spot = _current_spot(frames, pos["ticker"])
    if spot is None:
        # Keep last-known mark, just advance days
        spot = pos.get("current_spot", pos["spot_at_open"])
    days_elapsed = _days_between(pos["opened_on"], today_str)
    dte_remaining = max(0, pos["dte_days"] - days_elapsed)
    t_years = dte_remaining / 365.0
    mid = bsm_call(spot, pos["strike"], t_years, pos["iv"])
    exit_fill = mid * (1 - SLIPPAGE_PCT)
    mark = pos["contracts"] * (exit_fill * 100 - COMMISSION_PER_CONTRACT)
    pnl = mark - pos["cost"]
    return {
        **pos,
        "current_spot": round(spot, 2),
        "current_mid": round(mid, 4),
        "mark": round(mark, 2),
        "pnl": round(pnl, 2),
        "pnl_pct": round(pnl / pos["cost"], 4) if pos["cost"] else 0.0,
        "days_elapsed": days_elapsed,
        "dte_remaining": dte_remaining,
    }


def _close_reason(marked: dict) -> str | None:
    if marked["dte_remaining"] <= 0:
        return "expiry"
    if marked["mark"] >= marked["cost"] * TAKE_PROFIT_MULT:
        return "take_profit"
    return None


def _position_from_trade(trade, hit_type: str, iv: float) -> dict:
    return {
        "ticker": trade.ticker,
        "opened_on": trade.opened_on,
        "spot_at_open": round(trade.spot, 2),
        "strike": trade.strike,
        "dte_days": trade.dte_days,
        "iv": round(iv, 4),
        "contracts": trade.contracts,
        "cost": round(trade.cost, 2),
        "entry_premium": round(trade.fill_premium, 4),
        "hit_type": hit_type,
        "current_spot": round(trade.spot, 2),
        "current_mid": round(trade.mid_premium, 4),
        "mark": round(trade.cost * (1 - SLIPPAGE_PCT * 2), 2),
        "pnl": 0.0,
        "pnl_pct": 0.0,
        "days_elapsed": 0,
        "dte_remaining": trade.dte_days,
    }


def update_portfolio(prior: dict | None, top5: list[str], scores: dict,
                     hit_types: dict, frames: dict, today_str: str) -> dict:
    """Advance portfolio state by one day.

    - prior: existing portfolio blob from Edge Config, or None
    - top5: list of ticker strings in rank order
    - scores: {ticker: score}
    - hit_types: {ticker: 'turnaround'|'trend'}
    - frames: {ticker: yahoo df} — used for current_spot and IV estimation
    - today_str: 'YYYY-MM-DD'
    """
    p = prior if isinstance(prior, dict) and prior.get("seed") else _empty_portfolio(today_str)

    # Idempotent: if we already ran today, replay from prior day's state would
    # be complex. Simplest: skip cash-affecting work if last_updated == today,
    # but still re-mark positions so equity reflects latest prices.
    already_ran_today = (p.get("last_updated") == today_str)

    # 1) Mark every open position to today's price
    open_positions = [_mark_position(pos, frames, today_str) for pos in p["open_positions"]]

    # 2) Close anything that hit expiry or take-profit
    still_open = []
    cash = float(p["cash"])
    closed = list(p["closed_trades"])
    stats = dict(p["stats"])
    for pos in open_positions:
        reason = _close_reason(pos)
        if reason and not already_ran_today:
            cash += pos["mark"]
            closed.append({
                "ticker": pos["ticker"],
                "opened_on": pos["opened_on"],
                "closed_on": today_str,
                "cost": pos["cost"],
                "proceeds": pos["mark"],
                "pnl": pos["pnl"],
                "pnl_pct": pos["pnl_pct"],
                "close_reason": reason,
                "hit_type": pos.get("hit_type", "unknown"),
            })
            stats["total_trades"] = stats.get("total_trades", 0) + 1
            if pos["pnl"] > 0:
                stats["wins"] = stats.get("wins", 0) + 1
            else:
                stats["losses"] = stats.get("losses", 0) + 1
            stats["total_pnl"] = round(stats.get("total_pnl", 0.0) + pos["pnl"], 2)
        else:
            still_open.append(pos)

    # 3) Maybe open a new position (Mon/Thu, not already-ran-today)
    weekday = datetime.strptime(today_str, "%Y-%m-%d").weekday()
    if weekday in TRADE_WEEKDAYS and not already_ran_today:
        held = {pos["ticker"] for pos in still_open}
        for ticker in top5:
            if ticker in held:
                continue
            df = frames.get(ticker)
            if df is None:
                continue
            try:
                closes = df["Close"].squeeze().dropna().tolist()
                spot = float(closes[-1])
            except Exception:
                continue
            iv = estimate_iv(closes)
            trade = plan_entry(ticker, spot, iv, today_str)
            if trade is None or trade.cost > cash:
                continue
            cash -= trade.cost
            still_open.append(_position_from_trade(trade, hit_types.get(ticker, "unknown"), iv))
            break   # one new trade per day

    # 4) Recompute equity, curve, trim caps
    equity = round(cash + sum(pos["mark"] for pos in still_open), 2)

    curve = list(p.get("equity_curve", []))
    # Replace today's point if present, else append
    curve = [pt for pt in curve if pt[0] != today_str]
    curve.append([today_str, equity])
    curve = curve[-MAX_EQUITY_POINTS:]

    if len(closed) > MAX_CLOSED_TRADES:
        closed = closed[-MAX_CLOSED_TRADES:]

    return {
        "seed": p["seed"],
        "cash": round(cash, 2),
        "equity": equity,
        "last_updated": today_str,
        "open_positions": still_open,
        "closed_trades": closed,
        "equity_curve": curve,
        "stats": stats,
    }
