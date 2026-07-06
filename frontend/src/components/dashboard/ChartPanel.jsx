import React from "react";
import { Activity, History } from "lucide-react";
import { Widget } from "./Widget";
import RenkoChart from "@/components/RenkoChart";

export function ChartPanel({ state, loadHistory, trades }) {
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
            <RenkoChart bricks={state.bricks} trades={trades} />
          </Widget>
  );
}
