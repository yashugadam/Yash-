import React, { useMemo } from "react";

export const RenkoChart = ({ bricks }) => {
  const W = 1100;
  const H = 430;
  const padTop = 24;
  const padBottom = 28;
  const brickW = 16;
  const gap = 4;

  const { rects, levels, yScale } = useMemo(() => {
    if (!bricks || bricks.length === 0) return { rects: [], levels: [], yScale: null };
    let min = Infinity, max = -Infinity;
    bricks.forEach((b) => {
      min = Math.min(min, b.open, b.close);
      max = Math.max(max, b.open, b.close);
    });
    const range = max - min || 50;
    const pad = range * 0.12;
    min -= pad; max += pad;
    const innerH = H - padTop - padBottom;
    const yScale = (price) => padTop + ((max - price) / (max - min)) * innerH;

    const totalW = bricks.length * (brickW + gap);
    const offsetX = Math.max(W - totalW - 16, 16);

    const rects = bricks.map((b, i) => {
      const top = yScale(Math.max(b.open, b.close));
      const bottom = yScale(Math.min(b.open, b.close));
      return {
        x: offsetX + i * (brickW + gap),
        y: top,
        h: Math.max(bottom - top, 2),
        color: b.color,
        signal: b.signal,
        index: b.index,
        cy: (top + bottom) / 2,
      };
    });

    const step = (max - min) / 5;
    const levels = Array.from({ length: 6 }, (_, i) => {
      const price = max - step * i;
      return { y: yScale(price), price: Math.round(price) };
    });

    return { rects, levels, yScale };
  }, [bricks]);

  if (!bricks || bricks.length === 0) {
    return (
      <div className="relative w-full h-[430px] flex items-center justify-center bg-white" data-testid="renko-empty">
        <div className="text-center">
          <p className="font-mono text-xs uppercase tracking-widest text-slate-400">Awaiting price feed</p>
          <p className="font-mono text-[11px] text-slate-300 mt-2">Start the bot to begin building bricks</p>
        </div>
      </div>
    );
  }

  return (
    <div className="w-full overflow-x-auto bg-white" data-testid="renko-chart">
      <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} className="block">
        {levels.map((l, i) => (
          <g key={i}>
            <line x1={0} x2={W - 52} y1={l.y} y2={l.y} stroke="#F1F5F9" strokeWidth="1" />
            <text x={W - 46} y={l.y + 4} fontSize="10" fontFamily="IBM Plex Mono" fill="#94A3B8">
              {l.price}
            </text>
          </g>
        ))}
        {rects.map((r, i) => (
          <g key={i}>
            <rect
              x={r.x}
              y={r.y}
              width={brickW}
              height={r.h}
              fill={r.color === "green" ? "#10B981" : "#EF4444"}
              stroke={r.color === "green" ? "#059669" : "#DC2626"}
              strokeWidth="1"
              rx="1"
            />
            {r.signal === "SHORT" && (
              <g>
                <line x1={r.x + brickW / 2} x2={r.x + brickW / 2} y1={r.y - 18} y2={r.y} stroke="#0F172A" strokeWidth="1" strokeDasharray="2 2" />
                <circle cx={r.x + brickW / 2} cy={r.y - 22} r="9" fill="#0F172A" />
                <text x={r.x + brickW / 2} y={r.y - 18} fontSize="9" fill="#fff" textAnchor="middle" fontFamily="IBM Plex Mono">S</text>
              </g>
            )}
            {r.signal === "COVER" && (
              <g>
                <line x1={r.x + brickW / 2} x2={r.x + brickW / 2} y1={r.y + r.h} y2={r.y + r.h + 18} stroke="#3B82F6" strokeWidth="1" strokeDasharray="2 2" />
                <circle cx={r.x + brickW / 2} cy={r.y + r.h + 22} r="9" fill="#3B82F6" />
                <text x={r.x + brickW / 2} y={r.y + r.h + 26} fontSize="9" fill="#fff" textAnchor="middle" fontFamily="IBM Plex Mono">C</text>
              </g>
            )}
          </g>
        ))}
      </svg>
    </div>
  );
};

export default RenkoChart;
