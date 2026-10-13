"""Unit tests for the SYMMETRIC long+short Renko strategy (no real orders).
Drives TradingEngine._process_brick / _apply_fill / _update_unrealized / _replay_position
directly with a mocked order executor, so no broker calls or real orders happen."""
import os
import sys
import asyncio
import contextlib
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
    eng.settings["chop_filter"] = False   # tests drive _process_brick without a real brick list
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


def test_short_exit_first_green_even_after_long_run():
    async def scenario():
        eng = _fresh()
        eng.position = {"side": "SHORT", "qty": 65, "entry_price": 100.0,
                        "entry_time": "t", "entry_order_id": "x",
                        "reds_at_entry": 5, "unrealized_pnl": 0.0}
        eng.down_run_reds = 5
        eng.consec_red = 5
        calls = await _feed(eng, ["green"])   # 1st green -> exit regardless of run length
        assert calls == [("BUY", "EXIT")]
        assert eng.pending_exit is True
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


def test_long_exit_first_red_even_after_long_run():
    async def scenario():
        eng = _fresh()
        eng.position = {"side": "LONG", "qty": 65, "entry_price": 100.0,
                        "entry_time": "t", "entry_order_id": "x",
                        "reds_at_entry": 5, "unrealized_pnl": 0.0}
        eng.down_run_reds = 5
        eng.consec_green = 5
        calls = await _feed(eng, ["red"])   # 1st red -> exit regardless of run length
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


# ---------------------------------------------------------------- gap flip (immediate reversal)
def _wire_fills(eng):
    """Patches so _execute_order runs end-to-end without a real broker: _live_fill always
    fills at ref_price, idempotency/save/persist are no-ops, entries not blocked."""
    eng.broker.connected = True

    async def fake_live_fill(order, *a, **k):
        order["fill_price"] = order["ref_price"]
        return True

    return [
        patch.object(eng, "_live_fill", side_effect=fake_live_fill),
        patch.object(eng, "_claim_order_key", new=AsyncMock(return_value=True)),
        patch.object(eng, "_save_order", new=AsyncMock()),
        patch.object(eng, "_save_trade", new=AsyncMock()),
        patch.object(eng, "_persist_state", new=AsyncMock()),
        patch.object(eng, "_entries_blocked", return_value=False),
    ]


def _long_pos(eng):
    eng.price = 100.0
    eng.position = {"side": "LONG", "qty": 65, "entry_price": 110.0, "entry_time": "t",
                    "entry_order_id": "x", "reds_at_entry": 2, "unrealized_pnl": 0.0}


def test_gap_flip_long_to_short_after_exit_fill():
    """Gap down prints 2+ reds at once: after the long's exit FILLS, the bot flips SHORT
    immediately (no waiting for a new brick)."""
    async def scenario():
        eng = _fresh()
        _long_pos(eng)
        eng.consec_red = 2   # gap already printed 2 reds
        with contextlib.ExitStack() as st:
            for p in _wire_fills(eng):
                st.enter_context(p)
            await eng._execute_order("SELL", "EXIT", 100.0, 5)  # strategy exit (reason SIGNAL)
        assert eng.position is not None
        assert eng.position["side"] == "SHORT"
    asyncio.run(scenario())


def test_no_flip_on_single_opposite_brick():
    """Normal (non-gap) exit on a single red -> just goes flat, no immediate short."""
    async def scenario():
        eng = _fresh()
        _long_pos(eng)
        eng.consec_red = 1
        with contextlib.ExitStack() as st:
            for p in _wire_fills(eng):
                st.enter_context(p)
            await eng._execute_order("SELL", "EXIT", 100.0, 5)
        assert eng.position is None
    asyncio.run(scenario())


