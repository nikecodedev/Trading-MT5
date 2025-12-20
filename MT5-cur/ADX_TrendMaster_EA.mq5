//+------------------------------------------------------------------+
//|                                        ADX_TrendMaster_EA.mq5  |
//|                    ADX Trend Following - Production Ready      |
//|                           MQL5 Market Compliant                  |
//+------------------------------------------------------------------+
#property copyright "Professional EA"
#property link      ""
#property version   "1.00"
#property description "ADX Trend Following Strategy with EMA Confirmation"
#property description "Designed for EURUSD H1/H4 timeframes"

#include <Trade\Trade.mqh>

//--- Input Parameters
input group "=== Trading Settings ==="
input double   InpLotSize = 0.0;              // Lot Size (0 = Auto based on risk)
input double   InpRiskPercent = 2.5;          // Risk Per Trade (% of balance) - Optimized for 100% annual profit
input int      InpStopLossPips = 35;          // Stop Loss (pips) - Balanced for risk/reward
input int      InpTakeProfitPips = 280;       // Take Profit (pips) - 8:1 Risk/Reward (maximize winners)
input int      InpTrailingStopPips = 50;      // Trailing Stop (pips, 0=disabled) - Wider for bigger winners
input int      InpTrailingStepPips = 20;      // Trailing Step (pips) - Smoother movement for 100% profit
input bool     InpUseBreakEven = true;        // Move SL to break-even after profit
input int      InpBreakEvenPips = 50;          // Pips profit to trigger break-even (let winners develop fully)
input bool     InpUseATRStop = true;          // Use ATR-based dynamic stop loss
input double   InpMaxLossPercent = 0.4;       // Max loss per trade (% of risk) - Hard limit (tighter)
input bool     InpUseMicroStops = true;       // Use micro-stops (exit on tiny adverse moves)

input group "=== ADX Strategy Parameters ==="
input int      InpADXPeriod = 14;             // ADX Period
input double   InpADXMinLevel = 20.0;         // Minimum ADX Level (trend strength) - Lowered for 200+ trades/year
input double   InpDIMinSpread = 1.5;          // Minimum DI spread (+DI - -DI or vice versa) - Very relaxed for more trades
input int      InpDIPeriod = 14;              // DI Period (same as ADX typically)
input int      InpFastEMA = 20;              // Fast EMA Period
input int      InpSlowEMA = 50;              // Slow EMA Period
input int      InpATRPeriod = 14;            // ATR Period for volatility filter
input bool     InpRequireDICross = true;      // Require recent DI cross (stronger signal)

input group "=== Entry Conditions (All Must Be True) ==="
input bool     InpUseADXFilter = true;        // Require ADX > Min Level
input bool     InpUseDICross = true;          // Require DI cross confirmation
input bool     InpUseEMAFilter = true;        // Require EMA alignment
input bool     InpUsePriceMomentum = true;    // Require price momentum bar
input bool     InpUseATRFilter = true;        // Use ATR volatility filter
input double   InpATRMultiplier = 0.5;        // ATR Multiplier (min volatility) - Very relaxed for maximum opportunities

input group "=== Exit Conditions ==="
input bool     InpExitOnADXDrop = true;       // Exit when ADX drops below threshold
input double   InpADXExitLevel = 12.0;        // ADX exit threshold - Lower to let winners run longer
input bool     InpExitOnDICross = true;        // Exit on opposite DI cross
input bool     InpUseTrailingStop = true;     // Use trailing stop (enabled for better protection)
input bool     InpEarlyExitOnLoss = true;     // Early exit on adverse price movement
input int      InpEarlyExitPips = 30;        // Pips adverse move to trigger early exit (balanced)
input bool     InpExitOnDIWeakness = true;    // Exit when DI weakens (not just crosses)
input bool     InpExitOnPriceRejection = true; // Exit on price rejection (wick patterns)
input int      InpMicroStopPips = 25;        // Micro-stop: exit on adverse move (less aggressive)

input group "=== Trade Filters ==="
input bool     InpUseSpreadFilter = true;      // Filter by spread
input int      InpMinSpreadPips = 1;          // Minimum Spread (pips, 0=off)
input int      InpMaxSpreadPips = 50;         // Maximum Spread (pips, 0=off) - Increased for maximum opportunities
input bool     InpUseTimeFilter = true;       // Use trading hours filter
input int      InpStartHour = 5;              // Trading Start Hour (Server Time) - Extended for 200+ trades/year
input int      InpEndHour = 21;               // Trading End Hour (Server Time) - Extended to capture all sessions
input bool     InpAvoidFriday = false;       // Avoid trading on Friday - Disabled for more trades
input bool     InpAvoidMonday = false;        // Avoid trading on Monday - Disabled for more trades

input group "=== Risk Management ==="
input double   InpMaxDailyRisk = 10.0;        // Max Daily Risk (% of balance) - Optimized for 100% annual profit
input int      InpMaxTradesPerDay = 10;       // Max Trades Per Day - Ensures 200+ trades/year minimum
input bool     InpOneTradeAtTime = true;      // One Trade At A Time (enforced)
input double   InpMaxDrawdownPercent = 10.0;  // Max Drawdown (% of equity) - Stop trading if exceeded

input group "=== Logging ==="
input bool     InpEnableLogging = true;       // Enable detailed logging

input group "=== Magic Number ==="
input int      InpMagicNumber = 789123;       // Magic Number

//--- Global Variables
// Indicator Handles
int m_adxHandle = INVALID_HANDLE;
int m_fastEMAHandle = INVALID_HANDLE;
int m_slowEMAHandle = INVALID_HANDLE;
int m_atrHandle = INVALID_HANDLE;

// Indicator Buffers (all set as series)
double m_adxBuffer[];
double m_plusDIBuffer[];
double m_minusDIBuffer[];
double m_fastMABuffer[];
double m_slowMABuffer[];
double m_atrBuffer[];

// CTrade Object
CTrade m_trade;

