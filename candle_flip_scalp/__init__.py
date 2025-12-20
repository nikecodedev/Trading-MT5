"""Candle Flip Scalp trading bot package."""

from .bot import CandleFlipScalpBot
from .config import CandleFlipScalpConfig
from .timeframe import timeframe_from_code
from .backtest import (
    run_backtest,
    BacktestResult,
    BacktestTrade,
    generate_backtest_report,
    print_summary,
    export_backtest_to_excel,
)
from .visualize import (
    load_backtest_excel,
    filter_by_date_range,
    generate_html_chart,
)

__all__ = [
    "CandleFlipScalpBot",
    "CandleFlipScalpConfig",
    "timeframe_from_code",
    "run_backtest",
    "BacktestResult",
    "BacktestTrade",
    "generate_backtest_report",
    "print_summary",
    "export_backtest_to_excel",
    "load_backtest_excel",
    "filter_by_date_range",
    "generate_html_chart",
]

