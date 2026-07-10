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
              <button onClick={saveSettings} disabled={state.running} data-testid="save-settings-button"
                className="w-full mt-3 bg-slate-900 hover:bg-slate-800 disabled:bg-slate-300 text-white font-mono uppercase text-xs tracking-wider px-4 py-2 transition-colors flex items-center justify-center gap-2">
                <Save className="h-3.5 w-3.5" /> Apply {state.running && "(stop bot first)"}
              </button>
            </Widget>
  );
}