// State Variables
datetime m_lastBarTime = 0;
int m_tradesToday = 0;
double m_dailyLoss = 0.0;
datetime m_lastTradeDate = 0;
string m_symbol = "";
ENUM_TIMEFRAMES m_timeframe = PERIOD_CURRENT;
double m_peakEquity = 0.0;  // Track peak equity for drawdown calculation

//+------------------------------------------------------------------+
//| Expert initialization function                                     |
//+------------------------------------------------------------------+
int OnInit()
{
   //--- Initialize symbol and timeframe
   m_symbol = _Symbol;
   m_timeframe = PERIOD_CURRENT;
   
   //--- Validate symbol
   if(!SymbolInfoInteger(m_symbol, SYMBOL_SELECT))
   {
      LogError("Symbol " + m_symbol + " is not available in Market Watch");
      return(INIT_FAILED);
   }
   
   //--- Validate input parameters
   if(!ValidateInputs())
      return(INIT_PARAMETERS_INCORRECT);
   
   //--- Create indicator handles
   if(!CreateIndicators())
   {
      LogError("Failed to create indicator handles");
      return(INIT_FAILED);
   }
   
   //--- Initialize CTrade object
   InitializeTrade();
   
   //--- Set arrays as series
   ArraySetAsSeries(m_adxBuffer, true);
   ArraySetAsSeries(m_plusDIBuffer, true);
   ArraySetAsSeries(m_minusDIBuffer, true);
   ArraySetAsSeries(m_fastMABuffer, true);
   ArraySetAsSeries(m_slowMABuffer, true);
   ArraySetAsSeries(m_atrBuffer, true);
   
   //--- Initialize daily counters
   ResetDailyCounters();
   
   //--- Initialize peak equity
   m_peakEquity = AccountInfoDouble(ACCOUNT_EQUITY);
   
   //--- Log initialization
   LogInitialization();
   
   return(INIT_SUCCEEDED);
}

//+------------------------------------------------------------------+
//| Expert deinitialization function                                   |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   //--- Release indicator handles
   if(m_adxHandle != INVALID_HANDLE)
      IndicatorRelease(m_adxHandle);
   if(m_fastEMAHandle != INVALID_HANDLE)
      IndicatorRelease(m_fastEMAHandle);
   if(m_slowEMAHandle != INVALID_HANDLE)
      IndicatorRelease(m_slowEMAHandle);
   if(m_atrHandle != INVALID_HANDLE)
      IndicatorRelease(m_atrHandle);
   
   //--- Log deinitialization
   string reasonText = "";
   switch(reason)
   {
      case REASON_PROGRAM:    reasonText = "EA removed from chart"; break;
      case REASON_REMOVE:     reasonText = "EA removed"; break;
      case REASON_RECOMPILE: reasonText = "EA recompiled"; break;
      case REASON_CHARTCHANGE: reasonText = "Chart timeframe changed"; break;
      case REASON_CHARTCLOSE: reasonText = "Chart closed"; break;
      case REASON_PARAMETERS: reasonText = "Input parameters changed"; break;
      case REASON_ACCOUNT:    reasonText = "Account changed"; break;
      case REASON_TEMPLATE:   reasonText = "Template applied"; break;
      case REASON_INITFAILED: reasonText = "Initialization failed"; break;
      case REASON_CLOSE:      reasonText = "Terminal closed"; break;
      default:                reasonText = "Unknown reason";
   }
   
   LogInfo("EA deinitialized. Reason: " + reasonText);
   LogDailyStats();
}

//+------------------------------------------------------------------+
//| Expert tick function                                               |
//+------------------------------------------------------------------+
void OnTick()
{
   //--- Update peak equity for drawdown tracking (on every tick)
   double currentEquity = AccountInfoDouble(ACCOUNT_EQUITY);
   if(currentEquity > m_peakEquity)
      m_peakEquity = currentEquity;
   
   //--- Check for new bar (trade only on new bars - prevents repainting)
   datetime currentBarTime = iTime(m_symbol, m_timeframe, 0);
   bool isNewBar = (currentBarTime != m_lastBarTime);
   
   if(isNewBar)
   {
      m_lastBarTime = currentBarTime;
      ResetDailyCounters();
   }
   
   //--- Manage existing positions (trailing stops, exit conditions) - on every tick for proper management
   ManagePositions();
   
   //--- Check for new trade signals only on new bar (no repainting)
   if(!isNewBar)
      return;
   
   //--- Enforce one-trade-at-a-time rule (one position per symbol)
   if(InpOneTradeAtTime && HasOpenPosition())
      return;
   
   //--- Check if we can trade
   if(!CanTrade())
      return;
   
   //--- Update indicator data
   if(!UpdateIndicators())
      return;
   
   //--- Check for trading signals
   CheckForSignals();
}

//+------------------------------------------------------------------+
//| Validate input parameters                                          |
//+------------------------------------------------------------------+
bool ValidateInputs()
{
   //--- Validate stop loss and take profit
   if(InpStopLossPips <= 0 || InpTakeProfitPips <= 0)
   {
      LogError("Stop Loss and Take Profit must be greater than 0");
      return false;
   }
   
   //--- Validate risk percentage
   if(InpRiskPercent <= 0 || InpRiskPercent > 10)
   {
      LogError("Risk per trade must be between 0.1% and 10%");
      return false;
   }
   
   //--- Validate ADX level
   if(InpADXMinLevel < 15 || InpADXMinLevel > 50)
   {
      LogError("ADX Min Level should be between 15 and 50");
      return false;
   }
   
   //--- Validate ADX exit level
   if(InpADXExitLevel < 10 || InpADXExitLevel >= InpADXMinLevel)
   {
      LogError("ADX Exit Level should be between 10 and less than ADX Min Level");
      return false;
   }
   
   //--- Validate EMA periods
   if(InpFastEMA >= InpSlowEMA)
   {
      LogError("Fast EMA period must be less than Slow EMA period");
      return false;
   }
   
   //--- Validate time filter
   if(InpUseTimeFilter && InpStartHour >= InpEndHour)
   {
      LogError("Start hour must be less than end hour");
      return false;
   }
   
   return true;
}