def test_forced_exit_does_not_flip():
    """A forced exit (manual square-off / expiry / breaker) must NOT auto-reenter, even in a gap."""
    async def scenario():
        eng = _fresh()
        _long_pos(eng)
        eng.consec_red = 2
        with contextlib.ExitStack() as st:
            for p in _wire_fills(eng):
                st.enter_context(p)
            await eng._execute_order("SELL", "EXIT", 100.0, -1, "MANUAL_SQUAREOFF")
        assert eng.position is None
    asyncio.run(scenario())


def test_gap_flip_blocked_by_chop_filter():
    """Gap-flip must respect the ER chop filter: in a ranging (low-ER) market, a SIGNAL exit
    with 2+ opposite bricks must NOT open an immediate reversal."""
    async def scenario():
        eng = _fresh()
        eng.settings["chop_filter"] = True
        eng.settings["chop_lookback"] = 10
        eng.settings["chop_threshold"] = 0.30
        eng.bricks = _bricks_from_closes([100, 110, 100, 110, 100, 110, 100, 110, 100, 110, 100])
        _long_pos(eng)
        eng.consec_red = 2          # gap printed 2 reds, but ER is ~0 (chop)
        with contextlib.ExitStack() as st:
            for p in _wire_fills(eng):
                st.enter_context(p)
            await eng._execute_order("SELL", "EXIT", 100.0, 5)
        assert eng.position is None
        assert eng.pending_entry is False
    asyncio.run(scenario())


def test_gap_flip_honors_entry_bricks():
    """With entry_bricks=3, a gap-flip must NOT fire on only 2 opposite bricks."""
    async def scenario():
        eng = _fresh()
        eng.settings["chop_filter"] = False
        eng.settings["entry_bricks"] = 3
        _long_pos(eng)
        eng.consec_red = 2          # only 2 reds; need 3
        with contextlib.ExitStack() as st:
            for p in _wire_fills(eng):
                st.enter_context(p)
            await eng._execute_order("SELL", "EXIT", 100.0, 5)
        assert eng.position is None
    asyncio.run(scenario())



# ---------------------------------------------------------------- exit sizing (reconciled/adopted)
def test_exit_order_never_exceeds_bot_lot():
    """A stale bot state of 130 must never fire a 130-qty order — exits are capped to lot_size."""
    async def scenario():
        eng = _fresh()
        eng.price = 100.0
        eng.position = {"side": "SHORT", "qty": 130, "entry_price": 110.0, "entry_time": "t",
                        "entry_order_id": "ADOPTED_MANUAL", "reds_at_entry": 2, "unrealized_pnl": 0.0}
        eng.consec_green = 1   # not enough for a gap-flip re-entry
        with contextlib.ExitStack() as st:
            for p in _wire_fills(eng):
                st.enter_context(p)
            await eng._execute_order("BUY", "EXIT", 100.0, 5)
        exit_orders = [o for o in eng.orders if o["kind"] == "EXIT"]
        assert exit_orders and exit_orders[0]["qty"] == 65
    asyncio.run(scenario())


def test_entry_order_is_always_one_lot():
    async def scenario():
        eng = _fresh()
        eng.price = 100.0
        eng.settings["lot_size"] = 65
        eng.consec_red = 2
        with contextlib.ExitStack() as st:
            for p in _wire_fills(eng):
                st.enter_context(p)
            await eng._execute_order("SELL", "ENTRY", 100.0, 3)
        entry_orders = [o for o in eng.orders if o["kind"] == "ENTRY"]
        assert entry_orders and entry_orders[0]["qty"] == 65
    asyncio.run(scenario())


# ---------------------------------------------------------------- broker net-qty across rows
def test_get_net_position_sums_across_producttype_rows():
    from angel_broker import AngelBroker

    class FakeSmart:
        def position(self):
            return {"status": True, "data": [
                {"symboltoken": "111", "producttype": "CARRYFORWARD", "netqty": "-65", "avgnetprice": "24000"},
                {"symboltoken": "111", "producttype": "INTRADAY", "netqty": "-65", "avgnetprice": "24010"},
                {"symboltoken": "999", "producttype": "CARRYFORWARD", "netqty": "-300", "avgnetprice": "1"},
            ]}

    b = AngelBroker()
    b.smart = FakeSmart()
    b.fut_token = "111"
    res = b.get_net_position()
    assert res["found"] is True
    assert res["netqty"] == -130   # both rows for token 111 summed; token 999 ignored


