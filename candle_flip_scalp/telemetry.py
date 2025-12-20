from __future__ import annotations

import json
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Dict, Optional


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_from_timestamp(timestamp: Optional[float]) -> Optional[str]:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def _copy_state(state: Dict[str, Any]) -> Dict[str, Any]:
    # Use JSON round-trip to avoid sharing mutable nested structures
    return json.loads(json.dumps(state))


class BotTelemetry:
    """Thread-safe container for publishing bot state to external observers."""

    def __init__(self, symbol: str, timeframe: int, history_limit: int = 500) -> None:
        self._lock = Lock()
        self._history_limit = max(50, history_limit)
        self._state: Dict[str, Any] = {
            "symbol": symbol,
            "timeframe": timeframe,
            "status": "starting",
            "last_updated": _iso_now(),
            "orders_attempted": 0,
            "orders_succeeded": 0,
            "orders_failed": 0,
            "monitor_url": None,
            "history_bars": [],
            "history_equity": [],
        }

    def _append_history_locked(self, key: str, value: Dict[str, Any]) -> None:
        history = self._state.setdefault(key, [])
        history.append(value)
        if len(history) > self._history_limit:
            del history[0 : len(history) - self._history_limit]

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return _copy_state(self._state)

    def update(self, **kwargs: Any) -> None:
        if not kwargs:
            return
        with self._lock:
            self._state.update(kwargs)
            self._state["last_updated"] = _iso_now()

    def set_status(self, status: str) -> None:
        self.update(status=status)

    def update_tick(
        self,
        *,
        bar_time: Optional[float],
        tick_time: Optional[float],
        bid: Optional[float],
        ask: Optional[float],
        mid_price: Optional[float],
        spread_points: Optional[float],
        flips_this_bar: int,
        position_dir: int,
        decision: str,
    ) -> None:
        position_label = {1: "long", -1: "short", 0: "flat"}.get(position_dir, "unknown")
        bar_time_iso = _iso_from_timestamp(bar_time)
        tick_time_iso = _iso_from_timestamp(tick_time) or _iso_now()
        with self._lock:
            self._state.update(
                {
                    "last_bar_time": bar_time_iso,
                    "last_tick_time": tick_time_iso,
                    "bid": bid,
                    "ask": ask,
                    "mid_price": mid_price,
                    "spread_points": spread_points,
                    "flips_this_bar": flips_this_bar,
                    "position_dir": position_dir,
                    "position_label": position_label,
                    "last_decision": decision,
                    "last_updated": _iso_now(),
                }
            )

    def append_bar(
        self,
        *,
        time: Optional[float],
        open_price: Optional[float],
        high: Optional[float],
        low: Optional[float],
        close: Optional[float],
        decision: Optional[str],
        position_label: Optional[str],
        trade: Optional[Dict[str, Any]] = None,
        position_dir: Optional[int] = None,
    ) -> None:
        time_iso = _iso_from_timestamp(time) or _iso_now()
        with self._lock:
            payload = {
                "time": time_iso,
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "decision": decision,
                "position": position_label,
                "position_dir": position_dir,
                "events": [],
            }
            if trade:
                trade_record = dict(trade)
                if "time" not in trade_record or trade_record["time"] is None:
                    trade_record["time"] = time_iso
                payload["trade"] = trade_record
                payload["events"].append(trade_record)
            self._append_history_locked("history_bars", payload)
            self._state["last_updated"] = _iso_now()

    def update_or_append_bar(
        self,
        *,
        time: Optional[float],
        open_price: Optional[float],
        high: Optional[float],
        low: Optional[float],
        close: Optional[float],
        decision: Optional[str],
        position_label: Optional[str],
        trade: Optional[Dict[str, Any]] = None,
        position_dir: Optional[int] = None,
    ) -> None:
        """Update existing bar if same timestamp, otherwise append new bar."""
        time_iso = _iso_from_timestamp(time) or _iso_now()
        with self._lock:
            history = self._state.get("history_bars", [])

            # Check if last bar has same timestamp - if so, update it
            if history and history[-1].get("time") == time_iso:
                # Update existing bar (keep events from previous updates)
                existing_events = history[-1].get("events", [])
                history[-1].update({
                    "open": open_price,
                    "high": high,
                    "low": low,
                    "close": close,
                    "decision": decision,
                    "position": position_label,
                    "position_dir": position_dir,
                })

                # Add new trade event if provided
                if trade:
                    trade_record = dict(trade)
                    if "time" not in trade_record or trade_record["time"] is None:
                        trade_record["time"] = time_iso
                    existing_events.append(trade_record)
                    history[-1]["trade"] = trade_record

                history[-1]["events"] = existing_events
            else:
                # New bar - append it
                payload = {
                    "time": time_iso,
                    "open": open_price,
                    "high": high,
                    "low": low,
                    "close": close,
                    "decision": decision,
                    "position": position_label,
                    "position_dir": position_dir,
                    "events": [],
                }
                if trade:
                    trade_record = dict(trade)
                    if "time" not in trade_record or trade_record["time"] is None:
                        trade_record["time"] = time_iso
                    payload["trade"] = trade_record
                    payload["events"].append(trade_record)
                self._append_history_locked("history_bars", payload)

            self._state["last_updated"] = _iso_now()

    def update_equity(
        self,
        timestamp: Optional[float],
        balance: Optional[float],
        equity: Optional[float],
    ) -> None:
        if balance is None and equity is None:
            return
        time_iso = _iso_from_timestamp(timestamp) or _iso_now()
        with self._lock:
            payload: Dict[str, Any] = {"last_updated": _iso_now()}
            if balance is not None:
                payload["last_balance"] = balance
            if equity is not None:
                payload["last_equity"] = equity
            self._state.update(payload)
            self._append_history_locked(
                "history_equity",
                {
                    "time": time_iso,
                    "balance": balance,
                    "equity": equity,
                },
            )

    def record_trade(self, timestamp: float, direction: int) -> None:
        direction_label = {1: "long", -1: "short"}.get(direction, "flat")
        self.update(
            last_trade_time=_iso_from_timestamp(timestamp),
            position_dir=direction,
            position_label=direction_label,
        )

    def record_order_attempt(
        self,
        *,
        context: str,
        filling_mode: Optional[str],
        success: bool,
        retcode: Optional[int],
        comment: Optional[str],
    ) -> None:
        entry = {
            "timestamp": _iso_now(),
            "context": context,
            "filling_mode": filling_mode,
            "success": success,
            "retcode": retcode,
            "comment": comment,
        }
        with self._lock:
            self._state["orders_attempted"] = self._state.get("orders_attempted", 0) + 1
            if success:
                self._state["orders_succeeded"] = self._state.get("orders_succeeded", 0) + 1
            else:
                self._state["orders_failed"] = self._state.get("orders_failed", 0) + 1
                if comment:
                    self._state["last_error"] = comment
            self._state["last_order"] = entry
            self._state["last_updated"] = entry["timestamp"]

    def record_error(self, message: str) -> None:
        self.update(last_error=message)

    def set_monitor_url(self, url: str) -> None:
        self.update(monitor_url=url)