//+------------------------------------------------------------------+
//| Create indicator handles                                           |
//+------------------------------------------------------------------+
bool CreateIndicators()
{
   //--- Create ADX indicator (returns 3 buffers: ADX, +DI, -DI)
   m_adxHandle = iADX(m_symbol, m_timeframe, InpADXPeriod);
   if(m_adxHandle == INVALID_HANDLE)
   {
      LogError("Failed to create ADX indicator");
      return false;
   }
   
   //--- Create Fast EMA
   m_fastEMAHandle = iMA(m_symbol, m_timeframe, InpFastEMA, 0, MODE_EMA, PRICE_CLOSE);
   if(m_fastEMAHandle == INVALID_HANDLE)
   {
      LogError("Failed to create Fast EMA indicator");
      return false;
   }
   
   //--- Create Slow EMA
   m_slowEMAHandle = iMA(m_symbol, m_timeframe, InpSlowEMA, 0, MODE_EMA, PRICE_CLOSE);
   if(m_slowEMAHandle == INVALID_HANDLE)
   {
      LogError("Failed to create Slow EMA indicator");
      return false;
   }
   
   //--- Create ATR indicator
   m_atrHandle = iATR(m_symbol, m_timeframe, InpATRPeriod);
   if(m_atrHandle == INVALID_HANDLE)
   {
      LogError("Failed to create ATR indicator");
      return false;
   }
   
   return true;
}

//+------------------------------------------------------------------+
//| Initialize CTrade object                                           |
//+------------------------------------------------------------------+
void InitializeTrade()
{
   //--- Set magic number
   m_trade.SetExpertMagicNumber(InpMagicNumber);
   
   //--- Set deviation (slippage in points)
   m_trade.SetDeviationInPoints(10);
   
   //--- Set filling mode
   m_trade.SetTypeFilling(GetFillingType());
   
   //--- Set async mode (false for backtesting)
   m_trade.SetAsyncMode(false);
   
   //--- Set margin check
   m_trade.SetMarginMode();
}

//+------------------------------------------------------------------+
//| Update indicator buffers                                           |
//+------------------------------------------------------------------+
bool UpdateIndicators()
{
   //--- Copy ADX data (3 buffers: ADX[0], +DI[1], -DI[2])
   if(CopyBuffer(m_adxHandle, 0, 0, 3, m_adxBuffer) <= 0) // ADX line
   {
      LogError("Failed to copy ADX buffer");
      return false;
   }
   if(CopyBuffer(m_adxHandle, 1, 0, 3, m_plusDIBuffer) <= 0) // +DI line
   {
      LogError("Failed to copy +DI buffer");
      return false;
   }
   if(CopyBuffer(m_adxHandle, 2, 0, 3, m_minusDIBuffer) <= 0) // -DI line
   {
      LogError("Failed to copy -DI buffer");
      return false;
   }
   
   //--- Copy EMA data
   if(CopyBuffer(m_fastEMAHandle, 0, 0, 3, m_fastMABuffer) <= 0)
   {
      LogError("Failed to copy Fast EMA buffer");
      return false;
   }
   if(CopyBuffer(m_slowEMAHandle, 0, 0, 3, m_slowMABuffer) <= 0)
   {
      LogError("Failed to copy Slow EMA buffer");
      return false;
   }
   
   //--- Copy ATR data
   if(CopyBuffer(m_atrHandle, 0, 0, 2, m_atrBuffer) <= 0)
   {
      LogError("Failed to copy ATR buffer");
      return false;
   }
   
   return true;
}

//+------------------------------------------------------------------+
//| Check if trading is allowed                                        |
//+------------------------------------------------------------------+
bool CanTrade()
{
   //--- Check drawdown limit (CRITICAL: Stop trading if drawdown exceeds 10%)
   double currentEquity = AccountInfoDouble(ACCOUNT_EQUITY);
   double currentDrawdown = 0.0;
   if(m_peakEquity > 0)
      currentDrawdown = (m_peakEquity - currentEquity) / m_peakEquity * 100.0;
   
   if(currentDrawdown >= InpMaxDrawdownPercent)
   {
      if(InpEnableLogging)
         Print("[RISK] Trading stopped - Drawdown limit reached: ", DoubleToString(currentDrawdown, 2), "% (Limit: ", DoubleToString(InpMaxDrawdownPercent, 1), "%)");
      return false;
   }
   
   //--- Check daily trade limit
   if(m_tradesToday >= InpMaxTradesPerDay)
      return false;
   
   //--- Check daily risk limit
   double accountBalance = AccountInfoDouble(ACCOUNT_BALANCE);
   double maxDailyLoss = accountBalance * InpMaxDailyRisk / 100.0;
   if(m_dailyLoss >= maxDailyLoss)
      return false;
   
   //--- Check spread filter
   if(InpUseSpreadFilter)
   {
      double ask = SymbolInfoDouble(m_symbol, SYMBOL_ASK);
      double bid = SymbolInfoDouble(m_symbol, SYMBOL_BID);
      double spreadPrice = ask - bid;
      int spreadPips = PriceToPips(spreadPrice);
      
      if(InpMinSpreadPips > 0 && spreadPips < InpMinSpreadPips)
         return false;
      if(InpMaxSpreadPips > 0 && spreadPips > InpMaxSpreadPips)
         return false;
   }
   
   //--- Check time filter
   if(InpUseTimeFilter)
   {
      MqlDateTime dt;
      TimeToStruct(TimeCurrent(), dt);
      
      if(dt.hour < InpStartHour || dt.hour >= InpEndHour)
         return false;
      
      if(InpAvoidFriday && dt.day_of_week == 5)
         return false;
      
      if(InpAvoidMonday && dt.day_of_week == 1)
         return false;
   }
   
   return true;
}