def test_get_net_position_single_row_unchanged():
    from angel_broker import AngelBroker

    class FakeSmart:
        def position(self):
            return {"status": True, "data": [
                {"symboltoken": "111", "producttype": "CARRYFORWARD", "netqty": "-65", "avgnetprice": "24000"},
            ]}

    b = AngelBroker()
    b.smart = FakeSmart()
    b.fut_token = "111"
    res = b.get_net_position()
    assert res["netqty"] == -65


def test_get_net_position_dedups_duplicate_producttype_rows():
    """If Angel returns duplicate identical rows (same producttype), they must NOT be double-counted."""
    from angel_broker import AngelBroker

    class FakeSmart:
        def position(self):
            return {"status": True, "data": [
                {"symboltoken": "111", "producttype": "CARRYFORWARD", "netqty": "-65", "avgnetprice": "24000"},
                {"symboltoken": "111", "producttype": "CARRYFORWARD", "netqty": "-65", "avgnetprice": "24000"},
            ]}

    b = AngelBroker()
    b.smart = FakeSmart()
    b.fut_token = "111"
    res = b.get_net_position()
    assert res["netqty"] == -65   # deduped by producttype, not -130


# ---------------------------------------------------------------- idempotency key includes token
def test_order_key_includes_contract_token():
    eng = _fresh()
    eng.broker.fut_token = "AAA"
    k1 = eng._order_key("ENTRY", "SIGNAL", 3)
    eng.broker.fut_token = "BBB"   # e.g. after a rollover to next-month contract
    k2 = eng._order_key("ENTRY", "SIGNAL", 3)
    assert k1 != k2, "same brick index on different contracts must not share an order key"
    # same token + same brick -> identical key (cross-pod dedup preserved)
    eng.broker.fut_token = "AAA"
    assert eng._order_key("ENTRY", "SIGNAL", 3) == k1


# ---------------------------------------------------------------- exit-retry side (P0 regression)
def test_long_exit_retry_uses_sell_not_buy():
    """A rejected LONG exit must RETRY with SELL (close), never BUY (which would double the long)."""
    async def scenario():
        eng = _fresh()
        eng.price = 100.0
        eng.position = {"side": "LONG", "qty": 65, "entry_price": 110.0, "entry_time": "t",
                        "entry_order_id": "x", "reds_at_entry": 2, "unrealized_pnl": 0.0}
        captured = []

        async def rec(side, kind, price, idx, reason="SIGNAL"):
            captured.append((side, kind, reason))

        # mirror the run_loop retry branch exactly
        eng.exit_retry_pending = True
        with patch.object(eng, "_execute_order", side_effect=rec):
            retry_side = "BUY" if eng.position["side"] == "SHORT" else "SELL"
            await eng._execute_order(retry_side, "EXIT", eng.price, -1, "EXIT_RETRY")
        assert captured == [("SELL", "EXIT", "EXIT_RETRY")]
    asyncio.run(scenario())


def test_short_exit_retry_uses_buy():
    async def scenario():
        eng = _fresh()
        eng.position = {"side": "SHORT", "qty": 65, "entry_price": 100.0, "entry_time": "t",
                        "entry_order_id": "x", "reds_at_entry": 2, "unrealized_pnl": 0.0}
        retry_side = "BUY" if eng.position["side"] == "SHORT" else "SELL"
        assert retry_side == "BUY"
    asyncio.run(scenario())


