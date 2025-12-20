from __future__ import annotations

import argparse
import os
import sys
from typing import Callable, TypeVar

from dotenv import load_dotenv

try:
    from .bot import CandleFlipScalpBot
    from .config import CandleFlipScalpConfig
    from .timeframe import timeframe_from_code
    from .backtest import main as backtest_cli_main
    from .visualize import main as visualize_cli_main
except ImportError:  # pragma: no cover - script execution
    from bot import CandleFlipScalpBot
    from config import CandleFlipScalpConfig
    from timeframe import timeframe_from_code
    from backtest import main as backtest_cli_main
    from visualize import main as visualize_cli_main

load_dotenv()

_T = TypeVar("_T")
_ENV_PREFIX = "CFS_"


def _env_var(name: str) -> str | None:
    return os.getenv(f"{_ENV_PREFIX}{name}")


def _env_typed(name: str, cast: Callable[[str], _T], default: _T) -> _T:
    raw = _env_var(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return cast(raw)
    except Exception as exc:  # pragma: no cover - defensive fallback
        print(
            f"[env] Ignoring invalid value for {_ENV_PREFIX}{name}: {raw!r} ({exc})",
            file=sys.stderr,
        )
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env_var(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    print(
        f"[env] Ignoring invalid boolean for {_ENV_PREFIX}{name}: {raw!r}",
        file=sys.stderr,
    )
    return default


def build_parser() -> argparse.ArgumentParser:
    symbol_default = _env_var("SYMBOL")
    parser = argparse.ArgumentParser(description="Candle Flip Scalp MT5 bot")
    parser.add_argument(
        "--symbol",
        required=symbol_default is None,
        default=symbol_default,
        help="Trading symbol (e.g. EURUSD)",
    )
    parser.add_argument(
        "--timeframe",
        default=_env_var("TIMEFRAME") or "M15",
        help="MT5 timeframe code (default: M15)",
    )
    parser.add_argument(
        "--fixed-lots",
        type=float,
        default=_env_typed("FIXED_LOTS", float, 0.10),
        help="Fixed lot size when risk percent disabled",
    )
    parser.add_argument(
        "--use-risk-percent",
        dest="use_risk_percent",
        action="store_true",
        help="Enable risk-based position sizing",
    )
    parser.add_argument(
        "--no-use-risk-percent",
        dest="use_risk_percent",
        action="store_false",
        help="Disable risk-based position sizing",
    )
    parser.set_defaults(use_risk_percent=_env_bool("USE_RISK_PERCENT", False))
    parser.add_argument(
        "--risk-percent",
        type=float,
        default=_env_typed("RISK_PERCENT", float, 0.5),
        help="Percent of balance to risk per trade",
    )
    parser.add_argument(
        "--sl-points",
        type=int,
        default=_env_typed("SL_POINTS", int, 60),
        help="Stop loss in points",
    )
    parser.add_argument(
        "--tp-points",
        type=int,
        default=_env_typed("TP_POINTS", int, 60),
        help="Take profit in points",
    )
    parser.add_argument(
        "--hysteresis-points",
        type=int,
        default=_env_typed("HYSTERESIS_POINTS", int, 10),
        help="Entry hysteresis in points",
    )
    parser.add_argument(
        "--max-spread-points",
        type=int,
        default=_env_typed("MAX_SPREAD_POINTS", int, 30),
        help="Maximum allowed spread in points",
    )
    parser.add_argument(
        "--cooldown-seconds",
        type=int,
        default=_env_typed("COOLDOWN_SECONDS", int, 5),
        help="Cooldown after each trade",
    )
    parser.add_argument(
        "--magic",
        type=int,
        default=_env_typed("MAGIC", int, 902715),
        help="Magic number",
    )
    parser.add_argument(
        "--max-flips-per-bar",
        type=int,
        default=_env_typed("MAX_FLIPS_PER_BAR", int, 2),
        help="Maximum direction changes per bar",
    )
    parser.add_argument(
        "--deviation",
        type=int,
        default=_env_typed("DEVIATION", int, 20),
        help="Maximum slippage in points",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=_env_typed("POLL_INTERVAL", float, 0.2),
        help="Tick polling interval in seconds",
    )
    parser.add_argument(
        "--login",
        type=int,
        default=_env_typed("LOGIN", int, None)
        if _env_var("LOGIN") is not None
        else None,
        help="MT5 account login (optional)",
    )
    parser.add_argument(
        "--password",
        default=_env_var("PASSWORD"),
        help="MT5 account password (optional)",
    )
    parser.add_argument(
        "--server",
        default=_env_var("SERVER"),
        help="MT5 trade server name (optional)",
    )
    parser.add_argument(
        "--filling-mode",
        default=_env_var("FILLING_MODE"),
        help="Override filling mode (RETURN, IOC, FOK, or numeric MT5 constant)",
    )
    parser.add_argument(
        "--monitor",
        dest="monitor_enabled",
        action="store_true",
        help="Enable built-in telemetry web monitor",
    )
    parser.add_argument(
        "--no-monitor",
        dest="monitor_enabled",
        action="store_false",
        help="Disable telemetry web monitor",
    )
    parser.set_defaults(monitor_enabled=_env_bool("MONITOR_ENABLED", False))
    parser.add_argument(
        "--monitor-host",
        default=_env_var("MONITOR_HOST") or "127.0.0.1",
        help="Host/interface to bind telemetry monitor (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--monitor-port",
        type=int,
        default=_env_typed("MONITOR_PORT", int, 8765),
        help="Port for telemetry monitor (default: 8765)",
    )
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = build_parser()
    return parser.parse_args(argv)


def create_bot_from_args(args: argparse.Namespace) -> CandleFlipScalpBot:
    try:
        timeframe = timeframe_from_code(args.timeframe)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc

    try:
        filling_mode = CandleFlipScalpConfig.parse_filling_mode(args.filling_mode)
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
        poll_interval=args.poll_interval,
        login=args.login,
        password=args.password,
        server=args.server,
        filling_mode=filling_mode,
        monitor_enabled=args.monitor_enabled,
        monitor_host=args.monitor_host,
        monitor_port=args.monitor_port,
    )
    return CandleFlipScalpBot(config)


def main(argv: list[str] | None = None) -> None:
    args_list = list(argv) if argv is not None else sys.argv[1:]

    if args_list:
        mode = args_list[0].lower()
        if mode in {"backtest", "bt"}:
            backtest_cli_main(args_list[1:])
            return
        if mode in {"visualize", "viz", "chart"}:
            visualize_cli_main(args_list[1:])
            return
        if mode in {"trade", "live"}:
            args_list = args_list[1:]

    args = parse_args(args_list or None)
    bot = create_bot_from_args(args)
    bot.run()


if __name__ == "__main__":
    main()

