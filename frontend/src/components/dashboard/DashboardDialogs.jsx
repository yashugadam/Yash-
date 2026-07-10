import React from "react";
import { AlertTriangle, Zap } from "lucide-react";
import {
  AlertDialog, AlertDialogContent, AlertDialogHeader, AlertDialogFooter,
  AlertDialogTitle, AlertDialogDescription, AlertDialogAction, AlertDialogCancel,
} from "@/components/ui/alert-dialog";

export function DashboardDialogs({ showStopConfirm, setShowStopConfirm, state, confirmStop, showStartConfirm, setShowStartConfirm, confirmStart, resolveAdoption }) {
  return (
    <>
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
              Once started, it will place real CARRYFORWARD LIMIT orders automatically — it takes both directions: <b>SHORT on 2 red bricks</b> and <b>LONG on 2 green bricks</b>, and if the market is already trending it will <b>enter immediately</b> in that direction.
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
    </>
  );
}