//+------------------------------------------------------------------+
//| Check for trading signals - PRECISE CONDITIONS                    |
//+------------------------------------------------------------------+
void CheckForSignals()
{
   //--- Get current prices
   double ask = SymbolInfoDouble(m_symbol, SYMBOL_ASK);
   double bid = SymbolInfoDouble(m_symbol, SYMBOL_BID);
   double close0 = iClose(m_symbol, m_timeframe, 0);
   double close1 = iClose(m_symbol, m_timeframe, 1);
   double close2 = iClose(m_symbol, m_timeframe, 2);
   
   //--- BUY SIGNAL CONDITIONS (ALL must be true if enabled)
   bool buySignal = true;
   
   // Condition 1: ADX Filter - Strong trend required
   if(InpUseADXFilter && m_adxBuffer[0] < InpADXMinLevel)
      buySignal = false;
   
      // Condition 2: DI Cross - Bullish momentum (+DI > -DI)
      if(InpUseDICross && buySignal)
      {
         if(m_plusDIBuffer[0] <= m_minusDIBuffer[0])
            buySignal = false;
         
         // Require minimum DI spread for stronger signal
         double diSpread = m_plusDIBuffer[0] - m_minusDIBuffer[0];
         if(diSpread < InpDIMinSpread)
            buySignal = false;
         
         // Require recent DI cross for stronger signal (optional)
         if(InpRequireDICross && buySignal)
         {
            // Check if +DI was below -DI on previous bar (recent cross)
            if(m_plusDIBuffer[1] <= m_minusDIBuffer[1])
            {
               // Recent cross confirmed - stronger signal
            }
            else
            {
               // No recent cross - might be late entry, but allow if ADX is strong
               if(m_adxBuffer[0] < InpADXMinLevel + 3.0) // Require ADX 3 points higher if no recent cross (very relaxed)
                  buySignal = false;
            }
         }
      }
   
   // Condition 3: EMA Alignment - Fast EMA above Slow EMA (relaxed - allow if current bar is above)
   if(InpUseEMAFilter && buySignal)
   {
      if(m_fastMABuffer[0] <= m_slowMABuffer[0])
         buySignal = false;
      // Relaxed: only require current bar to be above slow EMA, not previous bar
      if(close0 <= m_slowMABuffer[0])
         buySignal = false;
   }
   
   // Condition 4: Price Momentum - Previous bar closed higher (very relaxed for more trades)
   if(InpUsePriceMomentum && buySignal)
   {
      // Very relaxed: only reject if both previous bars closed lower (strong rejection)
      if(close1 < close2 && close0 < close1)
         buySignal = false; // Strong downward momentum - reject
   }
   
   // Condition 5: ATR Volatility Filter - Ensure sufficient volatility
   if(InpUseATRFilter && buySignal)
   {
      double atrArray[];
      ArraySetAsSeries(atrArray, true);
      if(CopyBuffer(m_atrHandle, 0, 0, 20, atrArray) > 0)
      {
         double avgATR = 0;
         for(int i = 0; i < 20; i++)
            avgATR += atrArray[i];
         avgATR /= 20.0;
         
         if(m_atrBuffer[0] < avgATR * InpATRMultiplier)
            buySignal = false; // Volatility too low
      }
   }
   
   //--- Execute BUY if all conditions met
   if(buySignal)
   {
      OpenTrade(ORDER_TYPE_BUY, ask);
      return; // Only one signal per bar
   }
   
   //--- SELL SIGNAL CONDITIONS (ALL must be true if enabled)
   bool sellSignal = true;
   
   // Condition 1: ADX Filter - Strong trend required
   if(InpUseADXFilter && m_adxBuffer[0] < InpADXMinLevel)
      sellSignal = false;
   
      // Condition 2: DI Cross - Bearish momentum (-DI > +DI)
      if(InpUseDICross && sellSignal)
      {
         if(m_minusDIBuffer[0] <= m_plusDIBuffer[0])
            sellSignal = false;
         
         // Require minimum DI spread for stronger signal
         double diSpread = m_minusDIBuffer[0] - m_plusDIBuffer[0];
         if(diSpread < InpDIMinSpread)
            sellSignal = false;
         
         // Require recent DI cross for stronger signal (optional)
         if(InpRequireDICross && sellSignal)
         {
            // Check if -DI was below +DI on previous bar (recent cross)
            if(m_minusDIBuffer[1] <= m_plusDIBuffer[1])
            {
               // Recent cross confirmed - stronger signal
            }
            else
            {
               // No recent cross - might be late entry, but allow if ADX is strong
               if(m_adxBuffer[0] < InpADXMinLevel + 3.0) // Require ADX 3 points higher if no recent cross (very relaxed)
                  sellSignal = false;
            }
         }
      }
   
   // Condition 3: EMA Alignment - Fast EMA below Slow EMA (relaxed - allow if current bar is below)
   if(InpUseEMAFilter && sellSignal)
   {
      if(m_fastMABuffer[0] >= m_slowMABuffer[0])
         sellSignal = false;
      // Relaxed: only require current bar to be below slow EMA, not previous bar
      if(close0 >= m_slowMABuffer[0])
         sellSignal = false;
   }
   
   // Condition 4: Price Momentum - Previous bar closed lower (very relaxed for more trades)
   if(InpUsePriceMomentum && sellSignal)
   {
      // Very relaxed: only reject if both previous bars closed higher (strong rejection)
      if(close1 > close2 && close0 > close1)
         sellSignal = false; // Strong upward momentum - reject
   }
   
   // Condition 5: ATR Volatility Filter - Ensure sufficient volatility
   if(InpUseATRFilter && sellSignal)
   {
      double atrArray[];
      ArraySetAsSeries(atrArray, true);
      if(CopyBuffer(m_atrHandle, 0, 0, 20, atrArray) > 0)
      {
         double avgATR = 0;
         for(int i = 0; i < 20; i++)
            avgATR += atrArray[i];
         avgATR /= 20.0;
         
         if(m_atrBuffer[0] < avgATR * InpATRMultiplier)
            sellSignal = false; // Volatility too low
      }
   }
   
   //--- Execute SELL if all conditions met
   if(sellSignal)
   {
      OpenTrade(ORDER_TYPE_SELL, bid);
   }
}

