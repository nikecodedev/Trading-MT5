from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import json

import MetaTrader5 as mt5
import pandas as pd

try:
    from .config import CandleFlipScalpConfig
    from .timeframe import timeframe_from_code
except ImportError:  # pragma: no cover - script execution
    from config import CandleFlipScalpConfig
    from timeframe import timeframe_from_code


@dataclass
class BacktestTrade:
    direction: int  # 1 for long, -1 for short
    volume: float
    entry_time: datetime
    entry_price: float
    exit_time: datetime
    exit_price: float
    pnl_points: float
    pnl_currency: float
    exit_reason: str


@dataclass
class BacktestResult:
    symbol: str
    timeframe: int
    start: datetime
    end: datetime
    initial_balance: float
    final_balance: float
    net_profit: float
    net_profit_pct: float
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    profit_factor: Optional[float]
    max_drawdown: float
    max_drawdown_pct: float
    trades: List[BacktestTrade] = field(default_factory=list)
    equity_curve: List[Tuple[datetime, float]] = field(default_factory=list)
    bars: List[Dict[str, Any]] = field(default_factory=list)


def _ensure_mt5_initialized(config: CandleFlipScalpConfig) -> None:
    if not mt5.initialize():
        raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")

    if config.login is not None:
        authorized = mt5.login(
            config.login,
            password=config.password,
            server=config.server,
        )
        if not authorized:
            raise RuntimeError(f"MT5 login failed: {mt5.last_error()}")

    if not mt5.symbol_select(config.symbol, True):
        raise RuntimeError(f"Failed to select symbol {config.symbol}")


def _calc_volume(
    config: CandleFlipScalpConfig,
    symbol_info,
    point: float,
    balance: float,
) -> float:
    if not config.use_risk_percent or config.sl_points <= 0:
        return config.fixed_lots

    tick_value = getattr(symbol_info, "trade_tick_value", 0.0)
    tick_size = getattr(symbol_info, "trade_tick_size", 0.0)
    if tick_value <= 0 or tick_size <= 0 or point <= 0:
        return config.fixed_lots

    risk_money = balance * (config.risk_percent / 100.0)
    value_per_point = (tick_value / tick_size) * point
    if value_per_point <= 0:
        return config.fixed_lots

    raw_lots = risk_money / (config.sl_points * value_per_point)

    volume_step = symbol_info.volume_step or 0.01
    volume_min = symbol_info.volume_min or volume_step
    volume_max = symbol_info.volume_max or raw_lots

    steps = math.floor(raw_lots / volume_step) if volume_step > 0 else raw_lots
    lots = steps * volume_step if volume_step > 0 else raw_lots

    if lots < volume_min:
        lots = volume_min
    if lots > volume_max:
        lots = volume_max

    return round(lots, 8)


def _max_drawdown(equity: List[Tuple[datetime, float]]) -> float:
    """Calculate maximum drawdown in absolute currency."""
    peak = None
    max_dd = 0.0
    for _, value in equity:
        if peak is None or value > peak:
            peak = value
        if peak is None:
            continue
        drawdown = peak - value
        if drawdown > max_dd:
            max_dd = drawdown
    return max_dd


def _max_drawdown_pct(equity: List[Tuple[datetime, float]]) -> float:
    """Calculate maximum drawdown as percentage."""
    peak = None
    max_dd_pct = 0.0
    for _, value in equity:
        if peak is None or value > peak:
            peak = value
        if peak is None or peak == 0:
            continue
        drawdown_pct = ((peak - value) / peak) * 100
        if drawdown_pct > max_dd_pct:
            max_dd_pct = drawdown_pct
    return max_dd_pct


def _profit_factor(trades: List[BacktestTrade]) -> Optional[float]:
    gross_win = sum(t.pnl_currency for t in trades if t.pnl_currency > 0)
    gross_loss = sum(-t.pnl_currency for t in trades if t.pnl_currency < 0)
    if gross_loss == 0:
        return None
    return gross_win / gross_loss if gross_loss > 0 else None


