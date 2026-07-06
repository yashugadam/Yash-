export const fmt = (n, d = 2) =>
  (n === null || n === undefined) ? "--" : Number(n).toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d });
export const pnlClass = (n) => (n > 0 ? "text-emerald-600" : n < 0 ? "text-red-500" : "text-slate-500");
export const sign = (n) => (n > 0 ? "+" : "");
