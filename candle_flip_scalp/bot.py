from __future__ import annotations

import math
import sys
import time
import threading
from datetime import datetime, timezone
from typing import Optional

import MetaTrader5 as mt5

try:
    from .telemetry import BotTelemetry
    from .monitor_server import start_monitor_server, stop_monitor_server
    from .config import CandleFlipScalpConfig
except ImportError:  # pragma: no cover - script execution
    from telemetry import BotTelemetry
    from monitor_server import start_monitor_server, stop_monitor_server
    from config import CandleFlipScalpConfig

class CandleFlipScalpBot:
    """Trading bot that mirrors the Candle Flip scalp strategy from MQL5."""

    def __init__(self, config: CandleFlipScalpConfig) -> None:
        self.config = config
        self.symbol_info = None
        self.point = 0.0
        self.default_filling = (
            config.filling_mode
            if getattr(config, "filling_mode", None) is not None
            else mt5.ORDER_FILLING_RETURN
        )
        self.last_trade_time: Optional[float] = None
        self.curr_bar_time: Optional[int] = None
        self.flips_this_bar: int = 0
        self.telemetry = BotTelemetry(config.symbol, config.timeframe)
        self.telemetry.set_status("initializing")
        self.telemetry.update(
            default_filling=self._filling_mode_label(self.default_filling)
        )
        self._monitor_server = None

        # CRITICAL FIX: Thread safety for state management
        self._state_lock = threading.Lock()

        # CRITICAL FIX: Connection monitoring
        self._last_successful_tick: float = time.time()
        self._max_tick_silence_seconds: int = 60  # Alert if no data for 1 min
        self._consecutive_failures: int = 0

        # CRITICAL FIX: Account protection tracking
        self._initial_equity: Optional[float] = None
        self._equity_high_water_mark: Optional[float] = None
        self._consecutive_losses: int = 0
        self._daily_start_equity: Optional[float] = None
        self._last_daily_reset: Optional[str] = None

    def initialize(self) -> bool:
        if not mt5.initialize():
            message = f"MT5 initialize failed: {mt5.last_error()}"
            print(f"[init] {message}", file=sys.stderr)
            self.telemetry.record_error(message)
            self.telemetry.set_status("error")
            return False

        if self.config.login is not None:
            authorized = mt5.login(
                self.config.login,
                password=self.config.password,
                server=self.config.server,
            )
            if not authorized:
                message = f"MT5 login failed: {mt5.last_error()}"
                print(f"[init] {message}", file=sys.stderr)
                self.telemetry.record_error(message)
                self.telemetry.set_status("error")
                return False

        if not mt5.symbol_select(self.config.symbol, True):
            message = f"Failed to select symbol {self.config.symbol}"
            print(f"[init] {message}", file=sys.stderr)
            self.telemetry.record_error(message)
            self.telemetry.set_status("error")
            return False

        if not self._refresh_symbol_info():
            return False

        # Load historical bars for chart display (200 bars standard)
        self._load_historical_bars(200)

        # CRITICAL FIX: Initialize account protection tracking
        account = mt5.account_info()
        if account:
            self._initial_equity = account.equity
            self._equity_high_water_mark = account.equity
            self._daily_start_equity = account.equity
            self._last_daily_reset = datetime.now().strftime("%Y-%m-%d")
            print(f"[init] Initial account equity: ${account.equity:.2f}")
        else:
            print("[init] Warning: Could not fetch initial account info", file=sys.stderr)

        # CRITICAL FIX: Print configuration summary
        self._print_config_summary()

        print(f"[init] Bot ready on {self.config.symbol} timeframe {self.config.timeframe}")
        self.telemetry.set_status("ready")
        self._ensure_monitor_server()
        return True

    def shutdown(self) -> None:
        stop_monitor_server(self._monitor_server)
        self._monitor_server = None
        mt5.shutdown()
        self.telemetry.set_status("stopped")
        print("[shutdown] MT5 connection closed")

    def run(self) -> None:
        if not self.initialize():
            return

        self.telemetry.set_status("running")
        consecutive_errors = 0
        max_consecutive_errors = 20  # CRITICAL FIX: Shutdown after N consecutive errors

        try:
            while True:
                try:
                    self.process_tick()
                    consecutive_errors = 0  # CRITICAL FIX: Reset on success
                except KeyboardInterrupt:
                    raise  # Don't catch this
                except Exception as err:  # pragma: no cover - defensive logging
                    consecutive_errors += 1
                    message = f"Error #{consecutive_errors}: {err}"
                    print(f"[loop] {message}", file=sys.stderr)
                    self.telemetry.record_error(message)

                    # CRITICAL FIX: Emergency shutdown on persistent errors
                    if consecutive_errors >= max_consecutive_errors:
                        print(
                            f"\n[CRITICAL] {consecutive_errors} consecutive errors - SHUTTING DOWN",
                            file=sys.stderr,
                        )
                        self.telemetry.set_status("error_shutdown")
                        break

                    # CRITICAL FIX: Exponential backoff on errors
                    backoff = min(10, self.config.poll_interval * (2 ** consecutive_errors))
                    time.sleep(backoff)
                    continue

                time.sleep(self.config.poll_interval)
        except KeyboardInterrupt:
            print("\n[run] Interrupted by user")
            self.telemetry.set_status("stopping")
        finally:
            self.shutdown()

    def _ensure_monitor_server(self) -> None:
        if not getattr(self.config, "monitor_enabled", False):
            return
        if self._monitor_server is not None:
            return
        host = getattr(self.config, "monitor_host", "127.0.0.1")
        port = getattr(self.config, "monitor_port", 8765)
        try:
            self._monitor_server = start_monitor_server(self.telemetry, host, port)
        except OSError as exc:
            message = f"Monitor server failed on {host}:{port} ({exc})"
            print(f"[monitor] {message}", file=sys.stderr)
            self.telemetry.record_error(message)
            return
        display_host = "localhost" if host in ("0.0.0.0", "127.0.0.1") else host
        monitor_url = f"http://{display_host}:{port}"
        self.telemetry.set_monitor_url(monitor_url)
        print(f"[monitor] telemetry available at {monitor_url}")

    def process_tick(self) -> None:
        # CRITICAL FIX: Connection monitoring - track failures
        if not self._refresh_symbol_info():
            self._handle_tick_failure("symbol_info_failed")
            return

        rates = mt5.copy_rates_from_pos(self.config.symbol, self.config.timeframe, 0, 10)
        if rates is None or len(rates) < 2:
            self._handle_tick_failure("rates_fetch_failed")
            return

        # CRITICAL FIX: Connection monitoring - reset failure counter on success
        self._consecutive_failures = 0
        self._last_successful_tick = time.time()

        current_bar = rates[-1]
        current_bar_time = int(current_bar["time"])

        # CRITICAL FIX: Thread-safe bar state management
        with self._state_lock:
            if self.curr_bar_time != current_bar_time:
                self.curr_bar_time = current_bar_time
                self.flips_this_bar = 0

        candle_open = float(current_bar["open"])
        candle_high = float(current_bar["high"])
        candle_low = float(current_bar["low"])
        candle_close = float(current_bar["close"])
        bar_dt = datetime.fromtimestamp(current_bar_time, tz=timezone.utc)

        tick = mt5.symbol_info_tick(self.config.symbol)
        if tick is None or tick.ask <= 0 or tick.bid <= 0:
            return

        account = mt5.account_info()
        balance = getattr(account, "balance", None) if account else None
        equity = getattr(account, "equity", None) if account else None

        # CRITICAL FIX: Account protection check - stop trading if limits breached
        if not self._check_account_limits(account):
            # Account limits breached - update telemetry but don't trade
            self.telemetry.update_tick(
                bar_time=current_bar_time,
                tick_time=time.time(),
                bid=float(tick.bid),
                ask=float(tick.ask),
                mid_price=(tick.ask + tick.bid) * 0.5,
                spread_points=(tick.ask - tick.bid) / self.point if self.point > 0 else None,
                flips_this_bar=self.flips_this_bar,
                position_dir=self.get_position_dir(),
                decision="risk_limit_stop",
            )
            return

        mid_price = (tick.ask + tick.bid) * 0.5
        threshold = self.config.hysteresis_points * self.point

        want_long = mid_price > candle_open + threshold
        want_short = mid_price < candle_open - threshold
        spread_points = (tick.ask - tick.bid) / self.point if self.point > 0 else None
        now = time.time()
        position_dir = self.get_position_dir()
        final_position_dir = position_dir
        trade_event = None
        decision = "signal-long" if want_long else "signal-short" if want_short else "idle"

        if not (want_long or want_short):
            self.telemetry.update_tick(
                bar_time=current_bar_time,
                tick_time=now,
                bid=float(tick.bid),
                ask=float(tick.ask),
                mid_price=mid_price,
                spread_points=spread_points,
                flips_this_bar=self.flips_this_bar,
                position_dir=position_dir,
                decision=decision,
            )
            self.telemetry.update_equity(now, balance, equity)
            self.telemetry.update_or_append_bar(
                time=current_bar_time,
                open_price=candle_open,
                high=candle_high,
                low=candle_low,
                close=candle_close,
                decision=decision,
                position_label={1: "long", -1: "short", 0: "flat"}.get(position_dir, "flat"),
                trade=None,
                position_dir=position_dir,
            )
            return

        # CRITICAL FIX: Thread-safe cooldown check
        with self._state_lock:
            cooldown_active = self.last_trade_time is not None and (now - self.last_trade_time) < self.config.cooldown_seconds
            flips_exceeded = self.flips_this_bar >= self.config.max_flips_per_bar

        if cooldown_active:
            # HIGH PRIORITY FIX: Log cooldown block
            remaining = self.config.cooldown_seconds - (now - self.last_trade_time)
            print(f"[decision] COOLDOWN: {remaining:.1f}s remaining")
            self.telemetry.update(last_decision="cooldown")
            self.telemetry.update_tick(
                bar_time=current_bar_time,
                tick_time=now,
                bid=float(tick.bid),
                ask=float(tick.ask),
                mid_price=mid_price,
                spread_points=spread_points,
                flips_this_bar=self.flips_this_bar,
                position_dir=position_dir,
                decision=decision,
            )
            self.telemetry.update_equity(now, balance, equity)
            self.telemetry.update_or_append_bar(
                time=current_bar_time,
                open_price=candle_open,
                high=candle_high,
                low=candle_low,
                close=candle_close,
                decision="cooldown",
                position_label={1: "long", -1: "short", 0: "flat"}.get(position_dir, "flat"),
                trade=None,
                position_dir=position_dir,
            )
            return

        if flips_exceeded:
            # HIGH PRIORITY FIX: Log max flips block
            print(f"[decision] MAX FLIPS: {self.flips_this_bar}/{self.config.max_flips_per_bar} reached")
            self.telemetry.update(last_decision="max-flips")
            self.telemetry.update_tick(
                bar_time=current_bar_time,
                tick_time=now,
                bid=float(tick.bid),
                ask=float(tick.ask),
                mid_price=mid_price,
                spread_points=spread_points,
                flips_this_bar=self.flips_this_bar,
                position_dir=position_dir,
                decision=decision,
            )
            self.telemetry.update_equity(now, balance, equity)
            self.telemetry.update_or_append_bar(
                time=current_bar_time,
                open_price=candle_open,
                high=candle_high,
                low=candle_low,
                close=candle_close,
                decision="max-flips",
                position_label={1: "long", -1: "short", 0: "flat"}.get(position_dir, "flat"),
                trade=None,
                position_dir=position_dir,
            )
            return

        if want_long:
            if position_dir == -1:
                if self.close_if_dir(-1, tick) and self.place_order(1, tick):
                    self._record_trade(now, 1)
                    final_position_dir = 1
                    trade_event = {
                        "direction": "long",
                        "type": "flip",
                        "price": tick.ask,
                        "time": bar_dt.isoformat(),
                    }
            elif position_dir == 0:
                if self.place_order(1, tick):
                    self._record_trade(now, 1)
                    final_position_dir = 1
                    trade_event = {
                        "direction": "long",
                        "type": "entry",
                        "price": tick.ask,
                        "time": bar_dt.isoformat(),
                    }
        elif want_short:
            if position_dir == 1:
                if self.close_if_dir(1, tick) and self.place_order(-1, tick):
                    self._record_trade(now, -1)
                    final_position_dir = -1
                    trade_event = {
                        "direction": "short",
                        "type": "flip",
                        "price": tick.bid,
                        "time": bar_dt.isoformat(),
                    }
            elif position_dir == 0:
                if self.place_order(-1, tick):
                    self._record_trade(now, -1)
                    final_position_dir = -1
                    trade_event = {
                        "direction": "short",
                        "type": "entry",
                        "price": tick.bid,
                        "time": bar_dt.isoformat(),
                    }

        self.telemetry.update_tick(
            bar_time=current_bar_time,
            tick_time=now,
            bid=float(tick.bid),
            ask=float(tick.ask),
            mid_price=mid_price,
            spread_points=spread_points,
            flips_this_bar=self.flips_this_bar,
            position_dir=final_position_dir,
            decision=decision,
        )
        self.telemetry.update_equity(now, balance, equity)
        self.telemetry.update_or_append_bar(
            time=current_bar_time,
            open_price=candle_open,
            high=candle_high,
            low=candle_low,
            close=candle_close,
            decision=decision,
            position_label={1: "long", -1: "short", 0: "flat"}.get(final_position_dir, "flat"),
            trade=trade_event,
            position_dir=final_position_dir,
        )

        if not trade_event:
            return

    def calc_lots_by_risk(self, sl_points: int) -> float:
        if not self.config.use_risk_percent or sl_points <= 0:
            return self.config.fixed_lots

        account = mt5.account_info()
        if account is None:
            print("[risk] account_info unavailable", file=sys.stderr)
            return self.config.fixed_lots

        # HIGH PRIORITY FIX: Use equity (not balance) for risk calculation
        # Equity accounts for current open P&L, more accurate
        equity = account.equity
        if equity <= 0:
            print(f"[risk] Invalid equity: {equity}", file=sys.stderr)
            return self.config.fixed_lots

        # HIGH PRIORITY FIX: Check minimum balance threshold
        min_balance = getattr(self.config, "min_balance_to_trade", 100.0)
        if equity < min_balance:
            print(f"[risk] Equity ${equity:.2f} below minimum ${min_balance:.2f}", file=sys.stderr)
            self.telemetry.record_error(f"Equity below minimum trading threshold")
            return 0.0  # Return 0 to prevent trading

        tick_value = getattr(self.symbol_info, "trade_tick_value", 0.0)
        tick_size = getattr(self.symbol_info, "trade_tick_size", 0.0)
        if tick_value <= 0 or tick_size <= 0 or self.point <= 0:
            return self.config.fixed_lots

        # Use equity instead of balance
        risk_money = equity * (self.config.risk_percent / 100.0)
        value_per_point = (tick_value / tick_size) * self.point
        if value_per_point <= 0:
            return self.config.fixed_lots

        raw_lots = risk_money / (sl_points * value_per_point)

        # HIGH PRIORITY FIX: Safer fallback for volume_max
        volume_step = self.symbol_info.volume_step
        volume_min = self.symbol_info.volume_min
        volume_max = self.symbol_info.volume_max

        if not volume_step or not volume_min or not volume_max:
            print("[risk] Symbol info incomplete - using fixed lots", file=sys.stderr)
            return self.config.fixed_lots

        steps = math.floor(raw_lots / volume_step)
        lots = steps * volume_step

        # Clamp to valid range
        lots = max(volume_min, min(volume_max, lots))

        # HIGH PRIORITY FIX: Log calculated volume for debugging
        print(f"[risk] Calculated: equity=${equity:.2f}, risk={self.config.risk_percent}%, "
              f"sl={sl_points}pts → {lots:.2f} lots", file=sys.stderr)

        return round(lots, 8)

    def _refresh_symbol_info(self) -> bool:
        info = mt5.symbol_info(self.config.symbol)
        if info is None:
            message = f"Unable to retrieve info for {self.config.symbol}"
            print(f"[symbol] {message}", file=sys.stderr)
            self.telemetry.record_error(message)
            return False

        self.symbol_info = info
        self.point = info.point or 0.0

        if self.config.filling_mode is not None:
            self.default_filling = self.config.filling_mode
        else:
            filling_mode = getattr(info, "filling_mode", None)
            if filling_mode in (
                mt5.ORDER_FILLING_FOK,
                mt5.ORDER_FILLING_IOC,
                mt5.ORDER_FILLING_RETURN,
            ):
                self.default_filling = filling_mode
            else:
                self.default_filling = mt5.ORDER_FILLING_RETURN

        if self.point <= 0:
            message = f"Invalid point size for {self.config.symbol}"
            print(f"[symbol] {message}", file=sys.stderr)
            self.telemetry.record_error(message)
            return False

        self.telemetry.update(
            point=self.point,
            default_filling=self._filling_mode_label(self.default_filling),
            spread_limit=self.config.max_spread_points,
        )
        return True

    def _load_historical_bars(self, count: int = 200) -> None:
        """Load historical bars from MT5 for chart display."""
        try:
            # Get historical rates from MT5
            rates = mt5.copy_rates_from_pos(
                self.config.symbol,
                self.config.timeframe,
                0,  # Start from current bar
                count  # Number of bars to load
            )

            if rates is None or len(rates) == 0:
                print(f"[init] Warning: No historical bars loaded from MT5", file=sys.stderr)
                return

            # Append each bar to telemetry (oldest first)
            for rate in rates:
                self.telemetry.append_bar(
                    time=rate['time'],
                    open_price=rate['open'],
                    high=rate['high'],
                    low=rate['low'],
                    close=rate['close'],
                    decision=None,  # Historical bars have no decision
                    position_label=None,
                    position_dir=None,
                )

            print(f"[init] Loaded {len(rates)} historical bars for chart display")
        except Exception as e:
            print(f"[init] Error loading historical bars: {e}", file=sys.stderr)

    def _record_trade(self, timestamp: float, direction: int) -> None:
        """HIGH PRIORITY FIX: Enhanced trade recording with comprehensive logging."""
        # CRITICAL FIX: Thread-safe state updates
        with self._state_lock:
            self.last_trade_time = timestamp
            self.flips_this_bar += 1

        # HIGH PRIORITY FIX: Comprehensive audit trail logging
        account = mt5.account_info()
        positions = mt5.positions_get(symbol=self.config.symbol)
        position = None
        if positions:
            our_positions = [p for p in positions if p.magic == self.config.magic]
            if our_positions:
                position = our_positions[0]

        direction_str = {1: "LONG", -1: "SHORT", 0: "FLAT"}.get(direction, "UNKNOWN")
        timestamp_str = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")

        log_entry = f"\n{'='*70}\n"
        log_entry += f"[TRADE] {timestamp_str} - {direction_str} EXECUTED\n"
        log_entry += f"{'='*70}\n"
        log_entry += f"  Symbol: {self.config.symbol}\n"
        log_entry += f"  Flip count this bar: {self.flips_this_bar}\n"

        if position:
            log_entry += f"  Position:\n"
            log_entry += f"    Ticket: {position.ticket}\n"
            log_entry += f"    Volume: {position.volume}\n"
            log_entry += f"    Entry: {position.price_open}\n"
            log_entry += f"    SL: {position.sl}\n"
            log_entry += f"    TP: {position.tp}\n"
            log_entry += f"    Current P&L: ${position.profit:.2f}\n"

        if account:
            log_entry += f"  Account:\n"
            log_entry += f"    Balance: ${account.balance:.2f}\n"
            log_entry += f"    Equity: ${account.equity:.2f}\n"
            log_entry += f"    Margin: ${account.margin:.2f}\n"
            log_entry += f"    Free margin: ${account.margin_free:.2f}\n"

        log_entry += f"{'='*70}\n"
        print(log_entry)

        self.telemetry.record_trade(timestamp, direction)
        self.telemetry.update(last_decision="executed")

    def get_position_dir(self) -> int:
        # CRITICAL FIX: Filter positions by magic number
        positions = mt5.positions_get(symbol=self.config.symbol)
        if positions is None:
            return 0

        # Filter by magic number (only this bot's positions)
        our_positions = [p for p in positions if p.magic == self.config.magic]

        if len(our_positions) == 0:
            return 0

        if len(our_positions) > 1:
            # CRITICAL: Multiple positions detected!
            self.telemetry.record_error(
                f"Multiple positions detected for magic {self.config.magic}: {len(our_positions)}"
            )
            # Return position with largest volume (most significant)
            our_positions.sort(key=lambda p: p.volume, reverse=True)

        position = our_positions[0]
        if position.type == mt5.POSITION_TYPE_BUY:
            return 1
        elif position.type == mt5.POSITION_TYPE_SELL:
            return -1
        else:
            self.telemetry.record_error(f"Unknown position type: {position.type}")
            return 0

    def close_if_dir(self, dir_to_close: int, tick) -> bool:
        # CRITICAL FIX: Filter positions by magic number
        positions = mt5.positions_get(symbol=self.config.symbol)
        if positions is None:
            error = mt5.last_error()
            if error != (0, ''):
                self.telemetry.record_error(f"positions_get failed: {error}")
            return True

        # Filter by magic number
        our_positions = [p for p in positions if p.magic == self.config.magic]

        if len(our_positions) == 0:
            return True

        if len(our_positions) > 1:
            # CRITICAL: Multiple positions detected!
            self.telemetry.record_error(
                f"Multiple positions detected for magic {self.config.magic}: {len(our_positions)}"
            )
            # Use largest position
            our_positions.sort(key=lambda p: p.volume, reverse=True)

        position = our_positions[0]
        if dir_to_close == 1 and position.type != mt5.POSITION_TYPE_BUY:
            return True
        if dir_to_close == -1 and position.type != mt5.POSITION_TYPE_SELL:
            return True

        price = tick.bid if position.type == mt5.POSITION_TYPE_BUY else tick.ask
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.config.symbol,
            "volume": position.volume,
            "type": (
                mt5.ORDER_TYPE_SELL
                if position.type == mt5.POSITION_TYPE_BUY
                else mt5.ORDER_TYPE_BUY
            ),
            "position": position.ticket,
            "price": price,
            "deviation": self.config.deviation,
            "magic": self.config.magic,
            "comment": "Flip close",
            "type_time": mt5.ORDER_TIME_GTC,
        }

        success = self._send_order_with_fallback(request, "close")

        # CRITICAL FIX: Verify position actually closed
        if success:
            time.sleep(0.1)  # Brief delay for MT5 state update
            new_position_dir = self.get_position_dir()
            if new_position_dir != 0:
                error_msg = (
                    f"Close order reported success but position still exists. "
                    f"Expected: flat, got: {new_position_dir}"
                )
                self.telemetry.record_error(error_msg)
                print(f"[close] {error_msg}", file=sys.stderr)
                return False  # Close failed despite success report

        return success

    def place_order(self, direction: int, tick) -> bool:
        ask = tick.ask
        bid = tick.bid
        if ask <= 0 or bid <= 0 or self.point <= 0:
            self.telemetry.update(last_decision="invalid-pricing")
            return False

        # CRITICAL FIX: Validate spread defensively
        if ask <= bid:
            self.telemetry.record_error(f"Invalid quote: ask={ask} bid={bid}")
            return False

        spread_points = (ask - bid) / self.point
        if spread_points > self.config.max_spread_points:
            # HIGH PRIORITY FIX: Log spread block
            print(f"[decision] SPREAD BLOCK: {spread_points:.1f} pts > max {self.config.max_spread_points} pts")
            self.telemetry.update(
                last_decision="spread-block", spread_points=spread_points
            )
            return False

        # CRITICAL FIX: Check for abnormal spreads (feed issue indicator)
        if spread_points > self.config.max_spread_points * 3:
            self.telemetry.record_error(f"Abnormal spread detected: {spread_points:.1f} points")
            return False

        slp = max(1, self.config.sl_points)
        tpp = max(1, self.config.tp_points)
        volume = self.calc_lots_by_risk(slp)

        # CRITICAL FIX: Strict volume validation
        volume = self._validate_volume(volume)
        if volume is None or volume <= 0:
            self.telemetry.update(last_decision="volume-invalid")
            return False

        if direction == 1:
            # HIGH PRIORITY FIX: Calculate and validate SL/TP for long
            sl_raw = bid - slp * self.point
            tp_raw = bid + tpp * self.point
            sl = self._normalize_price(sl_raw)
            tp = self._normalize_price(tp_raw)

            # HIGH PRIORITY FIX: Validate SL/TP placement
            if not self._validate_sl_tp(ask, sl, tp, is_buy=True):
                self.telemetry.update(last_decision="sl-tp-invalid")
                return False

            order_type = mt5.ORDER_TYPE_BUY
            price = ask
            comment = "Flip long"
        elif direction == -1:
            # HIGH PRIORITY FIX: Calculate and validate SL/TP for short
            sl_raw = ask + slp * self.point
            tp_raw = ask - tpp * self.point
            sl = self._normalize_price(sl_raw)
            tp = self._normalize_price(tp_raw)

            # HIGH PRIORITY FIX: Validate SL/TP placement
            if not self._validate_sl_tp(bid, sl, tp, is_buy=False):
                self.telemetry.update(last_decision="sl-tp-invalid")
                return False

            order_type = mt5.ORDER_TYPE_SELL
            price = bid
            comment = "Flip short"
        else:
            return False

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.config.symbol,
            "volume": volume,
            "type": order_type,
            "price": price,
            "sl": sl,
            "tp": tp,
            "deviation": self.config.deviation,
            "magic": self.config.magic,
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
        }

        success = self._send_order_with_fallback(request, "order")

        # CRITICAL FIX: Verify position actually opened
        if success:
            time.sleep(0.1)  # Brief delay for MT5 state update
            new_position_dir = self.get_position_dir()
            if new_position_dir != direction:
                error_msg = (
                    f"Order reported success but position not found. "
                    f"Expected: {direction}, got: {new_position_dir}"
                )
                self.telemetry.record_error(error_msg)
                print(f"[order] {error_msg}", file=sys.stderr)
                return False  # Order failed despite success report

        return success

    def _filling_mode_candidates(self) -> list[int]:
        candidates: list[int] = []

        def add(mode: Optional[int]) -> None:
            if mode is None:
                return
            if mode in (
                mt5.ORDER_FILLING_FOK,
                mt5.ORDER_FILLING_IOC,
                mt5.ORDER_FILLING_RETURN,
            ):
                if mode not in candidates:
                    candidates.append(mode)

        add(getattr(self.config, "filling_mode", None))
        add(getattr(self.symbol_info, "filling_mode", None))
        add(self.default_filling)

        for fallback in (
            mt5.ORDER_FILLING_RETURN,
            mt5.ORDER_FILLING_IOC,
            mt5.ORDER_FILLING_FOK,
        ):
            add(fallback)

        return candidates

    def _send_order_with_fallback(self, base_request: dict, context: str) -> bool:
        """HIGH PRIORITY FIX: Enhanced order submission with detailed logging."""
        # HIGH PRIORITY FIX: Log order details before submission
        self._log_order_details(base_request, context, "SUBMITTING")

        last_result = None
        last_error = None
        unsupported_retcode = getattr(mt5, "TRADE_RETCODE_INVALID_FILLING_MODE", 10030)
        for mode in self._filling_mode_candidates():
            request = dict(base_request)
            request["type_filling"] = mode
            result = mt5.order_send(request)
            if result is None:
                last_error = mt5.last_error()
                self.telemetry.record_order_attempt(
                    context=context,
                    filling_mode=self._filling_mode_label(mode),
                    success=False,
                    retcode=None,
                    comment=self._format_mt5_error(last_error),
                )
                continue
            success = result.retcode == mt5.TRADE_RETCODE_DONE
            self.telemetry.record_order_attempt(
                context=context,
                filling_mode=self._filling_mode_label(mode),
                success=success,
                retcode=result.retcode,
                comment=result.comment,
            )
            if success:
                # HIGH PRIORITY FIX: Log successful order with details
                self._log_order_result(request, result, context, "SUCCESS")
                self.default_filling = mode
                self.telemetry.update(
                    default_filling=self._filling_mode_label(mode),
                    last_decision=f"{context}-sent",
                )
                return True
            last_result = result
            if result.retcode not in (unsupported_retcode, 10030):
                break

        # HIGH PRIORITY FIX: Log failed order with details
        if last_result is None:
            message = f"[{context}] order_send failed: {self._format_mt5_error(last_error or mt5.last_error())}"
            print(message, file=sys.stderr)
            self.telemetry.record_error(message)
        else:
            self._log_order_result(base_request, last_result, context, "FAILED")
            message = f"[{context}] retcode={last_result.retcode} comment={last_result.comment}"
            print(message, file=sys.stderr)
            self.telemetry.record_error(message)
        self.telemetry.update(last_decision="order-failed")
        return False

    @staticmethod
    def _filling_mode_label(mode: Optional[int]) -> str:
        mapping = {
            mt5.ORDER_FILLING_RETURN: "RETURN",
            mt5.ORDER_FILLING_IOC: "IOC",
            mt5.ORDER_FILLING_FOK: "FOK",
        }
        if mode is None:
            return "AUTO"
        return mapping.get(mode, str(mode))

    @staticmethod
    def _format_mt5_error(err) -> str:
        if isinstance(err, tuple) and len(err) >= 2:
            return f"{err[0]} {err[1]}"
        return str(err)

    # ========================================
    # CRITICAL FIX: New helper methods
    # ========================================

    def _validate_volume(self, volume: float) -> Optional[float]:
        """CRITICAL FIX: Validate and clamp volume to broker limits. Returns None if invalid."""
        if volume <= 0:
            self.telemetry.record_error(f"Volume is non-positive: {volume}")
            return None

        if not self.symbol_info:
            self.telemetry.record_error("Symbol info incomplete - cannot validate volume")
            return None

        vol_min = self.symbol_info.volume_min
        vol_max = self.symbol_info.volume_max
        vol_step = self.symbol_info.volume_step

        # Check minimums
        if not vol_min or not vol_max or not vol_step:
            self.telemetry.record_error("Symbol info incomplete - cannot validate volume")
            return None

        if volume < vol_min:
            self.telemetry.record_error(f"Volume {volume} below minimum {vol_min}")
            return None

        if volume > vol_max:
            self.telemetry.record_error(f"Volume {volume} exceeds maximum {vol_max}")
            return None

        # Round to step
        steps = round(volume / vol_step)
        rounded = steps * vol_step

        # Final clamp
        validated = max(vol_min, min(vol_max, rounded))

        # Log if volume was adjusted
        if abs(validated - volume) > vol_step / 2:
            print(
                f"[volume] Adjusted volume from {volume:.2f} to {validated:.2f}",
                file=sys.stderr
            )

        return validated

    def _normalize_price(self, price: float) -> float:
        """HIGH PRIORITY FIX: Normalize price to valid tick size."""
        if not self.symbol_info:
            return price

        tick_size = self.symbol_info.trade_tick_size
        if not tick_size or tick_size <= 0:
            return price

        # Round to nearest tick
        ticks = round(price / tick_size)
        normalized = ticks * tick_size

        return normalized

    def _validate_sl_tp(self, entry_price: float, sl: float, tp: float, is_buy: bool) -> bool:
        """HIGH PRIORITY FIX: Validate SL/TP prices are valid and meet broker requirements."""
        if not self.symbol_info:
            self.telemetry.record_error("Symbol info missing - cannot validate SL/TP")
            return False

        # Check SL/TP are positive
        if sl <= 0 or tp <= 0:
            self.telemetry.record_error(f"SL/TP must be positive: sl={sl}, tp={tp}")
            return False

        # Check SL/TP are on correct side of entry
        if is_buy:
            # Long: SL below entry, TP above entry
            if sl >= entry_price:
                self.telemetry.record_error(f"Long SL {sl} not below entry {entry_price}")
                return False
            if tp <= entry_price:
                self.telemetry.record_error(f"Long TP {tp} not above entry {entry_price}")
                return False
        else:
            # Short: SL above entry, TP below entry
            if sl <= entry_price:
                self.telemetry.record_error(f"Short SL {sl} not above entry {entry_price}")
                return False
            if tp >= entry_price:
                self.telemetry.record_error(f"Short TP {tp} not below entry {entry_price}")
                return False

        # Check minimum stop distance (SYMBOL_TRADE_STOPS_LEVEL)
        stops_level = self.symbol_info.trade_stops_level
        if stops_level and stops_level > 0:
            min_distance = stops_level * self.point

            sl_distance = abs(entry_price - sl)
            tp_distance = abs(entry_price - tp)

            if sl_distance < min_distance:
                self.telemetry.record_error(
                    f"SL distance {sl_distance:.5f} below minimum {min_distance:.5f}"
                )
                return False

            if tp_distance < min_distance:
                self.telemetry.record_error(
                    f"TP distance {tp_distance:.5f} below minimum {min_distance:.5f}"
                )
                return False

        # Check for obviously wrong values (sanity check)
        max_reasonable_distance = 1000 * self.point  # 1000 points
        if abs(entry_price - sl) > max_reasonable_distance:
            self.telemetry.record_error(
                f"SL distance suspiciously large: {abs(entry_price - sl):.5f}"
            )
            return False

        if abs(entry_price - tp) > max_reasonable_distance:
            self.telemetry.record_error(
                f"TP distance suspiciously large: {abs(entry_price - tp):.5f}"
            )
            return False

        return True

    def _log_order_details(self, request: dict, context: str, status: str) -> None:
        """HIGH PRIORITY FIX: Log comprehensive order details."""
        order_type_map = {
            mt5.ORDER_TYPE_BUY: "BUY",
            mt5.ORDER_TYPE_SELL: "SELL",
        }
        order_type = order_type_map.get(request.get("type"), "UNKNOWN")

        log_msg = f"\n[ORDER {status}] {context.upper()}\n"
        log_msg += f"  Type: {order_type}\n"
        log_msg += f"  Symbol: {request.get('symbol')}\n"
        log_msg += f"  Volume: {request.get('volume')}\n"
        log_msg += f"  Price: {request.get('price')}\n"

        if request.get("sl"):
            log_msg += f"  SL: {request.get('sl')}\n"
        if request.get("tp"):
            log_msg += f"  TP: {request.get('tp')}\n"

        log_msg += f"  Magic: {request.get('magic')}\n"
        log_msg += f"  Comment: {request.get('comment')}\n"

        print(log_msg)

    def _log_order_result(self, request: dict, result, context: str, status: str) -> None:
        """HIGH PRIORITY FIX: Log order execution result details."""
        log_msg = f"\n[ORDER {status}] {context.upper()}\n"
        log_msg += f"  Retcode: {result.retcode}\n"
        log_msg += f"  Comment: {result.comment}\n"

        if hasattr(result, 'order') and result.order:
            log_msg += f"  Order ticket: {result.order}\n"
        if hasattr(result, 'deal') and result.deal:
            log_msg += f"  Deal ticket: {result.deal}\n"
        if hasattr(result, 'volume') and result.volume:
            log_msg += f"  Volume executed: {result.volume}\n"
        if hasattr(result, 'price') and result.price:
            log_msg += f"  Execution price: {result.price}\n"
        if hasattr(result, 'bid') and result.bid:
            log_msg += f"  Bid: {result.bid}\n"
        if hasattr(result, 'ask') and result.ask:
            log_msg += f"  Ask: {result.ask}\n"

        print(log_msg)

    def _handle_tick_failure(self, reason: str) -> None:
        """Handle failed tick processing with connection monitoring."""
        self._consecutive_failures += 1
        silence_duration = time.time() - self._last_successful_tick

        error_detail = mt5.last_error()
        error_msg = (
            f"Tick processing failed: {reason}, "
            f"failures={self._consecutive_failures}, "
            f"silent={silence_duration:.0f}s, "
            f"mt5_error={error_detail}"
        )
        self.telemetry.record_error(error_msg)

        # Emergency shutdown if too many failures
        if self._consecutive_failures >= 10:
            print(
                f"\n[CRITICAL] {self._consecutive_failures} consecutive tick failures - SHUTTING DOWN",
                file=sys.stderr,
            )
            self.telemetry.set_status("error_connection_lost")
            raise SystemExit(1)

        # Alert if silent too long
        if silence_duration > self._max_tick_silence_seconds:
            print(
                f"\n[WARNING] No data for {silence_duration:.0f}s - possible connection loss",
                file=sys.stderr,
            )
            self.telemetry.set_status("warning_no_data")

    def _check_account_limits(self, account) -> bool:
        """Check if account protection limits are breached. Returns False if trading should stop."""
        if not account:
            return True  # Can't check, allow trading (will fail anyway if account unavailable)

        current_equity = account.equity

        # Update high water mark
        if self._equity_high_water_mark is None or current_equity > self._equity_high_water_mark:
            self._equity_high_water_mark = current_equity

        # Check for daily reset
        today = datetime.now().strftime("%Y-%m-%d")
        if self._last_daily_reset != today:
            self._daily_start_equity = current_equity
            self._last_daily_reset = today

        # Check max drawdown from high water mark
        if self._equity_high_water_mark and current_equity < self._equity_high_water_mark:
            drawdown_pct = (
                (self._equity_high_water_mark - current_equity) / self._equity_high_water_mark
            ) * 100

            max_dd = getattr(self.config, "max_drawdown_percent", 10.0)
            if drawdown_pct > max_dd:
                error_msg = (
                    f"Max drawdown exceeded: {drawdown_pct:.2f}% "
                    f"(limit: {max_dd}%, from peak: ${self._equity_high_water_mark:.2f})"
                )
                print(f"\n[CRITICAL] {error_msg}", file=sys.stderr)
                self.telemetry.record_error(error_msg)
                self.telemetry.set_status("stopped_risk_limit")
                return False

        # Check daily loss limit
        if self._daily_start_equity and current_equity < self._daily_start_equity:
            daily_loss_pct = ((self._daily_start_equity - current_equity) / self._daily_start_equity) * 100

            max_daily = getattr(self.config, "max_daily_loss_percent", 5.0)
            if daily_loss_pct > max_daily:
                error_msg = (
                    f"Max daily loss exceeded: {daily_loss_pct:.2f}% "
                    f"(limit: {max_daily}%, from start: ${self._daily_start_equity:.2f})"
                )
                print(f"\n[CRITICAL] {error_msg}", file=sys.stderr)
                self.telemetry.record_error(error_msg)
                self.telemetry.set_status("stopped_daily_loss")
                return False

        # Check minimum equity threshold
        if self._initial_equity:
            equity_pct = (current_equity / self._initial_equity) * 100
            min_equity = getattr(self.config, "min_equity_percent", 80.0)

            if equity_pct < min_equity:
                error_msg = (
                    f"Equity below minimum: {equity_pct:.1f}% "
                    f"(limit: {min_equity}%, initial: ${self._initial_equity:.2f})"
                )
                print(f"\n[CRITICAL] {error_msg}", file=sys.stderr)
                self.telemetry.record_error(error_msg)
                self.telemetry.set_status("stopped_min_equity")
                return False

        # Check consecutive losses (track via telemetry)
        max_losses = getattr(self.config, "max_consecutive_losses", 5)
        consecutive_losses = getattr(self.telemetry, "_consecutive_losses", 0)

        if consecutive_losses >= max_losses:
            error_msg = f"Max consecutive losses reached: {consecutive_losses} (limit: {max_losses})"
            print(f"\n[CRITICAL] {error_msg}", file=sys.stderr)
            self.telemetry.record_error(error_msg)
            self.telemetry.set_status("stopped_loss_streak")
            return False

        return True

    def _print_config_summary(self) -> None:
        """Print configuration summary at startup for verification."""
        print("\n" + "=" * 50)
        print("[config] Trading Configuration Summary")
        print("=" * 50)
        print(f"  Symbol: {self.config.symbol}")
        print(f"  Timeframe: {self.config.timeframe}")
        print(f"  Position sizing: {'Risk-based' if self.config.use_risk_percent else 'Fixed lots'}")

        if self.config.use_risk_percent:
            print(f"    Risk per trade: {self.config.risk_percent}%")
        else:
            print(f"    Fixed lots: {self.config.fixed_lots}")

        print(f"  Stop Loss: {self.config.sl_points} points")
        print(f"  Take Profit: {self.config.tp_points} points")
        print(f"  Hysteresis: {self.config.hysteresis_points} points")
        print(f"  Max spread: {self.config.max_spread_points} points")
        print(f"  Cooldown: {self.config.cooldown_seconds}s")
        print(f"  Max flips/bar: {self.config.max_flips_per_bar}")
        print(f"  Magic number: {self.config.magic}")

        # Account protection limits
        print("\n  Account Protection:")
        print(f"    Max drawdown: {getattr(self.config, 'max_drawdown_percent', 10.0)}%")
        print(f"    Max daily loss: {getattr(self.config, 'max_daily_loss_percent', 5.0)}%")
        print(f"    Min equity: {getattr(self.config, 'min_equity_percent', 80.0)}%")
        print(f"    Max consecutive losses: {getattr(self.config, 'max_consecutive_losses', 5)}")
        print("=" * 50 + "\n")

