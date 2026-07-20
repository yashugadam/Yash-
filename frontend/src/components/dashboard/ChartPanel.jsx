import React from "react";
import { Activity, History, TrendingUp, TrendingDown } from "lucide-react";
import { Widget } from "./Widget";
import RenkoChart from "@/components/RenkoChart";

function UnlockPill({ side, data, threshold }) {
  const isLong = side === "LONG";
  const Icon = isLong ? TrendingUp : TrendingDown;
  const cls = isLong ? "text-emerald-600 border-emerald-200 bg-emerald-50"
                     : "text-red-600 border-red-200 bg-red-50";
  const price = data?.unlock_price;
  return (
    <span data-testid={`er-unlock-pill-${side.toLowerCase()}`}
      className={`flex items-center gap-1 px-2 py-0.5 border ${cls}`}
      title={price != null
        ? `${side} arms after +${data.bricks} brick(s) → ER ${data.er} (need ≥ ${threshold})`
        : `${side} cannot reach ER ${threshold} within 15 bricks (deep chop)`}>
      <Icon className="h-3 w-3" />
      {price != null
        ? <>{isLong ? "≥" : "≤"} <b>{price}</b> <span className="opacity-60">+{data.bricks}brk</span></>
        : <>—</>}
    </span>
  );
}

export function ChartPanel({ state, loadHistory, trades }) {
  const proj = state.er_projection;
  return (
          <Widget title="Renko Chart" testid="chart-widget"
            icon={<Activity className="h-3.5 w-3.5 text-slate-500" />}
            right={
              <div className="flex items-center gap-3 font-mono text-[11px]">
                <button onClick={loadHistory} data-testid="load-history-btn"
                  className="flex items-center gap-1 px-2 py-0.5 border border-slate-200 uppercase text-slate-500 hover:bg-slate-50 transition-colors disabled:opacity-40"
                  disabled={!state.angel.connected} title={state.angel.connected ? "Load real 5-day history" : "Connect Angel One first"}>
                  <History className="h-3 w-3" /> History
                </button>
                <span className="flex items-center gap-1"><span className="h-2.5 w-2.5 bg-emerald-500 inline-block" /> Green</span>
                <span className="flex items-center gap-1"><span className="h-2.5 w-2.5 bg-red-500 inline-block" /> Red</span>
                <span className="text-slate-400">Bar <b className="text-slate-700">{state.ticks_in_bar}/{state.settings.bar_seconds}s</b> · Reds <b className="text-slate-700">{state.consec_red}</b> · Greens <b className="text-slate-700">{state.consec_green}</b></span>
              </div>
            }>
            {proj?.enabled && (
              <div className="flex flex-wrap items-center gap-2 px-3 py-1.5 border-b border-slate-100 font-mono text-[11px]"
                data-testid="er-unlock-bar">
                <span className="uppercase tracking-wide text-slate-400">ER Unlock</span>
                <span className="text-slate-500">now <b className="text-slate-700">{proj.current_er ?? "—"}</b> / thr {proj.threshold}</span>
                <UnlockPill side="LONG" data={proj.long} threshold={proj.threshold} />
                <UnlockPill side="SHORT" data={proj.short} threshold={proj.threshold} />
                <span className="text-slate-300 hidden sm:inline">recalculates each new brick</span>
              </div>
            )}
            <RenkoChart bricks={state.bricks} trades={trades} erProjection={proj} />
          </Widget>
  );
}
