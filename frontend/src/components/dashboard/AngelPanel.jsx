import React from "react";
import { Activity, Lock, Search, ShieldCheck } from "lucide-react";
import { Widget } from "./Widget";

export function AngelPanel({ state, showInstSearch, setShowInstSearch, searchInstruments, instQuery, instResults, selectInstrument, disconnectAngel, connectAngel }) {
  return (
          <Widget title="Angel One" testid="angel-widget" icon={<Lock className="h-3.5 w-3.5 text-slate-500" />}
            right={state.angel.connected
              ? <span className="bg-emerald-100 text-emerald-700 border border-emerald-200 px-2 py-0.5 text-[10px] font-mono uppercase">Connected</span>
              : <span className="font-mono text-[10px] text-amber-600 uppercase">Not connected</span>}>
            {state.angel.connected ? (
              <div data-testid="angel-connected">
                <div className="grid grid-cols-2 gap-2">
                  <div><p className="font-mono text-[10px] uppercase text-slate-400">Client</p><p className="font-mono text-sm font-semibold">{state.angel.client_code || "—"}</p></div>
                  <div><p className="font-mono text-[10px] uppercase text-slate-400">Lot Size</p><p className="font-mono text-sm font-semibold">{state.angel.lotsize ?? "—"}</p></div>
                  <div className="col-span-2"><p className="font-mono text-[10px] uppercase text-slate-400">Future</p><p className="font-mono text-sm font-semibold break-all">{state.angel.future || "—"}</p></div>
                  <div className="col-span-2"><p className="font-mono text-[10px] uppercase text-slate-400">Expiry</p><p className="font-mono text-sm font-semibold">{state.angel.expiry || "—"}</p></div>
                </div>
                {/* contract search / selector */}
                <button onClick={() => { setShowInstSearch(!showInstSearch); if (!showInstSearch) searchInstruments("NIFTY"); }}
                  data-testid="change-instrument-btn"
                  className="w-full mt-3 border border-slate-300 hover:bg-slate-50 text-slate-700 font-mono text-[11px] uppercase px-3 py-1.5 transition-colors flex items-center justify-center gap-2">
                  <Search className="h-3.5 w-3.5" /> Change contract
                </button>
                {showInstSearch && (
                  <div className="mt-2 border border-slate-200 p-2" data-testid="instrument-search">
                    <input autoFocus value={instQuery} onChange={(e) => searchInstruments(e.target.value)}
                      placeholder="Search e.g. NIFTY, BANKNIFTY, RELIANCE"
                      className="w-full border border-slate-300 px-2 py-1.5 font-mono text-xs focus:outline-none focus:border-slate-900" data-testid="instrument-search-input" />
                    <div className="max-h-44 overflow-y-auto mt-1">
                      {instResults.length === 0 && <p className="font-mono text-[11px] text-slate-400 text-center py-2">No matches</p>}
                      {instResults.map((it) => (
                        <button key={it.token} onClick={() => selectInstrument(it.token, it.symbol)}
                          data-testid={`instrument-${it.token}`}
                          className={`w-full text-left px-2 py-1.5 border-b border-slate-100 hover:bg-slate-50 transition-colors ${it.token === state.angel.token ? "bg-blue-50" : ""}`}>
                          <div className="flex items-center justify-between">
                            <span className="font-mono text-[11px] font-semibold">{it.symbol}</span>
                            <span className="font-mono text-[10px] text-slate-400">lot {it.lotsize}</span>
                          </div>
                          <span className="font-mono text-[10px] text-slate-400">{it.name} · exp {it.expiry} · {it.type}</span>
                        </button>
                      ))}
                    </div>
                  </div>
                )}
                <button onClick={disconnectAngel} data-testid="angel-disconnect-button"
                  className="w-full mt-3 border border-red-300 hover:bg-red-50 text-red-700 font-mono text-xs uppercase px-4 py-2 transition-colors">Disconnect</button>
              </div>
            ) : (
              <div data-testid="angel-disconnected">
                <p className="font-mono text-[11px] text-slate-500">Credentials are read from the server <span className="text-slate-700">.env</span> (API key, client code, MPIN, TOTP secret).</p>
                <button onClick={connectAngel} data-testid="angel-connect-button"
                  className="w-full mt-3 bg-slate-900 hover:bg-slate-800 text-white font-mono text-xs uppercase px-4 py-2 transition-colors flex items-center justify-center gap-2">
                  <Activity className="h-3.5 w-3.5" /> Connect (live data)
                </button>
                {state.angel.error && <p className="font-mono text-[10px] text-red-600 mt-2 break-all">{state.angel.error}</p>}
              </div>
            )}
            <p className="font-mono text-[10px] text-slate-400 mt-2 flex items-center gap-1"><ShieldCheck className="h-3 w-3" /> Live DATA only — orders stay PAPER. No real orders are placed.</p>
          </Widget>
  );
}
