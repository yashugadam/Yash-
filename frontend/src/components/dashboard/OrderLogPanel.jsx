import React from "react";
import { History } from "lucide-react";
import { Widget } from "./Widget";
import { fmt } from "@/lib/format";

export function OrderLogPanel({ orderLog, clearOrderLog, state }) {
  return (
          <Widget title="Order Log" testid="order-log-widget"
            icon={<History className="h-3.5 w-3.5 text-slate-500" />}
            right={
              <div className="flex items-center gap-2">
                <span className="font-mono text-[11px] text-slate-400">{orderLog.length} orders</span>
                {orderLog.length > 0 && (
                  <button onClick={clearOrderLog} data-testid="clear-order-log-button"
                    className="font-mono text-[10px] uppercase tracking-wider border border-slate-300 hover:bg-slate-50 text-slate-600 px-2 py-0.5 transition-colors">
                    Clear
                  </button>
                )}
              </div>
            }>
            {!state.market_open && (
              <div className="bg-amber-50 border-b border-amber-100 px-3 py-1.5 font-mono text-[10px] text-amber-700" data-testid="order-log-closed-note">
                Market closed — bot is idle, no new orders are being placed. Rows below are past history.
              </div>
            )}
            <div className="overflow-x-auto max-h-[280px] overflow-y-auto">
              <table className="w-full text-sm border-collapse">
                <thead className="sticky top-0">
                  <tr>
                    {["Time", "Side", "Type", "Status", "Limit", "Fill", "Reason / Reject detail"].map((h) => (
                      <th key={h} className="text-[10px] uppercase text-slate-500 bg-slate-50 px-3 py-2 font-mono text-left border-b border-slate-200">{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {orderLog.length === 0 && (
                    <tr><td colSpan={7} className="text-center py-8 font-mono text-xs text-slate-400">No orders yet — placed orders & rejection reasons will appear here</td></tr>
                  )}
                  {orderLog.map((o) => (
                    <tr key={o.id} className="hover:bg-slate-50 transition-colors" data-testid={`order-row-${o.id}`}>
                      <td className="font-mono text-xs px-3 py-2 border-b border-slate-100 text-slate-400 whitespace-nowrap">{o.time ? new Date(o.time).toLocaleString("en-IN", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", second: "2-digit", timeZone: "Asia/Kolkata" }) : "--"}</td>
                      <td className="font-mono text-xs px-3 py-2 border-b border-slate-100">{o.side}</td>
                      <td className="font-mono text-xs px-3 py-2 border-b border-slate-100">{o.kind}</td>
                      <td className="font-mono text-xs px-3 py-2 border-b border-slate-100">
                        <span className={`px-1.5 py-0.5 border ${o.status === "COMPLETE" ? "bg-emerald-50 text-emerald-700 border-emerald-100" : "bg-red-50 text-red-600 border-red-100"}`}>{o.status}</span>
                      </td>
                      <td className="font-mono text-xs px-3 py-2 border-b border-slate-100">{fmt(o.limit_price)}</td>
                      <td className="font-mono text-xs px-3 py-2 border-b border-slate-100">{o.fill_price ? fmt(o.fill_price) : "--"}</td>
                      <td className={`font-mono text-[11px] px-3 py-2 border-b border-slate-100 ${o.status === "REJECTED" ? "text-red-600" : "text-slate-500"}`} title={o.note}>{o.note}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Widget>
  );
}
