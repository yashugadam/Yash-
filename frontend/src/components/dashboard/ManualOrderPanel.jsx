import React from "react";
import { Zap } from "lucide-react";
import { Widget } from "./Widget";

export function ManualOrderPanel({ state, manualOrder, manualBusy }) {
  return (
          <Widget title="Manual Order (test)" testid="manual-order-widget"
            icon={<Zap className="h-3.5 w-3.5 text-slate-500" />}>
            <p className="font-mono text-[11px] text-slate-400 mb-3">
              Places a REAL 1-lot ({state.settings?.lot_size}) LIMIT order near LTP to verify execution. To flatten: Sell closes a long, Buy closes a short. Only works from your whitelisted IP (production).
            </p>
            <div className="grid grid-cols-2 gap-2">
              <button onClick={() => manualOrder("BUY")} disabled={manualBusy || !state.angel.connected}
                data-testid="manual-buy-button"
                className="font-mono text-[11px] uppercase tracking-wider bg-emerald-600 hover:bg-emerald-700 text-white px-3 py-2 disabled:opacity-40">
                Buy 1 lot
              </button>
              <button onClick={() => manualOrder("SELL")} disabled={manualBusy || !state.angel.connected}
                data-testid="manual-sell-button"
                className="font-mono text-[11px] uppercase tracking-wider bg-red-600 hover:bg-red-700 text-white px-3 py-2 disabled:opacity-40">
                Sell 1 lot
              </button>
            </div>
            <p className="font-mono text-[10px] text-slate-400 mt-2">Result & exact reason appear in the Order Log below the chart.</p>
          </Widget>
  );
}
