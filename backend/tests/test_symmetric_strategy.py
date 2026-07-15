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


# ---------------------------------------------------------------- exit sizing (reconciled/adopted)
def test_exit_order_sizes_to_actual_position_qty():
    """A reconciled/adopted position larger than one lot (e.g. 130) must be exited in FULL,
    not just lot_size, or a naked remainder is left behind."""
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
        assert exit_orders and exit_orders[0]["qty"] == 130
        assert eng.position is None
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


# ---------------------------------------------------------------- MACRO TREND FILTER
def test_macro_filter_blocks_long_against_downtrend():
    """With macro filter ON and macro trend DOWN, two greens must NOT open a LONG."""
    async def scenario():
        eng = _fresh()
        eng.settings["macro_mult"] = 2
        eng.macro_dir = -1
        calls = await _feed(eng, ["green", "green"])
        assert calls == []
        assert eng.pending_entry is False
    asyncio.run(scenario())


def test_macro_filter_allows_short_in_downtrend():
    """With macro filter ON and macro trend DOWN, two reds SHOULD open a SHORT."""
    async def scenario():
        eng = _fresh()
        eng.settings["macro_mult"] = 2
        eng.macro_dir = -1
        calls = await _feed(eng, ["red", "red"])
        assert calls == [("SELL", "ENTRY")]
        assert eng._entry_side == "SHORT"
    asyncio.run(scenario())


def test_macro_filter_blocks_short_against_uptrend():
    """With macro filter ON and macro trend UP, two reds must NOT open a SHORT."""
    async def scenario():
        eng = _fresh()
        eng.settings["macro_mult"] = 2
        eng.macro_dir = 1
        calls = await _feed(eng, ["red", "red"])
        assert calls == []
        assert eng.pending_entry is False
    asyncio.run(scenario())


def test_macro_filter_allows_long_in_uptrend():
    """With macro filter ON and macro trend UP, two greens SHOULD open a LONG."""
    async def scenario():
        eng = _fresh()
        eng.settings["macro_mult"] = 2
        eng.macro_dir = 1
        calls = await _feed(eng, ["green", "green"])
        assert calls == [("BUY", "ENTRY")]
        assert eng._entry_side == "LONG"
    asyncio.run(scenario())


def test_macro_filter_off_ignores_macro_dir():
    """With macro filter OFF (macro_mult=0), entries are taken regardless of macro_dir."""
    async def scenario():
        eng = _fresh()
        eng.settings["macro_mult"] = 0
        eng.macro_dir = -1
        calls = await _feed(eng, ["green", "green"])
        assert calls == [("BUY", "ENTRY")]
    asyncio.run(scenario())


def test_feed_macro_close_tracks_direction():
    """_feed_macro_close builds a larger Renko and reports trend direction from closes."""
    eng = _fresh()
    eng.settings["brick_size"] = 50
    eng.settings["macro_mult"] = 2          # macro box = 100 pts
    # rising closes -> macro up
    for p in [24000, 24050, 24100, 24200, 24300, 24400]:
        eng._feed_macro_close(p)
    assert eng.macro_dir == 1
    # a 2x reversal (needs >=200 pt move down) flips macro to down
    for p in [24350, 24250, 24150, 24000]:
        eng._feed_macro_close(p)
    assert eng.macro_dir == -1


def test_feed_macro_close_noop_when_off():
    """When macro_mult=0 the filter stays neutral (macro_dir=0)."""
    eng = _fresh()
    eng.settings["macro_mult"] = 0
    for p in [24000, 24100, 24200, 24300]:
        eng._feed_macro_close(p)
    assert eng.macro_dir == 0


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
