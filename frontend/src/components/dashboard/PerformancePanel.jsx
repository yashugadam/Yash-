import React from "react";
import { Zap } from "lucide-react";
import { Widget } from "./Widget";
import { fmt, pnlClass, sign } from "@/lib/format";

export function PerformancePanel({ m }) {
  return (
          <Widget title="Performance" testid="metrics-widget" icon={<Zap className="h-3.5 w-3.5 text-slate-500" />}>
            <p className="font-mono text-[10px] uppercase tracking-widest text-slate-400">Total P&L (₹)</p>
            <p className={`font-mono text-3xl font-bold tabular-nums mt-1 ${pnlClass(m.realized_pnl)}`} data-testid="total-pnl">
              {sign(m.realized_pnl)}{fmt(m.realized_pnl)}
            </p>
            <div className="grid grid-cols-3 gap-2 mt-4 border-t border-slate-100 pt-3">
              <div><p className="font-mono text-[10px] uppercase text-slate-400">Trades</p><p className="font-mono text-lg font-semibold">{m.trades}</p></div>
              <div><p className="font-mono text-[10px] uppercase text-slate-400">Win %</p><p className="font-mono text-lg font-semibold text-emerald-600">{fmt(m.win_rate, 1)}</p></div>
              <div><p className="font-mono text-[10px] uppercase text-slate-400">W / L</p><p className="font-mono text-lg font-semibold">{m.wins}/{m.losses}</p></div>
            </div>
          </Widget>
  );
}
