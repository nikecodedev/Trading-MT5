//+------------------------------------------------------------------+
//|                                                CandleFlipScalp.mq5
//+------------------------------------------------------------------+
#property strict
#property version "1.4"
#include <Trade/Trade.mqh>

input ENUM_TIMEFRAMES InpTF             = PERIOD_M15;
input double          InpFixedLots      = 0.10;
input bool            InpUseRiskPercent = false;
input double          InpRiskPercent    = 0.5;
input int             InpSL_Points      = 60;
input int             InpTP_Points      = 60;
input int             InpHysteresisPts  = 10;
input int             InpMaxSpreadPts   = 30;
input int             InpCooldownSec    = 5;
input int             InpMagic          = 902715;
input int             InpMaxFlipsPerBar = 2;

CTrade trade;
datetime g_lastTradeTime = 0;
datetime g_currBarTime   = 0;
int      g_flipsThisBar  = 0;

//--- helpers
double MidPrice()
{
   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   return (bid + ask)*0.5;
}

// NETTING: at most one position per symbol
int GetPositionDir() // 1=buy, -1=sell, 0=none
{
   if(!PositionSelect(_Symbol)) return 0;
   long type = PositionGetInteger(POSITION_TYPE);
   if(type==POSITION_TYPE_BUY)  return 1;
   if(type==POSITION_TYPE_SELL) return -1;
   return 0;
}

bool CloseIfDir(int dir_to_close)
{
   if(!PositionSelect(_Symbol)) return true;
   long type = PositionGetInteger(POSITION_TYPE);
   if( (dir_to_close== 1 && type==POSITION_TYPE_BUY) ||
       (dir_to_close==-1 && type==POSITION_TYPE_SELL) )
   {
      trade.SetExpertMagicNumber(InpMagic);
      trade.SetAsyncMode(false);
      return trade.PositionClose(_Symbol);
   }
   return true;
}

double CalcLotsByRisk(int sl_points)
{
   if(!InpUseRiskPercent) return InpFixedLots;
   if(sl_points<=0) return InpFixedLots;

   double tick_val=0, tick_size=0;
   SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE, tick_val);
   SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE,  tick_size);

   double balance = AccountInfoDouble(ACCOUNT_BALANCE);
   double risk_money = balance * InpRiskPercent/100.0;
   double value_per_point = (tick_val/tick_size)*_Point;
   if(value_per_point<=0) return InpFixedLots;

   double lots = risk_money / (sl_points * value_per_point);
   double step = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   double maxl = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   double minl = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   lots = MathMax(minl, MathMin(maxl, MathFloor(lots/step)*step));
   return lots;
}

bool PlaceOrder(int dir)
{
   double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   int slp = MathMax(1, InpSL_Points);
   int tpp = MathMax(1, InpTP_Points);

   double spread_pts = (ask - bid)/_Point;
   if(spread_pts > InpMaxSpreadPts) return false;

   double lots = CalcLotsByRisk(slp);
   if(lots<=0) return false;

   trade.SetExpertMagicNumber(InpMagic);
   trade.SetAsyncMode(false);

   if(dir==1)
   {
      double sl = bid - slp*_Point;
      double tp = bid + tpp*_Point;
      return trade.Buy(lots, _Symbol, 0.0, sl, tp, "Flip long");
   }
   if(dir==-1)
   {
      double sl = ask + slp*_Point;
      double tp = ask - tpp*_Point;
      return trade.Sell(lots, _Symbol, 0.0, sl, tp, "Flip short");
   }
   return false;
}

//--- EA
int OnInit()
{
   // Warn if not netting
   long margin_mode = AccountInfoInteger(ACCOUNT_MARGIN_MODE);
   // 2 = ACCOUNT_MARGIN_MODE_RETAIL_NETTING on most builds; ignore if unknown on very old builds
   g_lastTradeTime = 0;
   g_currBarTime   = 0;
   g_flipsThisBar  = 0;
   return(INIT_SUCCEEDED);
}

void OnTick()
{
   if(Bars(_Symbol, InpTF) < 10) return;

   datetime times[2];
   if(CopyTime(_Symbol, InpTF, 0, 2, times) != 2) return;
   datetime currBar = times[0];
   if(currBar != g_currBarTime)
   {
      g_currBarTime = currBar;
      g_flipsThisBar = 0;
   }

   double opens[1];
   if(CopyOpen(_Symbol, InpTF, 0, 1, opens) != 1) return;
   double candleOpen = opens[0];

   double mid = MidPrice();
   double thr = InpHysteresisPts * _Point;

   bool wantLong  = (mid > candleOpen + thr);
   bool wantShort = (mid < candleOpen - thr);
   if(!(wantLong || wantShort)) return;

   if(g_lastTradeTime>0 && (TimeCurrent() - g_lastTradeTime) < InpCooldownSec) return;
   if(g_flipsThisBar >= InpMaxFlipsPerBar) return;

   int curDir = GetPositionDir();

   if(wantLong)
   {
      if(curDir==-1)
      {
         if(CloseIfDir(-1) && PlaceOrder(1))
         {
            g_lastTradeTime = TimeCurrent();
            g_flipsThisBar++;
         }
      }
      else if(curDir==0 && PlaceOrder(1))
      {
         g_lastTradeTime = TimeCurrent();
         g_flipsThisBar++;
      }
   }
   else if(wantShort)
   {
      if(curDir==1)
      {
         if(CloseIfDir(1) && PlaceOrder(-1))
         {
            g_lastTradeTime = TimeCurrent();
            g_flipsThisBar++;
         }
      }
      else if(curDir==0 && PlaceOrder(-1))
      {
         g_lastTradeTime = TimeCurrent();
         g_flipsThisBar++;
      }
   }
}
