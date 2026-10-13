import React from "react";
import { Save, Settings as SettingsIcon } from "lucide-react";
import { Widget } from "./Widget";

export function StrategySettingsPanel({ form, state, setForm, saveSettings }) {
  return (
            <Widget title="Strategy" testid="settings-widget" icon={<SettingsIcon className="h-3.5 w-3.5 text-slate-500" />}>
              <div className="grid grid-cols-2 gap-2">
                {[
                  ["Brick Size", "brick_size"], ["Bar Secs", "bar_seconds"],
                  ["Lot Size", "lot_size"], ["Buffer (pt)", "buffer_points"],
                  ["Max Slip (pt)", "max_slippage"], ["Expiry Slip (pt)", "forced_exit_slippage"],
                  ["Day Max Loss ₹", "daily_max_loss"],
                ].map(([label, key]) => (
                  <div key={key}>
                    <label className="font-mono text-[10px] uppercase text-slate-400 block mb-1">{label}</label>
                    <input type="number" value={form[key]} disabled={state.running}
                      onChange={(e) => setForm({ ...form, [key]: e.target.value })}
                      className="w-full border border-slate-300 px-2 py-1 font-mono text-xs focus:outline-none focus:border-slate-900 disabled:bg-slate-50 disabled:text-slate-400"
                      data-testid={`setting-${key}`} />
                  </div>
                ))}
              </div>
              <label className="flex items-center justify-between gap-2 mt-3 cursor-pointer select-none" data-testid="rollover-toggle-label">
                <span className="font-mono text-[10px] uppercase text-slate-500">Position rollover at expiry<br /><span className="text-slate-400 normal-case">re-open same side on next month after square-off</span></span>
                <input type="checkbox" checked={!!form.rollover_position} disabled={state.running}
                  onChange={(e) => setForm({ ...form, rollover_position: e.target.checked })}
                  className="h-4 w-4 accent-slate-900 disabled:opacity-40" data-testid="setting-rollover_position" />
              </label>

              <div className="mt-3 border-t border-slate-100 pt-3">
                <div className="flex items-center justify-between gap-2 mb-2">
                  <label className="font-mono text-[10px] uppercase text-slate-400">Entry — bricks to confirm</label>
                  <input type="number" min="1" max="6" value={form.entry_bricks ?? 2} disabled={state.running}
                    onChange={(e) => setForm({ ...form, entry_bricks: e.target.value })}
                    className="w-16 border border-slate-300 px-2 py-1 font-mono text-xs text-right focus:outline-none focus:border-slate-900 disabled:bg-slate-50 disabled:text-slate-400"
                    data-testid="setting-entry_bricks" />
                </div>
                <div className="flex items-center justify-between gap-2" data-testid="chop-filter-label">
                  <span className="font-mono text-[10px] uppercase text-slate-500">Chop filter (ER)<br /><span className="text-slate-400 normal-case">skip entries while the market is ranging (efficiency ratio)</span></span>
                  <span className="font-mono text-[9px] uppercase font-bold px-2 py-0.5 bg-emerald-100 text-emerald-700" data-testid="chop-filter-status">Always on</span>
                </div>
                <div className="grid grid-cols-2 gap-2 mt-2">
                    <div>
                      <label className="font-mono text-[10px] uppercase text-slate-400 block mb-1">ER threshold</label>
                      <input type="number" step="0.05" min="0.05" max="1" value={form.chop_threshold ?? 0.30} disabled={state.running}
                        onChange={(e) => setForm({ ...form, chop_threshold: e.target.value })}
                        className="w-full border border-slate-300 px-2 py-1 font-mono text-xs focus:outline-none focus:border-slate-900 disabled:bg-slate-50 disabled:text-slate-400"
                        data-testid="setting-chop_threshold" />
                    </div>
                    <div>
                      <label className="font-mono text-[10px] uppercase text-slate-400 block mb-1">Lookback (bricks)</label>
                      <input type="number" min="2" max="200" value={form.chop_lookback ?? 50} disabled={state.running}
                        onChange={(e) => setForm({ ...form, chop_lookback: e.target.value })}
                        className="w-full border border-slate-300 px-2 py-1 font-mono text-xs focus:outline-none focus:border-slate-900 disabled:bg-slate-50 disabled:text-slate-400"
                        data-testid="setting-chop_lookback" />
                    </div>
                  </div>
                <div className="flex items-center justify-between gap-2 mt-2" data-testid="chop-er-indicator">
                    <span className="font-mono text-[10px] uppercase text-slate-400">Current efficiency (ER)</span>
                    <span className={`font-mono text-[10px] uppercase font-bold px-2 py-0.5 ${
                      state.chop_er == null ? "bg-slate-100 text-slate-500"
                      : state.chop_er >= (state.chop_threshold ?? 0.3) ? "bg-emerald-100 text-emerald-700"
                      : "bg-amber-100 text-amber-700"}`}>
                      {state.chop_er == null ? "\u2014 warming up"
                        : `${state.chop_er} ${state.chop_er >= (state.chop_threshold ?? 0.3) ? "\u2022 trading" : "\u2022 chop (blocked)"}`}
                    </span>
                  </div>
              </div>

              <div className="mt-3 border-t border-slate-100 pt-3">
                <label className="flex items-center justify-between gap-2 cursor-pointer select-none" data-testid="auto-sync-label">
                  <span className="font-mono text-[10px] uppercase text-slate-500">Auto-sync on mismatch<br /><span className="text-slate-400 normal-case">if the bot and Angel disagree, adopt Angel's real position automatically (self-heals while you're away)</span></span>
                  <input type="checkbox" checked={form.auto_sync !== false} disabled={state.running}
                    onChange={(e) => setForm({ ...form, auto_sync: e.target.checked })}
                    className="h-4 w-4 accent-slate-900 disabled:opacity-40" data-testid="setting-auto_sync" />
                </label>
              </div>

              <div className="mt-3 border-t border-slate-100 pt-3">
                <label className="flex items-center justify-between gap-2 cursor-pointer select-none" data-testid="dir-soft-label">
                  <span className="font-mono text-[10px] uppercase text-slate-500">Directional filter<br /><span className="text-slate-400 normal-case">don't trade against a strong trend (block shorts in strong uptrend / longs in strong downtrend)</span></span>
                  <input type="checkbox" checked={!!form.dir_soft} disabled={state.running}
                    onChange={(e) => setForm({ ...form, dir_soft: e.target.checked })}
                    className="h-4 w-4 accent-slate-900 disabled:opacity-40" data-testid="setting-dir_soft" />
                </label>
                <div className="grid grid-cols-2 gap-2 mt-2">
                  <div>
                    <label className="font-mono text-[10px] uppercase text-slate-400 block mb-1">Strength X (veto)</label>
                    <input type="number" step="0.05" min="0.05" max="1" value={form.dir_threshold ?? 0.30} disabled={state.running || !form.dir_soft}
                      onChange={(e) => setForm({ ...form, dir_threshold: e.target.value })}
                      className="w-full border border-slate-300 px-2 py-1 font-mono text-xs focus:outline-none focus:border-slate-900 disabled:bg-slate-50 disabled:text-slate-400"
                      data-testid="setting-dir_threshold" />
                  </div>
                  <div className="flex flex-col justify-end" data-testid="signed-er-indicator">
                    <label className="font-mono text-[10px] uppercase text-slate-400 block mb-1">Trend (signed ER)</label>
                    <span className={`font-mono text-[10px] uppercase font-bold px-2 py-1 text-center ${
                      state.signed_er == null ? "bg-slate-100 text-slate-500"
                      : state.signed_er >= (state.dir_threshold ?? 0.3) ? "bg-emerald-100 text-emerald-700"
                      : state.signed_er <= -(state.dir_threshold ?? 0.3) ? "bg-rose-100 text-rose-700"
                      : "bg-slate-100 text-slate-600"}`}>
                      {state.signed_er == null ? "\u2014"
                        : `${state.signed_er > 0 ? "+" : ""}${state.signed_er} ${
                            state.signed_er >= (state.dir_threshold ?? 0.3) ? "\u25B2 up"
                            : state.signed_er <= -(state.dir_threshold ?? 0.3) ? "\u25BC down" : "\u2022 flat"}`}
                    </span>
                  </div>
                </div>
              </div>

              <button onClick={saveSettings} disabled={state.running} data-testid="save-settings-button"
                className="w-full mt-3 bg-slate-900 hover:bg-slate-800 disabled:bg-slate-300 text-white font-mono uppercase text-xs tracking-wider px-4 py-2 transition-colors flex items-center justify-center gap-2">
                <Save className="h-3.5 w-3.5" /> Apply {state.running && "(stop bot first)"}
              </button>
            </Widget>
  );
}
