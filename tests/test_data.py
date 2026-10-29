import numpy as np
import pandas as pd
import pytest

from bot.data import (OHLCV_COLS, TF_MS, DataStore, check_candles, paginate,
                      resample, to_frame)

STEP = TF_MS["15m"]


def make_rows(n, start=0):
    rows = []
    for i in range(n):
        p = 100 + i
        rows.append([start + i * STEP, p, p + 2, p - 1, p + 1, 10.0])
    return rows


def fake_fetch(all_rows, calls=None):
    def f(since, limit):
        if calls is not None:
            calls.append(since)
        return [r for r in all_rows if r[0] >= since][:limit]
    return f


def test_paginate_collects_everything_in_pages():
    rows = make_rows(250)
    calls = []
    got = paginate(fake_fetch(rows, calls), 0, 250 * STEP, STEP, limit=100)
    assert [r[0] for r in got] == [r[0] for r in rows]
    assert len(calls) == 3


def test_paginate_respects_until():
    got = paginate(fake_fetch(make_rows(250)), 0, 50 * STEP, STEP, limit=100)
    assert len(got) == 50


def test_paginate_retries_then_raises(monkeypatch):
    monkeypatch.setattr("bot.data.time.sleep", lambda s: None)
    n = {"c": 0}

    def flaky(since, limit):
        n["c"] += 1
        if n["c"] < 3:
            raise RuntimeError("boom")
        return make_rows(5)
    assert len(paginate(flaky, 0, 5 * STEP, STEP)) == 5

    def dead(since, limit):
        raise RuntimeError("down")
    with pytest.raises(RuntimeError):
        paginate(dead, 0, STEP, STEP, max_retries=2)


def test_paginate_stops_on_no_progress():
    stuck = [[0, 1, 1, 1, 1, 1]]
    assert len(paginate(lambda s, l: stuck, 0, 10 * STEP, STEP)) <= 10


def test_resample_ohlc_correct_and_drops_partial():
    df = to_frame(make_rows(4 * 4 + 2), OHLCV_COLS)   # 4 full hours + 2 extra candles
    h = resample(df, "1h")
    assert len(h) == 4
    first = h.iloc[0]
    assert first.open == 100 and first.close == 104
    assert first.high == 105 and first.low == 99 and first.volume == 40
    h4 = resample(df, "4h")
    assert len(h4) == 1 and h4.iloc[0].close == 116


def test_resample_drops_bucket_with_gap():
    rows = make_rows(8)
    del rows[2]
    h = resample(to_frame(rows, OHLCV_COLS), "1h")
    assert len(h) == 1 and h.iloc[0].ts == 3_600_000


def test_check_candles_flags_problems():
    rows = make_rows(10)
    del rows[5]
    rows[2][2] = 0.5  # high below close
    q = check_candles(to_frame(rows, OHLCV_COLS), STEP)
    assert q["gaps"] == 1 and q["bad_ohlc"] == 1


def test_store_roundtrip_and_resume(tmp_path):
    s = DataStore(tmp_path)
    rows = make_rows(100)
    s.update_candles("BTC/USDT:USDT", fake_fetch(rows), 0, 60 * STEP)
    assert len(s.load("BTC/USDT:USDT", "15m")) == 60
    calls = []
    s.update_candles("BTC/USDT:USDT", fake_fetch(rows, calls), 0, 100 * STEP)
    df = s.load("BTC/USDT:USDT", "15m")
    assert len(df) == 100 and df.ts.is_unique and calls[0] == 60 * STEP
    assert len(s.load_tf("BTC/USDT:USDT", "1h")) == 25


def test_missing_data_message(tmp_path):
    with pytest.raises(FileNotFoundError, match="download"):
        DataStore(tmp_path).load("X", "15m")
    assert DataStore(tmp_path).load_funding("X") is None


def test_funding_roundtrip(tmp_path):
    s = DataStore(tmp_path)
    rows = [[i * 8 * 3_600_000, 0.0001] for i in range(10)]
    s.update_funding("ETH/USDT:USDT", fake_fetch(rows), 0, 10 ** 12)
    assert len(s.load_funding("ETH/USDT:USDT")) == 10