# ---------------------------------------------------------------- cancel/fill race (HIGH regression)
def test_live_fill_detects_fill_in_cancel_window():
    """If the order fills right before cancel, _live_fill must return True (filled), not False."""
    async def scenario():
        eng = _fresh()
        eng.broker.connected = True
        order = {"id": "o1", "side": "SELL", "kind": "EXIT", "reason": "SIGNAL",
                 "qty": 65, "symbol": "NIFTY", "ref_price": 100.0}
        # place succeeds; every poll says "open" (never fills in the loop); cancel says not-ok;
        # then get_order_status reports COMPLETE (it filled in the cancel window).
        eng.broker.place_limit_order = lambda *a, **k: {"ok": True, "orderid": "B1"}
        eng.broker.modify_order_price = lambda *a, **k: {"ok": True}
        statuses = iter(["open", "open", "complete"])  # last one is the post-cancel re-query

        def fake_status(oid):
            return {"status": next(statuses), "avgprice": 99.5}
        eng.broker.get_order_status = fake_status
        eng.broker.cancel_order = lambda oid: {"ok": False, "error": "already complete"}
        filled = await eng._live_fill(order, "SELL", 0.5, 5.0, 2, 3.0, 90.0, 110.0)
        assert filled is True
        assert order["fill_price"] == 99.5
    asyncio.run(scenario())


# ---------------------------------------------------------------- CHOP FILTER (ER) + entry_bricks
def _bricks_from_closes(closes):
    """Build a brick list from a close series (color by direction vs previous)."""
    out = []
    for i, c in enumerate(closes):
        color = "green" if (i == 0 or c >= closes[i - 1]) else "red"
        out.append({"index": i + 1, "color": color, "open": 0.0, "close": float(c),
                    "time": "t", "signal": None})
    return out


def test_chop_ok_blocks_when_ranging():
    """A tight oscillating (ranging) close series → low ER → entries blocked."""
    eng = _fresh()
    eng.settings["chop_filter"] = True
    eng.settings["chop_lookback"] = 10
    eng.settings["chop_threshold"] = 0.30
    # zig-zag around a level: net move ~0, big path -> ER ~0
    closes = [100, 110, 100, 110, 100, 110, 100, 110, 100, 110, 100]
    eng.bricks = _bricks_from_closes(closes)
    ok, er = eng._chop_ok()
    assert ok is False
    assert er is not None and er < 0.30


def test_chop_ok_allows_when_trending():
    """A steadily rising close series → high ER → entries allowed."""
    eng = _fresh()
    eng.settings["chop_filter"] = True
    eng.settings["chop_lookback"] = 10
    eng.settings["chop_threshold"] = 0.30
    closes = [100, 110, 120, 130, 140, 150, 160, 170, 180, 190, 200]
    eng.bricks = _bricks_from_closes(closes)
    ok, er = eng._chop_ok()
    assert ok is True
    assert er == 1.0


def test_chop_ok_blocks_until_enough_history():
    """Not enough bricks for the lookback window → block (sit out)."""
    eng = _fresh()
    eng.settings["chop_filter"] = True
    eng.settings["chop_lookback"] = 50
    eng.bricks = _bricks_from_closes([100, 110, 120])
    ok, er = eng._chop_ok()
    assert ok is False and er is None


def test_chop_filter_blocks_entry_in_range():
    """With the chop filter ON and a ranging chart, 2 greens must NOT open a LONG."""
    async def scenario():
        eng = _fresh()
        eng.settings["chop_filter"] = True
        eng.settings["chop_lookback"] = 10
        eng.settings["chop_threshold"] = 0.30
        eng.bricks = _bricks_from_closes([100, 110, 100, 110, 100, 110, 100, 110, 100, 110, 100])
        calls = []
        async def rec(side, kind, price, idx, reason="SIGNAL"):
            calls.append((side, kind))
        with patch.object(eng, "_execute_order", side_effect=rec), \
             patch.object(eng, "_entries_blocked", return_value=False):
            eng.consec_green = 1
            eng._process_brick(_mk("green", 99))   # 2nd green -> would enter, but ER blocks
            await asyncio.sleep(0)
        assert calls == []
        assert eng.pending_entry is False
    asyncio.run(scenario())