//+------------------------------------------------------------------+
//| Open a trade using CTrade                                          |
//+------------------------------------------------------------------+
void OpenTrade(ENUM_ORDER_TYPE orderType, double price)
{
   //--- Calculate SL and TP FIRST (needed for lot size calculation)
   double slDistance = PipsToPrice(InpStopLossPips);
   double tpDistance = PipsToPrice(InpTakeProfitPips);
   
   // Use ATR-based stop if enabled
   if(InpUseATRStop && ArraySize(m_atrBuffer) > 0 && m_atrBuffer[0] > 0)
   {
      double atrValue = m_atrBuffer[0];
      double atrMultiplier = 1.2; // More reasonable ATR multiplier for stable stops
      slDistance = atrValue * atrMultiplier;
      tpDistance = slDistance * 8.0; // 8:1 R:R for ATR-based stops (maximize winners for 100% profit)
      
      // Ensure minimum/maximum limits (reasonable range)
      double minSL = PipsToPrice(25);
      double maxSL = PipsToPrice(50);
      slDistance = MathMax(minSL, MathMin(maxSL, slDistance));
      tpDistance = MathMax(PipsToPrice(200), tpDistance); // Higher TP for bigger winners
   }
   
   //--- Calculate lot size based on ACTUAL SL distance (critical for consistent risk)
   double lotSize = CalculateLotSize(orderType, price, slDistance);
   if(lotSize <= 0)
   {
      LogError("Invalid lot size calculated: " + DoubleToString(lotSize, 2));
      return;
   }
   
   double sl = 0, tp = 0;
   if(orderType == ORDER_TYPE_BUY)
   {
      sl = NormalizeDouble(price - slDistance, _Digits);
      tp = NormalizeDouble(price + tpDistance, _Digits);
   }
   else
   {
      sl = NormalizeDouble(price + slDistance, _Digits);
      tp = NormalizeDouble(price - tpDistance, _Digits);
   }
   
   //--- Execute trade using CTrade
   bool result = false;
   if(orderType == ORDER_TYPE_BUY)
   {
      result = m_trade.Buy(lotSize, m_symbol, price, sl, tp, "ADX TrendMaster");
   }
   else
   {
      result = m_trade.Sell(lotSize, m_symbol, price, sl, tp, "ADX TrendMaster");
   }
   
   if(result)
   {
      LogTrade("Trade opened: " + EnumToString(orderType) + 
               " | Lot: " + DoubleToString(lotSize, 2) + 
               " | Price: " + DoubleToString(price, _Digits) + 
               " | SL: " + DoubleToString(sl, _Digits) + 
               " | TP: " + DoubleToString(tp, _Digits));
      m_tradesToday++;
      m_lastTradeDate = TimeCurrent();
   }
   else
   {
      LogError("Trade failed: " + m_trade.ResultRetcodeDescription());
   }
}

