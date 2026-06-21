import React, { useEffect, useRef, useState, useCallback } from "react";
import axios from "axios";
import { toast } from "sonner";
import {
  Activity, Play, Square, RotateCcw, Settings as SettingsIcon, TrendingDown,
  Zap, ShieldCheck, ChevronUp, ChevronDown, Layers, Lock, Save,
} from "lucide-react";
import RenkoChart from "@/components/RenkoChart";

const API = `${process.env.REACT_APP_BACKEND_URL}/api`;

const fmt = (n, d = 2) =>
  (n === null || n === undefined) ? "--" : Number(n).toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d });
const pnlClass = (n) => (n > 0 ? "text-emerald-600" : n < 0 ? "text-red-500" : "text-slate-500");
const sign = (n) => (n > 0 ? "+" : "");

const Widget = ({ title, icon, children, right, testid }) => (
  <div className="bg-white border border-slate-200 flex flex-col" data-testid={testid}>
    <div className="flex items-center justify-between border-b border-slate-200 px-4 py-2.5 bg-slate-50">
      <div className="flex items-center gap-2">
        {icon}
        <span className="font-mono text-xs uppercase tracking-[0.08em] text-slate-600">{title}</span>
      </div>
      {right}
    </div>
    <div className="p-4 flex-1">{children}</div>
  </div>
);

