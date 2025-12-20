from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Optional

import MetaTrader5 as mt5
from dotenv import load_dotenv

load_dotenv()

_FILLING_NAME_TO_CONST = {
    "RETURN": mt5.ORDER_FILLING_RETURN,
    "IOC": mt5.ORDER_FILLING_IOC,
    "FOK": mt5.ORDER_FILLING_FOK,
}


@dataclass
class CandleFlipScalpConfig:
    symbol: str
    timeframe: int = mt5.TIMEFRAME_M15
    fixed_lots: float = 0.10
    use_risk_percent: bool = False
    risk_percent: float = 0.5
    sl_points: int = 60
    tp_points: int = 60
    hysteresis_points: int = 10
    max_spread_points: int = 30
    cooldown_seconds: int = 5
    magic: int = 902715
    max_flips_per_bar: int = 2
    deviation: int = 20
    poll_interval: float = 0.2
    login: Optional[int] = None
    password: Optional[str] = None
    server: Optional[str] = None
    filling_mode: Optional[int] = None
    monitor_enabled: bool = False
    monitor_host: str = "127.0.0.1"
    monitor_port: int = 8765

    # CRITICAL FIX: Account protection parameters
    max_daily_loss_percent: float = 5.0  # Stop if daily loss exceeds %
    max_drawdown_percent: float = 10.0  # Stop if equity drops % from peak
    max_consecutive_losses: int = 5  # Circuit breaker
    min_equity_percent: float = 80.0  # Stop if equity < % of initial

    def __post_init__(self):
        """CRITICAL FIX: Validate configuration parameters."""
        errors = []

        # Symbol validation
        if not self.symbol or not self.symbol.strip():
            errors.append("symbol cannot be empty")

        # Lot size validation
        if self.fixed_lots <= 0:
            errors.append(f"fixed_lots must be positive, got {self.fixed_lots}")
        if self.fixed_lots > 100:
            errors.append(f"fixed_lots seems unreasonably large: {self.fixed_lots}")

        # Risk percent validation
        if self.use_risk_percent:
            if self.risk_percent <= 0:
                errors.append(f"risk_percent must be positive, got {self.risk_percent}")
            if self.risk_percent > 10:
                errors.append(f"risk_percent > 10% is very aggressive: {self.risk_percent}")
            if self.risk_percent > 100:
                errors.append(f"risk_percent cannot exceed 100%, got {self.risk_percent}")

        # SL/TP validation
        if self.sl_points <= 0:
            errors.append(f"sl_points must be positive, got {self.sl_points}")
        if self.tp_points <= 0:
            errors.append(f"tp_points must be positive, got {self.tp_points}")
        if self.sl_points > 10000:
            errors.append(f"sl_points seems unreasonably large: {self.sl_points}")

        # Hysteresis validation
        if self.hysteresis_points < 0:
            errors.append(f"hysteresis_points cannot be negative, got {self.hysteresis_points}")
        if self.hysteresis_points > self.sl_points:
            errors.append(
                f"hysteresis_points ({self.hysteresis_points}) should be less than "
                f"sl_points ({self.sl_points})"
            )

        # Spread validation
        if self.max_spread_points <= 0:
            errors.append(f"max_spread_points must be positive, got {self.max_spread_points}")

        # Cooldown validation
        if self.cooldown_seconds < 0:
            errors.append(f"cooldown_seconds cannot be negative, got {self.cooldown_seconds}")
        if self.cooldown_seconds > 3600:
            errors.append(f"cooldown_seconds seems unreasonably large: {self.cooldown_seconds}")

        # Max flips validation
        if self.max_flips_per_bar < 0:
            errors.append(f"max_flips_per_bar cannot be negative, got {self.max_flips_per_bar}")
        if self.max_flips_per_bar > 100:
            errors.append(f"max_flips_per_bar seems unreasonably large: {self.max_flips_per_bar}")

        # Deviation validation
        if self.deviation < 0:
            errors.append(f"deviation cannot be negative, got {self.deviation}")
        if self.deviation > 1000:
            errors.append(f"deviation seems unreasonably large: {self.deviation}")

        # Poll interval validation
        if self.poll_interval <= 0:
            errors.append(f"poll_interval must be positive, got {self.poll_interval}")
        if self.poll_interval < 0.01:
            errors.append(
                f"poll_interval < 0.01s may cause excessive CPU usage: {self.poll_interval}"
            )
        if self.poll_interval > 60:
            errors.append(
                f"poll_interval > 60s may miss trading opportunities: {self.poll_interval}"
            )

        # Account protection validation
        if self.max_daily_loss_percent <= 0 or self.max_daily_loss_percent > 100:
            errors.append(
                f"max_daily_loss_percent must be between 0 and 100, got {self.max_daily_loss_percent}"
            )
        if self.max_drawdown_percent <= 0 or self.max_drawdown_percent > 100:
            errors.append(
                f"max_drawdown_percent must be between 0 and 100, got {self.max_drawdown_percent}"
            )
        if self.max_consecutive_losses < 0:
            errors.append(
                f"max_consecutive_losses cannot be negative, got {self.max_consecutive_losses}"
            )
        if self.min_equity_percent <= 0 or self.min_equity_percent > 100:
            errors.append(
                f"min_equity_percent must be between 0 and 100, got {self.min_equity_percent}"
            )

        if errors:
            raise ValueError(f"Invalid configuration:\n  " + "\n  ".join(errors))

    @staticmethod
    def parse_filling_mode(value: str | int | None) -> Optional[int]:
        if value is None:
            return None
        if isinstance(value, int):
            return value
        raw = str(value).strip()
        if raw == "":
            return None
        upper = raw.upper()
        if upper in _FILLING_NAME_TO_CONST:
            return _FILLING_NAME_TO_CONST[upper]
        if raw.isdigit():
            return int(raw)
        raise ValueError(
            "Filling mode must be one of {RETURN, IOC, FOK} or a numeric MT5 filling mode constant"
        )

    @classmethod
    def from_env(cls, prefix: str = "CFS_") -> "CandleFlipScalpConfig":
        def env(key: str) -> str | None:
            return os.getenv(f"{prefix}{key}")

        def env_typed(key: str, cast, default):
            raw = env(key)
            if raw is None or raw.strip() == "":
                return default
            try:
                return cast(raw)
            except Exception as exc:
                print(
                    f"[config] Ignoring invalid value for {prefix}{key}: {raw!r} ({exc})",
                    file=sys.stderr,
                )
                return default

        def env_bool(key: str, default: bool) -> bool:
            raw = env(key)
            if raw is None:
                return default
            normalized = raw.strip().lower()
            if normalized in {"1", "true", "yes", "y", "on"}:
                return True
            if normalized in {"0", "false", "no", "n", "off"}:
                return False
            print(
                f"[config] Ignoring invalid boolean for {prefix}{key}: {raw!r}",
                file=sys.stderr,
            )
            return default

        symbol = env("SYMBOL")
        if not symbol:
            raise ValueError("Environment variable CFS_SYMBOL is required")

        timeframe_code = env("TIMEFRAME") or "M15"
        from .timeframe import timeframe_from_code

        timeframe = timeframe_from_code(timeframe_code)

        try:
            filling_mode = cls.parse_filling_mode(env("FILLING_MODE"))
        except ValueError as exc:
            print(f"[config] {exc}", file=sys.stderr)
            filling_mode = None

        login_value = env("LOGIN")
        login = int(login_value) if login_value not in (None, "") else None

        config = cls(
            symbol=symbol,
            timeframe=timeframe,
            fixed_lots=env_typed("FIXED_LOTS", float, cls.fixed_lots),
            use_risk_percent=env_bool("USE_RISK_PERCENT", cls.use_risk_percent),
            risk_percent=env_typed("RISK_PERCENT", float, cls.risk_percent),
            sl_points=env_typed("SL_POINTS", int, cls.sl_points),
            tp_points=env_typed("TP_POINTS", int, cls.tp_points),
            hysteresis_points=env_typed("HYSTERESIS_POINTS", int, cls.hysteresis_points),
            max_spread_points=env_typed("MAX_SPREAD_POINTS", int, cls.max_spread_points),
            cooldown_seconds=env_typed("COOLDOWN_SECONDS", int, cls.cooldown_seconds),
            magic=env_typed("MAGIC", int, cls.magic),
            max_flips_per_bar=env_typed(
                "MAX_FLIPS_PER_BAR", int, cls.max_flips_per_bar
            ),
            deviation=env_typed("DEVIATION", int, cls.deviation),
            poll_interval=env_typed("POLL_INTERVAL", float, cls.poll_interval),
            login=login,
            password=env("PASSWORD"),
            server=env("SERVER"),
            filling_mode=filling_mode,
            monitor_enabled=env_bool("MONITOR_ENABLED", cls.monitor_enabled),
            monitor_host=env("MONITOR_HOST") or cls.monitor_host,
            monitor_port=env_typed("MONITOR_PORT", int, cls.monitor_port),
            # CRITICAL FIX: Account protection parameters
            max_daily_loss_percent=env_typed(
                "MAX_DAILY_LOSS_PERCENT", float, cls.max_daily_loss_percent
            ),
            max_drawdown_percent=env_typed(
                "MAX_DRAWDOWN_PERCENT", float, cls.max_drawdown_percent
            ),
            max_consecutive_losses=env_typed(
                "MAX_CONSECUTIVE_LOSSES", int, cls.max_consecutive_losses
            ),
            min_equity_percent=env_typed(
                "MIN_EQUITY_PERCENT", float, cls.min_equity_percent
            ),
        )
        return config

