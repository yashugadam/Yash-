import React, { useEffect, useRef, useState, useCallback } from "react";
import axios from "axios";
import { toast } from "sonner";
import {
  Activity, Play, Square, RotateCcw, Settings as SettingsIcon, TrendingDown,
  Zap, ShieldCheck, ChevronUp, ChevronDown, Layers, Lock, Save,
  CalendarClock, AlertTriangle, XCircle, ShieldAlert, ShieldX, History, Search,
} from "lucide-react";
import RenkoChart from "@/components/RenkoChart";
import {
  AlertDialog, AlertDialogContent, AlertDialogHeader, AlertDialogFooter,
  AlertDialogTitle, AlertDialogDescription, AlertDialogAction, AlertDialogCancel,
} from "@/components/ui/alert-dialog";

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
  const prevPrice = useRef(null);
  const lastAlertId = useRef(null);
  const [flash, setFlash] = useState("");
  const [instQuery, setInstQuery] = useState("");
  const [instResults, setInstResults] = useState([]);
  const [showInstSearch, setShowInstSearch] = useState(false);
  const [showStopConfirm, setShowStopConfirm] = useState(false);
  const [showStartConfirm, setShowStartConfirm] = useState(false);
  const [recon, setRecon] = useState(null);
  const [reconBusy, setReconBusy] = useState(false);
  const [orderLog, setOrderLog] = useState([]);

  const searchInstruments = async (q) => {
    setInstQuery(q);
    try {
      const { data } = await axios.get(`${API}/angel/instruments`, { params: { q } });
      setInstResults(data.items || []);
    } catch (e) { setInstResults([]); }
  };

  const selectInstrument = async (token, symbol) => {
    const { data } = await axios.post(`${API}/angel/select-instrument`, { token });
    if (data.ok) {
      toast.success(`Trading instrument set: ${symbol}`);
      setShowInstSearch(false); setInstQuery(""); setInstResults([]);
    } else toast.error(data.error || "Could not select");
    poll();
  };

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
      // Toast alerts deduped by id (outside the state updater so it can't double-fire).
      // Routine market open/close status is shown via the badge — never toasted.
      if (data.alert && data.alert.id !== lastAlertId.current) {
        lastAlertId.current = data.alert.id;
        const msg = data.alert.msg || "";
        const routine = /^Market (closed|open)/i.test(msg);
        if (!routine) {
          const lvl = data.alert.level === "error" ? "error" : data.alert.level === "warning" ? "warning" : "info";
          toast[lvl](msg, { duration: 8000 });
        }
      }
      if (!form) setForm(data.settings);
    } catch (e) { console.debug("poll failed (transient):", e?.message); }
  }, [form]);

  const loadTrades = useCallback(async () => {
    try { const { data } = await axios.get(`${API}/trades`); setTrades(data); } catch (e) { console.debug("loadTrades failed (transient):", e?.message); }
  }, []);

  const loadOrderLog = useCallback(async () => {
    try { const { data } = await axios.get(`${API}/orders/log`); setOrderLog(data); } catch (e) { console.debug("loadOrderLog failed (transient):", e?.message); }
  }, []);

  const clearOrderLog = async () => {
    try {
      const { data } = await axios.post(`${API}/orders/log/clear`);
      toast.success(`Order log cleared (${data.cleared} rows). Open position & trades are untouched.`);
      setOrderLog([]);
    } catch (e) { toast.error("Could not clear order log"); }
  };

  useEffect(() => {
    poll(); loadTrades(); loadOrderLog();
    const a = setInterval(poll, 1000);
    const b = setInterval(() => { loadTrades(); loadOrderLog(); }, 3000);
    return () => { clearInterval(a); clearInterval(b); };
  }, [poll, loadTrades, loadOrderLog]);

  const startStop = () => {
    if (state?.running) { setShowStopConfirm(true); return; }
    setShowStartConfirm(true);
  };

  const confirmStart = async () => {
    setShowStartConfirm(false);
    await axios.post(`${API}/bot/start`);
    toast.warning("Bot started — LIVE real-money trading is ACTIVE");
    poll();
  };

  const confirmStop = async () => {
    setShowStopConfirm(false);
    const hadPosition = !!state?.position;
    const { data } = await axios.post(`${API}/bot/stop`, { square_off: true });
    if (data.squared_off) toast.info("Position squared off (forced exit) — bot stopped");
    else toast.info(hadPosition ? "Bot stopped" : "Bot stopped — no open position");
    poll(); loadTrades();
  };

  const resolveAdoption = async (confirm) => {
    try {
      const { data } = await axios.post(`${API}/bot/adopt`, { confirm });
      data.ok ? toast.success(data.message) : toast.error(data.message);
    } catch (e) { toast.error("Adoption request failed"); }
    poll(); loadTrades();
  };

  const checkReconcile = useCallback(async () => {
    setReconBusy(true);
    try { const { data } = await axios.get(`${API}/bot/reconcile`); setRecon(data); }
    catch (e) { setRecon({ available: false, reason: "Reconcile request failed" }); }
    setReconBusy(false);
  }, []);

  const resolveReconcile = async (action) => {
    setReconBusy(true);
    const { data } = await axios.post(`${API}/bot/reconcile/resolve`, { action });
    if (data.ok) toast.success(data.message); else toast.error(data.message);
    setReconBusy(false);
    await checkReconcile(); poll(); loadTrades();
  };

  const [manualBusy, setManualBusy] = useState(false);
  const manualOrder = async (side) => {
    setManualBusy(true);
    try {
      const { data } = await axios.post(`${API}/orders/manual`, { side });
      if (data.ok && (data.order_status === "COMPLETE")) toast.success(`${side} filled @ ${data.fill_price} (order ${data.broker_order_id})`);
      else if (data.ok) toast.warning(`${side} placed — status: ${data.order_status}. ${data.note || ""}`);
      else toast.error(`${side} rejected — ${data.message}`);
    } catch (e) { toast.error("Manual order request failed"); }
    setManualBusy(false);
    loadOrderLog(); poll();
  };

  const reset = async () => {
    await axios.post(`${API}/bot/reset`);
    toast.info("Session reset — bricks & trades cleared");
    poll(); loadTrades();
  };

  const saveSettings = async () => {
    await axios.post(`${API}/settings`, {
      brick_size: Number(form.brick_size), bar_seconds: Number(form.bar_seconds),
      lot_size: Number(form.lot_size), buffer_points: Number(form.buffer_points),
      max_slippage: Number(form.max_slippage),
      forced_exit_slippage: Number(form.forced_exit_slippage),
      max_red_single_green: Number(form.max_red_single_green),
      greens_to_exit_extended: Number(form.greens_to_exit_extended),
      daily_max_loss: Number(form.daily_max_loss),
      rollover_position: !!form.rollover_position,
    });
    toast.success("Strategy parameters updated");
    poll();
  };

  const connectAngel = async () => {
    toast.info("Connecting to Angel One…");
    const { data } = await axios.post(`${API}/angel/connect`);
    if (data.connected) toast.success(`Connected — live feed: ${data.future || "NIFTY FUT"}`);
    else toast.error(data.error || "Connection failed");
    poll();
  };

  const disconnectAngel = async () => {
    await axios.post(`${API}/angel/disconnect`);
    toast.info("Disconnected from Angel One");
    poll();
  };

  const loadHistory = async () => {
    if (!state?.angel?.connected) { toast.error("Connect Angel One first to load history"); return; }
    const now = new Date();
    const fromDate = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-01`;
    toast.info(`Loading candles since ${fromDate}…`);
    const { data } = await axios.post(`${API}/angel/load-history`, { from_date: fromDate }, { timeout: 120000 });
    if (data.ok) toast.success(`Loaded ${data.bricks} bricks from ${data.candles} candles (${data.from} → ${data.to})`);
    else toast.error(data.error || "History load failed");
    poll();
  };

  const squareOff = async () => {
    const { data } = await axios.post(`${API}/bot/square-off`);
    toast[data.ok ? "warning" : "info"](data.message);
    poll();
  };

  const armBreaker = async () => {
    const { data } = await axios.post(`${API}/bot/arm`);
    toast.success(data.message);
    poll();
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
            <p className="font-mono text-[10px] uppercase tracking-[0.12em] text-slate-400 leading-none mt-0.5">Nifty Fut · Traditional · {state.settings.brick_size}pt</p>
          </div>
        </div>

        <div className={`flex items-center gap-4 px-4 py-1.5 border border-slate-200 ${flash}`} data-testid="ticker-price">
          <span className="font-mono text-[10px] uppercase tracking-widest text-slate-400">{state.settings.symbol}</span>
          <span className="font-mono text-xl font-bold tabular-nums">{fmt(state.price)}</span>
          {priceUp ? <ChevronUp className="h-4 w-4 text-emerald-600" /> : <ChevronDown className="h-4 w-4 text-red-500" />}
        </div>

        <div className="flex items-center gap-2">
          <span className={`px-2 py-1 text-[11px] font-mono uppercase flex items-center gap-1 border ${state.angel?.connected ? "bg-blue-100 text-blue-800 border-blue-200" : "bg-red-100 text-red-700 border-red-200"}`} data-testid="feed-badge" title={state.angel?.streaming ? "Live LTP streaming over websocket" : (state.feed_error || "")}>
            <Activity className="h-3 w-3" /> {state.angel?.connected ? (state.angel?.streaming ? "Streaming" : "Live Data") : "Disconnected"}
          </span>
          <span className="bg-red-600 text-white border border-red-700 px-2 py-1 text-[11px] font-mono uppercase flex items-center gap-1" data-testid="mode-badge" title="This bot trades LIVE with real money on Angel One">
            <Zap className="h-3 w-3" /> Live · Real Money
          </span>
          <span className={`px-2 py-1 text-[11px] font-mono uppercase flex items-center gap-1 border ${state.market_open ? "bg-emerald-100 text-emerald-800 border-emerald-200" : "bg-amber-100 text-amber-800 border-amber-200"}`} data-testid="market-badge" title={state.market_open ? "NSE market open (09:15–15:30 IST)" : "Market closed — bot is idle, no orders are placed"}>
            <span className={`h-1.5 w-1.5 rounded-full ${state.market_open ? "bg-emerald-500 pulse-dot" : "bg-amber-500"}`} /> {state.market_open ? "Mkt Open" : "Mkt Closed"}
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

      <AlertDialog open={showStopConfirm} onOpenChange={setShowStopConfirm}>
        <AlertDialogContent data-testid="stop-confirm-dialog">
          <AlertDialogHeader>
            <AlertDialogTitle className="flex items-center gap-2">
              <AlertTriangle className="h-5 w-5 text-red-600" /> Stop the bot?
            </AlertDialogTitle>
            <AlertDialogDescription>
              {state?.position ? (
                <>
                  You currently have an <b>OPEN SHORT position</b> ({state.position.qty} qty).
                  Confirming will <b>force-exit (square off) this trade now</b> at market — even
                  though the green-brick exit condition has not been met — and then stop the bot.
                  While stopped, auto-exit, the circuit breaker and expiry square-off are disabled.
                </>
              ) : (
                <>No open position. The bot will simply stop feeding and watching the market. No new entries will be taken until you start again.</>
              )}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel data-testid="stop-cancel-button">Keep running</AlertDialogCancel>
            <AlertDialogAction onClick={confirmStop} data-testid="stop-confirm-button"
              className="bg-red-600 hover:bg-red-700">
              {state?.position ? "Square off & Stop" : "Stop bot"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog open={!!(state?.pending_adoption && !state.pending_adoption.declined)}>
        <AlertDialogContent data-testid="adoption-dialog">
          <AlertDialogHeader>
            <AlertDialogTitle>Existing Angel One position found</AlertDialogTitle>
            <AlertDialogDescription>
              {state?.pending_adoption?.side === "LONG" ? (
                <>
                  Angel One shows a <b>LONG position</b> ({state.pending_adoption.qty} qty), which is
                  outside your short-only strategy. The bot will <b>not trade</b> until this is resolved.
                  Close it on Angel One, or dismiss this to keep the bot idle.
                </>
              ) : (
                <>
                  Angel One shows an <b>open SHORT</b> of <b>{state?.pending_adoption?.qty} qty</b>
                  {state?.pending_adoption?.avgprice ? <> @ avg <b>{state.pending_adoption.avgprice}</b></> : null}
                  {" "}that the bot didn't open (e.g. a manual trade). Adopt it so the bot
                  <b> manages the exit per your strategy</b> (exit on the green-brick reversal)?
                  Until you decide, the bot won't open any new trade (to avoid stacking).
                </>
              )}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel onClick={() => resolveAdoption(false)} data-testid="adoption-decline-button">
              Don't adopt
            </AlertDialogCancel>
            {state?.pending_adoption?.side !== "LONG" && (
              <AlertDialogAction onClick={() => resolveAdoption(true)} data-testid="adoption-confirm-button"
                className="bg-slate-900 hover:bg-slate-800">
                Adopt &amp; manage exit
              </AlertDialogAction>
            )}
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>


      <AlertDialog open={showStartConfirm} onOpenChange={setShowStartConfirm}>
        <AlertDialogContent data-testid="start-confirm-dialog">
          <AlertDialogHeader>
            <AlertDialogTitle className="flex items-center gap-2">
              <Zap className="h-5 w-5 text-red-600" /> Start LIVE trading?
            </AlertDialogTitle>
            <AlertDialogDescription>
              This bot trades <b>REAL money</b> on your Angel One account ({state.angel?.future || "selected future"}, qty {state.settings?.lot_size}).
              Once started, it will place real CARRYFORWARD LIMIT orders automatically — and if the market is already in a 2+ red down-run, it will <b>enter a SHORT immediately</b>.
              {!state.angel?.connected && (
                <span className="block mt-2 text-red-600 font-semibold" data-testid="start-not-ready-note">
                  ⚠ Angel One is disconnected — connect it first or no orders can be placed.
                </span>
              )}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel data-testid="start-cancel-button">Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={confirmStart} data-testid="start-confirm-button"
              className="bg-red-600 hover:bg-red-700">
              Start Live Trading
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <main className="grid grid-cols-1 lg:grid-cols-4 gap-4 p-4">
        {/* Left column */}
        <div className="lg:col-span-3 flex flex-col gap-4">
          <Widget title="Renko Chart" testid="chart-widget"
            icon={<Activity className="h-3.5 w-3.5 text-slate-500" />}
            right={
              <div className="flex items-center gap-3 font-mono text-[11px]">
                <button onClick={loadHistory} data-testid="load-history-btn"
                  className="flex items-center gap-1 px-2 py-0.5 border border-slate-200 uppercase text-slate-500 hover:bg-slate-50 transition-colors disabled:opacity-40"
                  disabled={!state.angel.connected} title={state.angel.connected ? "Load real 5-day history" : "Connect Angel One first"}>
                  <History className="h-3 w-3" /> History
                </button>
                <span className="flex items-center gap-1"><span className="h-2.5 w-2.5 bg-emerald-500 inline-block" /> Green</span>
                <span className="flex items-center gap-1"><span className="h-2.5 w-2.5 bg-red-500 inline-block" /> Red</span>
                <span className="text-slate-400">Bar <b className="text-slate-700">{state.ticks_in_bar}/{state.settings.bar_seconds}s</b> · Reds <b className="text-slate-700">{state.consec_red}</b> · Greens <b className="text-slate-700">{state.consec_green}</b></span>
              </div>
            }>
            <RenkoChart bricks={state.bricks} trades={trades} />
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
                      <td className="font-mono text-xs px-3 py-2 border-b border-slate-100 text-slate-400">{new Date(t.exit_time).toLocaleString("en-IN", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", second: "2-digit", timeZone: "Asia/Kolkata" })}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Widget>

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
        </div>

        {/* Right column */}
        <div className="lg:col-span-1 flex flex-col gap-4">
          {/* Position reconciliation (LIVE) */}
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

          {/* Manual / test orders */}
          <Widget title="Manual Order (test)" testid="manual-order-widget"
            icon={<Zap className="h-3.5 w-3.5 text-slate-500" />}>
            <p className="font-mono text-[11px] text-slate-400 mb-3">
              Places a REAL 1-lot ({state.settings?.lot_size}) LIMIT order near LTP to verify execution. To flatten: Sell closes a long, Buy closes a short. Only works from your whitelisted IP (production).
            </p>
            <div className="grid grid-cols-2 gap-2">
              <button onClick={() => manualOrder("BUY")} disabled={manualBusy || !state.angel.connected}
                data-testid="manual-buy-button"
                className="font-mono text-[11px] uppercase tracking-wider bg-emerald-600 hover:bg-emerald-700 text-white px-3 py-2 disabled:opacity-40">
                Buy 1 lot
              </button>
              <button onClick={() => manualOrder("SELL")} disabled={manualBusy || !state.angel.connected}
                data-testid="manual-sell-button"
                className="font-mono text-[11px] uppercase tracking-wider bg-red-600 hover:bg-red-700 text-white px-3 py-2 disabled:opacity-40">
                Sell 1 lot
              </button>
            </div>
            <p className="font-mono text-[10px] text-slate-400 mt-2">Result & exact reason appear in the Order Log below the chart.</p>
          </Widget>

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
                <button onClick={squareOff} data-testid="square-off-button"
                  className="w-full mt-3 border border-red-300 hover:bg-red-100 text-red-700 font-mono uppercase text-[11px] tracking-wider px-3 py-1.5 transition-colors flex items-center justify-center gap-2">
                  <XCircle className="h-3.5 w-3.5" /> Square off now
                </button>
              </div>
            ) : (
              <div className="text-center py-6" data-testid="no-position">
                <p className="font-mono text-xs uppercase tracking-widest text-slate-400">Flat — no position</p>
                <p className="font-mono text-[11px] text-slate-300 mt-1">{state.pending_entry ? "Entry order placing…" : state.expiry.entries_blocked ? "Entries blocked (expiry square-off window)" : "Waiting for 2 red bricks"}</p>
              </div>
            )}
          </Widget>

          {/* Expiry / Square-off */}
          <Widget title="Expiry & Square-off" testid="expiry-widget"
            icon={<CalendarClock className="h-3.5 w-3.5 text-slate-500" />}
            right={state.expiry.is_today
              ? <span className="bg-red-100 text-red-700 border border-red-200 px-2 py-0.5 text-[10px] font-mono uppercase">Expiry Today</span>
              : <span className="font-mono text-[10px] text-slate-400 uppercase">Carry forward</span>}>
            <div className="grid grid-cols-2 gap-2">
              <div><p className="font-mono text-[10px] uppercase text-slate-400">Next Expiry</p><p className="font-mono text-sm font-semibold" data-testid="next-expiry">{state.expiry.next}</p></div>
              <div><p className="font-mono text-[10px] uppercase text-slate-400">IST Now</p><p className="font-mono text-sm font-semibold">{state.expiry.ist_time}</p></div>
              <div><p className="font-mono text-[10px] uppercase text-slate-400">Square-off</p><p className="font-mono text-sm font-semibold">{state.expiry.square_off_time}</p></div>
              <div><p className="font-mono text-[10px] uppercase text-slate-400">Auto</p><p className={`font-mono text-sm font-semibold ${state.expiry.auto_square_off ? "text-emerald-600" : "text-slate-400"}`}>{state.expiry.auto_square_off ? "ON" : "OFF"}</p></div>
            </div>
            {state.expiry.squared_off && (
              <p className="font-mono text-[10px] text-red-600 mt-2 flex items-center gap-1"><AlertTriangle className="h-3 w-3" /> Squared off the expiring contract{state.settings.rollover_position ? " — rolling short to next month" : " — new entries blocked today"}</p>
            )}
            <p className="font-mono text-[10px] text-slate-400 mt-2">Auto-exits the expiring contract at {state.expiry.square_off_time} IST on expiry day; positions carry forward on all other days. {state.expiry.auto_roll ? (state.settings.rollover_position ? "On expiry it rolls AND re-opens the short on next month (position rollover)." : "Auto-rolls to next month after expiry.") : "Manual roll."}</p>
          </Widget>

          {/* Risk / Circuit Breaker */}
          <Widget title="Risk · Circuit Breaker" testid="risk-widget"
            icon={<ShieldAlert className="h-3.5 w-3.5 text-slate-500" />}
            right={state.risk.breaker_tripped
              ? <span className="bg-red-600 text-white px-2 py-0.5 text-[10px] font-mono uppercase flex items-center gap-1"><ShieldX className="h-3 w-3" /> Tripped</span>
              : <span className="bg-emerald-100 text-emerald-700 border border-emerald-200 px-2 py-0.5 text-[10px] font-mono uppercase">Armed</span>}>
            <div className="grid grid-cols-2 gap-2">
              <div>
                <p className="font-mono text-[10px] uppercase text-slate-400">Day P&L (₹)</p>
                <p className={`font-mono text-lg font-bold ${pnlClass(state.risk.day_total)}`} data-testid="day-pnl">{sign(state.risk.day_total)}{fmt(state.risk.day_total)}</p>
              </div>
              <div>
                <p className="font-mono text-[10px] uppercase text-slate-400">Max Loss (₹)</p>
                <p className="font-mono text-lg font-bold text-red-500">-{fmt(state.risk.daily_max_loss, 0)}</p>
              </div>
            </div>
            {state.risk.broker_pnl && (
              <div className="mt-3 pt-3 border-t border-slate-100" data-testid="broker-pnl-block">
                <p className="font-mono text-[10px] uppercase tracking-widest text-slate-400 flex items-center gap-1">
                  Angel One Day P&L {state.market_open ? "(live)" : "(at close)"}
                  {!state.market_open
                    ? <span className="bg-amber-100 text-amber-700 px-1.5 py-0.5 text-[9px]">FROZEN</span>
                    : state.risk.broker_pnl.found
                    ? <span className="bg-emerald-100 text-emerald-700 px-1.5 py-0.5 text-[9px]">SYNCED</span>
                    : <span className="bg-slate-100 text-slate-500 px-1.5 py-0.5 text-[9px]">—</span>}
                </p>
                <div className="grid grid-cols-3 gap-2 mt-1">
                  <div>
                    <p className="font-mono text-[9px] uppercase text-slate-400">Realised</p>
                    <p className={`font-mono text-sm font-bold ${pnlClass(state.risk.broker_pnl.realised)}`} data-testid="broker-realised-pnl">{sign(state.risk.broker_pnl.realised)}{fmt(state.risk.broker_pnl.realised)}</p>
                  </div>
                  <div>
                    <p className="font-mono text-[9px] uppercase text-slate-400">Unrealised</p>
                    <p className={`font-mono text-sm font-bold ${pnlClass(state.risk.broker_pnl.unrealised)}`} data-testid="broker-unrealised-pnl">{sign(state.risk.broker_pnl.unrealised)}{fmt(state.risk.broker_pnl.unrealised)}</p>
                  </div>
                  <div>
                    <p className="font-mono text-[9px] uppercase text-slate-400">Total</p>
                    <p className={`font-mono text-sm font-bold ${pnlClass(state.risk.broker_pnl.total)}`} data-testid="broker-total-pnl">{sign(state.risk.broker_pnl.total)}{fmt(state.risk.broker_pnl.total)}</p>
                  </div>
                </div>
                <p className="font-mono text-[9px] text-slate-400 mt-1">{state.market_open ? "Real booked P&L from your Angel One account — includes manual orders." : "Market closed — showing last value from market hours (Angel One sends unreliable data after close)."}</p>
              </div>
            )}
            {/* loss progress bar */}
            <div className="mt-2 h-1.5 w-full bg-slate-100">
              <div className="h-1.5 bg-red-500 transition-all" style={{ width: `${Math.min(100, Math.max(0, (-Math.min(0, state.risk.day_total) / state.risk.daily_max_loss) * 100))}%` }} />
            </div>
            {state.risk.breaker_tripped ? (
              <div className="mt-3">
                <p className="font-mono text-[10px] text-red-600 flex items-center gap-1"><AlertTriangle className="h-3 w-3" /> Breaker tripped — bot auto-stopped & entries blocked for today.</p>
                <button onClick={armBreaker} data-testid="rearm-button"
                  className="w-full mt-2 border border-slate-300 hover:bg-slate-50 text-slate-700 font-mono uppercase text-[11px] tracking-wider px-3 py-1.5 transition-colors flex items-center justify-center gap-2">
                  <ShieldCheck className="h-3.5 w-3.5" /> Re-arm breaker
                </button>
              </div>
            ) : (
              <p className="font-mono text-[10px] text-slate-400 mt-2">Auto-stops the bot & squares off if today's P&L (realized + unrealized) hits -₹{fmt(state.risk.daily_max_loss, 0)}.</p>
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
                  ["Brick Size", "brick_size"], ["Bar Secs", "bar_seconds"],
                  ["Lot Size", "lot_size"], ["Buffer (pt)", "buffer_points"],
                  ["Max Slip (pt)", "max_slippage"], ["Expiry Slip (pt)", "forced_exit_slippage"],
                  ["Max Reds→1G", "max_red_single_green"], ["Greens (ext)", "greens_to_exit_extended"],
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
                <span className="font-mono text-[10px] uppercase text-slate-500">Position rollover at expiry<br /><span className="text-slate-400 normal-case">re-open short on next month after square-off</span></span>
                <input type="checkbox" checked={!!form.rollover_position} disabled={state.running}
                  onChange={(e) => setForm({ ...form, rollover_position: e.target.checked })}
                  className="h-4 w-4 accent-slate-900 disabled:opacity-40" data-testid="setting-rollover_position" />
              </label>
              <button onClick={saveSettings} disabled={state.running} data-testid="save-settings-button"
                className="w-full mt-3 bg-slate-900 hover:bg-slate-800 disabled:bg-slate-300 text-white font-mono uppercase text-xs tracking-wider px-4 py-2 transition-colors flex items-center justify-center gap-2">
                <Save className="h-3.5 w-3.5" /> Apply {state.running && "(stop bot first)"}
              </button>
            </Widget>
          )}

          {/* Angel One */}
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
        </div>
      </main>
    </div>
  );
}
