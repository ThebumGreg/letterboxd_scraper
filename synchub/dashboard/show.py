"""Putting the dashboard on the TV at the start of the morning window and taking it off at the end.

A job runs every minute (tick). State for the day is kept in the `dash_state` setting so a restart in the middle of the morning does not open the page
again after someone closed it: {"day", "shown", "hidden", "tries", "was_standby"}.
"""
import json
import logging
import time
from datetime import datetime, timedelta

from synchub import settings
from synchub.dashboard import config, school
from synchub.tv import orchestrator
from synchub.tv.devices import DeviceError

logger = logging.getLogger(__name__)
MAX_TRIES = 3
RETRY_SECONDS = 90
HOLD_MINUTES = 30


class ShowError(Exception):
    pass


MODES = ("morning", "evening")
_KEYS = {"morning": "dash_state", "evening": "dash_state_evening"}
_last_try = {"morning": 0.0, "evening": 0.0}


def _state(day, mode="morning"):
    try:
        state = json.loads(settings.get(_KEYS[mode]) or "{}")
    except ValueError:
        state = {}
    return state if state.get("day") == day.isoformat() else {"day": day.isoformat(), "shown": False, "hidden": False, "tries": 0, "was_standby": False}


def _save(state, mode="morning"):
    settings.set_value(_KEYS[mode], json.dumps(state))


def page_url(mode="morning"):
    base = settings.get("synchub_url").strip().rstrip("/")
    if not base:
        raise ShowError("Set the SyncHub address (Settings, Connections) so the TV knows where to open the page.")
    return f"{base}/dash/{config.token()}" + ("?mode=evening" if mode == "evening" else "")


def _runtime_device():
    from synchub.tv import runtime
    r = runtime.get()
    dev_id = settings.get("dash_tv")
    if not dev_id:
        raise ShowError("Choose the TV for the dashboard in Settings.")
    try:
        return r, r.device(dev_id)
    except DeviceError:
        raise ShowError("The TV chosen for the dashboard no longer exists. Choose it again in Settings.")


def busy(r, dev, package):
    """Why the TV should be left alone, or ''. Something playing in Jellyfin, a camera on screen, or another app in front."""
    if r.camera.get(dev.id):
        return "a camera is showing"
    try:
        jf = r.jf()
        playing = next((s for s in orchestrator.device_sessions(jf, dev) if s.get("NowPlayingItem") and not (s.get("PlayState") or {}).get("IsPaused")), None)
    except Exception:
        playing = None
    if playing:
        return f'"{playing["NowPlayingItem"].get("Name")}" is playing'
    try:
        status = dev.status(quiet=True) or {}
    except DeviceError:
        return ""
    app = status.get("app")
    from synchub.tv.devices import androidtv, googletv
    resting = androidtv.DREAM_APPS | getattr(googletv, "LAUNCHERS", set())
    if status.get("power") == "on" and app and app not in resting and app != package:
        return f"{status.get('appName') or app} is open"
    return ""


def start(manual=False, now=None, mode="morning"):
    """Wakes the TV if needed and opens the page (the `mode` screen: morning, or the evening one that shows tomorrow). -> a list of steps taken. Raises ShowError when it cannot."""
    r, dev = _runtime_device()
    package = settings.get("dash_app")
    url = page_url(mode)
    steps = []

    def step(message, ok=True):
        steps.append(message)
        r.logbook.add("system", dev.id, "dashboard", response=message, ok=ok)

    if "stream_view" not in dev.capabilities:
        raise ShowError(f"{dev.name} cannot open a page yet. Use a Google TV or Android TV.")
    if not manual and settings.get("dash_skip_busy") == "1":
        why = busy(r, dev, package)
        if why:
            step(f"Left {dev.name} alone: {why}")
            state = _state((now or config.now()).date(), mode)
            state["hidden"] = True                    # not shown later in the window either: the person chose something else
            _save(state, mode)
            return steps
    before = dev.status(quiet=True) or {}
    standby = before.get("power") in ("standby", None)
    if not orchestrator.ensure_on(dev, step):
        raise ShowError(f"{dev.name} would not wake up (see the TV log).")
    step(f"Opening the dashboard in {package}: {dev.view_stream(url, package)}")
    state = _state((now or config.now()).date(), mode)
    state.update(shown=True, hidden=False, was_standby=standby)
    # "Show on the TV now" outside the window stays up for a while: the minute job would otherwise close it at once for being outside the window
    state["hold_until"] = ((now or config.now()) + timedelta(minutes=HOLD_MINUTES)).isoformat() if manual else None
    _save(state, mode)
    return steps


