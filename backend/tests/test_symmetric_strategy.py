"""Unit tests for the SYMMETRIC long+short Renko strategy (no real orders).
Drives TradingEngine._process_brick / _apply_fill / _update_unrealized / _replay_position
directly with a mocked order executor, so no broker calls or real orders happen."""
import os
import sys
import asyncio
import importlib
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))
load_dotenv(Path(__file__).resolve().parents[2] / "frontend" / ".env")

engine_mod = importlib.import_module("engine")
TradingEngine = engine_mod.TradingEngine
from db import db  # noqa: E402


def _mk(color, idx=0):
    return {"index": idx, "color": color, "open": 0.0, "close": 0.0, "time": "t", "signal": None}


def _fresh():
    eng = TradingEngine(db)
    eng.running = True
    eng.settings["max_red_single_green"] = 4
    eng.settings["greens_to_exit_extended"] = 2
    return eng


async def _feed(eng, colors):
    """Feed a list of brick colours through _process_brick, recording order calls.
    Returns the list of (side, kind) order calls."""
    calls = []

    async def rec(side, kind, price, idx, reason="SIGNAL"):
        calls.append((side, kind))

    with patch.object(eng, "_execute_order", side_effect=rec), \
         patch.object(eng, "_entries_blocked", return_value=False):
        for i, c in enumerate(colors):
            eng._process_brick(_mk(c, i + 1))
            await asyncio.sleep(0)
    return calls


# ---------------------------------------------------------------- SHORT side
def test_short_entry_on_two_reds():
    async def scenario():
        eng = _fresh()
        calls = await _feed(eng, ["red", "red"])
        assert calls == [("SELL", "ENTRY")]
        assert eng.pending_entry is True
        assert eng._entry_side == "SHORT"
        assert eng.down_run_reds == 2
    asyncio.run(scenario())


def test_short_exit_first_green_when_run_le_4():
    async def scenario():
        eng = _fresh()
        # simulate an open SHORT that rode a 3-red run
        eng.position = {"side": "SHORT", "qty": 65, "entry_price": 100.0,
                        "entry_time": "t", "entry_order_id": "x",
                        "reds_at_entry": 3, "unrealized_pnl": 0.0}
        eng.down_run_reds = 3
        eng.consec_red = 3
        calls = await _feed(eng, ["green"])   # 1st green -> exit (<=4 reds)
        assert calls == [("BUY", "EXIT")]
        assert eng.pending_exit is True
    asyncio.run(scenario())


def test_short_needs_two_greens_when_run_gt_4():
    async def scenario():
        eng = _fresh()
        eng.position = {"side": "SHORT", "qty": 65, "entry_price": 100.0,
                        "entry_time": "t", "entry_order_id": "x",
                        "reds_at_entry": 5, "unrealized_pnl": 0.0}
        eng.down_run_reds = 5
        eng.consec_red = 5
        calls = await _feed(eng, ["green"])   # 1st green -> NO exit
        assert calls == []
        assert eng.pending_exit is False
        calls = await _feed(eng, ["green"])   # 2nd green -> exit
        assert calls == [("BUY", "EXIT")]
    asyncio.run(scenario())


# ---------------------------------------------------------------- LONG side
def test_long_entry_on_two_greens():
    async def scenario():
        eng = _fresh()
        calls = await _feed(eng, ["green", "green"])
        assert calls == [("BUY", "ENTRY")]
        assert eng.pending_entry is True
        assert eng._entry_side == "LONG"
        assert eng.down_run_reds == 2
    asyncio.run(scenario())


def test_long_exit_first_red_when_run_le_4():
    async def scenario():
        eng = _fresh()
        eng.position = {"side": "LONG", "qty": 65, "entry_price": 100.0,
                        "entry_time": "t", "entry_order_id": "x",
                        "reds_at_entry": 3, "unrealized_pnl": 0.0}
        eng.down_run_reds = 3
        eng.consec_green = 3
        calls = await _feed(eng, ["red"])   # 1st red -> exit (<=4 greens)
        assert calls == [("SELL", "EXIT")]
        assert eng.pending_exit is True
    asyncio.run(scenario())