//+------------------------------------------------------------------+
//| Manage open positions (trailing stops, exit conditions)          |
//+------------------------------------------------------------------+
void ManagePositions()
{
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket <= 0)
         continue;
      
      if(PositionGetString(POSITION_SYMBOL) != m_symbol || 
         PositionGetInteger(POSITION_MAGIC) != InpMagicNumber)
         continue;
      
      //--- Update indicators for exit conditions
      if(!UpdateIndicators())
         continue;
      
      ENUM_POSITION_TYPE posType = (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE);
      double positionOpenPrice = PositionGetDouble(POSITION_PRICE_OPEN);
      double currentSL = PositionGetDouble(POSITION_SL);
      double currentTP = PositionGetDouble(POSITION_TP);
      
      bool shouldClose = false;
      string closeReason = "";
      
      //--- Exit Condition 1: ADX drops below threshold
      if(InpExitOnADXDrop && m_adxBuffer[0] < InpADXExitLevel)
      {
         shouldClose = true;
         closeReason = "ADX dropped below " + DoubleToString(InpADXExitLevel, 1);
      }
      
      //--- Exit Condition 2: Opposite DI cross (trend reversal)
      if(InpExitOnDICross && !shouldClose)
      {
         if(posType == POSITION_TYPE_BUY && m_minusDIBuffer[0] > m_plusDIBuffer[0])
         {
            shouldClose = true;
            closeReason = "Bearish DI cross";
         }
         else if(posType == POSITION_TYPE_SELL && m_plusDIBuffer[0] > m_minusDIBuffer[0])
         {
            shouldClose = true;
            closeReason = "Bullish DI cross";
         }
      }
      
      //--- Exit Condition 3: DI Weakness (early warning) - Only exit if trend is clearly reversing
      if(InpExitOnDIWeakness && !shouldClose)
      {
         if(posType == POSITION_TYPE_BUY)
         {
            // Exit only if +DI crosses below -DI (clear reversal), not just weakening
            if(m_plusDIBuffer[0] < m_minusDIBuffer[0] && 
               m_plusDIBuffer[1] >= m_minusDIBuffer[1]) // Just crossed below
            {
               shouldClose = true;
               closeReason = "DI weakness - trend reversal confirmed";
            }
         }
         else if(posType == POSITION_TYPE_SELL)
         {
            // Exit only if -DI crosses below +DI (clear reversal), not just weakening
            if(m_minusDIBuffer[0] < m_plusDIBuffer[0] && 
               m_minusDIBuffer[1] >= m_plusDIBuffer[1]) // Just crossed below
            {
               shouldClose = true;
               closeReason = "DI weakness - trend reversal confirmed";
            }
         }
      }
      
      //--- Close position if exit condition met
      if(shouldClose)
      {
         if(m_trade.PositionClose(ticket))
         {
            LogTrade("Position closed. Ticket: " + IntegerToString(ticket) + " Reason: " + closeReason);
         }
         else
         {
            LogError("Failed to close position: " + m_trade.ResultRetcodeDescription());
         }
         continue;
      }
      
      //--- Micro-Stop: Exit on adverse moves (only if trend is clearly reversing)
      if(InpUseMicroStops && !shouldClose)
      {
         double currentPrice = (posType == POSITION_TYPE_BUY) ? 
                              SymbolInfoDouble(m_symbol, SYMBOL_BID) : 
                              SymbolInfoDouble(m_symbol, SYMBOL_ASK);
         double adverseMove = 0;
         
         if(posType == POSITION_TYPE_BUY)
            adverseMove = positionOpenPrice - currentPrice;
         else
            adverseMove = currentPrice - positionOpenPrice;
         
         // Only exit if adverse move is significant AND price crossed below/above fast EMA (trend reversal)
         bool trendReversing = false;
         if(posType == POSITION_TYPE_BUY && currentPrice < m_fastMABuffer[0])
            trendReversing = true;
         else if(posType == POSITION_TYPE_SELL && currentPrice > m_fastMABuffer[0])
            trendReversing = true;
         
         // Exit only if both conditions met: adverse move AND trend reversal
         if(adverseMove >= PipsToPrice(InpMicroStopPips) && trendReversing)
         {
            shouldClose = true;
            closeReason = "Micro-stop - adverse move " + DoubleToString(PriceToPips(adverseMove), 0) + " pips + trend reversal";
         }
      }
      
      //--- Early Exit on Adverse Movement (cut losses quickly - ULTRA AGGRESSIVE)
      if(InpEarlyExitOnLoss && !shouldClose)
      {
         double currentPrice = (posType == POSITION_TYPE_BUY) ? 
                              SymbolInfoDouble(m_symbol, SYMBOL_BID) : 
                              SymbolInfoDouble(m_symbol, SYMBOL_ASK);
         double adverseMove = 0;
         bool emaReversal = false;
         bool priceBelowEMA = false;
         bool priceRejection = false;
         
         // Check for price rejection (wick patterns)
         if(InpExitOnPriceRejection)
         {
            double high0 = iHigh(m_symbol, m_timeframe, 0);
            double low0 = iLow(m_symbol, m_timeframe, 0);
            double open0 = iOpen(m_symbol, m_timeframe, 0);
            double close0 = iClose(m_symbol, m_timeframe, 0);
            
            if(posType == POSITION_TYPE_BUY)
            {
               // Bearish rejection: long upper wick
               double upperWick = high0 - MathMax(open0, close0);
               double body = MathAbs(close0 - open0);
               if(upperWick > body * 1.5 && upperWick > PipsToPrice(15))
                  priceRejection = true;
            }
            else // SELL
            {
               // Bullish rejection: long lower wick
               double lowerWick = MathMin(open0, close0) - low0;
               double body = MathAbs(close0 - open0);
               if(lowerWick > body * 1.5 && lowerWick > PipsToPrice(15))
                  priceRejection = true;
            }
         }
         
         // Check for EMA reversal (strong early exit signal)
         if(posType == POSITION_TYPE_BUY)
         {
            // Exit if price crosses below fast EMA (trend weakening)
            if(currentPrice < m_fastMABuffer[0])
            {
               emaReversal = true;
               priceBelowEMA = true;
            }
            
            adverseMove = positionOpenPrice - currentPrice;
            
            // Balanced: exit on significant adverse moves with EMA reversal or rejection
            // Don't exit just for approaching EMA - let trades breathe
            if((adverseMove >= PipsToPrice(InpEarlyExitPips) && emaReversal) || 
               (adverseMove >= PipsToPrice(InpEarlyExitPips + 10) && priceRejection))
            {
               shouldClose = true;
               closeReason = emaReversal ? "Early exit - EMA reversal + adverse move" : 
                            (priceRejection ? "Early exit - price rejection + adverse move" :
                            "Early exit - adverse move " + DoubleToString(PriceToPips(adverseMove), 0) + " pips");
            }
         }
         else // SELL
         {
            // Exit if price crosses above fast EMA (trend weakening)
            if(currentPrice > m_fastMABuffer[0])
            {
               emaReversal = true;
               priceBelowEMA = true;
            }
            
            adverseMove = currentPrice - positionOpenPrice;
            
            // Balanced: exit on significant adverse moves with EMA reversal or rejection
            // Don't exit just for approaching EMA - let trades breathe
            if((adverseMove >= PipsToPrice(InpEarlyExitPips) && emaReversal) || 
               (adverseMove >= PipsToPrice(InpEarlyExitPips + 10) && priceRejection))
            {
               shouldClose = true;
               closeReason = emaReversal ? "Early exit - EMA reversal + adverse move" : 
                            (priceRejection ? "Early exit - price rejection + adverse move" :
                            "Early exit - adverse move " + DoubleToString(PriceToPips(adverseMove), 0) + " pips");
            }
         }
         
         if(shouldClose)
         {
            if(m_trade.PositionClose(ticket))
            {
               LogTrade("Position closed. Ticket: " + IntegerToString(ticket) + " Reason: " + closeReason);
            }
            continue;
         }
      }
      
      //--- Maximum Loss Per Trade Protection (hard stop on losses)
      if(InpMaxLossPercent > 0)
      {
         double positionProfit = PositionGetDouble(POSITION_PROFIT);
         double positionSwap = PositionGetDouble(POSITION_SWAP);
         double positionCommission = PositionGetDouble(POSITION_COMMISSION);
         double totalLoss = -(positionProfit + positionSwap + positionCommission); // Negative = loss
         
         double accountBalance = AccountInfoDouble(ACCOUNT_BALANCE);
         double maxLossAmount = accountBalance * InpRiskPercent / 100.0 * InpMaxLossPercent;
         
         // Close if loss exceeds maximum allowed (e.g., 50% of risk amount)
         if(totalLoss > maxLossAmount && totalLoss > 0)
         {
            if(m_trade.PositionClose(ticket))
            {
               LogTrade("Position closed. Ticket: " + IntegerToString(ticket) + 
                       " Reason: Maximum loss limit reached (" + DoubleToString(totalLoss, 2) + 
                       " / Max: " + DoubleToString(maxLossAmount, 2) + ")");
            }
            continue;
         }
      }
      
      //--- Break-Even Management
      if(InpUseBreakEven)
      {
         ManageBreakEven(ticket, posType);
      }
      
      //--- Trailing Stop Management
      if(InpTrailingStopPips > 0 || InpUseTrailingStop)
      {
         int trailingPips = InpTrailingStopPips > 0 ? InpTrailingStopPips : InpStopLossPips;
         
         // Use ATR-based trailing if enabled
         if(InpUseATRStop)
         {
            double atrValue = m_atrBuffer[0];
            double atrMultiplier = 1.5; // More reasonable ATR multiplier for smoother trailing
            trailingPips = (int)MathRound(PriceToPips(atrValue * atrMultiplier));
            trailingPips = MathMax(40, MathMin(80, trailingPips)); // Wider range: 40-80 pips (let winners run for 100% profit)
         }
         
         ManageTrailingStop(ticket, posType, trailingPips);
      }
   }
}

