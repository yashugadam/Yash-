import React, { useState } from "react";
import axios from "axios";
import { X, Play, Loader2, TrendingDown, TrendingUp } from "lucide-react";

const API = `${process.env.REACT_APP_BACKEND_URL}/api`;

const fmt = (n) => (n == null ? "—" : Number(n).toLocaleString("en-IN", { maximumFractionDigits: 2 }));
const today = () => new Date().toISOString().slice(0, 10);
const daysAgo = (d) => new Date(Date.now() - d * 864e5).toISOString().slice(0, 10);

const Stat = ({ label, value, tone }) => (
  <div className="border border-slate-200 p-3">
    <p className="text-[10px] uppercase tracking-widest text-slate-400">{label}</p>
    <p className={`font-mono text-lg font-bold tabular-nums ${tone || "text-slate-800"}`}>{value}</p>
  </div>
);

// simple inline equity curve
const EquityCurve = ({ trades }) => {
  if (!trades?.length) return null;
  const eq = trades.map((t) => t.equity);
  const min = Math.min(0, ...eq), max = Math.max(0, ...eq);
  const range = max - min || 1;
  const W = 560, H = 90;
  const pts = eq.map((v, i) => `${(i / Math.max(eq.length - 1, 1)) * W},${H - ((v - min) / range) * H}`).join(" ");
  const zeroY = H - ((0 - min) / range) * H;
  const up = eq[eq.length - 1] >= 0;
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full h-24 border border-slate-200 bg-slate-50" preserveAspectRatio="none">
      <line x1="0" y1={zeroY} x2={W} y2={zeroY} stroke="#cbd5e1" strokeWidth="1" strokeDasharray="4 4" />
      <polyline points={pts} fill="none" stroke={up ? "#059669" : "#dc2626"} strokeWidth="2" />
    </svg>
  );
};

