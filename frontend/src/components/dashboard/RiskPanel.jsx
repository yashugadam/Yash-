import React from "react";
import { AlertTriangle, ShieldAlert, ShieldCheck, ShieldX } from "lucide-react";
import { Widget } from "./Widget";
import { fmt, pnlClass, sign } from "@/lib/format";

export function RiskPanel({ state, armBreaker }) {
  return (
          <Widget title="Risk · Circuit Breaker" testid="risk-widget"
            icon={<ShieldAlert className="h-3.5 w-3.5 text-slate-500" />}
            right={state.risk.breaker_tripped
              ? <span className="bg-red-600 text-white px-2 py-0.5 text-[10px] font-mono uppercase flex items-center gap-1"><ShieldX className="h-3 w-3" /> Tripped</span>
              : <span className="bg-emerald-100 text-emerald-700 border border-emerald-200 px-2 py-0.5 text-[10px] font-mono uppercase">Armed</span>}>
            <div className="grid grid-cols-2 gap-2">
              <div>
                <p className="font-mono text-[10px] uppercase text-slate-400">Day P&L (₹)</p>
                <p className={`font-mono text-lg font-bold ${pnlClass(state.risk.day_total)}`} data-testid="day-pnl">{sign(state.risk.day_total)}{fmt(state.risk.day_total)}</p>
              </div>
              <div>
                <p className="font-mono text-[10px] uppercase text-slate-400">Max Loss (₹)</p>
                <p className="font-mono text-lg font-bold text-red-500">-{fmt(state.risk.daily_max_loss, 0)}</p>
              </div>
            </div>
            {state.risk.broker_pnl && (
              <div className="mt-3 pt-3 border-t border-slate-100" data-testid="broker-pnl-block">
                <p className="font-mono text-[10px] uppercase tracking-widest text-slate-400 flex items-center gap-1">
                  Angel One Day P&L {state.market_open ? "(live)" : "(at close)"}
                  {!state.market_open
                    ? <span className="bg-amber-100 text-amber-700 px-1.5 py-0.5 text-[9px]">FROZEN</span>
                    : state.risk.broker_pnl.found
                    ? <span className="bg-emerald-100 text-emerald-700 px-1.5 py-0.5 text-[9px]">SYNCED</span>
                    : <span className="bg-slate-100 text-slate-500 px-1.5 py-0.5 text-[9px]">—</span>}
                </p>
                <div className="grid grid-cols-3 gap-2 mt-1">
                  <div>
                    <p className="font-mono text-[9px] uppercase text-slate-400">Realised</p>
                    <p className={`font-mono text-sm font-bold ${pnlClass(state.risk.broker_pnl.realised)}`} data-testid="broker-realised-pnl">{sign(state.risk.broker_pnl.realised)}{fmt(state.risk.broker_pnl.realised)}</p>
                  </div>
                  <div>
                    <p className="font-mono text-[9px] uppercase text-slate-400">Unrealised</p>
                    <p className={`font-mono text-sm font-bold ${pnlClass(state.risk.broker_pnl.unrealised)}`} data-testid="broker-unrealised-pnl">{sign(state.risk.broker_pnl.unrealised)}{fmt(state.risk.broker_pnl.unrealised)}</p>
                  </div>
                  <div>
                    <p className="font-mono text-[9px] uppercase text-slate-400">Total</p>
                    <p className={`font-mono text-sm font-bold ${pnlClass(state.risk.broker_pnl.total)}`} data-testid="broker-total-pnl">{sign(state.risk.broker_pnl.total)}{fmt(state.risk.broker_pnl.total)}</p>
                  </div>
                </div>
                <p className="font-mono text-[9px] text-slate-400 mt-1">{state.market_open ? "Real booked P&L from your Angel One account — includes manual orders." : "Market closed — showing last value from market hours (Angel One sends unreliable data after close)."}</p>
              </div>
            )}
            {/* loss progress bar */}
            <div className="mt-2 h-1.5 w-full bg-slate-100">
              <div className="h-1.5 bg-red-500 transition-all" style={{ width: `${Math.min(100, Math.max(0, (-Math.min(0, state.risk.day_total) / state.risk.daily_max_loss) * 100))}%` }} />
            </div>
            {state.risk.breaker_tripped ? (
              <div className="mt-3">
                <p className="font-mono text-[10px] text-red-600 flex items-center gap-1"><AlertTriangle className="h-3 w-3" /> Breaker tripped — bot auto-stopped & entries blocked for today.</p>
                <button onClick={armBreaker} data-testid="rearm-button"
                  className="w-full mt-2 border border-slate-300 hover:bg-slate-50 text-slate-700 font-mono uppercase text-[11px] tracking-wider px-3 py-1.5 transition-colors flex items-center justify-center gap-2">
                  <ShieldCheck className="h-3.5 w-3.5" /> Re-arm breaker
                </button>
              </div>
            ) : (
              <p className="font-mono text-[10px] text-slate-400 mt-2">Auto-stops the bot & squares off if today's P&L (realized + unrealized) hits -₹{fmt(state.risk.daily_max_loss, 0)}.</p>
            )}
          </Widget>
  );
}