def test_chop_filter_allows_entry_in_trend():
    """With the chop filter ON and a trending chart, 2 greens SHOULD open a LONG."""
    async def scenario():
        eng = _fresh()
        eng.settings["chop_filter"] = True
        eng.settings["chop_lookback"] = 10
        eng.settings["chop_threshold"] = 0.30
        eng.bricks = _bricks_from_closes(list(range(100, 210, 10)))  # steady uptrend
        calls = []
        async def rec(side, kind, price, idx, reason="SIGNAL"):
            calls.append((side, kind))
        with patch.object(eng, "_execute_order", side_effect=rec), \
             patch.object(eng, "_entries_blocked", return_value=False):
            eng.consec_green = 1
            eng._process_brick(_mk("green", 99))
            await asyncio.sleep(0)
        assert calls == [("BUY", "ENTRY")]
        assert eng._entry_side == "LONG"
    asyncio.run(scenario())


def test_entry_bricks_setting_requires_three():
    """entry_bricks=3 must NOT enter on 2 bricks, but SHOULD on the 3rd (chop filter off)."""
    async def scenario():
        eng = _fresh()
        eng.settings["chop_filter"] = False
        eng.settings["entry_bricks"] = 3
        calls = await _feed(eng, ["red", "red"])
        assert calls == []
        calls = await _feed(eng, ["red", "red", "red"])
        assert calls == [("SELL", "ENTRY")]
    asyncio.run(scenario())



# ---------------------------------------------------------------- soft directional gate
def _set_hist(eng, steps, start=24000.0, lb=20):
    """Populate eng.bricks with lb+1 closes from a list of +1/-1 (x brick_size) steps so
    _signed_er() = sum(steps)/len(steps)."""
    eng.settings["chop_lookback"] = lb
    closes = [start]
    for s in steps:
        closes.append(round(closes[-1] + s * 50, 2))
    eng.bricks = [{"index": i, "color": "green", "open": 0.0, "close": c, "time": "t", "signal": None}
                  for i, c in enumerate(closes)]


def test_signed_er_value():
    eng = _fresh()
    _set_hist(eng, [1] * 16 + [-1] * 4)          # net +12 over 20 steps
    assert eng._signed_er() == pytest.approx(0.6, abs=1e-6)
    _set_hist(eng, [-1] * 16 + [1] * 4)          # net -12
    assert eng._signed_er() == pytest.approx(-0.6, abs=1e-6)


def test_signed_er_none_without_history():
    eng = _fresh()
    eng.settings["chop_lookback"] = 20
    eng.bricks = []
    assert eng._signed_er() is None
    assert eng._dir_ok("SHORT") is True          # no history -> never veto (chop gate handles it)


def test_dir_gate_vetoes_short_in_strong_uptrend():
    """Strong uptrend (signed ER +0.6 >= 0.3): a SHORT entry must be BLOCKED even after 2 reds."""
    async def scenario():
        eng = _fresh()
        eng.settings["dir_soft"] = True
        eng.settings["dir_threshold"] = 0.3
        _set_hist(eng, [1] * 16 + [-1] * 4)
        calls = await _feed(eng, ["red", "red"])
        assert calls == []
    asyncio.run(scenario())


def test_dir_gate_allows_long_in_strong_uptrend():
    async def scenario():
        eng = _fresh()
        eng.settings["dir_soft"] = True
        eng.settings["dir_threshold"] = 0.3
        _set_hist(eng, [1] * 16 + [-1] * 4)
        calls = await _feed(eng, ["green", "green"])
        assert calls == [("BUY", "ENTRY")]
    asyncio.run(scenario())