def test_long_needs_two_reds_when_run_gt_4():
    async def scenario():
        eng = _fresh()
        eng.position = {"side": "LONG", "qty": 65, "entry_price": 100.0,
                        "entry_time": "t", "entry_order_id": "x",
                        "reds_at_entry": 5, "unrealized_pnl": 0.0}
        eng.down_run_reds = 5
        eng.consec_green = 5
        calls = await _feed(eng, ["red"])
        assert calls == []
        calls = await _feed(eng, ["red"])
        assert calls == [("SELL", "EXIT")]
    asyncio.run(scenario())


# ---------------------------------------------------------------- P&L formulas
def test_apply_fill_sets_side_and_pnl():
    async def scenario():
        eng = _fresh()
        with patch.object(eng, "_save_trade", new=AsyncMock()):
            # LONG entry (BUY fill)
            entry = {"kind": "ENTRY", "side": "BUY", "qty": 65, "fill_price": 100.0,
                     "fill_time": "t", "id": "e1"}
            eng.down_run_reds = 2
            eng._apply_fill(entry)
            assert eng.position["side"] == "LONG"
            # LONG exit at higher price -> profit = (exit-entry)*qty
            exit_o = {"kind": "EXIT", "side": "SELL", "qty": 65, "fill_price": 110.0,
                      "fill_time": "t", "reason": "SIGNAL"}
            eng._apply_fill(exit_o)
            assert eng.metrics["realized_pnl"] == round((110.0 - 100.0) * 65, 2)
            assert eng.position is None

        # SHORT round-trip
        eng2 = _fresh()
        with patch.object(eng2, "_save_trade", new=AsyncMock()):
            eng2.down_run_reds = 2
            eng2._apply_fill({"kind": "ENTRY", "side": "SELL", "qty": 65, "fill_price": 100.0,
                              "fill_time": "t", "id": "e2"})
            assert eng2.position["side"] == "SHORT"
            eng2._apply_fill({"kind": "EXIT", "side": "BUY", "qty": 65, "fill_price": 90.0,
                              "fill_time": "t", "reason": "SIGNAL"})
            assert eng2.metrics["realized_pnl"] == round((100.0 - 90.0) * 65, 2)
        await asyncio.sleep(0)
    asyncio.run(scenario())


def test_update_unrealized_both_sides():
    eng = _fresh()
    eng.price = 110.0
    eng.position = {"side": "LONG", "qty": 65, "entry_price": 100.0, "unrealized_pnl": 0.0}
    eng._update_unrealized()
    assert eng.position["unrealized_pnl"] == round((110.0 - 100.0) * 65, 2)
    eng.position = {"side": "SHORT", "qty": 65, "entry_price": 100.0, "unrealized_pnl": 0.0}
    eng._update_unrealized()
    assert eng.position["unrealized_pnl"] == round((100.0 - 110.0) * 65, 2)


# ---------------------------------------------------------------- flip after exit
def test_flip_short_to_long_after_exit():
    async def scenario():
        eng = _fresh()
        # open SHORT riding 2 reds
        eng.position = {"side": "SHORT", "qty": 65, "entry_price": 100.0,
                        "entry_time": "t", "entry_order_id": "x",
                        "reds_at_entry": 2, "unrealized_pnl": 0.0}
        eng.down_run_reds = 2
        eng.consec_red = 2
        calls = await _feed(eng, ["green"])       # exit short on 1st green
        assert calls == [("BUY", "EXIT")]
        # simulate the exit filling -> flat
        eng.position = None
        eng.pending_exit = False
        # 2nd green now forms -> should flip LONG (consec_green already 1, becomes 2)
        calls = await _feed(eng, ["green"])
        assert calls == [("BUY", "ENTRY")]
        assert eng._entry_side == "LONG"
    asyncio.run(scenario())


# ---------------------------------------------------------------- replay on start
def test_replay_position_uptrend_long():
    eng = _fresh()
    # 3 greens with no reversal -> should be LONG
    eng.bricks = [_mk("green", i) for i in range(3)]
    side, run, cr, cg = eng._replay_position()
    assert side == "LONG"


def test_replay_position_reversal_flat():
    eng = _fresh()
    # 3 reds then 2 greens (<=4 reds -> 1 green exits; 2nd green would flip to long entry)
    eng.bricks = [_mk("red", 0), _mk("red", 1), _mk("red", 2), _mk("green", 3), _mk("green", 4)]
    side, run, cr, cg = eng._replay_position()
    # after exiting the short on 1st green, 2nd green -> LONG entry
    assert side == "LONG"
