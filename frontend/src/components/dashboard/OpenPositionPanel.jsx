import React from "react";
import { TrendingDown, TrendingUp, XCircle } from "lucide-react";
import { Widget } from "./Widget";
import { fmt, pnlClass, sign } from "@/lib/format";

export function OpenPositionPanel({ pos, state, m, squareOff }) {
  const isLong = pos?.side === "LONG";
  const runTrend = isLong ? "Greens" : "Reds";
  const exitBrick = isLong ? "red" : "green";
  const needTwo = state.down_run_reds > state.settings.max_red_single_green;
  const exitCount = needTwo ? state.settings.greens_to_exit_extended : 1;
  return (
          <Widget title="Open Position" testid="position-widget" icon={<TrendingDown className="h-3.5 w-3.5 text-slate-500" />}>
            {pos ? (
              <div className={`border p-3 ${isLong ? "border-emerald-200 bg-emerald-50/50" : "border-red-200 bg-red-50/50"}`} data-testid="active-position">
                <div className="flex items-center justify-between">
                  <span className={`text-white px-2 py-0.5 font-mono text-xs uppercase flex items-center gap-1 ${isLong ? "bg-emerald-600" : "bg-red-600"}`} data-testid="position-side">
                    {isLong ? <TrendingUp className="h-3 w-3" /> : <TrendingDown className="h-3 w-3" />}{pos.side}
                  </span>
                  <span className="font-mono text-xs text-slate-500">{pos.qty} qty</span>
                </div>
                <div className="grid grid-cols-2 gap-2 mt-3">
                  <div><p className="font-mono text-[10px] uppercase text-slate-400">Entry</p><p className="font-mono text-sm font-semibold">{fmt(pos.entry_price)}</p></div>
                  <div><p className="font-mono text-[10px] uppercase text-slate-400">LTP</p><p className="font-mono text-sm font-semibold">{fmt(state.price)}</p></div>
                </div>
                <div className={`mt-3 border-t pt-2 ${isLong ? "border-emerald-200" : "border-red-200"}`}>
                  <p className="font-mono text-[10px] uppercase text-slate-400">Unrealized P&L</p>
                  <p className={`font-mono text-xl font-bold ${pnlClass(m.unrealized_pnl)}`} data-testid="unrealized-pnl">{sign(m.unrealized_pnl)}{fmt(m.unrealized_pnl)}</p>
                </div>
                <p className="font-mono text-[10px] text-slate-400 mt-2">{runTrend} in run: {state.down_run_reds} · exit on {exitCount} {exitBrick}{exitCount > 1 ? "s" : ""}</p>
                <button onClick={squareOff} data-testid="square-off-button"
                  className={`w-full mt-3 border font-mono uppercase text-[11px] tracking-wider px-3 py-1.5 transition-colors flex items-center justify-center gap-2 ${isLong ? "border-emerald-300 hover:bg-emerald-100 text-emerald-700" : "border-red-300 hover:bg-red-100 text-red-700"}`}>
                  <XCircle className="h-3.5 w-3.5" /> Square off now
                </button>
              </div>
            ) : (
              <div className="text-center py-6" data-testid="no-position">
                <p className="font-mono text-xs uppercase tracking-widest text-slate-400">Flat — no position</p>
                <p className="font-mono text-[11px] text-slate-300 mt-1">{state.pending_entry ? "Entry order placing…" : state.expiry.entries_blocked ? "Entries blocked (expiry square-off window)" : "Waiting for 2 red (short) or 2 green (long) bricks"}</p>
              </div>
            )}
          </Widget>
  );
}