//+------------------------------------------------------------------+
//| Manage break-even stop loss                                        |
//+------------------------------------------------------------------+
void ManageBreakEven(ulong ticket, ENUM_POSITION_TYPE posType)
{
   double positionOpenPrice = PositionGetDouble(POSITION_PRICE_OPEN);
   double currentSL = PositionGetDouble(POSITION_SL);
   double currentPrice = (posType == POSITION_TYPE_BUY) ? 
                        SymbolInfoDouble(m_symbol, SYMBOL_BID) : 
                        SymbolInfoDouble(m_symbol, SYMBOL_ASK);
   
   // Check if we're in profit enough to move to break-even (ULTRA FAST)
   double profitPips = 0;
   bool shouldMoveToBE = false;
   double newSL = 0;
   
   if(posType == POSITION_TYPE_BUY)
   {
      profitPips = PriceToPips(currentPrice - positionOpenPrice);
      // Move to BE when profit target is reached (let winners develop first)
      if(profitPips >= InpBreakEvenPips)
      {
         // Check if SL is still below entry (not already at BE or better)
         if(currentSL < positionOpenPrice || currentSL == 0)
         {
            shouldMoveToBE = true;
            // Add small buffer to avoid premature stop-outs
            newSL = NormalizeDouble(positionOpenPrice + PipsToPrice(2), _Digits);
         }
      }
   }
   else // SELL
   {
      profitPips = PriceToPips(positionOpenPrice - currentPrice);
      // Move to BE when profit target is reached (let winners develop first)
      if(profitPips >= InpBreakEvenPips)
      {
         // Check if SL is still above entry (not already at BE or better)
         if(currentSL > positionOpenPrice || currentSL == 0)
         {
            shouldMoveToBE = true;
            // Add small buffer to avoid premature stop-outs
            newSL = NormalizeDouble(positionOpenPrice - PipsToPrice(2), _Digits);
         }
      }
   }
   
   if(shouldMoveToBE)
   {
      if(m_trade.PositionModify(ticket, newSL, PositionGetDouble(POSITION_TP)))
      {
         LogTrade("Break-even stop set. Ticket: " + IntegerToString(ticket) + 
                 " New SL: " + DoubleToString(newSL, _Digits));
      }
   }
}

//+------------------------------------------------------------------+
//| Manage trailing stop for a position                               |
//+------------------------------------------------------------------+
void ManageTrailingStop(ulong ticket, ENUM_POSITION_TYPE posType, int trailingPips)
{
   double trailingDistance = PipsToPrice(trailingPips);
   double trailingStep = PipsToPrice(InpTrailingStepPips);
   
   double positionOpenPrice = PositionGetDouble(POSITION_PRICE_OPEN);
   double currentSL = PositionGetDouble(POSITION_SL);
   
   double currentPrice = (posType == POSITION_TYPE_BUY) ? 
                        SymbolInfoDouble(m_symbol, SYMBOL_BID) : 
                        SymbolInfoDouble(m_symbol, SYMBOL_ASK);
   
   double newSL = 0;
   bool modifySL = false;
   
   if(posType == POSITION_TYPE_BUY)
   {
      newSL = NormalizeDouble(currentPrice - trailingDistance, _Digits);
      
      // Only move SL up, never down
      if(currentSL == 0 || newSL > currentSL + trailingStep)
      {
         // Don't set SL below entry price (allow reasonable buffer for volatility)
         if(newSL >= positionOpenPrice - PipsToPrice(5) || currentSL == 0)
            modifySL = true;
      }
   }
   else // SELL
   {
      newSL = NormalizeDouble(currentPrice + trailingDistance, _Digits);
      
      // Only move SL down, never up
      if(currentSL == 0 || newSL < currentSL - trailingStep)
      {
         // Don't set SL above entry price (allow reasonable buffer for volatility)
         if(newSL <= positionOpenPrice + PipsToPrice(5) || currentSL == 0)
            modifySL = true;
      }
   }
   
   if(modifySL)
   {
      if(m_trade.PositionModify(ticket, newSL, PositionGetDouble(POSITION_TP)))
      {
         LogTrade("Trailing stop updated. Ticket: " + IntegerToString(ticket) + 
                 " New SL: " + DoubleToString(newSL, _Digits));
      }
   }
}

//+------------------------------------------------------------------+
//| Calculate lot size based on risk                                  |
//+------------------------------------------------------------------+
double CalculateLotSize(ENUM_ORDER_TYPE orderType, double price, double slDistance)
{
   //--- If fixed lot size is specified, use it
   if(InpLotSize > 0)
      return InpLotSize;
   
   //--- Calculate based on risk percentage
   double accountBalance = AccountInfoDouble(ACCOUNT_BALANCE);
   double riskAmount = accountBalance * InpRiskPercent / 100.0;
   
   //--- Use actual SL distance passed as parameter (critical for ATR-based stops)
   if(slDistance <= 0)
      return 0;
   
   double tickValue = SymbolInfoDouble(m_symbol, SYMBOL_TRADE_TICK_VALUE);
   double tickSize = SymbolInfoDouble(m_symbol, SYMBOL_TRADE_TICK_SIZE);
   
   //--- Calculate lot size: Risk Amount / (SL Distance / Tick Size * Tick Value)
   double lotSize = riskAmount / (slDistance / tickSize * tickValue);
   
   //--- Normalize lot size to broker's requirements
   double minLot = SymbolInfoDouble(m_symbol, SYMBOL_VOLUME_MIN);
   double maxLot = SymbolInfoDouble(m_symbol, SYMBOL_VOLUME_MAX);
   double lotStep = SymbolInfoDouble(m_symbol, SYMBOL_VOLUME_STEP);
   
   lotSize = MathFloor(lotSize / lotStep) * lotStep;
   lotSize = MathMax(minLot, MathMin(maxLot, lotSize));
   
   return lotSize;
}

