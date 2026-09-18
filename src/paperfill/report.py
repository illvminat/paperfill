"""Metrics of one run, computed from the journal only.

Reading the journal rather than in-memory state means the report can be regenerated
later from the file, and that anything the report says was also recorded. Losing runs
are reported exactly like winning ones.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from paperfill.journal import Entry

ZERO = Decimal("0")
Q = Decimal("0.0001")
USDC = Decimal("0.000001")  # money is shown at USDC precision so per-token figures add up


def _d(v: Any) -> Decimal:
    return Decimal(str(v)) if v is not None else ZERO


@dataclass(slots=True)
class Metrics:
    market: str = ""
    question: str = ""
    strategy: str = ""
    mode: str = ""
    capital: Decimal = ZERO
    events: int = 0
    orders_submitted: int = 0
    orders_rejected: int = 0
    orders_cancelled: int = 0
    orders_expired: int = 0
    risk_blocks: int = 0
    fills: int = 0
    maker_fills: int = 0
    taker_fills: int = 0
    fill_rate: Decimal = ZERO  # orders that got at least one fill / orders submitted
    unrealized_pnl: Decimal = ZERO  # open positions at last mark minus their basis (unsettled runs)
    volume: Decimal = ZERO  # shares
    notional: Decimal = ZERO
    fees: Decimal = ZERO
    rebates_estimated: Decimal = ZERO
    realized_pnl: Decimal = ZERO  # after fees, before rebates
    net_pnl_with_rebates: Decimal = ZERO
    final_equity: Decimal = ZERO
    max_drawdown: Decimal = ZERO
    max_drawdown_pct: Decimal = ZERO
    both_sides: bool = False
    settled: bool = False
    halted: str | None = None
    gaps: int = 0
    per_token: dict[str, dict[str, Any]] = field(default_factory=dict)
    lean_buckets: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        out = {}
        for k in self.__slots__:
            v = getattr(self, k)
            out[k] = str(v) if isinstance(v, Decimal) else v
        return out


def compute(entries: list[Entry]) -> Metrics:
    m = Metrics()
    equity_curve: list[Decimal] = []
    token_stats: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    lean_by_order: dict[str, Decimal] = {}
    filled_orders: set[str] = set()
    last_marks: dict[str, Decimal] = {}
    lean_stats: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    payouts: dict[str, Decimal] = {}
    for e in entries:
        d = e.data
        if e.kind == "run_start":
            m.market, m.question, m.strategy, m.mode = (
                d["market"],
                d["question"],
                d["strategy"],
                d["mode"],
            )
            m.capital = _d(d["capital"])
        elif e.kind == "order_submitted":
            m.orders_submitted += 1
            lean_by_order[d["order_id"]] = _d(d.get("lean"))
        elif e.kind == "order_rejected":
            m.orders_rejected += 1
        elif e.kind == "order_cancelled":
            m.orders_cancelled += 1
        elif e.kind == "order_expired":
            m.orders_expired += 1
        elif e.kind == "risk_block":
            m.risk_blocks += 1
        elif e.kind == "gap":
            m.gaps += 1
        elif e.kind == "fill":
            m.fills += 1
            filled_orders.add(d["order_id"])
            if d["liquidity"] == "maker":
                m.maker_fills += 1
            else:
                m.taker_fills += 1
            size, price = _d(d["size"]), _d(d["price"])
            m.volume += size
            m.notional += size * price
            ts = token_stats[d["token"]]
            ts["fills"] += 1
            ts["shares"] += size if d["side"] == "BUY" else -size
            # cost basis includes the buy fee, exactly as the portfolio keeps it
            fee = _d(d["fee"])
            ts["cost"] += size * price + fee if d["side"] == "BUY" else -(size * price) + fee
            ts["fees"] += fee
            bucket = _bucket(lean_by_order.get(d["order_id"], ZERO))
            ls = lean_stats[bucket]
            ls["fills"] += 1
            ls["shares"] += size
            ls["cost"] += size * price
            ls.setdefault("token:" + d["token"], ZERO)
            ls["token:" + d["token"]] += size
        elif e.kind == "mark":
            equity_curve.append(_d(d["equity"]))
            last_marks = {k: _d(v) for k, v in (d.get("marks") or {}).items()}
        elif e.kind == "settle":
            payouts = {k: _d(v) for k, v in d["payouts"].items()}
        elif e.kind == "run_end":
            m.events = int(d["events"])
            m.fees = _d(d["fees"])
            m.rebates_estimated = _d(d["rebates_estimated"])
            m.realized_pnl = _d(d["realized_pnl"])
            m.halted = d.get("halted")
            m.settled = bool(d.get("settled"))
            m.final_equity = _d(d["cash"])
    if m.orders_submitted:
        m.fill_rate = (Decimal(len(filled_orders)) / Decimal(m.orders_submitted)).quantize(Q)
    if not m.settled:
        m.unrealized_pnl = sum(
            (s["shares"] * last_marks.get(t, ZERO) - s["cost"] for t, s in token_stats.items()),
            ZERO,
        )
    m.net_pnl_with_rebates = m.realized_pnl + m.rebates_estimated
    if equity_curve:
        peak, dd = equity_curve[0], ZERO
        for eq in equity_curve:
            peak = max(peak, eq)
            dd = max(dd, peak - eq)
        m.max_drawdown = dd
        m.max_drawdown_pct = (dd / peak * 100).quantize(Q) if peak else ZERO
        if not m.settled:
            m.final_equity = equity_curve[-1]
    filled_tokens = [t for t, s in token_stats.items() if s["fills"] > 0]
    m.both_sides = len(filled_tokens) >= 2
    for token, s in token_stats.items():
        payout = payouts.get(token)
        settled_value = (s["shares"] * payout) if payout is not None else None
        m.per_token[token] = {
            "fills": int(s["fills"]),
            "shares": str(s["shares"]),
            "cost": str(s["cost"].quantize(USDC)),
            "fees": str(s["fees"]),
            "payout": str(payout) if payout is not None else None,
            "settled_pnl": str((settled_value - s["cost"]).quantize(USDC))
            if settled_value is not None
            else None,
        }
    for bucket, s in lean_stats.items():
        won = ZERO
        for k, v in s.items():
            if k.startswith("token:") and payouts.get(k[6:]) == Decimal("1"):
                won += v
        m.lean_buckets[bucket] = {
            "fills": int(s["fills"]),
            "shares": str(s["shares"]),
            "avg_price": str((s["cost"] / s["shares"]).quantize(Q)) if s["shares"] else None,
            "win_rate": str((won / s["shares"]).quantize(Q)) if payouts and s["shares"] else None,
        }
    return m


def _bucket(lean: Decimal) -> str:
    """Conviction attached to the quote by the strategy: for the two-sided quoter the drift
    lean of the Up mid, for the fair-value quoter the edge on the quoted token."""
    if lean >= Decimal("0.25"):
        return "strong_positive"
    if lean > ZERO:
        return "positive"
    if lean == ZERO:
        return "flat"
    if lean > Decimal("-0.25"):
        return "negative"
    return "strong_negative"


def to_markdown(m: Metrics) -> str:
    pct = lambda v: f"{(v * 100).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}%"  # noqa: E731
    lines = [
        "# paperfill run report",
        "",
        f"**Market:** {m.question} (`{m.market}`)  ",
        f"**Strategy:** {m.strategy} · **Mode:** {m.mode} · **Capital:** {m.capital} USDC  ",
        f"**Events:** {m.events} · **Gaps:** {m.gaps} · **Settled:** {'yes' if m.settled else 'no'}"
        + (f" · **Halted:** {m.halted}" if m.halted else ""),
        "",
        "| Metric | Value |",
        "|---|---|",
        "| Orders submitted / rejected / cancelled / expired | "
        f"{m.orders_submitted} / {m.orders_rejected} / {m.orders_cancelled} / {m.orders_expired} |",
        f"| Risk blocks | {m.risk_blocks} |",
        f"| Fills (maker / taker) | {m.fills} ({m.maker_fills} / {m.taker_fills}) |",
        f"| Orders with at least one fill | {pct(m.fill_rate)} |",
        f"| Both sides filled | {'yes' if m.both_sides else 'no'} |",
        f"| Volume (shares) / notional (USDC) | {m.volume} / {m.notional.quantize(Q)} |",
        f"| Fees paid | {m.fees} |",
        f"| Maker rebates (estimate, upper bound) | {m.rebates_estimated} |",
        f"| Realized P&L after fees | **{m.realized_pnl.quantize(Q)}** |",
        (
            f"| Unrealized P&L at last mark (not settled) | {m.unrealized_pnl.quantize(Q)} |"
            if not m.settled
            else "| Unrealized P&L | — (settled) |"
        ),
        f"| P&L incl. rebate estimate | {m.net_pnl_with_rebates.quantize(Q)} |",
        f"| Final equity | {m.final_equity.quantize(Q)} |",
        f"| Max drawdown | {m.max_drawdown.quantize(Q)} ({m.max_drawdown_pct}%) |",
        "",
        "## Per token",
        "",
        "| Token | Fills | Shares | Cost incl. fees | Fees | Payout | Settled P&L |",
        "|---|---|---|---|---|---|---|",
    ]
    for t, s in m.per_token.items():
        lines.append(
            f"| `{t[:8]}…` | {s['fills']} | {s['shares']} | {s['cost']} | {s['fees']} "
            f"| {s['payout'] or '—'} | {s['settled_pnl'] or '—'} |"
        )
    lines += [
        "",
        "## Fills by conviction bucket (strategy-defined lean)",
        "",
        "| Bucket | Fills | Shares | Avg price | Win rate |",
        "|---|---|---|---|---|",
    ]
    for b in ("strong_positive", "positive", "flat", "negative", "strong_negative"):
        s = m.lean_buckets.get(b)
        if s:
            lines.append(
                f"| {b} | {s['fills']} | {s['shares']} | {s['avg_price']} "
                f"| {s['win_rate'] or '—'} |"
            )
    lines += [
        "",
        "Fill model is conservative (see `execution.py`). A P&L here is a property of the",
        "strategy on this tape, not a forecast. Losses are reported exactly like gains.",
        "Win rates by lean bucket come from a single market's resolution: one observation,",
        "not a measure of skill. Buy fees are part of the cost basis, so realized P&L is",
        "after all fees.",
        "",
    ]
    return "\n".join(lines)


def to_json(m: Metrics) -> str:
    return json.dumps(m.to_json(), indent=2, sort_keys=True)