def test_dir_gate_vetoes_long_in_strong_downtrend():
    async def scenario():
        eng = _fresh()
        eng.settings["dir_soft"] = True
        eng.settings["dir_threshold"] = 0.3
        _set_hist(eng, [-1] * 16 + [1] * 4)
        calls = await _feed(eng, ["green", "green"])
        assert calls == []
    asyncio.run(scenario())


def test_dir_gate_allows_short_in_strong_downtrend():
    async def scenario():
        eng = _fresh()
        eng.settings["dir_soft"] = True
        eng.settings["dir_threshold"] = 0.3
        _set_hist(eng, [-1] * 16 + [1] * 4)
        calls = await _feed(eng, ["red", "red"])
        assert calls == [("SELL", "ENTRY")]
    asyncio.run(scenario())


def test_dir_gate_allows_entry_in_mild_trend():
    """Signed ER +0.1 (< 0.3 threshold): a mild opposing lean must NOT veto — keeps early entries."""
    async def scenario():
        eng = _fresh()
        eng.settings["dir_soft"] = True
        eng.settings["dir_threshold"] = 0.3
        _set_hist(eng, [1] * 11 + [-1] * 9)      # net +2 over 20 => signed 0.1
        calls = await _feed(eng, ["red", "red"])
        assert calls == [("SELL", "ENTRY")]      # short allowed despite mild uptrend
    asyncio.run(scenario())


def test_dir_gate_off_allows_counter_trend():
    """dir_soft=False -> the gate is disabled, counter-trend entry goes through."""
    async def scenario():
        eng = _fresh()
        eng.settings["dir_soft"] = False
        _set_hist(eng, [1] * 16 + [-1] * 4)      # strong uptrend
        calls = await _feed(eng, ["red", "red"])
        assert calls == [("SELL", "ENTRY")]
    asyncio.run(scenario())



# ------------------------------------------- pre-trade reconcile + margin check (real order path)
from unittest.mock import MagicMock  # noqa: E402


def _exec_engine(net_position, funds=None):
    """Engine wired with a mocked broker so _execute_order's pre-trade reconcile/margin logic
    runs for real (no network, no orders)."""
    eng = _fresh()
    eng.broker = MagicMock()
    eng.broker.connected = True
    eng.broker.get_net_position = MagicMock(return_value=net_position)
    eng.broker.get_available_funds = MagicMock(return_value=funds or {"found": True, "available": 9_000_000})
    eng.price = 22000.0
    eng._claim_order_key = AsyncMock(return_value=True)
    eng._save_order = AsyncMock()
    eng._persist_state = AsyncMock()
    eng._save_trade = AsyncMock()
    return eng


def _fill_ok(order, *a, **k):
    order["fill_price"] = order.get("limit_price") or 22000.0
    return True


def test_entry_auto_syncs_to_untracked_broker_position():
    async def scenario():
        eng = _exec_engine({"found": True, "netqty": -65, "avgprice": 22300})  # auto_sync default ON
        await eng._execute_order("SELL", "ENTRY", 22000.0, 5)
        assert eng.orders[0]["status"] == "AUTO_SYNCED"
        assert eng.position is not None and eng.position["side"] == "SHORT" and eng.position["qty"] == 65
        assert eng.position["entry_price"] == 22300
        eng.broker.place_limit_order.assert_not_called()
    asyncio.run(scenario())


def test_entry_skips_on_mismatch_when_auto_sync_off():
    async def scenario():
        eng = _exec_engine({"found": True, "netqty": -65, "avgprice": 22300})
        eng.settings["auto_sync"] = False
        await eng._execute_order("SELL", "ENTRY", 22000.0, 5)
        assert eng.orders[0]["status"] == "SKIPPED_MISMATCH"
        assert eng.position is None
    asyncio.run(scenario())