export default function Dashboard() {
  const [state, setState] = useState(null);
  const [trades, setTrades] = useState([]);
  const [form, setForm] = useState(null);
  const [angel, setAngel] = useState({ api_key: "", client_id: "" });
  const prevPrice = useRef(null);
  const [flash, setFlash] = useState("");

  const poll = useCallback(async () => {
    try {
      const { data } = await axios.get(`${API}/state`);
      setState((old) => {
        if (old && data.price !== old.price) {
          setFlash(data.price > old.price ? "flash-green" : "flash-red");
          setTimeout(() => setFlash(""), 600);
        }
        return data;
      });
      if (!form) setForm(data.settings);
    } catch (e) { /* ignore transient */ }
  }, [form]);

  const loadTrades = useCallback(async () => {
    try { const { data } = await axios.get(`${API}/trades`); setTrades(data); } catch (e) {}
  }, []);

  useEffect(() => {
    poll(); loadTrades();
    const a = setInterval(poll, 1000);
    const b = setInterval(loadTrades, 3000);
    return () => { clearInterval(a); clearInterval(b); };
  }, [poll, loadTrades]);

  const startStop = async () => {
    const running = state?.running;
    await axios.post(`${API}/bot/${running ? "stop" : "start"}`);
    toast[running ? "info" : "success"](running ? "Bot stopped" : "Bot started — feeding live ticks");
    poll();
  };

  const reset = async () => {
    await axios.post(`${API}/bot/reset`);
    toast.info("Session reset — bricks & trades cleared");
    poll(); loadTrades();
  };

  const saveSettings = async () => {
    await axios.post(`${API}/settings`, {
      brick_size: Number(form.brick_size), lot_size: Number(form.lot_size),
      buffer_points: Number(form.buffer_points),
      max_red_single_green: Number(form.max_red_single_green),
      greens_to_exit_extended: Number(form.greens_to_exit_extended),
    });
    toast.success("Strategy parameters updated");
    poll();
  };

  const saveAngel = async () => {
    await axios.post(`${API}/angel/config`, angel);
    toast.success("Angel One details saved — bot stays in DEMO mode");
  };

  if (!state) {
    return <div className="min-h-screen flex items-center justify-center font-mono text-sm text-slate-400">Connecting to engine…</div>;
  }

  const m = state.metrics;
  const pos = state.position;
  const priceUp = state.price >= state.prev_price;

  return (
    <div className="min-h-screen bg-slate-100 text-slate-900">
      {/* Header */}
      <header className="flex items-center justify-between border-b border-slate-200 bg-white px-4 py-2.5 sticky top-0 z-50">
        <div className="flex items-center gap-3">
          <div className="h-8 w-8 bg-slate-900 flex items-center justify-center">
            <Layers className="h-4 w-4 text-white" strokeWidth={2} />
          </div>
          <div>
            <h1 className="font-heading font-extrabold text-base tracking-tight leading-none">RENKO ALGO</h1>
            <p className="font-mono text-[10px] uppercase tracking-[0.12em] text-slate-400 leading-none mt-0.5">Nifty Fut · 1m · {state.settings.brick_size}pt</p>
          </div>
        </div>

        <div className={`flex items-center gap-4 px-4 py-1.5 border border-slate-200 ${flash}`} data-testid="ticker-price">
          <span className="font-mono text-[10px] uppercase tracking-widest text-slate-400">{state.settings.symbol}</span>
          <span className="font-mono text-xl font-bold tabular-nums">{fmt(state.price)}</span>
          {priceUp ? <ChevronUp className="h-4 w-4 text-emerald-600" /> : <ChevronDown className="h-4 w-4 text-red-500" />}
        </div>

        <div className="flex items-center gap-2">
          <span className="bg-amber-100 text-amber-800 border border-amber-200 px-2 py-1 text-[11px] font-mono uppercase flex items-center gap-1" data-testid="mode-badge">
            <ShieldCheck className="h-3 w-3" /> Demo
          </span>
          <span className={`px-2 py-1 text-[11px] font-mono uppercase flex items-center gap-1 border ${state.running ? "bg-emerald-100 text-emerald-800 border-emerald-200" : "bg-slate-100 text-slate-500 border-slate-200"}`} data-testid="status-badge">
            <span className={`h-1.5 w-1.5 rounded-full ${state.running ? "bg-emerald-500 pulse-dot" : "bg-slate-400"}`} /> {state.running ? "Live" : "Idle"}
          </span>
          <button onClick={reset} className="border border-slate-300 hover:bg-slate-50 text-slate-600 px-3 py-1.5 transition-colors" data-testid="reset-button" title="Reset session">
            <RotateCcw className="h-3.5 w-3.5" />
          </button>
          <button onClick={startStop} data-testid="start-stop-button"
            className={`font-mono uppercase text-xs tracking-wider px-4 py-1.5 transition-colors flex items-center gap-2 text-white ${state.running ? "bg-red-600 hover:bg-red-700" : "bg-emerald-600 hover:bg-emerald-700"}`}>
            {state.running ? <Square className="h-3.5 w-3.5" /> : <Play className="h-3.5 w-3.5" />}
            {state.running ? "Stop" : "Start"}
          </button>
        </div>
      </header>

      {/* Main grid */}
      <main className="grid grid-cols-1 lg:grid-cols-4 gap-4 p-4">
        {/* Left column */}
        <div className="lg:col-span-3 flex flex-col gap-4">
          <Widget title="Renko Chart" testid="chart-widget"
            icon={<Activity className="h-3.5 w-3.5 text-slate-500" />}
            right={
              <div className="flex items-center gap-3 font-mono text-[11px]">
                <span className="flex items-center gap-1"><span className="h-2.5 w-2.5 bg-emerald-500 inline-block" /> Green</span>
                <span className="flex items-center gap-1"><span className="h-2.5 w-2.5 bg-red-500 inline-block" /> Red</span>
                <span className="text-slate-400">Reds: <b className="text-slate-700">{state.consec_red}</b> · Greens: <b className="text-slate-700">{state.consec_green}</b></span>
              </div>
            }>
            <RenkoChart bricks={state.bricks} />
          </Widget>

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
                      <td className="font-mono text-xs px-3 py-2 border-b border-slate-100 text-slate-400">{new Date(t.exit_time).toLocaleTimeString("en-IN")}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Widget>
        </div>

        {/* Right column */}
        <div className="lg:col-span-1 flex flex-col gap-4">
          {/* Metrics */}
          <Widget title="Performance" testid="metrics-widget" icon={<Zap className="h-3.5 w-3.5 text-slate-500" />}>
            <p className="font-mono text-[10px] uppercase tracking-widest text-slate-400">Total P&L (₹)</p>
            <p className={`font-mono text-3xl font-bold tabular-nums mt-1 ${pnlClass(m.realized_pnl)}`} data-testid="total-pnl">
              {sign(m.realized_pnl)}{fmt(m.realized_pnl)}
            </p>
            <div className="grid grid-cols-3 gap-2 mt-4 border-t border-slate-100 pt-3">
              <div><p className="font-mono text-[10px] uppercase text-slate-400">Trades</p><p className="font-mono text-lg font-semibold">{m.trades}</p></div>
              <div><p className="font-mono text-[10px] uppercase text-slate-400">Win %</p><p className="font-mono text-lg font-semibold text-emerald-600">{fmt(m.win_rate, 1)}</p></div>
              <div><p className="font-mono text-[10px] uppercase text-slate-400">W / L</p><p className="font-mono text-lg font-semibold">{m.wins}/{m.losses}</p></div>
            </div>
          </Widget>

          {/* Open position */}
          <Widget title="Open Position" testid="position-widget" icon={<TrendingDown className="h-3.5 w-3.5 text-slate-500" />}>
            {pos ? (
              <div className="border border-red-200 bg-red-50/50 p-3" data-testid="active-position">
                <div className="flex items-center justify-between">
                  <span className="bg-red-600 text-white px-2 py-0.5 font-mono text-xs uppercase">Short</span>
                  <span className="font-mono text-xs text-slate-500">{pos.qty} qty</span>
                </div>
                <div className="grid grid-cols-2 gap-2 mt-3">
                  <div><p className="font-mono text-[10px] uppercase text-slate-400">Entry</p><p className="font-mono text-sm font-semibold">{fmt(pos.entry_price)}</p></div>
                  <div><p className="font-mono text-[10px] uppercase text-slate-400">LTP</p><p className="font-mono text-sm font-semibold">{fmt(state.price)}</p></div>
                </div>
                <div className="mt-3 border-t border-red-200 pt-2">
                  <p className="font-mono text-[10px] uppercase text-slate-400">Unrealized P&L</p>
                  <p className={`font-mono text-xl font-bold ${pnlClass(m.unrealized_pnl)}`} data-testid="unrealized-pnl">{sign(m.unrealized_pnl)}{fmt(m.unrealized_pnl)}</p>
                </div>
                <p className="font-mono text-[10px] text-slate-400 mt-2">Reds in run: {state.down_run_reds} · exit on {state.down_run_reds > state.settings.max_red_single_green ? state.settings.greens_to_exit_extended : 1} green</p>
              </div>
            ) : (
              <div className="text-center py-6" data-testid="no-position">
                <p className="font-mono text-xs uppercase tracking-widest text-slate-400">Flat — no position</p>
                <p className="font-mono text-[11px] text-slate-300 mt-1">{state.pending_entry ? "Entry order placing…" : "Waiting for 2 red bricks"}</p>
              </div>
            )}
          </Widget>

          {/* Orders */}
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

          {/* Strategy settings */}
          {form && (
            <Widget title="Strategy" testid="settings-widget" icon={<SettingsIcon className="h-3.5 w-3.5 text-slate-500" />}>
              <div className="grid grid-cols-2 gap-2">
                {[
                  ["Brick Size", "brick_size"], ["Lot Size", "lot_size"],
                  ["Buffer (pt)", "buffer_points"], ["Max Reds→1G", "max_red_single_green"],
                  ["Greens (ext)", "greens_to_exit_extended"],
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
              <button onClick={saveSettings} disabled={state.running} data-testid="save-settings-button"
                className="w-full mt-3 bg-slate-900 hover:bg-slate-800 disabled:bg-slate-300 text-white font-mono uppercase text-xs tracking-wider px-4 py-2 transition-colors flex items-center justify-center gap-2">
                <Save className="h-3.5 w-3.5" /> Apply {state.running && "(stop bot first)"}
              </button>
            </Widget>
          )}

          {/* Angel One */}
          <Widget title="Angel One" testid="angel-widget" icon={<Lock className="h-3.5 w-3.5 text-slate-500" />}
            right={<span className="font-mono text-[10px] text-amber-600 uppercase">Not connected</span>}>
            <input placeholder="API Key" value={angel.api_key} onChange={(e) => setAngel({ ...angel, api_key: e.target.value })}
              className="w-full border border-slate-300 px-2 py-1.5 font-mono text-xs mb-2 focus:outline-none focus:border-slate-900" data-testid="angel-apikey" />
            <input placeholder="Client ID" value={angel.client_id} onChange={(e) => setAngel({ ...angel, client_id: e.target.value })}
              className="w-full border border-slate-300 px-2 py-1.5 font-mono text-xs mb-2 focus:outline-none focus:border-slate-900" data-testid="angel-clientid" />
            <button onClick={saveAngel} data-testid="save-angel-button"
              className="w-full border border-slate-300 hover:bg-slate-50 text-slate-700 font-mono text-xs uppercase px-4 py-2 transition-colors">Save (stays demo)</button>
            <p className="font-mono text-[10px] text-slate-400 mt-2">Stored for later. No real orders are placed in DEMO mode.</p>
          </Widget>
        </div>
      </main>
    </div>
  );
}
