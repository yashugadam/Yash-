import React from "react";
import { Activity, Lock } from "lucide-react";
import { Widget } from "./Widget";
import { fmt } from "@/lib/format";

export function OrderBookPanel({ state }) {
  return (
          <Widget title="Order Book" testid="orders-widget" icon={<Activity className="h-3.5 w-3.5 text-slate-500" />}>
            <div className="space-y-1.5 max-h-[160px] overflow-y-auto">
              {state.orders.length === 0 && <p className="font-mono text-[11px] text-slate-400 text-center py-3">No orders yet</p>}
              {state.orders.map((o) => (
                <div key={o.id} className="flex items-center justify-between border border-slate-100 px-2 py-1.5" data-testid={`order-${o.id}`}>
                  <div className="flex items-center gap-2">
                    <span className={`font-mono text-[10px] px-1.5 py-0.5 ${o.side === "SELL" ? "bg-red-50 text-red-600" : "bg-blue-50 text-blue-600"}`}>{o.side}</span>
                    <span className="font-mono text-[11px] text-slate-600">LMT {fmt(o.limit_price)}</span>
                  </div>
                  <span className={`font-mono text-[10px] uppercase ${o.status === "COMPLETE" ? "text-emerald-600" : o.status === "RETRYING" ? "text-amber-600" : "text-slate-400"}`}>
                    {o.status === "RETRYING" ? `Retry x${o.attempts}` : o.status}
                  </span>
                </div>
              ))}
            </div>
            <p className="font-mono text-[10px] text-slate-400 mt-2 flex items-center gap-1"><Lock className="h-3 w-3" /> Limit orders only · {state.settings.buffer_points}pt buffer (SEBI safe)</p>
          </Widget>
  );
}