def run_backtest(
    config: CandleFlipScalpConfig,
    months: int = 6,
    initial_balance: float = 10_000.0,
    realistic_fills: bool = True,
    show_progress: bool = True,
) -> BacktestResult:
    """Run backtest simulation.

    Args:
        config: Strategy configuration
        months: Number of months to backtest
        initial_balance: Starting account balance
        realistic_fills: If True, simulate bid/ask spread and slippage
        show_progress: If True, print progress updates
    """
    _ensure_mt5_initialized(config)
    try:
        symbol_info = mt5.symbol_info(config.symbol)
        if symbol_info is None:
            raise RuntimeError(f"Unable to retrieve symbol info for {config.symbol}")

        point = symbol_info.point or 0.0
        if point <= 0:
            raise RuntimeError("Invalid point size from symbol info")

        end = datetime.now(timezone.utc)
        start = end - timedelta(days=30 * months)
        rates = mt5.copy_rates_range(config.symbol, config.timeframe, start, end)
        if rates is None or len(rates) == 0:
            raise RuntimeError("No historical data returned for requested range")

        total_bars = len(rates)
        if show_progress:
            print(f"[backtest] Processing {total_bars} bars from {start:%Y-%m-%d} to {end:%Y-%m-%d}")

        balance = initial_balance
        trades: List[BacktestTrade] = []
        equity_curve: List[Tuple[datetime, float]] = []
        bars: List[Dict[str, Any]] = []
        position = None  # dict with keys: direction, volume, entry_price, entry_time
        last_trade_time = None

        tick_value = getattr(symbol_info, "trade_tick_value", 0.0)
        tick_size = getattr(symbol_info, "trade_tick_size", 0.0)
        value_per_point = (tick_value / tick_size) * point if tick_size > 0 else 1.0
        if value_per_point <= 0:
            value_per_point = 1.0

        # Estimate average spread from symbol info or use config
        typical_spread = getattr(symbol_info, "spread", 0) * point
        if typical_spread <= 0:
            typical_spread = config.max_spread_points * point * 0.5  # Use half of max as typical

        progress_interval = max(1, total_bars // 20)  # Update every 5%

        for idx, bar in enumerate(rates):
            if show_progress and idx % progress_interval == 0:
                progress_pct = (idx / total_bars) * 100
                print(f"[backtest] Progress: {progress_pct:.0f}% ({idx}/{total_bars})", end="\r")
            bar_time = datetime.fromtimestamp(bar["time"], tz=timezone.utc)
            open_price = float(bar["open"])
            high = float(bar["high"])
            low = float(bar["low"])
            close = float(bar["close"])

            bar_record: Dict[str, Any] = {
                "time": bar_time,
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "signal": None,
                "position_before": position["direction"] if position else 0,
                "position_after": position["direction"] if position else 0,
                "events": [],
            }

            flips_this_bar = 0
            threshold = config.hysteresis_points * point
            long_trigger_price = open_price + threshold
            short_trigger_price = open_price - threshold

            # Manage existing position SL/TP before new signals
            if position is not None:
                exit_reason = None
                exit_price = None
                if position["direction"] == 1:
                    sl_price = position["entry_price"] - config.sl_points * point
                    tp_price = position["entry_price"] + config.tp_points * point
                    hit_sl = low <= sl_price
                    hit_tp = high >= tp_price
                    if hit_sl or hit_tp:
                        exit_price = sl_price if hit_sl else tp_price
                        exit_reason = "stop-loss" if hit_sl else "take-profit"
                        # conservative assumption: if both hit, stop-loss first
                        if hit_sl and hit_tp:
                            exit_price = sl_price
                            exit_reason = "stop-loss"
                else:
                    sl_price = position["entry_price"] + config.sl_points * point
                    tp_price = position["entry_price"] - config.tp_points * point
                    hit_sl = high >= sl_price
                    hit_tp = low <= tp_price
                    if hit_sl or hit_tp:
                        exit_price = sl_price if hit_sl else tp_price
                        exit_reason = "stop-loss" if hit_sl else "take-profit"
                        if hit_sl and hit_tp:
                            exit_price = sl_price
                            exit_reason = "stop-loss"

                if exit_price is not None:
                    pnl_points = (exit_price - position["entry_price"]) / point * position["direction"]
                    pnl_currency = pnl_points * value_per_point * position["volume"]
                    balance += pnl_currency
                    trades.append(
                        BacktestTrade(
                            direction=position["direction"],
                            volume=position["volume"],
                            entry_time=position["entry_time"],
                            entry_price=position["entry_price"],
                            exit_time=bar_time,
                            exit_price=exit_price,
                            pnl_points=pnl_points,
                            pnl_currency=pnl_currency,
                            exit_reason=exit_reason or "target",
                        )
                    )
                    bar_record["events"].append(
                        {
                            "type": "exit",
                            "time": bar_time,
                            "direction": "long" if position["direction"] == 1 else "short",
                            "price": exit_price,
                            "pnl": pnl_currency,
                            "reason": exit_reason or "target",
                        }
                    )
                    position = None
                    last_trade_time = bar_time
                    bar_record["position_after"] = 0

            want_long = high >= long_trigger_price
            want_short = low <= short_trigger_price

            if want_long and want_short:
                # If both thresholds touched, decide based on which side closed nearer
                if close >= open_price:
                    want_short = False
                else:
                    want_long = False

            # Simulate bid/ask spread for realistic fills
            if realistic_fills:
                mid_price = (high + low) / 2.0
                simulated_ask = mid_price + (typical_spread / 2.0)
                simulated_bid = mid_price - (typical_spread / 2.0)
                spread_points = (simulated_ask - simulated_bid) / point
            else:
                spread_points = 0.0
                simulated_ask = close
                simulated_bid = close

            # Check spread filter (like live bot)
            spread_too_wide = realistic_fills and spread_points > config.max_spread_points

            # Check cooldown (like live bot)
            cooldown_active = False
            if last_trade_time is not None and config.cooldown_seconds > 0:
                # Convert bar time to seconds for cooldown check
                time_since_last = (bar_time - last_trade_time).total_seconds()
                cooldown_active = time_since_last < config.cooldown_seconds

            # Evaluate trade signals (flip logic) with filters
            if want_long and flips_this_bar < config.max_flips_per_bar and not spread_too_wide and not cooldown_active:
                if position is not None and position["direction"] == -1:
                    # Close short position - use simulated bid (worse price for closing short)
                    exit_price = simulated_bid if realistic_fills else long_trigger_price
                    pnl_points = (exit_price - position["entry_price"]) / point * position["direction"]
                    pnl_currency = pnl_points * value_per_point * position["volume"]
                    balance += pnl_currency
                    trades.append(
                        BacktestTrade(
                            direction=position["direction"],
                            volume=position["volume"],
                            entry_time=position["entry_time"],
                            entry_price=position["entry_price"],
                            exit_time=bar_time,
                            exit_price=exit_price,
                            pnl_points=pnl_points,
                            pnl_currency=pnl_currency,
                            exit_reason="flip",
                        )
                    )
                    bar_record["events"].append(
                        {
                            "type": "exit",
                            "time": bar_time,
                            "direction": "short",
                            "price": exit_price,
                            "pnl": pnl_currency,
                            "reason": "flip",
                        }
                    )
                    position = None
                    flips_this_bar += 1
                    last_trade_time = bar_time
                    bar_record["position_after"] = 0

                if position is None and flips_this_bar < config.max_flips_per_bar:
                    volume = _calc_volume(config, symbol_info, point, balance)
                    # Open long position - use simulated ask (worse price for buying)
                    entry_price = simulated_ask if realistic_fills else long_trigger_price
                    position = {
                        "direction": 1,
                        "volume": volume,
                        "entry_time": bar_time,
                        "entry_price": entry_price,
                    }
                    flips_this_bar += 1
                    last_trade_time = bar_time
                    bar_record["events"].append(
                        {
                            "type": "entry",
                            "time": bar_time,
                            "direction": "long",
                            "price": entry_price,
                            "volume": volume,
                        }
                    )
                    bar_record["position_after"] = 1

            elif want_short and flips_this_bar < config.max_flips_per_bar and not spread_too_wide and not cooldown_active:
                if position is not None and position["direction"] == 1:
                    # Close long position - use simulated ask (worse price for closing long)
                    exit_price = simulated_ask if realistic_fills else short_trigger_price
                    pnl_points = (exit_price - position["entry_price"]) / point * position["direction"]
                    pnl_currency = pnl_points * value_per_point * position["volume"]
                    balance += pnl_currency
                    trades.append(
                        BacktestTrade(
                            direction=position["direction"],
                            volume=position["volume"],
                            entry_time=position["entry_time"],
                            entry_price=position["entry_price"],
                            exit_time=bar_time,
                            exit_price=exit_price,
                            pnl_points=pnl_points,
                            pnl_currency=pnl_currency,
                            exit_reason="flip",
                        )
                    )
                    bar_record["events"].append(
                        {
                            "type": "exit",
                            "time": bar_time,
                            "direction": "long",
                            "price": exit_price,
                            "pnl": pnl_currency,
                            "reason": "flip",
                        }
                    )
                    position = None
                    flips_this_bar += 1
                    last_trade_time = bar_time
                    bar_record["position_after"] = 0

                if position is None and flips_this_bar < config.max_flips_per_bar:
                    volume = _calc_volume(config, symbol_info, point, balance)
                    # Open short position - use simulated bid (worse price for selling)
                    entry_price = simulated_bid if realistic_fills else short_trigger_price
                    position = {
                        "direction": -1,
                        "volume": volume,
                        "entry_time": bar_time,
                        "entry_price": entry_price,
                    }
                    flips_this_bar += 1
                    last_trade_time = bar_time
                    bar_record["events"].append(
                        {
                            "type": "entry",
                            "time": bar_time,
                            "direction": "short",
                            "price": entry_price,
                            "volume": volume,
                        }
                    )
                    bar_record["position_after"] = -1

            equity_curve.append((bar_time, balance))
            if want_long and not want_short:
                bar_record["signal"] = "long"
            elif want_short and not want_long:
                bar_record["signal"] = "short"
            elif want_long and want_short:
                bar_record["signal"] = "both"
            else:
                bar_record["signal"] = "idle"

            if position is not None:
                bar_record["position_after"] = position["direction"]
            bars.append(bar_record)

        # Close any open position at final close price
        if position is not None:
            exit_price = float(rates[-1]["close"])
            pnl_points = (exit_price - position["entry_price"]) / point * position["direction"]
            pnl_currency = pnl_points * value_per_point * position["volume"]
            balance += pnl_currency
            trades.append(
                BacktestTrade(
                    direction=position["direction"],
                    volume=position["volume"],
                    entry_time=position["entry_time"],
                    entry_price=position["entry_price"],
                    exit_time=datetime.fromtimestamp(rates[-1]["time"], tz=timezone.utc),
                    exit_price=exit_price,
                    pnl_points=pnl_points,
                    pnl_currency=pnl_currency,
                    exit_reason="final-close",
                )
            )
            if bars:
                bars[-1].setdefault("events", []).append(
                    {
                        "type": "exit",
                        "time": bars[-1]["time"],
                        "direction": "long" if position["direction"] == 1 else "short",
                        "price": exit_price,
                        "pnl": pnl_currency,
                        "reason": "final-close",
                    }
                )
                bars[-1]["position_after"] = 0

        if show_progress:
            print()  # New line after progress indicator

        total_trades = len(trades)
        wins = sum(1 for t in trades if t.pnl_currency > 0)
        losses = sum(1 for t in trades if t.pnl_currency < 0)
        win_rate = (wins / total_trades) * 100 if total_trades > 0 else 0.0
        net_profit = balance - initial_balance
        net_profit_pct = (net_profit / initial_balance) * 100 if initial_balance != 0 else 0.0
        pf = _profit_factor(trades)
        max_dd = _max_drawdown(equity_curve)
        max_dd_pct = _max_drawdown_pct(equity_curve)

        if show_progress:
            print(f"[backtest] Completed: {total_trades} trades, {wins} wins, {losses} losses")
            print(f"[backtest] Net profit: {net_profit:,.2f} ({net_profit_pct:.2f}%), Max DD: {max_dd:,.2f} ({max_dd_pct:.2f}%)")

        return BacktestResult(
            symbol=config.symbol,
            timeframe=config.timeframe,
            start=datetime.fromtimestamp(rates[0]["time"], tz=timezone.utc),
            end=datetime.fromtimestamp(rates[-1]["time"], tz=timezone.utc),
            initial_balance=initial_balance,
            final_balance=balance,
            net_profit=net_profit,
            net_profit_pct=net_profit_pct,
            total_trades=total_trades,
            wins=wins,
            losses=losses,
            win_rate=win_rate,
            profit_factor=pf,
            max_drawdown=max_dd,
            max_drawdown_pct=max_dd_pct,
            trades=trades,
            equity_curve=equity_curve,
            bars=bars,
        )
    finally:
        mt5.shutdown()


def _format_timeframe(tf: int) -> str:
    mapping = {
        mt5.TIMEFRAME_M1: "M1",
        mt5.TIMEFRAME_M2: "M2",
        mt5.TIMEFRAME_M3: "M3",
        mt5.TIMEFRAME_M4: "M4",
        mt5.TIMEFRAME_M5: "M5",
        mt5.TIMEFRAME_M6: "M6",
        mt5.TIMEFRAME_M10: "M10",
        mt5.TIMEFRAME_M12: "M12",
        mt5.TIMEFRAME_M15: "M15",
        mt5.TIMEFRAME_M20: "M20",
        mt5.TIMEFRAME_M30: "M30",
        mt5.TIMEFRAME_H1: "H1",
        mt5.TIMEFRAME_H2: "H2",
        mt5.TIMEFRAME_H3: "H3",
        mt5.TIMEFRAME_H4: "H4",
        mt5.TIMEFRAME_H6: "H6",
        mt5.TIMEFRAME_H8: "H8",
        mt5.TIMEFRAME_H12: "H12",
        mt5.TIMEFRAME_D1: "D1",
        mt5.TIMEFRAME_W1: "W1",
        mt5.TIMEFRAME_MN1: "MN1",
    }
    return mapping.get(tf, str(tf))


def print_summary(result: BacktestResult) -> None:
    print(f"\nBacktest summary for {result.symbol} [{_format_timeframe(result.timeframe)}]")
    print(f"Period: {result.start:%Y-%m-%d} -> {result.end:%Y-%m-%d}")
    print(f"Initial balance: {result.initial_balance:,.2f}")
    print(f"Final balance:   {result.final_balance:,.2f}")
    print(f"Net profit:      {result.net_profit:,.2f} ({result.net_profit_pct:.2f}%)")
    print(f"Trades:          {result.total_trades} (Wins: {result.wins}, Losses: {result.losses}, Win rate: {result.win_rate:.2f}%)")
    if result.profit_factor is not None:
        print(f"Profit factor:   {result.profit_factor:.2f}")
    print(f"Max drawdown:    {result.max_drawdown:,.2f} ({result.max_drawdown_pct:.2f}%)")


def generate_backtest_report(
    result: BacktestResult,
    output_html: str,
    title: Optional[str] = None,
) -> str:
    try:
        import plotly.graph_objects as go
    except ImportError as exc:  # pragma: no cover - runtime dependency
        raise RuntimeError(
            "Plotly is required to generate the backtest report. Please install it with 'pip install plotly'."
        ) from exc

    if not result.bars:
        raise ValueError("BacktestResult does not contain bar data required for plotting.")

    times = [bar["time"].isoformat() if isinstance(bar["time"], datetime) else bar["time"] for bar in result.bars]
    opens = [bar["open"] for bar in result.bars]
    highs = [bar["high"] for bar in result.bars]
    lows = [bar["low"] for bar in result.bars]
    closes = [bar["close"] for bar in result.bars]

    fig = go.Figure()
    fig.add_trace(
        go.Candlestick(
            x=times,
            open=opens,
            high=highs,
            low=lows,
            close=closes,
            name="Price",
        )
    )

    long_entries_x = [trade.entry_time for trade in result.trades if trade.direction == 1]
    long_entries_y = [trade.entry_price for trade in result.trades if trade.direction == 1]
    short_entries_x = [trade.entry_time for trade in result.trades if trade.direction == -1]
    short_entries_y = [trade.entry_price for trade in result.trades if trade.direction == -1]

    exits_x = [trade.exit_time for trade in result.trades]
    exits_y = [trade.exit_price for trade in result.trades]
    exit_text = [
        f"{trade.exit_reason.title()} | PnL: {trade.pnl_currency:.2f}"
        for trade in result.trades
    ]

    if long_entries_x:
        fig.add_trace(
            go.Scatter(
                x=long_entries_x,
                y=long_entries_y,
                mode="markers",
                name="Long Entry",
                marker=dict(symbol="triangle-up", color="#32CD32", size=10),
            )
        )
    if short_entries_x:
        fig.add_trace(
            go.Scatter(
                x=short_entries_x,
                y=short_entries_y,
                mode="markers",
                name="Short Entry",
                marker=dict(symbol="triangle-down", color="#FF4D4F", size=10),
            )
        )
    if exits_x:
        fig.add_trace(
            go.Scatter(
                x=exits_x,
                y=exits_y,
                mode="markers",
                name="Exit",
                marker=dict(symbol="x", color="#FADB14", size=9),
                text=exit_text,
                hovertemplate="%{text}<br>%{x|%Y-%m-%d %H:%M}<extra></extra>",
            )
        )

    if result.equity_curve:
        equity_x = [ts.isoformat() if isinstance(ts, datetime) else ts for ts, _ in result.equity_curve]
        equity_y = [val for _, val in result.equity_curve]
        fig.add_trace(
            go.Scatter(
                x=equity_x,
                y=equity_y,
                mode="lines",
                name="Equity",
                line=dict(color="#1890FF", width=2),
                yaxis="y2",
            )
        )

    layout_title = title or f"{result.symbol} Backtest ({_format_timeframe(result.timeframe)})"
    fig.update_layout(
        title=layout_title,
        xaxis_title="Time",
        yaxis_title=f"{result.symbol} Price",
        yaxis2=dict(
            title="Equity",
            overlaying="y",
            side="right",
            showgrid=False,
        ),
        xaxis_rangeslider_visible=False,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        template="plotly_dark",
        margin=dict(l=60, r=60, t=60, b=40),
    )

    fig.write_html(output_html, include_plotlyjs="cdn", full_html=True)
    return output_html


def export_backtest_to_excel(result: BacktestResult, output_path: str) -> Path:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    summary_df = pd.DataFrame(
        [
            {
                "symbol": result.symbol,
                "timeframe": _format_timeframe(result.timeframe),
                "start": result.start.isoformat(),
                "end": result.end.isoformat(),
                "initial_balance": result.initial_balance,
                "final_balance": result.final_balance,
                "net_profit": result.net_profit,
                "net_profit_pct": result.net_profit_pct,
                "total_trades": result.total_trades,
                "wins": result.wins,
                "losses": result.losses,
                "win_rate_pct": result.win_rate,
                "profit_factor": result.profit_factor,
                "max_drawdown": result.max_drawdown,
                "max_drawdown_pct": result.max_drawdown_pct,
            }
        ]
    )

    # Enhanced trade records with additional analytics
    cumulative_pnl = 0.0
    trades_records = []
    for idx, trade in enumerate(result.trades, start=1):
        cumulative_pnl += trade.pnl_currency
        duration = (trade.exit_time - trade.entry_time).total_seconds() / 60.0  # minutes

        trades_records.append({
            "trade_number": idx,
            "direction": "long" if trade.direction == 1 else "short",
            "volume": trade.volume,
            "entry_time": trade.entry_time.isoformat(),
            "entry_price": trade.entry_price,
            "exit_time": trade.exit_time.isoformat(),
            "exit_price": trade.exit_price,
            "duration_minutes": round(duration, 2),
            "pnl_points": round(trade.pnl_points, 2),
            "pnl_currency": round(trade.pnl_currency, 2),
            "cumulative_pnl": round(cumulative_pnl, 2),
            "win_loss": "win" if trade.pnl_currency > 0 else "loss" if trade.pnl_currency < 0 else "breakeven",
            "exit_reason": trade.exit_reason,
        })

    trades_df = pd.DataFrame(trades_records)

    equity_records = [
        {
            "time": ts.isoformat() if isinstance(ts, datetime) else ts,
            "balance": value,
            "equity": value,
        }
        for ts, value in result.equity_curve
    ]
    equity_df = pd.DataFrame(equity_records)

    bars_records = []
    for bar in result.bars:
        serializable_events = []
        for event in bar.get("events", []):
            event_copy = dict(event)
            if isinstance(event_copy.get("time"), datetime):
                event_copy["time"] = event_copy["time"].isoformat()
            serializable_events.append(event_copy)
        events_json = json.dumps(serializable_events, ensure_ascii=False)
        bars_records.append(
            {
                "time": bar["time"].isoformat() if isinstance(bar.get("time"), datetime) else bar.get("time"),
                "open": bar.get("open"),
                "high": bar.get("high"),
                "low": bar.get("low"),
                "close": bar.get("close"),
                "signal": bar.get("signal"),
                "position_before": bar.get("position_before"),
                "position_after": bar.get("position_after"),
                "events": events_json,
            }
        )
    bars_df = pd.DataFrame(bars_records)

    # Create detailed trade statistics sheet
    trade_stats = []
    if len(result.trades) > 0:
        winning_trades = [t for t in result.trades if t.pnl_currency > 0]
        losing_trades = [t for t in result.trades if t.pnl_currency < 0]
        long_trades = [t for t in result.trades if t.direction == 1]
        short_trades = [t for t in result.trades if t.direction == -1]

        trade_stats.append({"metric": "Total Trades", "value": len(result.trades)})
        trade_stats.append({"metric": "Winning Trades", "value": len(winning_trades)})
        trade_stats.append({"metric": "Losing Trades", "value": len(losing_trades)})
        trade_stats.append({"metric": "Win Rate %", "value": f"{result.win_rate:.2f}"})
        trade_stats.append({"metric": "", "value": ""})

        if winning_trades:
            avg_win = sum(t.pnl_currency for t in winning_trades) / len(winning_trades)
            max_win = max(t.pnl_currency for t in winning_trades)
            trade_stats.append({"metric": "Average Win", "value": f"{avg_win:.2f}"})
            trade_stats.append({"metric": "Largest Win", "value": f"{max_win:.2f}"})

        if losing_trades:
            avg_loss = sum(t.pnl_currency for t in losing_trades) / len(losing_trades)
            max_loss = min(t.pnl_currency for t in losing_trades)
            trade_stats.append({"metric": "Average Loss", "value": f"{avg_loss:.2f}"})
            trade_stats.append({"metric": "Largest Loss", "value": f"{max_loss:.2f}"})

        trade_stats.append({"metric": "", "value": ""})
        trade_stats.append({"metric": "Long Trades", "value": len(long_trades)})
        if long_trades:
            long_wins = sum(1 for t in long_trades if t.pnl_currency > 0)
            long_win_rate = (long_wins / len(long_trades)) * 100
            long_pnl = sum(t.pnl_currency for t in long_trades)
            trade_stats.append({"metric": "Long Win Rate %", "value": f"{long_win_rate:.2f}"})
            trade_stats.append({"metric": "Long Total P&L", "value": f"{long_pnl:.2f}"})

        trade_stats.append({"metric": "", "value": ""})
        trade_stats.append({"metric": "Short Trades", "value": len(short_trades)})
        if short_trades:
            short_wins = sum(1 for t in short_trades if t.pnl_currency > 0)
            short_win_rate = (short_wins / len(short_trades)) * 100
            short_pnl = sum(t.pnl_currency for t in short_trades)
            trade_stats.append({"metric": "Short Win Rate %", "value": f"{short_win_rate:.2f}"})
            trade_stats.append({"metric": "Short Total P&L", "value": f"{short_pnl:.2f}"})

        # Exit reason breakdown
        trade_stats.append({"metric": "", "value": ""})
        trade_stats.append({"metric": "Exit Reasons:", "value": ""})
        exit_reasons = {}
        for t in result.trades:
            exit_reasons[t.exit_reason] = exit_reasons.get(t.exit_reason, 0) + 1
        for reason, count in sorted(exit_reasons.items()):
            trade_stats.append({"metric": f"  {reason}", "value": count})

    trade_stats_df = pd.DataFrame(trade_stats)

    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        summary_df.to_excel(writer, index=False, sheet_name="summary")
        trades_df.to_excel(writer, index=False, sheet_name="trades")
        trade_stats_df.to_excel(writer, index=False, sheet_name="trade_statistics")
        equity_df.to_excel(writer, index=False, sheet_name="equity")
        bars_df.to_excel(writer, index=False, sheet_name="bars")

    return output


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backtest Candle Flip Scalp strategy")
    parser.add_argument("--symbol", required=True, help="Trading symbol (e.g. EURUSD)")
    parser.add_argument(
        "--timeframe",
        default="M15",
        help="MT5 timeframe code (default: M15)",
    )
    parser.add_argument(
        "--months",
        type=int,
        default=6,
        help="Number of months to backtest (default: 6)",
    )
    parser.add_argument(
        "--initial-balance",
        type=float,
        default=10_000.0,
        help="Starting balance for backtest (default: 10,000)",
    )
    parser.add_argument(
        "--config-from-env",
        action="store_true",
        help="Load the rest of the strategy parameters from environment variables (CFS_*)",
    )
    parser.add_argument(
        "--fixed-lots",
        type=float,
        default=0.10,
        help="Fixed lot size when risk percent disabled",
    )
    parser.add_argument(
        "--use-risk-percent",
        action="store_true",
        help="Enable risk-based position sizing",
    )
    parser.add_argument(
        "--risk-percent",
        type=float,
        default=0.5,
        help="Percent of balance to risk per trade",
    )
    parser.add_argument("--sl-points", type=int, default=60, help="Stop loss in points")
    parser.add_argument("--tp-points", type=int, default=60, help="Take profit in points")
    parser.add_argument(
        "--hysteresis-points",
        type=int,
        default=10,
        help="Entry hysteresis in points",
    )
    parser.add_argument(
        "--max-spread-points",
        type=int,
        default=30,
        help="Maximum allowed spread in points",
    )
    parser.add_argument(
        "--cooldown-seconds",
        type=int,
        default=5,
        help="Cooldown after each trade (ignored in backtest)",
    )
    parser.add_argument(
        "--magic",
        type=int,
        default=902715,
        help="Magic number (unused in backtest)",
    )
    parser.add_argument(
        "--max-flips-per-bar",
        type=int,
        default=2,
        help="Maximum direction changes per bar",
    )
    parser.add_argument(
        "--deviation",
        type=int,
        default=20,
        help="Maximum slippage in points (unused in backtest)",
    )
    parser.add_argument(
        "--excel-report",
        help="Optional path to save an Excel workbook with summary, trades, equity, and bars",
    )
    parser.add_argument(
        "--html-report",
        help="Optional path to save an interactive HTML report with price chart and signals",
    )
    parser.add_argument(
        "--report-title",
        help="Optional custom title for the HTML report",
    )
    parser.add_argument(
        "--no-realistic-fills",
        action="store_true",
        help="Disable realistic bid/ask spread simulation (optimistic fills)",
    )
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="Disable progress indicator",
    )
    parser.add_argument(
        "--auto-html",
        action="store_true",
        help="Automatically generate HTML chart alongside Excel",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)

    if args.config_from_env:
        config = CandleFlipScalpConfig.from_env()
    else:
        try:
            timeframe = timeframe_from_code(args.timeframe)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            raise SystemExit(1) from exc

        config = CandleFlipScalpConfig(
            symbol=args.symbol,
            timeframe=timeframe,
            fixed_lots=args.fixed_lots,
            use_risk_percent=args.use_risk_percent,
            risk_percent=args.risk_percent,
            sl_points=args.sl_points,
            tp_points=args.tp_points,
            hysteresis_points=args.hysteresis_points,
            max_spread_points=args.max_spread_points,
            cooldown_seconds=args.cooldown_seconds,
            magic=args.magic,
            max_flips_per_bar=args.max_flips_per_bar,
            deviation=args.deviation,
        )

    try:
        result = run_backtest(
            config,
            months=args.months,
            initial_balance=args.initial_balance,
            realistic_fills=not args.no_realistic_fills,
            show_progress=not args.no_progress,
        )
    except RuntimeError as exc:
        print(f"[backtest] {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    print_summary(result)

    # Create results folder structure
    results_base = Path("results")

    # Auto-export Excel if path not specified
    excel_output = args.excel_report
    if not excel_output:
        # Create organized folder structure: results/{symbol}/{YYYY-MM}/
        symbol_folder = results_base / config.symbol
        month_folder = symbol_folder / result.start.strftime("%Y-%m")
        month_folder.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        timeframe_str = _format_timeframe(config.timeframe)
        filename = f"backtest_{config.symbol}_{timeframe_str}_{timestamp}.xlsx"
        excel_output = str(month_folder / filename)

    try:
        excel_path = export_backtest_to_excel(result, excel_output)
        print(f"[backtest] Excel report saved to {excel_path}")
    except Exception as exc:  # pragma: no cover - convenience feature
        print(f"[backtest] Failed to export Excel report: {exc}", file=sys.stderr)
        excel_path = None

    # Auto-generate HTML report in same folder as Excel
    if args.html_report:
        html_output = args.html_report
    elif args.auto_html and excel_path:
        # Generate HTML next to Excel file
        html_output = str(Path(excel_path).with_suffix('.html'))
    else:
        html_output = None

    if html_output:
        try:
            output_path = generate_backtest_report(
                result,
                html_output,
                title=args.report_title,
            )
        except (RuntimeError, ValueError) as exc:
            print(f"[backtest] Failed to generate HTML report: {exc}", file=sys.stderr)
        else:
            print(f"[backtest] HTML report saved to {output_path}")


if __name__ == "__main__":
    main()