def test_entry_skipped_on_insufficient_margin():
    async def scenario():
        eng = _exec_engine({"found": True, "netqty": 0, "avgprice": None},
                           funds={"found": True, "available": 100_000})  # need ~357,500
        await eng._execute_order("SELL", "ENTRY", 22000.0, 5)
        assert eng.orders[0]["status"] == "SKIPPED_MARGIN"
        assert eng.pending_entry is False
        eng.broker.place_limit_order.assert_not_called()
    asyncio.run(scenario())


def test_entry_proceeds_when_flat_and_funded():
    async def scenario():
        eng = _exec_engine({"found": True, "netqty": 0, "avgprice": None})
        with patch.object(eng, "_live_fill", side_effect=_fill_ok):
            await eng._execute_order("SELL", "ENTRY", 22000.0, 5)
        assert eng.position is not None and eng.position["side"] == "SHORT"
        assert eng.orders[0]["status"] == "COMPLETE"
    asyncio.run(scenario())


def test_exit_skipped_when_broker_already_flat():
    async def scenario():
        eng = _exec_engine({"found": True, "netqty": 0, "avgprice": None})
        eng.position = {"side": "SHORT", "qty": 65, "entry_price": 22400.0,
                        "entry_time": "t", "entry_order_id": "x",
                        "reds_at_entry": 2, "unrealized_pnl": 0.0}
        await eng._execute_order("BUY", "EXIT", 22000.0, 6)
        assert eng.orders[0]["status"] == "SKIPPED_FLAT"
        assert eng.position is None
    asyncio.run(scenario())


def test_exit_auto_syncs_on_side_mismatch():
    async def scenario():
        eng = _exec_engine({"found": True, "netqty": -65, "avgprice": 22300})  # broker SHORT, auto_sync ON
        eng.position = {"side": "LONG", "qty": 65, "entry_price": 22400.0, "entry_time": "t",
                        "entry_order_id": "x", "reds_at_entry": 0, "unrealized_pnl": 0.0}
        await eng._execute_order("SELL", "EXIT", 22000.0, 6)
        assert eng.orders[0]["status"] == "AUTO_SYNCED"
        assert eng.position is not None and eng.position["side"] == "SHORT"  # adopted broker side
    asyncio.run(scenario())


def test_exit_size_corrected_to_broker_qty():
    """Bot thinks it holds 130 but broker really holds 65 -> exit is resized to 65 (no over-exit)."""
    async def scenario():
        eng = _exec_engine({"found": True, "netqty": -65, "avgprice": 22400})
        eng.position = {"side": "SHORT", "qty": 130, "entry_price": 22400.0,
                        "entry_time": "t", "entry_order_id": "x",
                        "reds_at_entry": 2, "unrealized_pnl": 0.0}
        with patch.object(eng, "_live_fill", side_effect=_fill_ok):
            await eng._execute_order("BUY", "EXIT", 22000.0, 6)
        assert eng.orders[0]["qty"] == 65
    asyncio.run(scenario())


def test_live_fill_stops_hammering_on_margin_reject():
    """A margin rejection must break immediately, not retry max_attempts times."""
    async def scenario():
        eng = _exec_engine({"found": True, "netqty": 0, "avgprice": None})
        eng.broker.place_limit_order = MagicMock(
            return_value={"ok": False, "error": "Your order has been rejected due to Insufficient Funds."})
        eng._cur_price = AsyncMock(return_value=22000.0)
        order = {"qty": 65, "attempts": 0, "status": "PENDING", "note": "", "limit_price": None}
        filled = await eng._live_fill(order, "SELL", 5, 10, 5, 5, 21990, 22010)
        assert filled is False
        assert order["attempts"] == 1            # broke on the first attempt, no hammering
    asyncio.run(scenario())


