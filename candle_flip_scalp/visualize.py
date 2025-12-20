"""Visualize backtest results from Excel file with optional time period filtering."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd


def load_backtest_excel(excel_path: str) -> dict:
    """Load all sheets from backtest Excel file."""
    try:
        summary = pd.read_excel(excel_path, sheet_name="summary")
        trades = pd.read_excel(excel_path, sheet_name="trades")
        equity = pd.read_excel(excel_path, sheet_name="equity")
        bars = pd.read_excel(excel_path, sheet_name="bars")

        # Convert time strings to datetime
        if "entry_time" in trades.columns:
            trades["entry_time"] = pd.to_datetime(trades["entry_time"])
        if "exit_time" in trades.columns:
            trades["exit_time"] = pd.to_datetime(trades["exit_time"])
        if "time" in equity.columns:
            equity["time"] = pd.to_datetime(equity["time"])
        if "time" in bars.columns:
            bars["time"] = pd.to_datetime(bars["time"])

        return {
            "summary": summary,
            "trades": trades,
            "equity": equity,
            "bars": bars,
        }
    except Exception as exc:
        raise RuntimeError(f"Failed to load Excel file: {exc}") from exc


def filter_by_date_range(
    data: dict,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> dict:
    """Filter data by date range."""
    filtered = {}

    # Parse dates
    start_dt = pd.to_datetime(start_date) if start_date else None
    end_dt = pd.to_datetime(end_date) if end_date else None

    # Filter bars
    bars = data["bars"].copy()
    if start_dt is not None:
        bars = bars[bars["time"] >= start_dt]
    if end_dt is not None:
        bars = bars[bars["time"] <= end_dt]
    filtered["bars"] = bars

    # Filter trades
    trades = data["trades"].copy()
    if start_dt is not None:
        trades = trades[trades["entry_time"] >= start_dt]
    if end_dt is not None:
        trades = trades[trades["entry_time"] <= end_dt]
    filtered["trades"] = trades

    # Filter equity
    equity = data["equity"].copy()
    if start_dt is not None:
        equity = equity[equity["time"] >= start_dt]
    if end_dt is not None:
        equity = equity[equity["time"] <= end_dt]
    filtered["equity"] = equity

    # Summary stays the same
    filtered["summary"] = data["summary"]

    return filtered


def print_summary(data: dict, filtered: bool = False) -> None:
    """Print summary statistics."""
    summary = data["summary"]
    trades = data["trades"]
    equity = data["equity"]

    prefix = "[filtered] " if filtered else ""

    if len(summary) > 0:
        row = summary.iloc[0]
        print(f"\n{prefix}Backtest Summary:")
        print(f"Symbol:          {row.get('symbol', 'N/A')}")
        print(f"Timeframe:       {row.get('timeframe', 'N/A')}")
        print(f"Period:          {row.get('start', 'N/A')} -> {row.get('end', 'N/A')}")

    if len(trades) > 0:
        wins = len(trades[trades["pnl_currency"] > 0])
        losses = len(trades[trades["pnl_currency"] < 0])
        total_trades = len(trades)
        win_rate = (wins / total_trades * 100) if total_trades > 0 else 0

        total_pnl = trades["pnl_currency"].sum()

        print(f"\n{prefix}Trade Statistics:")
        print(f"Total trades:    {total_trades}")
        print(f"Wins:            {wins}")
        print(f"Losses:          {losses}")
        print(f"Win rate:        {win_rate:.2f}%")
        print(f"Total P&L:       {total_pnl:,.2f}")

        if total_trades > 0:
            avg_win = trades[trades["pnl_currency"] > 0]["pnl_currency"].mean() if wins > 0 else 0
            avg_loss = trades[trades["pnl_currency"] < 0]["pnl_currency"].mean() if losses > 0 else 0
            print(f"Avg win:         {avg_win:,.2f}")
            print(f"Avg loss:        {avg_loss:,.2f}")

    if len(equity) > 0:
        initial = equity["balance"].iloc[0] if len(equity) > 0 else 0
        final = equity["balance"].iloc[-1] if len(equity) > 0 else 0
        print(f"\n{prefix}Equity:")
        print(f"Initial:         {initial:,.2f}")
        print(f"Final:           {final:,.2f}")
        print(f"Net profit:      {final - initial:,.2f}")
        if initial > 0:
            print(f"Return:          {((final - initial) / initial * 100):.2f}%")


def generate_html_chart(
    data: dict,
    output_path: str,
    title: Optional[str] = None,
) -> str:
    """Generate interactive HTML chart from filtered data."""
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except ImportError as exc:
        raise RuntimeError(
            "Plotly is required for visualization. Install with: pip install plotly"
        ) from exc

    bars = data["bars"]
    trades = data["trades"]
    equity = data["equity"]

    if len(bars) == 0:
        raise ValueError("No bar data available for the selected period")

    # Create figure with secondary y-axis
    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.1,
        row_heights=[0.7, 0.3],
        subplot_titles=("Price & Trades", "Equity Curve"),
    )

    # Add candlestick chart
    fig.add_trace(
        go.Candlestick(
            x=bars["time"],
            open=bars["open"],
            high=bars["high"],
            low=bars["low"],
            close=bars["close"],
            name="Price",
        ),
        row=1, col=1,
    )

    # Add trade markers
    long_entries = trades[trades["direction"] == "long"]
    short_entries = trades[trades["direction"] == "short"]

    if len(long_entries) > 0:
        fig.add_trace(
            go.Scatter(
                x=long_entries["entry_time"],
                y=long_entries["entry_price"],
                mode="markers",
                name="Long Entry",
                marker=dict(symbol="triangle-up", color="#32CD32", size=12),
                hovertemplate="Long Entry<br>Price: %{y:.5f}<br>Time: %{x}<extra></extra>",
            ),
            row=1, col=1,
        )

    if len(short_entries) > 0:
        fig.add_trace(
            go.Scatter(
                x=short_entries["entry_time"],
                y=short_entries["entry_price"],
                mode="markers",
                name="Short Entry",
                marker=dict(symbol="triangle-down", color="#FF4D4F", size=12),
                hovertemplate="Short Entry<br>Price: %{y:.5f}<br>Time: %{x}<extra></extra>",
            ),
            row=1, col=1,
        )

    # Add exits
    if len(trades) > 0:
        exit_colors = ["#00FF00" if pnl > 0 else "#FF0000" for pnl in trades["pnl_currency"]]
        fig.add_trace(
            go.Scatter(
                x=trades["exit_time"],
                y=trades["exit_price"],
                mode="markers",
                name="Exit",
                marker=dict(symbol="x", color=exit_colors, size=10),
                text=[f"{reason}<br>P&L: {pnl:.2f}" for reason, pnl in
                      zip(trades["exit_reason"], trades["pnl_currency"])],
                hovertemplate="%{text}<br>Price: %{y:.5f}<br>Time: %{x}<extra></extra>",
            ),
            row=1, col=1,
        )

    # Add equity curve
    if len(equity) > 0:
        fig.add_trace(
            go.Scatter(
                x=equity["time"],
                y=equity["balance"],
                mode="lines",
                name="Equity",
                line=dict(color="#1890FF", width=2),
                hovertemplate="Balance: %{y:,.2f}<br>Time: %{x}<extra></extra>",
            ),
            row=2, col=1,
        )

    # Update layout
    summary = data["summary"]
    if len(summary) > 0:
        row = summary.iloc[0]
        default_title = f"{row.get('symbol', 'Backtest')} [{row.get('timeframe', '')}]"
    else:
        default_title = "Backtest Results"

    fig.update_layout(
        title=title or default_title,
        xaxis_rangeslider_visible=False,
        template="plotly_dark",
        height=900,
        showlegend=True,
        hovermode="x unified",
    )

    fig.update_xaxes(title_text="Time", row=2, col=1)
    fig.update_yaxes(title_text="Price", row=1, col=1)
    fig.update_yaxes(title_text="Equity", row=2, col=1)

    # Save to file
    fig.write_html(output_path, include_plotlyjs="cdn", full_html=True)
    return output_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Visualize backtest results from Excel file"
    )
    parser.add_argument(
        "excel_file",
        help="Path to Excel backtest results file",
    )
    parser.add_argument(
        "--start-date",
        help="Start date for filtering (YYYY-MM-DD or YYYY-MM-DD HH:MM:SS)",
    )
    parser.add_argument(
        "--end-date",
        help="End date for filtering (YYYY-MM-DD or YYYY-MM-DD HH:MM:SS)",
    )
    parser.add_argument(
        "--output",
        default="backtest_chart.html",
        help="Output HTML file path (default: backtest_chart.html)",
    )
    parser.add_argument(
        "--title",
        help="Custom chart title",
    )
    parser.add_argument(
        "--no-chart",
        action="store_true",
        help="Skip generating chart, only print summary",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """Main entry point."""
    args = parse_args(argv)

    # Check file exists
    if not Path(args.excel_file).exists():
        print(f"Error: File not found: {args.excel_file}", file=sys.stderr)
        raise SystemExit(1)

    print(f"Loading backtest data from {args.excel_file}...")
    try:
        data = load_backtest_excel(args.excel_file)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)

    # Print full summary
    print_summary(data, filtered=False)

    # Filter if dates specified
    if args.start_date or args.end_date:
        print(f"\nFiltering data...")
        if args.start_date:
            print(f"  Start: {args.start_date}")
        if args.end_date:
            print(f"  End: {args.end_date}")

        data = filter_by_date_range(data, args.start_date, args.end_date)
        print_summary(data, filtered=True)

    # Generate chart
    if not args.no_chart:
        print(f"\nGenerating interactive chart...")
        try:
            output_path = generate_html_chart(data, args.output, title=args.title)
            print(f"Chart saved to {output_path}")
            print(f"\nOpen {output_path} in your browser to view the interactive chart.")
        except (RuntimeError, ValueError) as exc:
            print(f"Error generating chart: {exc}", file=sys.stderr)
            raise SystemExit(1)


if __name__ == "__main__":
    main()