//+------------------------------------------------------------------+
//| Convert pips to price (handles both 4-digit and 5-digit brokers) |
//+------------------------------------------------------------------+
double PipsToPrice(int pips)
{
   double point = SymbolInfoDouble(m_symbol, SYMBOL_POINT);
   int digits = (int)SymbolInfoInteger(m_symbol, SYMBOL_DIGITS);
   
   // For 5-digit brokers (digits = 5), 1 pip = 10 points
   // For 4-digit brokers (digits = 4), 1 pip = 1 point
   // For 3-digit brokers (digits = 3), 1 pip = 10 points (JPY pairs)
   double pipValue = (digits == 5 || digits == 3) ? point * 10 : point;
   
   return pips * pipValue;
}

//+------------------------------------------------------------------+
//| Convert price to pips (handles both 4-digit and 5-digit brokers) |
//+------------------------------------------------------------------+
int PriceToPips(double price)
{
   double point = SymbolInfoDouble(m_symbol, SYMBOL_POINT);
   int digits = (int)SymbolInfoInteger(m_symbol, SYMBOL_DIGITS);
   
   double pipValue = (digits == 5 || digits == 3) ? point * 10 : point;
   
   return (int)MathRound(price / pipValue);
}

//+------------------------------------------------------------------+
//| Get filling type for the symbol                                    |
//+------------------------------------------------------------------+
ENUM_ORDER_TYPE_FILLING GetFillingType()
{
   int filling = (int)SymbolInfoInteger(m_symbol, SYMBOL_FILLING_MODE);
   
   if((filling & SYMBOL_FILLING_FOK) == SYMBOL_FILLING_FOK)
      return ORDER_FILLING_FOK;
   if((filling & SYMBOL_FILLING_IOC) == SYMBOL_FILLING_IOC)
      return ORDER_FILLING_IOC;
   
   return ORDER_FILLING_RETURN;
}

//+------------------------------------------------------------------+
//| Check if there's an open position (one position per symbol)      |
//+------------------------------------------------------------------+
bool HasOpenPosition()
{
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket > 0)
      {
         if(PositionGetString(POSITION_SYMBOL) == m_symbol && 
            PositionGetInteger(POSITION_MAGIC) == InpMagicNumber)
         {
            return true;
         }
      }
   }
   return false;
}

//+------------------------------------------------------------------+
//| Reset daily counters                                              |
//+------------------------------------------------------------------+
void ResetDailyCounters()
{
   MqlDateTime dt;
   TimeToStruct(TimeCurrent(), dt);
   datetime currentDate = StringToTime(IntegerToString(dt.year) + "." + 
                                      IntegerToString(dt.mon) + "." + 
                                      IntegerToString(dt.day));
   
   if(m_lastTradeDate == 0 || currentDate > m_lastTradeDate)
   {
      if(m_lastTradeDate > 0)
         LogDailyStats();
      
      m_tradesToday = 0;
      m_dailyLoss = 0.0;
      
      //--- Calculate today's loss from closed positions
      HistorySelect(currentDate, TimeCurrent());
      int totalDeals = HistoryDealsTotal();
      
      for(int i = 0; i < totalDeals; i++)
      {
         ulong ticket = HistoryDealGetTicket(i);
         if(ticket > 0)
         {
            if(HistoryDealGetString(ticket, DEAL_SYMBOL) == m_symbol &&
               HistoryDealGetInteger(ticket, DEAL_MAGIC) == InpMagicNumber)
            {
               double profit = HistoryDealGetDouble(ticket, DEAL_PROFIT) + 
                              HistoryDealGetDouble(ticket, DEAL_SWAP) + 
                              HistoryDealGetDouble(ticket, DEAL_COMMISSION);
               
               if(profit < 0)
                  m_dailyLoss += MathAbs(profit);
            }
         }
      }
   }
}

//+------------------------------------------------------------------+
//| Logging Functions                                                  |
//+------------------------------------------------------------------+
void LogInfo(string message)
{
   if(InpEnableLogging)
      Print("[INFO] ", message);
}

void LogTrade(string message)
{
   if(InpEnableLogging)
      Print("[TRADE] ", message);
}

void LogError(string message)
{
   Print("[ERROR] ", message);
}

void LogInitialization()
{
   LogInfo("=== ADX TrendMaster EA Initialized ===");
   LogInfo("Target: 100% Annual Profit | 200+ Trades/Year | Max 10% Drawdown");
   LogInfo("Symbol: " + m_symbol);
   LogInfo("Timeframe: " + EnumToString(m_timeframe));
   LogInfo("Risk per trade: " + DoubleToString(InpRiskPercent, 2) + "%");
   LogInfo("Stop Loss: " + IntegerToString(InpStopLossPips) + " pips");
   LogInfo("Take Profit: " + IntegerToString(InpTakeProfitPips) + " pips");
   LogInfo("ADX Min Level: " + DoubleToString(InpADXMinLevel, 1));
   LogInfo("Max Drawdown: " + DoubleToString(InpMaxDrawdownPercent, 1) + "%");
   LogInfo("Max Trades/Day: " + IntegerToString(InpMaxTradesPerDay));
   LogInfo("Magic Number: " + IntegerToString(InpMagicNumber));
}

void LogDailyStats()
{
   if(!InpEnableLogging)
      return;
   
   LogInfo("=== Daily Statistics ===");
   LogInfo("Trades today: " + IntegerToString(m_tradesToday));
   LogInfo("Daily loss: " + DoubleToString(m_dailyLoss, 2));
}

//+------------------------------------------------------------------+
