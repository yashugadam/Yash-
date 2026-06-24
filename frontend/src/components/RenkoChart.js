import React, { useMemo, useRef, useEffect, useState, useCallback } from "react";
import { ZoomIn, ZoomOut, Maximize2 } from "lucide-react";

const fmtTime = (t) => {
  try {
    return new Date(t).toLocaleTimeString("en-IN", {
      hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "Asia/Kolkata",
    });
  } catch { return ""; }
};
const fmtDate = (t) => {
  try {
    return new Date(t).toLocaleDateString("en-IN", {
      day: "2-digit", month: "short", timeZone: "Asia/Kolkata",
    });
  } catch { return ""; }
};
const dayKey = (t) => {
  try { return new Date(t).toLocaleDateString("en-IN", { timeZone: "Asia/Kolkata" }); }
  catch { return ""; }
};

const BASE_BRICK = 16;
const BASE_GAP = 6;
const MIN_ZOOM = 0.18;
const MAX_ZOOM = 4;
const clamp = (v, a, b) => Math.min(b, Math.max(a, v));

export const RenkoChart = ({ bricks, trades }) => {
  const scrollRef = useRef(null);
  const pinnedRef = useRef(true);
  const focusRef = useRef(null);     // {idx, offset} to keep focal point on zoom
  const dragRef = useRef(null);      // {startX, startScroll}
  const [zoom, setZoom] = useState(1);

  const H = 440;
  const padTop = 18;
  const axisH = 34;
  const chartH = H - padTop - axisH;
  const leftPad = 12;
  const rightAxis = 54;

  const brickW = BASE_BRICK * zoom;
  const gap = BASE_GAP * zoom;
  const step = brickW + gap;

  const { rects, levels, totalW, timeTicks, tradeMarks } = useMemo(() => {
    if (!bricks || bricks.length === 0) return { rects: [], levels: [], totalW: 0, timeTicks: [], tradeMarks: [] };
    let min = Infinity, max = -Infinity;
    bricks.forEach((b) => { min = Math.min(min, b.open, b.close); max = Math.max(max, b.open, b.close); });
    const range = max - min || 50;
    const pad = range * 0.1;
    min -= pad; max += pad;
    const yScale = (p) => padTop + ((max - p) / (max - min)) * chartH;

    const rects = bricks.map((b, i) => {
      const top = yScale(Math.max(b.open, b.close));
      const bottom = yScale(Math.min(b.open, b.close));
      return { x: leftPad + i * step, y: top, h: Math.max(bottom - top, 2),
        index: b.index, color: b.color, signal: b.signal, time: b.time, open: b.open, close: b.close };
    });

    const stepP = (max - min) / 6;
    const levels = Array.from({ length: 7 }, (_, i) => {
      const price = max - stepP * i;
      return { y: yScale(price), price: Math.round(price) };
    });

    const timeTicks = [];
    let lastDay = null, lastX = -999;
    const minSpace = 62;
    bricks.forEach((b, i) => {
      const dk = dayKey(b.time);
      const newDay = dk !== lastDay;
      const x = leftPad + i * step + brickW / 2;
      if ((newDay || i % 10 === 0) && x - lastX >= minSpace) {
        timeTicks.push({ x, time: b.time, newDay });
        lastX = x;
      }
      lastDay = dk;
    });

    const totalW = leftPad + bricks.length * step + rightAxis;

    // overlay closed trades: SELL marker at entry, BUY/cover at exit, connector colored by P&L
    const tBrick = bricks.map((b) => new Date(b.time).getTime());
    const firstT = tBrick[0], lastT = tBrick[tBrick.length - 1];
    const nearestIdx = (t) => {
      const tt = new Date(t).getTime();
      let best = 0, bd = Infinity;
      for (let i = 0; i < tBrick.length; i++) {
        const d = Math.abs(tBrick[i] - tt);
        if (d < bd) { bd = d; best = i; }
      }
      return best;
    };
    const tradeMarks = (trades || [])
      .filter((tr) => {
        const et = new Date(tr.entry_time).getTime();
        return et >= firstT - 3.6e6 && et <= lastT + 3.6e6;
      })
      .map((tr) => {
        const ei = nearestIdx(tr.entry_time);
        const xi = nearestIdx(tr.exit_time);
        return {
          ex: leftPad + ei * step + brickW / 2, ey: yScale(tr.entry_price),
          xx: leftPad + xi * step + brickW / 2, xy: yScale(tr.exit_price),
          entry: tr.entry_price, exit: tr.exit_price, pnl: tr.pnl,
          reason: tr.exit_reason, time: tr.exit_time,
        };
      });

    return { rects, levels, totalW, timeTicks, tradeMarks };
  }, [bricks, trades, chartH, step, brickW]);

  // keep focal point under cursor after a zoom; else auto-scroll to latest when pinned
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    if (focusRef.current) {
      const { idx, offset } = focusRef.current;
      el.scrollLeft = leftPad + idx * step - offset;
      focusRef.current = null;
    } else if (pinnedRef.current) {
      el.scrollLeft = el.scrollWidth;
    }
  }, [bricks, step]);

  const onScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    if (!dragRef.current && !focusRef.current)
      pinnedRef.current = el.scrollWidth - el.clientWidth - el.scrollLeft < 48;
  };

  const applyZoom = useCallback((factor, clientX) => {
    const el = scrollRef.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    const cx = (clientX ?? rect.left + el.clientWidth / 2) - rect.left;
    const contentX = el.scrollLeft + cx;
    const idx = (contentX - leftPad) / step;
    pinnedRef.current = false;
    focusRef.current = { idx, offset: cx };
    setZoom((z) => clamp(z * factor, MIN_ZOOM, MAX_ZOOM));
  }, [step]);

  const onWheel = (e) => {
    e.preventDefault();
    applyZoom(e.deltaY < 0 ? 1.15 : 1 / 1.15, e.clientX);
  };

  const fitAll = () => {
    const el = scrollRef.current;
    if (!el || !bricks?.length) return;
    const avail = el.clientWidth - leftPad - rightAxis - 8;
    const needed = bricks.length * (BASE_BRICK + BASE_GAP);
    pinnedRef.current = true;
    focusRef.current = null;
    setZoom(clamp(avail / needed, MIN_ZOOM, MAX_ZOOM));
  };

  // drag to pan
  const onMouseDown = (e) => {
    const el = scrollRef.current;
    if (!el) return;
    dragRef.current = { startX: e.clientX, startScroll: el.scrollLeft };
    pinnedRef.current = false;
  };
  const onMouseMove = (e) => {
    const el = scrollRef.current;
    if (!el || !dragRef.current) return;
    el.scrollLeft = dragRef.current.startScroll - (e.clientX - dragRef.current.startX);
  };
  const endDrag = () => { dragRef.current = null; };

  if (!bricks || bricks.length === 0) {
    return (
      <div className="relative w-full h-[440px] flex items-center justify-center bg-white" data-testid="renko-empty">
        <div className="text-center">
          <p className="font-mono text-xs uppercase tracking-widest text-slate-400">Awaiting price feed</p>
          <p className="font-mono text-[11px] text-slate-300 mt-2">Start the bot, or load history (when connected) to build bricks</p>
        </div>
      </div>
    );
  }

  const svgW = Math.max(totalW, 600);

  return (
    <div className="relative w-full bg-white select-none">
      {/* zoom controls */}
      <div className="absolute top-2 z-10 flex flex-col gap-1" style={{ right: rightAxis + 8 }}>
        <button onClick={() => applyZoom(1.25)} data-testid="zoom-in-btn" title="Zoom in"
          className="h-7 w-7 flex items-center justify-center bg-white border border-slate-200 text-slate-600 hover:bg-slate-50 transition-colors"><ZoomIn className="h-4 w-4" /></button>
        <button onClick={() => applyZoom(1 / 1.25)} data-testid="zoom-out-btn" title="Zoom out"
          className="h-7 w-7 flex items-center justify-center bg-white border border-slate-200 text-slate-600 hover:bg-slate-50 transition-colors"><ZoomOut className="h-4 w-4" /></button>
        <button onClick={fitAll} data-testid="zoom-fit-btn" title="Fit all"
          className="h-7 w-7 flex items-center justify-center bg-white border border-slate-200 text-slate-600 hover:bg-slate-50 transition-colors"><Maximize2 className="h-4 w-4" /></button>
      </div>

      <div ref={scrollRef} onScroll={onScroll} onWheel={onWheel}
        onMouseDown={onMouseDown} onMouseMove={onMouseMove} onMouseUp={endDrag} onMouseLeave={endDrag}
        className="w-full overflow-x-auto cursor-grab active:cursor-grabbing" data-testid="renko-chart">
        <svg width={svgW} height={H} viewBox={`0 0 ${svgW} ${H}`} className="block">
          {levels.map((l, i) => (
            <line key={`lv-${i}`} x1={0} x2={svgW - rightAxis} y1={l.y} y2={l.y} stroke="#F1F5F9" strokeWidth="1" />
          ))}
          {rects.map((r) => (
            <g key={`brick-${r.index}`}>
              <title>{fmtDate(r.time)} {fmtTime(r.time)} · {r.open} → {r.close}</title>
              <rect x={r.x} y={r.y} width={brickW} height={r.h}
                fill={r.color === "green" ? "#10B981" : "#EF4444"}
                stroke={r.color === "green" ? "#059669" : "#DC2626"} strokeWidth={zoom < 0.5 ? 0.5 : 1} rx="1" />
              {r.signal === "SHORT" && zoom >= 0.5 && (
                <g>
                  <circle cx={r.x + brickW / 2} cy={r.y - 14} r="8" fill="#0F172A" />
                  <text x={r.x + brickW / 2} y={r.y - 10.5} fontSize="9" fill="#fff" textAnchor="middle" fontFamily="IBM Plex Mono">S</text>
                </g>
              )}
              {r.signal === "COVER" && zoom >= 0.5 && (
                <g>
                  <circle cx={r.x + brickW / 2} cy={r.y + r.h + 14} r="8" fill="#3B82F6" />
                  <text x={r.x + brickW / 2} y={r.y + r.h + 17} fontSize="9" fill="#fff" textAnchor="middle" fontFamily="IBM Plex Mono">C</text>
                </g>
              )}
            </g>
          ))}
          {/* closed-trade overlay: SELL (entry) -> BUY (cover) */}
          {tradeMarks.map((m, i) => {
            const col = m.pnl >= 0 ? "#10B981" : "#EF4444";
            return (
              <g key={`tm-${i}`}>
                <title>SHORT {m.entry} → {m.exit} · P&L {m.pnl >= 0 ? "+" : ""}{m.pnl} · {m.reason}</title>
                <line x1={m.ex} y1={m.ey} x2={m.xx} y2={m.xy} stroke={col} strokeWidth="1.3" strokeDasharray="4 3" opacity="0.85" />
                {/* entry: SELL (down triangle, dark) */}
                <polygon points={`${m.ex - 5},${m.ey - 11} ${m.ex + 5},${m.ey - 11} ${m.ex},${m.ey - 2}`} fill="#0F172A" />
                <circle cx={m.ex} cy={m.ey} r="2.6" fill="#0F172A" />
                {/* exit: BUY/cover (up triangle, blue) */}
                <polygon points={`${m.xx - 5},${m.xy + 11} ${m.xx + 5},${m.xy + 11} ${m.xx},${m.xy + 2}`} fill="#3B82F6" />
                <circle cx={m.xx} cy={m.xy} r="2.6" fill="#3B82F6" />
              </g>
            );
          })}
          <line x1={0} x2={svgW - rightAxis} y1={H - axisH} y2={H - axisH} stroke="#E2E8F0" strokeWidth="1" />
          {timeTicks.map((t, i) => (
            <g key={`tt-${i}`}>
              <line x1={t.x} x2={t.x} y1={padTop} y2={H - axisH} stroke="#F8FAFC" strokeWidth="1" />
              <line x1={t.x} x2={t.x} y1={H - axisH} y2={H - axisH + 5} stroke="#CBD5E1" strokeWidth="1" />
              <text x={t.x} y={H - axisH + 16} fontSize="9.5" fill="#64748B" textAnchor="middle" fontFamily="IBM Plex Mono">{fmtTime(t.time)}</text>
              {t.newDay && (
                <text x={t.x} y={H - axisH + 28} fontSize="9" fill="#94A3B8" textAnchor="middle" fontFamily="IBM Plex Mono">{fmtDate(t.time)}</text>
              )}
            </g>
          ))}
        </svg>
      </div>

      {/* fixed right-side price axis */}
      <div className="pointer-events-none absolute top-0 right-0 h-full" style={{ width: rightAxis }}>
        <svg width={rightAxis} height={H} className="block bg-white border-l border-slate-100">
          {levels.map((l) => (
            <text key={`px-${l.price}`} x={6} y={l.y + 4} fontSize="10" fontFamily="IBM Plex Mono" fill="#64748B">{l.price}</text>
          ))}
        </svg>
      </div>
    </div>
  );
};

export default RenkoChart;