def stop(manual=False, now=None, mode="morning"):
    """Closes the page, and puts the TV back to sleep if it was asleep when the dashboard woke it. -> steps."""
    r, dev = _runtime_device()
    package = settings.get("dash_app")
    state = _state((now or config.now()).date(), mode)
    steps = []

    def step(message, ok=True):
        steps.append(message)
        r.logbook.add("system", dev.id, "dashboard", response=message, ok=ok)

    try:
        app = (dev.status(quiet=True) or {}).get("app")
    except DeviceError:
        app = None
    if app == package or manual:
        step(f"Closed the dashboard: {dev.close_stream(package)}")
        if state.get("was_standby") and settings.get("dash_sleep_after") == "1":
            time.sleep(2)
            dev.power(False)
            step("Put the TV back to sleep")
    else:
        step(f"The dashboard is not on screen ({app or 'TV off'}), so {dev.name} was left as it is")
    state.update(shown=False, hidden=manual or state.get("hidden", False))
    _save(state, mode)
    return steps


def window_of(mode):
    return config.window() if mode == "morning" else config.eve_window()


def active(mode):
    if mode == "morning":
        return config.enabled()
    return settings.get("dash_eve_on") == "1"


def in_window(now, mode="morning"):
    start_m, end_m = window_of(mode)
    minute = now.hour * 60 + now.minute
    chosen = config.days() if mode == "morning" else config.eve_days()
    return config.DAYS[now.weekday()] in chosen and start_m <= minute < end_m


def current_mode(now):
    """The screen the TV page should show at this time: the evening one inside its window, else the morning one."""
    return "evening" if active("evening") and in_window(now, "evening") else "morning"


def shown_now(day):
    """True when either screen is open on the TV (as far as SyncHub knows)."""
    return any(_state(day, m)["shown"] for m in MODES)


def tick(now=None):
    """The minute job: open each screen at the start of its window, close it at the end."""
    if not settings.get("dash_tv"):
        return
    now = now or config.now()
    for mode in MODES:
        if active(mode):
            _tick_window(now, mode)
        elif _state(now.date(), mode)["shown"] and not in_window(now, mode):
            _tick_window(now, mode)                       # switched off while it was open: still take it down


def _tick_window(now, mode):
    state = _state(now.date(), mode)
    label = "morning" if mode == "morning" else "evening"
    try:
        if active(mode) and in_window(now, mode):
            if mode == "morning" and state["shown"] and settings.get("dash_close_when_left") == "1" and school.all_left(now):
                logger.info("[dashboard] everyone has left for school: closing the morning dashboard")
                stop(now=now)
                state = _state(now.date())
                state["hidden"] = True                    # and it stays closed for the rest of the morning
                _save(state)
                return
            if state["shown"] or state["hidden"] or state["tries"] >= MAX_TRIES or time.time() - _last_try[mode] < RETRY_SECONDS:
                return
            _last_try[mode] = time.time()
            state["tries"] += 1
            _save(state, mode)
            logger.info(f"[dashboard] opening the {label} dashboard")
            start(now=now, mode=mode)
        elif state["shown"]:
            if state.get("hold_until") and now < datetime.fromisoformat(state["hold_until"]):
                return                                    # shown by hand: stays for a while, then closes
            logger.info(f"[dashboard] closing the {label} dashboard")
            stop(now=now, mode=mode)
    except ShowError as exc:
        logger.warning(f"[dashboard] {exc}")
        _note_problem(str(exc))
    except Exception as exc:
        logger.warning(f"[dashboard] {exc.__class__.__name__}: {exc}")
        _note_problem(f"{exc.__class__.__name__}: {exc}")


def _note_problem(message):
    settings.set_value("dash_problem", json.dumps({"at": config.now().isoformat(timespec="seconds"), "message": message}))
