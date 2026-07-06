import React from "react";
import { AlertTriangle, RotateCcw, ShieldCheck, ShieldX } from "lucide-react";
import { Widget } from "./Widget";
import { fmt } from "@/lib/format";

export function ReconcilePanel({ state, recon, reconBusy, checkReconcile, resolveReconcile }) {
  return (
          <Widget title="Broker Reconciliation" testid="reconcile-widget"
            icon={<ShieldCheck className="h-3.5 w-3.5 text-slate-500" />}
            right={
              <button onClick={checkReconcile} disabled={reconBusy || !state.angel.connected}
                data-testid="reconcile-check-button"
                className="font-mono text-[10px] uppercase tracking-wider border border-slate-300 px-2 py-1 hover:bg-slate-50 disabled:opacity-40 flex items-center gap-1">
                <RotateCcw className={`h-3 w-3 ${reconBusy ? "animate-spin" : ""}`} /> Check Angel One
              </button>
            }>
            {!state.angel.connected ? (
              <p className="font-mono text-[11px] text-slate-400">Connect Angel One to verify the bot's position against your real broker position on restart.</p>
            ) : !recon ? (
              <p className="font-mono text-[11px] text-slate-400">After each restart, click <b>Check Angel One</b> to confirm the bot and your broker agree on the open position.</p>
            ) : !recon.available ? (
              <p className="font-mono text-[11px] text-amber-600" data-testid="reconcile-unavailable">{recon.reason}</p>
            ) : recon.state === "GOOD" ? (
              <div data-testid="reconcile-good" className="flex items-start gap-2">
                <ShieldCheck className="h-4 w-4 text-emerald-600 mt-0.5" />
                <div>
                  <p className="font-mono text-xs font-semibold text-emerald-700">Everything is good</p>
                  <p className="font-mono text-[11px] text-slate-500 mt-1">{recon.message}</p>
                  <p className="font-mono text-[10px] text-slate-400 mt-1">Broker net qty: {recon.broker_netqty}</p>
                </div>
              </div>
            ) : recon.state === "ENTRY_MISSED" ? (
              <div data-testid="reconcile-entry-missed">
                <div className="flex items-start gap-2">
                  <AlertTriangle className="h-4 w-4 text-amber-600 mt-0.5" />
                  <div>
                    <p className="font-mono text-xs font-semibold text-amber-700">Short trade missed</p>
                    <p className="font-mono text-[11px] text-slate-500 mt-1">{recon.message}</p>
                    <p className="font-mono text-[10px] text-slate-400 mt-1">Bot qty: {recon.bot_position?.qty} · Broker net qty: {recon.broker_netqty}</p>
                  </div>
                </div>
                <div className="flex gap-2 mt-3">
                  <button onClick={() => resolveReconcile("reenter")} disabled={reconBusy}
                    data-testid="reconcile-reenter-button"
                    className="flex-1 font-mono text-[11px] uppercase tracking-wider bg-red-600 hover:bg-red-700 text-white px-3 py-1.5 disabled:opacity-40">
                    Take trade again
                  </button>
                  <button onClick={() => resolveReconcile("accept")} disabled={reconBusy}
                    data-testid="reconcile-accept-button"
                    className="font-mono text-[11px] uppercase tracking-wider border border-slate-300 px-3 py-1.5 hover:bg-slate-50 disabled:opacity-40">
                    Ignore
                  </button>
                </div>
              </div>
            ) : (
              <div data-testid="reconcile-exit-missed">
                <div className="flex items-start gap-2">
                  <ShieldX className="h-4 w-4 text-red-600 mt-0.5" />
                  <div>
                    <p className="font-mono text-xs font-semibold text-red-700">Exit missed</p>
                    <p className="font-mono text-[11px] text-slate-500 mt-1">{recon.message}</p>
                    <p className="font-mono text-[10px] text-slate-400 mt-1">Broker net qty: {recon.broker_netqty} @ {fmt(recon.broker_avgprice)}</p>
                  </div>
                </div>
                <div className="flex gap-2 mt-3">
                  <button onClick={() => resolveReconcile("reexit")} disabled={reconBusy}
                    data-testid="reconcile-reexit-button"
                    className="flex-1 font-mono text-[11px] uppercase tracking-wider bg-red-600 hover:bg-red-700 text-white px-3 py-1.5 disabled:opacity-40">
                    Exit trade again
                  </button>
                  <button onClick={() => resolveReconcile("accept")} disabled={reconBusy}
                    data-testid="reconcile-accept-button-2"
                    title="Accept Angel One as correct and sync the bot to match (clears the warning)"
                    className="font-mono text-[11px] uppercase tracking-wider border border-slate-300 px-3 py-1.5 hover:bg-slate-50 disabled:opacity-40">
                    Sync to broker
                  </button>
                </div>
              </div>
            )}
          </Widget>
  );
}
