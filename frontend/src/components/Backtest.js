import React, { useState, useRef } from "react";
import axios from "axios";
import { X, Play, Loader2, TrendingDown } from "lucide-react";

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

const SummaryGrid = ({ s }) => (
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
);

export const Backtest = ({ onClose, defaultBrick = 50 }) => {
  const [fromDate, setFromDate] = useState(daysAgo(730));
  const [toDate, setToDate] = useState(today());
  const [bricksInput, setBricksInput] = useState("30, 40, 50");
  const [source, setSource] = useState("index");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [res, setRes] = useState(null);
  const pollRef = useRef(null);

  const cleanup = () => { if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; } };

  const run = async () => {
    cleanup();
    setLoading(true); setError(""); setRes(null);
    const bricks = bricksInput.split(",").map((x) => parseInt(x.trim(), 10)).filter((x) => x > 0);
    if (!bricks.length) { setError("Enter at least one brick size (e.g. 30, 40, 50)."); setLoading(false); return; }
    try {
      const { data } = await axios.post(`${API}/backtest`, {
        from_date: fromDate, to_date: toDate,
        brick_sizes: bricks.length > 1 ? bricks : undefined,
        brick_size: bricks.length === 1 ? bricks[0] : undefined,
        source,
      });
      if (!data.job_id) { setError(data.error || "Failed to start backtest."); setLoading(false); return; }
      const jobId = data.job_id;
      let elapsed = 0;
      pollRef.current = setInterval(async () => {
        elapsed += 2;
        try {
          const { data: r } = await axios.get(`${API}/backtest/result/${jobId}`);
          if (r.status === "done") {
            cleanup(); setLoading(false);
            if (r.result?.ok) setRes(r.result);
            else setError(r.result?.error || r.result?.message || "Backtest failed.");
          } else if (r.status === "not_found") {
            cleanup(); setLoading(false); setError("Job expired. Please retry.");
          } else if (elapsed > 180) {
            cleanup(); setLoading(false); setError("Backtest timed out. Try a shorter range.");
          }
        } catch (e) {
          cleanup(); setLoading(false);
          setError(e.response?.data?.detail || "Backtest failed while polling.");
        }
      }, 2000);
    } catch (e) {
      setLoading(false);
      setError(e.response?.data?.detail || e.message || "Backtest failed.");
    }
  };

  const s = res?.summary || res?.best_summary;
  const trades = res?.trades || res?.best_trades;
  const sweep = res?.sweep;

  return (
    <div className="fixed inset-0 z-50 bg-black/50 flex items-start justify-center overflow-y-auto py-8 px-4" data-testid="backtest-modal">
      <div className="bg-white w-full max-w-3xl border border-slate-300 shadow-xl">
        <div className="flex items-center justify-between border-b border-slate-200 px-5 py-3">
          <div className="flex items-center gap-2 font-mono uppercase text-sm tracking-wider text-slate-700">
            <TrendingDown className="h-4 w-4 text-red-500" /> Strategy Backtest
          </div>
          <button onClick={() => { cleanup(); onClose(); }} data-testid="backtest-close" className="text-slate-400 hover:text-slate-700"><X className="h-5 w-5" /></button>
        </div>

        <div className="p-5">
          <p className="text-xs text-slate-500 mb-4">Replays real Angel One 1-min candles through your live Renko rules (short on 2 red bricks; exit on 1st green if ≤4 reds, else 2 greens). Simulation only — no orders. Fills use brick-close prices (excludes slippage/brokerage).</p>

          <div className="flex gap-2 mb-3">
            <button onClick={() => setSource("index")} data-testid="src-index" className={`px-3 py-1.5 text-xs uppercase tracking-wider border ${source === "index" ? "bg-slate-800 text-white border-slate-800" : "border-slate-300 text-slate-500"}`}>NIFTY Index (long history)</button>
            <button onClick={() => setSource("future")} data-testid="src-future" className={`px-3 py-1.5 text-xs uppercase tracking-wider border ${source === "future" ? "bg-slate-800 text-white border-slate-800" : "border-slate-300 text-slate-500"}`}>Current Future</button>
          </div>

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
              <label className="text-[10px] uppercase tracking-widest text-slate-400">Brick size(s)</label>
              <input value={bricksInput} onChange={(e) => setBricksInput(e.target.value)} data-testid="backtest-brick" placeholder="30, 40, 50" className="w-full border border-slate-300 px-2 py-1.5 text-sm font-mono" />
            </div>
            <button onClick={run} disabled={loading} data-testid="backtest-run" className="bg-slate-800 hover:bg-slate-900 disabled:opacity-60 text-white px-4 py-2 text-xs uppercase tracking-wider flex items-center justify-center gap-2 transition-colors">
              {loading ? <><Loader2 className="h-4 w-4 animate-spin" /> Running…</> : <><Play className="h-4 w-4" /> Run</>}
            </button>
          </div>

          {error && <p className="text-red-600 text-sm mb-4" data-testid="backtest-error">{error}</p>}
          {loading && <p className="text-slate-400 text-xs">Fetching history & simulating — a 2-year run can take up to ~2 minutes…</p>}

          {sweep && (
            <div className="mb-5" data-testid="backtest-sweep">
              <p className="text-[10px] uppercase tracking-widest text-slate-400 mb-1">Brick-size comparison — best: <span className="text-emerald-600 font-bold">{res.best_brick_size}</span></p>
              <table className="w-full text-xs font-mono border border-slate-200">
                <thead className="bg-slate-100 text-slate-500"><tr>
                  <th className="text-left px-2 py-1.5">Brick</th><th className="text-right px-2 py-1.5">Net P&L</th>
                  <th className="text-right px-2 py-1.5">Win%</th><th className="text-right px-2 py-1.5">Trades</th>
                  <th className="text-right px-2 py-1.5">PF</th><th className="text-right px-2 py-1.5">Max DD</th></tr></thead>
                <tbody>
                  {sweep.map((r) => (
                    <tr key={r.brick_size} className={`border-t border-slate-100 ${r.brick_size === res.best_brick_size ? "bg-emerald-50" : ""}`}>
                      <td className="px-2 py-1.5 font-bold">{r.brick_size}{r.brick_size === res.best_brick_size ? " ★" : ""}</td>
                      <td className={`px-2 py-1.5 text-right ${r.net_pnl >= 0 ? "text-emerald-600" : "text-red-600"}`}>{fmt(r.net_pnl)}</td>
                      <td className="px-2 py-1.5 text-right">{r.win_rate}%</td>
                      <td className="px-2 py-1.5 text-right">{r.trades}</td>
                      <td className="px-2 py-1.5 text-right">{r.profit_factor == null ? "—" : fmt(r.profit_factor)}</td>
                      <td className="px-2 py-1.5 text-right text-red-600">{fmt(r.max_drawdown)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {s && (
            <div data-testid="backtest-results">
              {sweep && <p className="text-[10px] uppercase tracking-widest text-slate-400 mb-1">Details — best brick ({res.best_brick_size})</p>}
              <SummaryGrid s={s} />
              <p className="text-[10px] uppercase tracking-widest text-slate-400 mb-1">Equity curve (cumulative ₹)</p>
              <EquityCurve trades={trades} />
              <p className="text-[10px] text-slate-400 mt-2 mb-3">
                {res.params.symbol} · {res.params.from} → {res.params.to} · {res.params.candles} candles · lot {res.params.lot_size}
                {s.open_position ? " · (a position was still open at range end — not counted)" : ""}
              </p>
              <div className="max-h-56 overflow-y-auto border border-slate-200">
                <table className="w-full text-xs font-mono">
                  <thead className="bg-slate-100 text-slate-500 sticky top-0"><tr>
                    <th className="text-left px-2 py-1">Entry</th><th className="text-left px-2 py-1">Exit</th>
                    <th className="text-right px-2 py-1">In</th><th className="text-right px-2 py-1">Out</th>
                    <th className="text-right px-2 py-1">Reds</th><th className="text-right px-2 py-1">Pts</th>
                    <th className="text-right px-2 py-1">P&L</th></tr></thead>
                  <tbody>
                    {[...trades].reverse().map((t, i) => (
                      <tr key={i} className="border-t border-slate-100">
                        <td className="px-2 py-1 text-slate-500">{(t.entry_time || "").replace("T", " ").slice(2, 16)}</td>
                        <td className="px-2 py-1 text-slate-500">{(t.exit_time || "").replace("T", " ").slice(2, 16)}</td>
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