export const Backtest = ({ onClose, defaultBrick = 50 }) => {
  const [fromDate, setFromDate] = useState(daysAgo(30));
  const [toDate, setToDate] = useState(today());
  const [brick, setBrick] = useState(defaultBrick);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [res, setRes] = useState(null);

  const run = async () => {
    setLoading(true); setError(""); setRes(null);
    try {
      const { data } = await axios.post(`${API}/backtest`, {
        from_date: fromDate, to_date: toDate, brick_size: Number(brick),
      });
      if (data.ok) setRes(data);
      else setError(data.error || data.message || "Backtest failed.");
    } catch (e) {
      setError(e.response?.data?.detail || e.message || "Backtest failed.");
    } finally { setLoading(false); }
  };

  const s = res?.summary;
  return (
    <div className="fixed inset-0 z-50 bg-black/50 flex items-start justify-center overflow-y-auto py-8 px-4" data-testid="backtest-modal">
      <div className="bg-white w-full max-w-3xl border border-slate-300 shadow-xl">
        <div className="flex items-center justify-between border-b border-slate-200 px-5 py-3">
          <div className="flex items-center gap-2 font-mono uppercase text-sm tracking-wider text-slate-700">
            <TrendingDown className="h-4 w-4 text-red-500" /> Strategy Backtest
          </div>
          <button onClick={onClose} data-testid="backtest-close" className="text-slate-400 hover:text-slate-700"><X className="h-5 w-5" /></button>
        </div>

        <div className="p-5">
          <p className="text-xs text-slate-500 mb-4">Replays real Angel One 1-min candles through your live Renko rules (short on 2 red bricks; exit on 1st green if ≤4 reds, else 2 greens). Simulation only — no orders. Fills use brick-close prices (excludes slippage/brokerage).</p>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 items-end mb-4">
            <div>
              <label className="text-[10px] uppercase tracking-widest text-slate-400">From</label>
              <input type="date" value={fromDate} max={toDate} onChange={(e) => setFromDate(e.target.value)} data-testid="backtest-from" className="w-full border border-slate-300 px-2 py-1.5 text-sm font-mono" />
            </div>
            <div>
              <label className="text-[10px] uppercase tracking-widest text-slate-400">To</label>
              <input type="date" value={toDate} max={today()} onChange={(e) => setToDate(e.target.value)} data-testid="backtest-to" className="w-full border border-slate-300 px-2 py-1.5 text-sm font-mono" />
            </div>
            <div>
              <label className="text-[10px] uppercase tracking-widest text-slate-400">Brick size</label>
              <input type="number" value={brick} onChange={(e) => setBrick(e.target.value)} data-testid="backtest-brick" className="w-full border border-slate-300 px-2 py-1.5 text-sm font-mono" />
            </div>
            <button onClick={run} disabled={loading} data-testid="backtest-run" className="bg-slate-800 hover:bg-slate-900 disabled:opacity-60 text-white px-4 py-2 text-xs uppercase tracking-wider flex items-center justify-center gap-2 transition-colors">
              {loading ? <><Loader2 className="h-4 w-4 animate-spin" /> Running…</> : <><Play className="h-4 w-4" /> Run</>}
            </button>
          </div>

          {error && <p className="text-red-600 text-sm mb-4" data-testid="backtest-error">{error}</p>}
          {loading && <p className="text-slate-400 text-xs">Fetching history & simulating — this can take up to a minute for long ranges…</p>}

          {s && (
            <div data-testid="backtest-results">
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 mb-4">
                <Stat label="Net P&L (₹)" value={fmt(s.net_pnl)} tone={s.net_pnl >= 0 ? "text-emerald-600" : "text-red-600"} />
                <Stat label="Win Rate" value={`${s.win_rate}%`} />
                <Stat label="Trades" value={`${s.trades}`} />
                <Stat label="Net Points" value={fmt(s.net_points)} tone={s.net_points >= 0 ? "text-emerald-600" : "text-red-600"} />
                <Stat label="Profit Factor" value={s.profit_factor == null ? "—" : fmt(s.profit_factor)} />
                <Stat label="Avg / Trade" value={fmt(s.avg_pnl)} tone={s.avg_pnl >= 0 ? "text-emerald-600" : "text-red-600"} />
                <Stat label="Best / Worst" value={`${fmt(s.best)} / ${fmt(s.worst)}`} />
                <Stat label="Max Drawdown" value={fmt(s.max_drawdown)} tone="text-red-600" />
              </div>
              <p className="text-[10px] uppercase tracking-widest text-slate-400 mb-1">Equity curve (cumulative ₹)</p>
              <EquityCurve trades={res.trades} />
              <p className="text-[10px] text-slate-400 mt-2 mb-3">
                {res.params.symbol} · {res.params.from} → {res.params.to} · {s.candles} candles · lot {res.params.lot_size}
                {s.open_position ? " · (a position was still open at range end — not counted)" : ""}
              </p>
              <div className="max-h-64 overflow-y-auto border border-slate-200">
                <table className="w-full text-xs font-mono">
                  <thead className="bg-slate-100 text-slate-500 sticky top-0"><tr>
                    <th className="text-left px-2 py-1">Entry</th><th className="text-left px-2 py-1">Exit</th>
                    <th className="text-right px-2 py-1">In</th><th className="text-right px-2 py-1">Out</th>
                    <th className="text-right px-2 py-1">Reds</th><th className="text-right px-2 py-1">Pts</th>
                    <th className="text-right px-2 py-1">P&L</th></tr></thead>
                  <tbody>
                    {[...res.trades].reverse().map((t, i) => (
                      <tr key={i} className="border-t border-slate-100">
                        <td className="px-2 py-1 text-slate-500">{(t.entry_time || "").replace("T", " ").slice(5, 16)}</td>
                        <td className="px-2 py-1 text-slate-500">{(t.exit_time || "").replace("T", " ").slice(5, 16)}</td>
                        <td className="px-2 py-1 text-right">{fmt(t.entry)}</td>
                        <td className="px-2 py-1 text-right">{fmt(t.exit)}</td>
                        <td className="px-2 py-1 text-right">{t.reds}</td>
                        <td className={`px-2 py-1 text-right ${t.points >= 0 ? "text-emerald-600" : "text-red-600"}`}>{fmt(t.points)}</td>
                        <td className={`px-2 py-1 text-right ${t.pnl >= 0 ? "text-emerald-600" : "text-red-600"}`}>{fmt(t.pnl)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
};

export default Backtest;
