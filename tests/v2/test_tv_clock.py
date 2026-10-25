"""The browser's playback clock (static/tv/clock.js), run with Node, and the server's estimate of where a playing session is now."""
import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from config import BASE_DIR
from synchub.tv.jellyfin import estimated_ticks, summarize_session

node = pytest.mark.skipif(shutil.which("node") is None, reason="Node is not installed")


def run(script):
    """Runs a script with `Clock` loaded, and returns what it prints as JSON."""
    code = f"const Clock = require({json.dumps(str(BASE_DIR / 'static/tv/clock.js'))}); const out = {{}}; {script}; console.log(JSON.stringify(out));"
    return json.loads(subprocess.run(["node", "-e", code], capture_output=True, text=True, check=True).stdout)


@node
def test_the_clock_counts_forward_while_playing_and_stands_still_while_paused():
    out = run("Clock.sync('a', 10, true, 100, 0); out.playing = Clock.position('a', 3);"
              "Clock.sync('b', 10, false, 100, 0); out.paused = Clock.position('b', 3)")
    assert out == {"playing": 13, "paused": 10}


@node
def test_a_small_gap_is_closed_gradually_and_a_big_one_snaps():
    out = run("Clock.sync('a', 10, true, 100, 0);"
              "out.small = Clock.sync('a', 15, true, 100, 3).pos;"          # we were at 13, the server says 15: move part of the way, never backwards
              "out.big = Clock.sync('a', 60, true, 100, 4).pos;"            # a seek
              "out.state = Clock.sync('a', 61, false, 100, 5)")
    assert 13 < out["small"] < 15 and abs(out["small"] - (13 + 2 * 0.35)) < 1e-9
    assert out["big"] == 60 and out["state"] == {"pos": 61, "playing": False}


@node
def test_a_report_a_second_or_so_behind_never_steps_the_timer_back():
    out = run("Clock.sync('a', 10, true, 100, 0);"
              "out.late = Clock.sync('a', 12.5, true, 100, 3).pos;"          # we are at 13; the server's 12.5 is just a late report
              "out.later = Clock.position('a', 4);"
              "out.behind = Clock.sync('a', 12, true, 100, 4).pos")          # now we are at 14, 2 s ahead: that is a real difference, closed gradually
    assert out["late"] == 13 and out["later"] == 14 and 12 < out["behind"] < 14


@node
def test_a_pause_made_here_shows_at_once_and_a_stale_report_does_not_undo_it():
    out = run("Clock.sync('a', 10, true, 100, 0); Clock.setPlaying('a', false, 10);"
              "out.frozen = Clock.position('a', 12);"                        # paused at 20 (10 + 10), still 20 two seconds later
              "out.stale = Clock.sync('a', 19, true, 100, 11);"              # the TV has not reported the pause yet
              "out.agreed = Clock.sync('a', 20, false, 100, 12);"            # now it has
              "out.after = Clock.position('a', 14)")
    assert out["frozen"] == 20 and out["stale"] == {"pos": 20, "playing": False} and out["agreed"]["playing"] is False and out["after"] == pytest.approx(20, abs=1)


@node
def test_a_local_change_gives_way_to_the_server_when_the_hold_runs_out():
    out = run("Clock.sync('a', 10, true, 100, 0); Clock.setPlaying('a', false, 1);"
              f"out.late = Clock.sync('a', 12, true, 100, 1 + {6} + 1)")
    assert out["late"]["playing"] is True


@node
def test_the_thirty_second_buttons_move_at_once_and_old_positions_are_ignored_meanwhile():
    out = run("Clock.sync('a', 50, false, 100, 0); Clock.shift('a', 30, 1);"
              "out.now = Clock.position('a', 1);"
              "out.stale = Clock.sync('a', 50, false, 100, 2).pos;"          # the TV still reports the old position
              "out.confirmed = Clock.sync('a', 80, false, 100, 3).pos")
    assert out["now"] == 80 and out["stale"] == 80 and out["confirmed"] == 80


@node
def test_the_position_stays_between_zero_and_the_length():
    out = run("Clock.sync('a', 95, true, 100, 0); out.end = Clock.position('a', 60);"
              "Clock.sync('b', 5, false, 100, 0); Clock.shift('b', -30, 0); out.start = Clock.position('b', 0);"
              "Clock.sync('c', 7, true, 0, 0); out.unknown = Clock.position('c', 3)")
    assert out == {"end": 100, "start": 0, "unknown": 10}                    # no length known: it just keeps counting


# ---------------------------------------------------------------- the server's estimate
NOW = datetime(2026, 10, 25, 12, 0, 30, tzinfo=timezone.utc)


def session(**over):
    base = {"Id": "s1", "NowPlayingItem": {"Id": "m1", "Name": "Heat", "RunTimeTicks": 7200 * 10_000_000},
            "PlayState": {"PositionTicks": 600 * 10_000_000, "IsPaused": False}, "LastPlaybackCheckIn": "2026-10-25T12:00:20.1234567Z"}
    base.update(over)
    return base


def test_a_playing_sessions_position_is_what_it_reported_plus_the_time_since():
    assert estimated_ticks(session(), NOW) == pytest.approx((600 + 10) * 10_000_000, abs=2_000_000)


def test_the_estimate_is_left_out_when_it_would_be_a_guess():
    assert estimated_ticks(session(PlayState={"PositionTicks": 1, "IsPaused": True}), NOW) is None      # paused
    assert estimated_ticks(session(LastPlaybackCheckIn=None), NOW) is None                              # the app did not say when
    assert estimated_ticks(session(LastPlaybackCheckIn="garbage"), NOW) is None
    assert estimated_ticks(session(NowPlayingItem=None), NOW) is None


def test_an_old_report_is_trusted_for_at_most_thirty_seconds_and_never_past_the_end():
    old = (NOW - timedelta(minutes=10)).isoformat()
    assert estimated_ticks(session(LastPlaybackCheckIn=old), NOW) == (600 + 30) * 10_000_000
    near_end = session(PlayState={"PositionTicks": 7195 * 10_000_000, "IsPaused": False})
    assert estimated_ticks(near_end, NOW) == 7200 * 10_000_000


def test_the_summary_carries_the_estimate_beside_the_reported_position():
    summary = summarize_session(session(), NOW)
    assert summary["PlayState"]["PositionTicks"] == 600 * 10_000_000 and summary["PlayState"]["EstimatedTicks"] > summary["PlayState"]["PositionTicks"]
    assert "EstimatedTicks" not in summarize_session(session(LastPlaybackCheckIn=None), NOW)["PlayState"]
