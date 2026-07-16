import React, { useEffect, useRef, useState, useCallback } from "react";
import axios from "axios";
import { toast } from "sonner";
import Backtest from "@/components/Backtest";
import { DashboardHeader } from "@/components/dashboard/DashboardHeader";
import { DashboardDialogs } from "@/components/dashboard/DashboardDialogs";
import { ChartPanel } from "@/components/dashboard/ChartPanel";
import { TradeLogPanel } from "@/components/dashboard/TradeLogPanel";
import { OrderLogPanel } from "@/components/dashboard/OrderLogPanel";
import { ReconcilePanel } from "@/components/dashboard/ReconcilePanel";
import { ManualOrderPanel } from "@/components/dashboard/ManualOrderPanel";
import { PerformancePanel } from "@/components/dashboard/PerformancePanel";
import { OpenPositionPanel } from "@/components/dashboard/OpenPositionPanel";
import { ExpiryPanel } from "@/components/dashboard/ExpiryPanel";
import { RiskPanel } from "@/components/dashboard/RiskPanel";
import { OrderBookPanel } from "@/components/dashboard/OrderBookPanel";
import { StrategySettingsPanel } from "@/components/dashboard/StrategySettingsPanel";
import { AngelPanel } from "@/components/dashboard/AngelPanel";

const API = `${process.env.REACT_APP_BACKEND_URL}/api`;

export default function Dashboard({ onLogout }) {
  const [state, setState] = useState(null);
  const [trades, setTrades] = useState([]);
  const [form, setForm] = useState(null);
  const prevPrice = useRef(null);
  const lastAlertId = useRef(null);
  const runLock = useRef(null);   // {running, until} — pin button to user's intent, ignore stale polls
  const [flash, setFlash] = useState("");
  const [instQuery, setInstQuery] = useState("");
  const [instResults, setInstResults] = useState([]);
  const [showInstSearch, setShowInstSearch] = useState(false);
  const [showStopConfirm, setShowStopConfirm] = useState(false);
  const [showStartConfirm, setShowStartConfirm] = useState(false);
  const [showBacktest, setShowBacktest] = useState(false);
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
      // A Start/Stop click briefly pins `running` to the user's intent so out-of-order
      // /state responses (stale requests still in flight) can't flip the button back.
      if (runLock.current) {
        if (Date.now() < runLock.current.until) data.running = runLock.current.running;
        else runLock.current = null;
      }
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
    runLock.current = { running: true, until: Date.now() + 6000 };
    await axios.post(`${API}/bot/start`);
    toast.warning("Bot started — LIVE real-money trading is ACTIVE");
    poll();
  };

  const confirmStop = async () => {
    setShowStopConfirm(false);
    runLock.current = { running: false, until: Date.now() + 6000 };
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
      entry_bricks: Number(form.entry_bricks) || 2,
      chop_filter: !!form.chop_filter,
      chop_lookback: Number(form.chop_lookback) || 50,
      chop_threshold: Number(form.chop_threshold),
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
      <DashboardHeader state={state} flash={flash} priceUp={priceUp} reset={reset}
        setShowBacktest={setShowBacktest} onLogout={onLogout} startStop={startStop} />

      {showBacktest && <Backtest onClose={() => setShowBacktest(false)} defaultBrick={state?.settings?.brick_size || 50} />}

      <DashboardDialogs showStopConfirm={showStopConfirm} setShowStopConfirm={setShowStopConfirm}
        state={state} confirmStop={confirmStop} showStartConfirm={showStartConfirm}
        setShowStartConfirm={setShowStartConfirm} confirmStart={confirmStart} resolveAdoption={resolveAdoption} />

      <main className="grid grid-cols-1 lg:grid-cols-4 gap-4 p-4">
        {/* Left column */}
        <div className="lg:col-span-3 flex flex-col gap-4">
          <ChartPanel state={state} loadHistory={loadHistory} trades={trades} />
          <TradeLogPanel trades={trades} />
          <OrderLogPanel orderLog={orderLog} clearOrderLog={clearOrderLog} state={state} />
        </div>

        {/* Right column */}
        <div className="lg:col-span-1 flex flex-col gap-4">
          <ReconcilePanel state={state} recon={recon} reconBusy={reconBusy}
            checkReconcile={checkReconcile} resolveReconcile={resolveReconcile} />
          <ManualOrderPanel state={state} manualOrder={manualOrder} manualBusy={manualBusy} />
          <PerformancePanel m={m} />
          <OpenPositionPanel pos={pos} state={state} m={m} squareOff={squareOff} />
          <ExpiryPanel state={state} />
          <RiskPanel state={state} armBreaker={armBreaker} />
          <OrderBookPanel state={state} />
          {form && <StrategySettingsPanel form={form} state={state} setForm={setForm} saveSettings={saveSettings} />}
          <AngelPanel state={state} showInstSearch={showInstSearch} setShowInstSearch={setShowInstSearch}
            searchInstruments={searchInstruments} instQuery={instQuery} instResults={instResults}
            selectInstrument={selectInstrument} disconnectAngel={disconnectAngel} connectAngel={connectAngel} />
        </div>
      </main>
    </div>
  );
}
