import React from "react";
import { AlertTriangle, CalendarClock } from "lucide-react";
import { Widget } from "./Widget";

export function ExpiryPanel({ state }) {
  return (
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
  );
}
