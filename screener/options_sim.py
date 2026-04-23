"""Options paper-trading simulator — v1 primitives.

Standalone so we can sanity-check pricing and entry rules before wiring
Edge Config writes or the Portfolio UI tab. Run directly to see a demo.

Design notes (v1):
- Pricer: Black-Scholes (European). S&P 500 options are American but for
  ~90d OTM calls with no dividends imminent, early-exercise premium is
  negligible — acceptable for a paper sim.
- IV: 20d realized vol (annualized) * 1.15, clamped [0.20, 0.80].
  Free, computable from the Yahoo daily bars run.py already downloads.
- Slippage: 3% of mid on entry (pay up) and 3% on exit (sell down).
- Commission: $0.65/contract each way.
- Risk-free rate: 4.5% flat. Close enough; doesn't move 90d option P&L much.

Entry rules (v1, subject to tuning after first weeks of live data):
- Fire on Mondays and Thursdays only (2 trades/week cap).
- Pick highest-scored name from top5 that we don't already hold.
- Always buy a ~90 DTE call, strike = nearest $2.50 increment >= spot * 1.02
  (slightly OTM — cheaper, more leverage, matches our 6-12mo bullish thesis).
- Contract count: floor(target_notional / (premium * 100)) where
  target_notional = $500 (5% of $10k seed). Minimum 1 contract if we can
  afford it; skip otherwise.
"""
from __future__ import annotations
import math
from dataclasses import dataclass


# ---------- Black-Scholes ----------

def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bsm_call(spot: float, strike: float, t_years: float, iv: float, r: float = 0.045) -> float:
    """Black-Scholes European call price. No dividends."""
    if t_years <= 0 or iv <= 0:
        return max(0.0, spot - strike)
    d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * t_years) / (iv * math.sqrt(t_years))
    d2 = d1 - iv * math.sqrt(t_years)
    return spot * _norm_cdf(d1) - strike * math.exp(-r * t_years) * _norm_cdf(d2)


def bsm_put(spot: float, strike: float, t_years: float, iv: float, r: float = 0.045) -> float:
    if t_years <= 0 or iv <= 0:
        return max(0.0, strike - spot)
    d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * t_years) / (iv * math.sqrt(t_years))
    d2 = d1 - iv * math.sqrt(t_years)
    return strike * math.exp(-r * t_years) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


# ---------- IV estimate from realized vol ----------

def realized_vol_annualized(closes, window: int = 20) -> float:
    """Stdev of daily log returns over `window` bars, annualized (sqrt 252).
    `closes` is an iterable of floats, oldest first."""
    import statistics
    closes = list(closes)[-(window + 1):]
    if len(closes) < window + 1:
        return 0.25
    logrets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    sd = statistics.pstdev(logrets)
    return sd * math.sqrt(252)


def estimate_iv(closes, rv_mult: float = 1.15, floor: float = 0.20, cap: float = 0.80) -> float:
    rv = realized_vol_annualized(closes)
    return max(floor, min(cap, rv * rv_mult))


# ---------- Entry sizing ----------

STRIKE_INCREMENT = 2.50
TARGET_NOTIONAL = 500.0      # per trade, of $10k seed
SEED_CAPITAL = 10_000.0
SLIPPAGE_PCT = 0.03
COMMISSION_PER_CONTRACT = 0.65
DEFAULT_DTE_DAYS = 90


def snap_strike(spot: float, otm_pct: float = 0.02, increment: float = STRIKE_INCREMENT) -> float:
    target = spot * (1 + otm_pct)
    return math.ceil(target / increment) * increment


@dataclass
class Trade:
    ticker: str
    spot: float
    strike: float
    dte_days: int
    iv: float
    mid_premium: float
    fill_premium: float       # after slippage
    contracts: int
    cost: float               # total $ out, incl commission
    opened_on: str            # ISO date


def plan_entry(ticker: str, spot: float, iv: float, opened_on: str,
               dte_days: int = DEFAULT_DTE_DAYS,
               target_notional: float = TARGET_NOTIONAL) -> Trade | None:
    strike = snap_strike(spot)
    t_years = dte_days / 365.0
    mid = bsm_call(spot, strike, t_years, iv)
    fill = mid * (1 + SLIPPAGE_PCT)
    per_contract_cost = fill * 100 + COMMISSION_PER_CONTRACT
    contracts = int(target_notional // per_contract_cost)
    if contracts < 1:
        return None
    cost = contracts * per_contract_cost
    return Trade(ticker, spot, strike, dte_days, iv, mid, fill, contracts, cost, opened_on)


def mark_to_market(trade: Trade, current_spot: float, days_elapsed: int) -> dict:
    dte_remaining = max(0, trade.dte_days - days_elapsed)
    t_years = dte_remaining / 365.0
    mid = bsm_call(current_spot, trade.strike, t_years, trade.iv)
    exit_fill = mid * (1 - SLIPPAGE_PCT)
    proceeds = trade.contracts * (exit_fill * 100 - COMMISSION_PER_CONTRACT)
    pnl = proceeds - trade.cost
    return {
        "current_spot": current_spot,
        "dte_remaining": dte_remaining,
        "mid": mid,
        "proceeds_if_closed": proceeds,
        "pnl": pnl,
        "pnl_pct": (pnl / trade.cost) if trade.cost else 0.0,
    }


# ---------- Demo ----------

if __name__ == "__main__":
    # Example: APTV at $45.23, estimated IV 38%, open today, mark 30d later at $52
    spot = 45.23
    iv = 0.38
    t = plan_entry("APTV", spot, iv, "2026-04-23")
    print(f"Entry: {t}")
    if t:
        for spot_later, days in [(45.23, 0), (48.00, 15), (52.00, 30), (40.00, 30), (60.00, 60)]:
            m = mark_to_market(t, spot_later, days)
            print(f"  day {days:3d} spot={spot_later:6.2f} -> mid={m['mid']:5.2f} "
                  f"pnl=${m['pnl']:+7.2f} ({m['pnl_pct']:+.1%})")
