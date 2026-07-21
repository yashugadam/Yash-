import React from "react";
import {
  Activity, AlertTriangle, ChevronDown, ChevronUp, History,
  Layers, LogOut, Play, RotateCcw, Square, Zap,
} from "lucide-react";
import { fmt } from "@/lib/format";

export function DashboardHeader({ state, flash, priceUp, reset, setShowBacktest, onLogout, startStop }) {
  return (
      <header className="flex items-center justify-between border-b border-slate-200 bg-white px-4 py-2.5 sticky top-0 z-50">
        <div className="flex items-center gap-3">
          <div className="h-8 w-8 bg-slate-900 flex items-center justify-center">
            <Layers className="h-4 w-4 text-white" strokeWidth={2} />
          </div>
          <div>
            <h1 className="font-heading font-extrabold text-base tracking-tight leading-none">RENKO ALGO</h1>
            <p className="font-mono text-[10px] uppercase tracking-[0.12em] text-slate-400 leading-none mt-0.5">Nifty Fut · Traditional · {state.settings.brick_size}pt</p>
          </div>
        </div>

        <div className={`flex items-center gap-4 px-4 py-1.5 border border-slate-200 ${flash}`} data-testid="ticker-price">
          <span className="font-mono text-[10px] uppercase tracking-widest text-slate-400">{state.settings.symbol}</span>
          <span className="font-mono text-xl font-bold tabular-nums">{fmt(state.price)}</span>
          {priceUp ? <ChevronUp className="h-4 w-4 text-emerald-600" /> : <ChevronDown className="h-4 w-4 text-red-500" />}
        </div>

        <div className="flex items-center gap-2">
          {typeof state.state_age_sec === "number" && state.state_age_sec > 30 && (
            <span className="bg-red-600 text-white border border-red-700 px-2 py-1 text-[11px] font-mono uppercase flex items-center gap-1 animate-pulse" data-testid="stale-state-badge" title="No active trading pod has updated the state recently. The bot may not be actively monitoring the market. Keep this tab open during market hours.">
              <AlertTriangle className="h-3 w-3" /> Trading pod idle
            </span>
          )}
          {(() => {
            const a = state.angel || {};
            const label = !a.connected ? "Disconnected"
              : !a.future ? "Loading instruments…"
              : a.streaming ? `Streaming · ${a.future}` : `Armed · ${a.future}`;
            const cls = !a.connected ? "bg-red-100 text-red-700 border-red-200"
              : !a.future ? "bg-amber-100 text-amber-800 border-amber-200 animate-pulse"
              : a.streaming ? "bg-emerald-100 text-emerald-800 border-emerald-200"
              : "bg-blue-100 text-blue-800 border-blue-200";
            const tip = !a.connected ? (state.feed_error || "Angel One not connected")
              : !a.future ? "Connected — downloading the instrument list (first-time can take ~2 min). Trading arms once the NIFTY contract resolves."
              : a.streaming ? "Live LTP streaming over websocket"
              : (state.feed_error || `Armed on ${a.future} — live feed starts at market open`);
            return (
              <span className={`px-2 py-1 text-[11px] font-mono uppercase flex items-center gap-1 border ${cls}`}
                data-testid="feed-badge" title={tip}>
                <Activity className="h-3 w-3" /> {label}
              </span>
            );
          })()}
          <span className="bg-red-600 text-white border border-red-700 px-2 py-1 text-[11px] font-mono uppercase flex items-center gap-1" data-testid="mode-badge" title="This bot trades LIVE with real money on Angel One">
            <Zap className="h-3 w-3" /> Live · Real Money
          </span>
          <span className={`px-2 py-1 text-[11px] font-mono uppercase flex items-center gap-1 border ${state.market_open ? "bg-emerald-100 text-emerald-800 border-emerald-200" : "bg-amber-100 text-amber-800 border-amber-200"}`} data-testid="market-badge" title={state.market_open ? "NSE market open (09:15–15:30 IST)" : "Market closed — bot is idle, no orders are placed"}>
            <span className={`h-1.5 w-1.5 rounded-full ${state.market_open ? "bg-emerald-500 pulse-dot" : "bg-amber-500"}`} /> {state.market_open ? "Mkt Open" : "Mkt Closed"}
          </span>
          <span className={`px-2 py-1 text-[11px] font-mono uppercase flex items-center gap-1 border ${state.running ? "bg-emerald-100 text-emerald-800 border-emerald-200" : "bg-slate-100 text-slate-500 border-slate-200"}`} data-testid="status-badge">
            <span className={`h-1.5 w-1.5 rounded-full ${state.running ? "bg-emerald-500 pulse-dot" : "bg-slate-400"}`} /> {state.running ? "Live" : "Idle"}
          </span>
          <button onClick={reset} className="border border-slate-300 hover:bg-slate-50 text-slate-600 px-3 py-1.5 transition-colors" data-testid="reset-button" title="Reset session">
            <RotateCcw className="h-3.5 w-3.5" />
          </button>
          <button onClick={() => setShowBacktest(true)} className="border border-slate-300 hover:bg-slate-50 text-slate-600 px-3 py-1.5 transition-colors flex items-center gap-1.5 text-xs font-mono uppercase tracking-wider" data-testid="backtest-button" title="Backtest the strategy on historical data">
            <History className="h-3.5 w-3.5" /> Backtest
          </button>
          <button onClick={onLogout} className="border border-slate-300 hover:bg-slate-50 text-slate-600 px-3 py-1.5 transition-colors" data-testid="logout-button" title="Log out">
            <LogOut className="h-3.5 w-3.5" />
          </button>
          <button onClick={startStop} data-testid="start-stop-button"
            className={`font-mono uppercase text-xs tracking-wider px-4 py-1.5 transition-colors flex items-center gap-2 text-white ${state.running ? "bg-red-600 hover:bg-red-700" : "bg-emerald-600 hover:bg-emerald-700"}`}>
            {state.running ? <Square className="h-3.5 w-3.5" /> : <Play className="h-3.5 w-3.5" />}
            {state.running ? "Stop" : "Start"}
          </button>
        </div>
      </header>
  );
}
