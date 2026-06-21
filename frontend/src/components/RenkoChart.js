import React, { useMemo, useRef, useEffect } from "react";

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
  try {
    return new Date(t).toLocaleDateString("en-IN", { timeZone: "Asia/Kolkata" });
  } catch { return ""; }
};

export const RenkoChart = ({ bricks }) => {
  const scrollRef = useRef(null);
  const pinnedRef = useRef(true);

  const H = 440;
  const padTop = 18;
  const axisH = 34;
  const chartH = H - padTop - axisH;
  const brickW = 16;
  const gap = 6;
  const step = brickW + gap;
  const leftPad = 12;
  const rightAxis = 54;

  const { rects, levels, totalW, timeTicks } = useMemo(() => {
    if (!bricks || bricks.length === 0) return { rects: [], levels: [], totalW: 0, timeTicks: [] };
    let min = Infinity, max = -Infinity;
    bricks.forEach((b) => {
      min = Math.min(min, b.open, b.close);
      max = Math.max(max, b.open, b.close);
    });
    const range = max - min || 50;
    const pad = range * 0.1;
    min -= pad; max += pad;
    const yScale = (price) => padTop + ((max - price) / (max - min)) * chartH;

    const rects = bricks.map((b, i) => {
      const top = yScale(Math.max(b.open, b.close));
      const bottom = yScale(Math.min(b.open, b.close));
      return {
        x: leftPad + i * step, y: top, h: Math.max(bottom - top, 2),
        color: b.color, signal: b.signal, time: b.time,
        open: b.open, close: b.close,
      };
    });

    const stepP = (max - min) / 6;
    const levels = Array.from({ length: 7 }, (_, i) => {
      const price = max - stepP * i;
      return { y: yScale(price), price: Math.round(price) };
    });

    // time ticks: label new-day boundaries + periodic, with a min pixel spacing to avoid overlap
    const timeTicks = [];
    let lastDay = null;
    let lastX = -999;
    bricks.forEach((b, i) => {
      const dk = dayKey(b.time);
      const newDay = dk !== lastDay;
      const x = leftPad + i * step + brickW / 2;
      const wantTick = newDay || i % 10 === 0;
      if (wantTick && (newDay || x - lastX >= 60)) {
        timeTicks.push({ x, time: b.time, newDay });
        lastX = x;
      }
      lastDay = dk;
    });

    const totalW = leftPad + bricks.length * step + rightAxis;
    return { rects, levels, totalW, timeTicks };
  }, [bricks, chartH]);

  // auto-scroll to latest unless the user has panned back
  useEffect(() => {
    const el = scrollRef.current;
    if (el && pinnedRef.current) el.scrollLeft = el.scrollWidth;
  }, [bricks]);

  const onScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    pinnedRef.current = el.scrollWidth - el.clientWidth - el.scrollLeft < 48;
  };

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
    <div className="relative w-full bg-white">
      <div ref={scrollRef} onScroll={onScroll} className="w-full overflow-x-auto" data-testid="renko-chart">
        <svg width={svgW} height={H} viewBox={`0 0 ${svgW} ${H}`} className="block">
          {/* price gridlines + right-axis labels (sticky-ish: drawn across full width) */}
          {levels.map((l, i) => (
            <g key={`lv-${i}`}>
              <line x1={0} x2={svgW - rightAxis} y1={l.y} y2={l.y} stroke="#F1F5F9" strokeWidth="1" />
            </g>
          ))}
          {/* bricks */}
          {rects.map((r, i) => (
            <g key={i}>
              <title>{fmtDate(r.time)} {fmtTime(r.time)} · {r.open} → {r.close}</title>
              <rect x={r.x} y={r.y} width={brickW} height={r.h}
                fill={r.color === "green" ? "#10B981" : "#EF4444"}
                stroke={r.color === "green" ? "#059669" : "#DC2626"} strokeWidth="1" rx="1" />
              {r.signal === "SHORT" && (
                <g>
                  <line x1={r.x + brickW / 2} x2={r.x + brickW / 2} y1={r.y - 16} y2={r.y} stroke="#0F172A" strokeWidth="1" strokeDasharray="2 2" />
                  <circle cx={r.x + brickW / 2} cy={r.y - 20} r="8" fill="#0F172A" />
                  <text x={r.x + brickW / 2} y={r.y - 16.5} fontSize="9" fill="#fff" textAnchor="middle" fontFamily="IBM Plex Mono">S</text>
                </g>
              )}
              {r.signal === "COVER" && (
                <g>
                  <line x1={r.x + brickW / 2} x2={r.x + brickW / 2} y1={r.y + r.h} y2={r.y + r.h + 16} stroke="#3B82F6" strokeWidth="1" strokeDasharray="2 2" />
                  <circle cx={r.x + brickW / 2} cy={r.y + r.h + 20} r="8" fill="#3B82F6" />
                  <text x={r.x + brickW / 2} y={r.y + r.h + 23} fontSize="9" fill="#fff" textAnchor="middle" fontFamily="IBM Plex Mono">C</text>
                </g>
              )}
            </g>
          ))}
          {/* bottom time axis */}
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
      {/* fixed right-side price axis overlay */}
      <div className="pointer-events-none absolute top-0 right-0 h-full" style={{ width: rightAxis }}>
        <svg width={rightAxis} height={H} className="block bg-white border-l border-slate-100">
          {levels.map((l, i) => (
            <text key={i} x={6} y={l.y + 4} fontSize="10" fontFamily="IBM Plex Mono" fill="#64748B">{l.price}</text>
          ))}
        </svg>
      </div>
    </div>
  );
};

export default RenkoChart;