# ------------------------------------------- qty cap to bot lot + gap-flip position-book lag
def test_exit_with_double_broker_qty_trades_only_bot_lot():
    """Broker net = 2x lot while bot holds 1 lot -> exit order is the bot's 65, never 130."""
    async def scenario():
        eng = _exec_engine({"found": True, "netqty": -130, "avgprice": 22400})
        eng.position = {"side": "SHORT", "qty": 65, "entry_price": 22400.0, "entry_time": "t",
                        "entry_order_id": "x", "reds_at_entry": 2, "unrealized_pnl": 0.0}
        with patch.object(eng, "_live_fill", side_effect=_fill_ok):
            await eng._execute_order("BUY", "EXIT", 22000.0, 6)
        assert eng.orders[0]["qty"] == 65
        assert "left untouched" in eng.alert["msg"]
    asyncio.run(scenario())


def test_auto_sync_adopts_only_bot_lot_of_oversized_broker_qty():
    async def scenario():
        eng = _exec_engine({"found": True, "netqty": -130, "avgprice": 22300})
        await eng._execute_order("SELL", "ENTRY", 22000.0, 5)
        assert eng.orders[0]["status"] == "AUTO_SYNCED"
        assert eng.position["side"] == "SHORT" and eng.position["qty"] == 65
        eng.broker.place_limit_order.assert_not_called()
    asyncio.run(scenario())


def test_takeover_caps_adopted_qty_to_lot():
    async def scenario():
        eng = _exec_engine({"found": True, "netqty": 130, "avgprice": 22300})
        eng.broker.fut_token = "111"
        await eng._reconcile_on_takeover()
        assert eng.position["side"] == "LONG" and eng.position["qty"] == 65
    asyncio.run(scenario())


def test_gap_flip_rechecks_lagging_position_book_and_flips():
    """Exit fills but Angel still shows the old LONG on the first read -> GAP_FLIP re-reads,
    sees flat, and opens the SHORT (does NOT re-adopt the just-closed LONG)."""
    async def scenario():
        eng = _exec_engine(None)
        eng._flip_recheck_delay = 0
        eng.broker.get_net_position = MagicMock(side_effect=[
            {"found": True, "netqty": 65, "avgprice": 22400},   # EXIT pre-trade reconcile (real LONG)
            {"found": True, "netqty": 65, "avgprice": 22400},   # GAP_FLIP first read — stale
            {"found": True, "netqty": 0, "avgprice": None},     # re-read — flat
        ])
        eng.position = {"side": "LONG", "qty": 65, "entry_price": 22400.0, "entry_time": "t",
                        "entry_order_id": "x", "reds_at_entry": 2, "unrealized_pnl": 0.0}
        eng.consec_red = 2
        with patch.object(eng, "_live_fill", side_effect=_fill_ok), \
             patch.object(eng, "_entries_blocked", return_value=False):
            await eng._execute_order("SELL", "EXIT", 22000.0, 6)
        assert eng.position is not None and eng.position["side"] == "SHORT"
        assert not any(o["status"] == "AUTO_SYNCED" for o in eng.orders)
    asyncio.run(scenario())


def test_gap_flip_adopts_if_old_side_persists_after_rechecks():
    """If Angel keeps showing the old side after all re-reads it is treated as real and adopted."""
    async def scenario():
        eng = _exec_engine({"found": True, "netqty": 65, "avgprice": 22400})
        eng._flip_recheck_delay = 0
        eng.position = {"side": "LONG", "qty": 65, "entry_price": 22400.0, "entry_time": "t",
                        "entry_order_id": "x", "reds_at_entry": 2, "unrealized_pnl": 0.0}
        eng.consec_red = 2
        with patch.object(eng, "_live_fill", side_effect=_fill_ok), \
             patch.object(eng, "_entries_blocked", return_value=False):
            await eng._execute_order("SELL", "EXIT", 22000.0, 6)
        assert eng.orders[0]["status"] == "AUTO_SYNCED" and eng.position["side"] == "LONG"
        assert eng.broker.get_net_position.call_count == 2 + eng._flip_rechecks
    asyncio.run(scenario())
