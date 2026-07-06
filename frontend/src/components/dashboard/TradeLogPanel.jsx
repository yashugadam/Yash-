import React from "react";
import { Layers } from "lucide-react";
import { Widget } from "./Widget";
import { fmt, pnlClass, sign } from "@/lib/format";

export function TradeLogPanel({ trades }) {
  return (
          <Widget title="Trade Log" testid="trades-widget"
            icon={<Layers className="h-3.5 w-3.5 text-slate-500" />}
            right={<span className="font-mono text-[11px] text-slate-400">{trades.length} closed</span>}>
            <div className="overflow-x-auto max-h-[280px] overflow-y-auto">
              <table className="w-full text-sm border-collapse">
                <thead className="sticky top-0">
                  <tr>
                    {["Side", "Qty", "Entry", "Exit", "Reds", "P&L", "Exit Time"].map((h) => (
                      <th key={h} className="text-[10px] uppercase text-slate-500 bg-slate-50 px-3 py-2 font-mono text-left border-b border-slate-200">{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {trades.length === 0 && (
                    <tr><td colSpan={7} className="text-center py-8 font-mono text-xs text-slate-400">No closed trades yet</td></tr>
                  )}
                  {trades.map((t) => (
                    <tr key={t.id} className="hover:bg-slate-50 transition-colors" data-testid={`trade-row-${t.id}`}>
                      <td className="font-mono text-xs px-3 py-2 border-b border-slate-100"><span className="bg-red-50 text-red-600 px-1.5 py-0.5 border border-red-100">{t.side}</span></td>
                      <td className="font-mono text-xs px-3 py-2 border-b border-slate-100">{t.qty}</td>
                      <td className="font-mono text-xs px-3 py-2 border-b border-slate-100">{fmt(t.entry_price)}</td>
                      <td className="font-mono text-xs px-3 py-2 border-b border-slate-100">{fmt(t.exit_price)}</td>
                      <td className="font-mono text-xs px-3 py-2 border-b border-slate-100">{t.reds}</td>
                      <td className={`font-mono text-xs px-3 py-2 border-b border-slate-100 font-semibold ${pnlClass(t.pnl)}`}>{sign(t.pnl)}{fmt(t.pnl)}</td>
                      <td className="font-mono text-xs px-3 py-2 border-b border-slate-100 text-slate-400">{new Date(t.exit_time).toLocaleString("en-IN", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", second: "2-digit", timeZone: "Asia/Kolkata" })}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Widget>
  );
}
