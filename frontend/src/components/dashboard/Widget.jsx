import React from "react";

export const Widget = ({ title, icon, children, right, testid }) => (
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
