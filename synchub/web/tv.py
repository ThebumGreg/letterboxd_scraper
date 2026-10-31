"""The TV menu: a remote for each TV, which apps it shows, the TVs themselves, and the advanced testing tools.

Pages are Remote (/tv/remote) and Remote Apps (/tv/settings), which need the "tv" permission, and TVs (/tv/devices) and Remote Tools (/tv/advanced), which need "tv_setup". The script on those pages
talks to /api/tv/... . Endpoint names matter: access.RULES gives every `tv_setup*` / `api_tv_setup*` endpoint the TV setup permission and every other
`tv_*` / `api_tv_*` endpoint the TV permission.
"""
import json
import logging
from pathlib import Path

from flask import Response, g, jsonify, redirect, render_template, request, url_for

from config import BASE_DIR
from synchub import db, protect, ratings, settings, watch, workers
from synchub.clients import ApiError, ConfigError
from synchub.tv import camera, discovery, orchestrator, runtime
from synchub.tv.devices import DeviceError
from synchub.tv.jellyfin import JellyfinError, is_stale, item_label, match_session, summarize_session
from synchub.tv.notices import NOTICES
from synchub.web import current_limit

logger = logging.getLogger(__name__)
TV_STATIC = BASE_DIR / "static" / "tv"
PLAYSTATE_COMMANDS = {"Stop", "Pause", "Unpause", "NextTrack", "PreviousTrack", "Seek", "Rewind", "FastForward", "PlayPause"}
PLAY_COMMANDS = {"PlayNow", "PlayNext", "PlayLast", "PlayInstantMix", "PlayShuffle"}


def _build_id():
    """Changes whenever the scripts or styles change, so a tab left open across an update reloads itself."""
    roots = (TV_STATIC, Path(__file__).resolve().parent.parent / "tv")
    newest = max((f.stat().st_mtime for root in roots for f in root.rglob("*") if f.suffix in (".py", ".js", ".css")), default=0)
    return str(int(newest))


BUILD = _build_id()


def reply(ok, body, status=None, **extra):
    return jsonify(ok=ok, status=status if status is not None else (200 if ok else 0), body=body, **extra)


def relay(resp):
    try:
        body = resp.json() if resp.content else None
    except ValueError:
        body = resp.text[:500]
    return reply(resp.ok, body, resp.status_code)


def json_body():
    return request.get_json(force=True, silent=True) or {}


def valid_id(value):
    import re
    return bool(value) and bool(re.match(r"^[0-9A-Za-z_-]+$", str(value)))


def play_params(d):
    params = {"playCommand": d["playCommand"], "itemIds": ",".join(d.get("itemIds") or [])}
    for key in ("startPositionTicks", "startIndex", "audioStreamIndex", "subtitleStreamIndex", "mediaSourceId"):
        if d.get(key) not in (None, ""):
            params[key] = d[key]
    return params


def session_owners(r, sessions):
    """{session id: the TV it belongs to}. When several TVs' rules match a session (two Rokus, say), the TV whose rule says the most wins
    (a rule naming the device beats one naming only the app); a tie goes to the first TV listed."""
    rules = {d.id: orchestrator.effective_rule(d, sessions) for d in r.devices.values()}
    said = lambda rule: sum(weight for key, weight in (("client", 1), ("device", 10), ("device_id", 100)) if rule.get(key))
    owners = {}
    for s in sessions:
        matching = [(said(rule), -i, did) for i, (did, rule) in enumerate(rules.items()) if match_session(rule, s)]
        owners[s["Id"]] = max(matching)[2] if matching else None
    return owners


def register(app):
    rt = runtime.get          # looked up per call, so a test can start over with a fresh folder

    @app.errorhandler(DeviceError)
    @app.errorhandler(JellyfinError)
    @app.errorhandler(discovery.DiscoveryError)
    def tv_known_error(e):
        return reply(False, str(e)), 502

    # ------------------------------------------------------------------ pages
    @app.get("/tv")
    def tv_index():
        return redirect(url_for("tv_remote_page"))

    @app.get("/tv/remote")
    def tv_remote_page():
        return render_template("tv_remote.html")

    @app.get("/tv/settings")
    def tv_settings_page():
        return render_template("tv_settings.html")

    @app.get("/tv/devices")
    def tv_setup_devices_page():
        return render_template("tv_devices.html")

    @app.get("/tv/advanced")
    def tv_setup_advanced_page():
        return render_template("tv_advanced.html")

    # ------------------------------------------------------------------ config, notices, log
    @app.get("/api/tv/config")
    def api_tv_config():
        r = rt()
        icons = TV_STATIC / "icons"
        return jsonify(jellyfinUrl=r.jellyfin_url(), jellyfinPublicUrl=r.jellyfin_url(),
                       iconFiles=sorted(f.name for f in icons.glob("*") if f.suffix in (".png", ".svg", ".webp", ".jpg")),
                       configPath=str(r.config_path), configError=r.config_error,
                       devices=[{**d.describe(), "managed": d.id in r.managed_ids()} for d in r.devices.values()])

    @app.post("/api/tv/setup/reload")
    def api_tv_setup_reload():
        rt().reload_devices()
        return api_tv_config()

    @app.get("/api/tv/notices")
    def api_tv_notices():
        return jsonify(notices=NOTICES)

    @app.get("/api/tv/log")
    def api_tv_log():
        since = int(request.args.get("since", 0))
        entries = rt().logbook.since(since)
        return jsonify(entries=entries, last=entries[-1]["id"] if entries else since, build=BUILD)

    @app.delete("/api/tv/setup/log")
    def api_tv_setup_log_clear():
        rt().logbook.clear()
        return reply(True, "cleared")

    @app.get("/api/tv/setup/log/download")
    def api_tv_setup_log_download():
        lines = "\n".join(json.dumps(e) for e in rt().logbook.dump())
        return Response(lines + "\n", mimetype="text/plain", headers={"Content-Disposition": "attachment; filename=tv-remote-log.jsonl"})

    # ------------------------------------------------------------------ saved choices
    @app.get("/api/tv/prefs")
    def api_tv_prefs():
        return jsonify(prefs=rt().prefs, quickMax=runtime.QUICK_MAX)

    @app.put("/api/tv/prefs/quick/<dev_id>")
    def api_tv_prefs_quick(dev_id):
        r = rt()
        r.device(dev_id)
        clean = runtime.clean_apps(json_body().get("apps"), runtime.QUICK_MAX)
        r.update_prefs(lambda p: p["quick"].__setitem__(dev_id, clean))
        return reply(True, f"saved {len(clean)} quick-launch app(s)", apps=clean)

    @app.delete("/api/tv/prefs/quick/<dev_id>")
    def api_tv_prefs_quick_reset(dev_id):
        rt().update_prefs(lambda p: p["quick"].pop(dev_id, None))
        return reply(True, "reset to the defaults")

    @app.put("/api/tv/prefs/apps/<dev_id>")
    def api_tv_prefs_apps(dev_id):
        """Which apps a TV's remote shows. Saved with names, so the remote can draw them before the TV's app list loads."""
        r = rt()
        r.device(dev_id)
        clean = runtime.clean_apps(json_body().get("apps"), runtime.APPS_MAX)
        r.update_prefs(lambda p: p["apps"].__setitem__(dev_id, clean))
        return reply(True, f"{len(clean)} app(s) will show on the remote", apps=clean)

    @app.delete("/api/tv/prefs/apps/<dev_id>")
    def api_tv_prefs_apps_reset(dev_id):
        rt().update_prefs(lambda p: p["apps"].pop(dev_id, None))
        return reply(True, "showing the default apps")

    @app.put("/api/tv/prefs/resume/<dev_id>")
    def api_tv_prefs_resume(dev_id):
        """How a title is resumed on a TV that can only be sent a deep link (see Driver.resume_mode)."""
        r = rt()
        d = r.device(dev_id)
        mode = str(json_body().get("mode") or "")
        if mode not in runtime.RESUME_MODES:
            raise DeviceError("mode must be one of " + ", ".join(runtime.RESUME_MODES))
        if "deeplink" not in d.capabilities:
            raise DeviceError(f"{d.name} resumes through its Jellyfin session, so there is nothing to choose")
        r.update_prefs(lambda p: p["resume"].__setitem__(dev_id, mode))
        r.apply_overrides()
        return reply(True, "saved", mode=mode)

    @app.put("/api/tv/setup/prefs/session/<dev_id>")
    def api_tv_setup_prefs_session(dev_id):
        """Pin which Jellyfin session belongs to a TV (chosen from what Jellyfin actually lists)."""
        r = rt()
        d = r.device(dev_id)
        body = json_body()
        rule = {k: str(body[k]).strip()[:120] for k in ("client", "device", "device_id") if str(body.get(k) or "").strip()}
        if not rule:
            raise DeviceError("give a client, device or device_id")
        r.update_prefs(lambda p: p["session"].__setitem__(dev_id, rule))
        r.apply_overrides()
        return reply(True, f"{d.name} will use sessions matching {rule}", rule=rule)

    @app.delete("/api/tv/setup/prefs/session/<dev_id>")
    def api_tv_setup_prefs_session_reset(dev_id):
        r = rt()
        r.update_prefs(lambda p: p["session"].pop(dev_id, None))
        r.apply_overrides()
        return reply(True, "back to the rule in devices.json")

    @app.delete("/api/tv/setup/prefs/play/<dev_id>")
    def api_tv_setup_prefs_play_reset(dev_id):
        """Forget that a device's Jellyfin app cannot be controlled, so the session route is tried again."""
        r = rt()
        r.update_prefs(lambda p: p["play"].pop(dev_id, None))
        r.apply_overrides()
        return reply(True, "will try the Jellyfin app's session again")

    # ------------------------------------------------------------------ TV management and discovery
    @app.get("/api/tv/setup/tvs")
    def api_tv_setup_tvs():
        r = rt()
        return jsonify(tvs=[r.tv_view(d) for d in r.devices.values()], configPath=str(r.config_path))

    @app.post("/api/tv/setup/tvs")
    def api_tv_setup_tvs_add():
        r = rt()
        cfg = r.add_tv(json_body())
        return reply(True, f'Added {cfg["name"]}', tv=r.tv_view(r.devices[cfg["id"]]) if cfg["id"] in r.devices else None)

    @app.put("/api/tv/setup/tvs/<dev_id>")
    def api_tv_setup_tvs_edit(dev_id):
        r = rt()
        cfg = r.edit_tv(dev_id, json_body())
        return reply(True, f'Saved {cfg["name"]}', tv=r.tv_view(r.devices[dev_id]) if dev_id in r.devices else None)

    @app.delete("/api/tv/setup/tvs/<dev_id>")
    def api_tv_setup_tvs_remove(dev_id):
        rt().remove_tv(dev_id)
        return reply(True, "Removed")

    @app.get("/api/tv/setup/discovery/ranges")
    def api_tv_setup_discovery_ranges():
        return jsonify(ranges=rt().suggested_ranges())

    @app.post("/api/tv/setup/discovery/scan")
    def api_tv_setup_discovery_scan():
        """Look for TVs on the network. Takes a few seconds."""
        import time
        r = rt()
        t0 = time.time()
        result = r.scan(json_body().get("ranges"))
        r.logbook.add("system", None, "discovery", response=f"found {len(result['found'])} TV(s) in {time.time() - t0:.1f}s"
                      + (f" ({'; '.join(result['notes'])})" if result["notes"] else ""), ok=bool(result["found"]))
        return jsonify(ok=True, **result)

    # ------------------------------------------------------------------ devices
    @app.get("/api/tv/devices/<dev_id>/status")
    def api_tv_device_status(dev_id):
        r = rt()
        d = r.device(dev_id)
        poll = request.args.get("poll") == "1"
        st = d.status(quiet=poll)
        st["mac"] = d.mac
        key = (st["online"], st.get("power"), st.get("app"))
        if poll:
            if r.last_status.get(dev_id) != key:
                previous = r.last_status.get(dev_id)
                r.last_status[dev_id] = key
                detail = (f"online={st['online']} power={st.get('power')} app={st.get('appName') or st.get('app')}"
                          + (f" ({st['error']})" if st.get("error") else ""))
                r.logbook.add("system", dev_id, "status change" if previous else "status (first)", response=detail, ok=st["online"])
        else:
            r.last_status[dev_id] = key
        return jsonify(st)

    @app.post("/api/tv/devices/<dev_id>/key")
    def api_tv_device_key(dev_id):
        d = rt().device(dev_id)
        body = json_body()
        if body.get("native"):
            native = str(body["native"])
            if not d.valid_native_key(native):
                raise DeviceError("bad key")
        else:
            native = d.keymap.get(body.get("key"))
            if not native:
                raise DeviceError(f'{d.name} has no "{body.get("key")}" key')
        count = max(1, min(int(body.get("count") or 1), 30))
        d.send_key(native, count)
        return reply(True, native if count == 1 else f"{native} x{count}")

    @app.post("/api/tv/devices/<dev_id>/power")
    def api_tv_device_power(dev_id):
        r = rt()
        d = r.device(dev_id)
        if json_body().get("state") == "on":
            result = orchestrator.power_on(d, r.logbook)
            return reply(result["ok"], "\n".join(result["steps"]), steps=result["steps"])
        return reply(True, d.sleep())

    @app.post("/api/tv/setup/devices/<dev_id>/sleep")
    def api_tv_setup_device_sleep(dev_id):
        return reply(True, rt().device(dev_id).sleep(json_body().get("mode")))

    @app.post("/api/tv/setup/devices/<dev_id>/screensaver")
    def api_tv_setup_device_screensaver(dev_id):
        return reply(True, rt().device(dev_id).screensaver())

    @app.post("/api/tv/setup/devices/<dev_id>/probe")
    def api_tv_setup_device_probe(dev_id):
        result = rt().device(dev_id).probe()
        return reply(True, result["summary"], ports=result["ports"], ping=result["ping"])

    @app.post("/api/tv/setup/devices/<dev_id>/diagnose")
    def api_tv_setup_device_diagnose(dev_id):
        return reply(True, rt().device(dev_id).diagnose())

    @app.post("/api/tv/setup/devices/<dev_id>/wol")
    def api_tv_setup_device_wol(dev_id):
        info = rt().device(dev_id).wol()
        return reply(True, "magic packet sent to " + ", ".join(info["targets"]))

    @app.post("/api/tv/setup/devices/<dev_id>/detect-mac")
    def api_tv_setup_device_detect_mac(dev_id):
        d = rt().device(dev_id)
        mac = d.detect_mac()
        return reply(True, f"{mac}", mac=mac)

    @app.post("/api/tv/setup/devices/<dev_id>/pair/start")
    def api_tv_setup_device_pair_start(dev_id):
        d = rt().device(dev_id)
        if not hasattr(d, "pair_start"):
            raise DeviceError(f"{d.name} does not need pairing")
        return reply(True, d.pair_start())

    @app.post("/api/tv/setup/devices/<dev_id>/pair/finish")
    def api_tv_setup_device_pair_finish(dev_id):
        d = rt().device(dev_id)
        if not hasattr(d, "pair_finish"):
            raise DeviceError(f"{d.name} does not need pairing")
        return reply(True, d.pair_finish(str(json_body().get("code", ""))))

    @app.post("/api/tv/setup/devices/<dev_id>/pair/forget")
    def api_tv_setup_device_pair_forget(dev_id):
        d = rt().device(dev_id)
        if not hasattr(d, "forget_pairing"):
            raise DeviceError(f"{d.name} does not need pairing")
        return reply(True, d.forget_pairing())

    @app.post("/api/tv/setup/devices/<dev_id>/adb/reset")
    def api_tv_setup_device_adb_reset(dev_id):
        d = rt().device(dev_id)
        if not hasattr(d, "adb_reset"):
            raise DeviceError(f"{d.name} does not use ADB")
        out = d.adb_reset(json_body().get("newKey", True) is not False)
        return reply(True, f"{out}. Now look at the TV: choose Allow and tick Always allow.")

    @app.get("/api/tv/setup/devices/<dev_id>/adb")
    def api_tv_setup_device_adb_info(dev_id):
        d = rt().device(dev_id)
        if not hasattr(d, "adb_info"):
            raise DeviceError(f"{d.name} does not use ADB")
        info = d.adb_info()
        return reply(True, f"key {info['fingerprint'] or 'not made yet'} · {info['state']}", **info)

    @app.post("/api/tv/setup/devices/<dev_id>/connect")
    def api_tv_setup_device_connect(dev_id):
        d = rt().device(dev_id)
        if not hasattr(d, "connect"):
            raise DeviceError(f"{d.name} does not need a connect step")
        return reply(True, d.connect())

    @app.get("/api/tv/setup/devices/<dev_id>/screenshot")
    def api_tv_setup_device_screenshot(dev_id):
        d = rt().device(dev_id)
        if "screenshot" not in d.capabilities:
            raise DeviceError(f"{d.name} does not support screenshots")
        return Response(d.screenshot(), mimetype="image/png")

    @app.post("/api/tv/setup/devices/<dev_id>/prepare")
    def api_tv_setup_device_prepare(dev_id):
        r = rt()
        result = orchestrator.prepare(r.device(dev_id), r.jf(), r.logbook)
        return reply(result["ok"], "\n".join(result["steps"]), steps=result["steps"], session=result["session"])

    @app.get("/api/tv/devices/<dev_id>/icon/<app_id>")
    def api_tv_device_icon(dev_id, app_id):
        r = rt()
        d = r.device(dev_id)
        key = (dev_id, app_id)
        if key not in r.icons:
            try:
                r.icons[key] = d.icon(app_id)
            except DeviceError:
                return "", 404
        data, mime = r.icons[key]
        return Response(data, mimetype=mime, headers={"Cache-Control": "max-age=86400"})

    @app.post("/api/tv/devices/<dev_id>/text")
    def api_tv_device_text(dev_id):
        rt().device(dev_id).text(str(json_body().get("text", "")))
        return reply(True, "sent")

    @app.get("/api/tv/devices/<dev_id>/apps")
    def api_tv_device_apps(dev_id):
        """Installed apps. Listing them can be slow (ADB), so the result is reused for a while."""
        import time
        r = rt()
        d = r.device(dev_id)
        cached = r.apps_cache.get(dev_id)
        if cached and time.time() - cached[0] < runtime.APPS_TTL and request.args.get("refresh") != "1":
            return jsonify(cached[1])
        apps = d.apps()
        r.apps_cache[dev_id] = (time.time(), apps)
        return jsonify(apps)

    @app.post("/api/tv/devices/<dev_id>/launch")
    def api_tv_device_launch(dev_id):
        body = json_body()
        out = rt().device(dev_id).launch(str(body.get("app", "")), body.get("params"))
        return reply(True, out or "launched")

    @app.post("/api/tv/devices/<dev_id>/jellyfin")
    def api_tv_device_open_jellyfin(dev_id):
        """Open the TV's Jellyfin app (the Jellyfin channel on a Roku, org.jellyfin.androidtv on a Google TV, or what the TV's jellyfin.app says)."""
        d = rt().device(dev_id)
        return reply(True, d.launch(d.jellyfin_app()) or "launched")

    # ------------------------------------------------------------------ playing a title on a TV
    def play_by_url(d, body, prior_steps=()):
        """Hand the device a stream URL (no Jellyfin app or session involved)."""
        r = rt()
        if body["playCommand"] != "PlayNow":
            raise DeviceError("Stream playback supports Play now only (there is no queue)")
        ids, index = body["itemIds"], int(body.get("startIndex") or 0)
        result = orchestrator.play_url(d, r.jf(), r.logbook, ids[index] if index < len(ids) else ids[0], body.get("startPositionTicks"))
        steps = list(prior_steps) + result["steps"]
        return reply(result["ok"], "\n".join(steps), steps=steps, confirmed=result["confirmed"], via="url")

    def play_by_deeplink(d, body, fallback):
        """Send the device's Jellyfin app a deep link. If the request itself fails, optionally stream instead."""
        r = rt()
        if body["playCommand"] != "PlayNow":
            raise DeviceError("A deep link starts playback straight away; there is no queue")
        ids, index = body["itemIds"], int(body.get("startIndex") or 0)
        item_id = ids[index] if index < len(ids) else ids[0]
        result = orchestrator.play_deeplink(d, r.jf(), r.logbook, item_id, body.get("startPositionTicks"),
                                            resume_mode=d.resume_mode() or "b", user_id=body.get("_jellyfinUser"))
        if not result["ok"] and result.get("retryable") and fallback:
            note = "The deep link request failed, so the movie is streamed directly instead"
            r.logbook.add("system", d.id, "play", response=note, ok=True)
            return play_by_url(d, body, result["steps"] + [note])
        return reply(result["ok"], "\n".join(result["steps"]), steps=result["steps"], confirmed=result["confirmed"], via="deeplink")

    def learn_url_play(d):
        r = rt()
        r.update_prefs(lambda p: p["play"].__setitem__(d.id, "url"))
        r.apply_overrides()

    def resolve_series(r, body):
        """A show is played from the episode its viewer should watch next (Jellyfin's "Next up", or the first episode when everything is seen),
        for the profile chosen on the Who-is-watching page (else Jellyfin's default user). Changes body["itemIds"] and returns a sentence saying
        what was chosen, or None when the item is not a show."""
        ids, index = body["itemIds"], int(body.get("startIndex") or 0)
        item_id = ids[index] if index < len(ids) else ids[0]
        jf = r.jf()
        item = jf.item(item_id)
        if item.get("Type") != "Series":
            return None
        profile = getattr(g, "profile", None) or {}
        user_id, who = profile.get("jellyfin_user_id") or None, profile.get("name")
        episode, how = jf.next_up(item_id, user_id), "next up"
        if not episode:
            episode, how = jf.first_episode(item_id, user_id), "from the first episode"
        if not episode:
            raise DeviceError(f'Jellyfin has no episodes of "{item.get("Name")}" to play.')
        body["itemIds"], body["startIndex"] = [episode["Id"]], 0
        body.pop("startPositionTicks", None)
        season, number = episode.get("ParentIndexNumber"), episode.get("IndexNumber")
        label = " - ".join(x for x in (f"S{season}E{number}" if season is not None and number is not None else None, episode.get("Name")) if x)
        return f'"{item.get("Name")}" is a show: playing {how}{" for " + who if who else ""}: {label or episode["Id"]}'

    def resume_point(r, jellyfin_user_id, item_id):
        """Where this viewer stopped in an item, in ticks (0: nowhere). Jellyfin is asked now, which is the freshest; if it cannot be reached, what
        SyncHub last copied from it is used. When Jellyfin kept no place (it drops anything before about 5% of the runtime), the Jellyfin events
        log may still know."""
        user = watch.user_of({"jellyfin_user_id": jellyfin_user_id})
        ticks = None
        try:
            ticks = watch.resume_ticks(r.jf().user_data(item_id, jellyfin_user_id))
        except Exception as exc:                          # not being able to find the place must never stop the title from playing
            logger.warning(f"Could not ask Jellyfin where {jellyfin_user_id} stopped in {item_id} ({exc.__class__.__name__}); using the copy SyncHub has")
            with db.connection() as conn:
                row = conn.execute("SELECT position_ticks FROM src_jellyfin_userdata WHERE jellyfin_user_id = ? AND item_id = ? AND removed_at IS NULL",
                                   (user, watch.norm(item_id))).fetchone()
            ticks = int(row["position_ticks"]) if row else 0
        if ticks:
            return ticks
        try:
            with db.connection() as conn:
                return watch.event_ticks(watch.event_progress(conn, user, [item_id]).get(watch.norm(item_id)))
        except Exception as exc:
            logger.warning(f"Could not read the Jellyfin events log for {item_id} ({exc.__class__.__name__})")
            return 0

    def resolve_resume(r, body):
        """Playing a title picks up where the person using this page left off (their Jellyfin user, set on People), unless the request already
        says where to start: a number starts exactly there and 0 starts over. Sets body["startPositionTicks"] and returns a sentence saying so,
        or None when there is nothing to pick up (a guest, a profile without a Jellyfin user, nothing started)."""
        if body.get("startPositionTicks") is not None or body["playCommand"] != "PlayNow":
            return None
        profile = getattr(g, "profile", None) or {}
        if not watch.user_of(profile):
            return None
        ids, index = body["itemIds"], int(body.get("startIndex") or 0)
        ticks = resume_point(r, profile["jellyfin_user_id"], ids[index] if index < len(ids) else ids[0])
        if ticks <= 0:
            return None
        body["startPositionTicks"] = ticks
        return f"Picking up where {profile.get('name') or 'they'} left off, at {watch.fmt_ticks(ticks)}"

    @app.post("/api/tv/devices/<dev_id>/play")
    def api_tv_device_play(dev_id):
        """Optionally prepare the device, then play. Via the device's Jellyfin session, a deep link, or (for devices that can) a stream URL.

        In auto mode a Roku uses its deep link and a Google TV its Jellyfin session; if a session reports it cannot be remote controlled,
        the movie is streamed instead and that is remembered so the next play skips straight to streaming. A show is played from its next episode.
        Where playback starts: from where the viewer stopped (see resolve_resume), unless the request gives `startPositionTicks`: 0 starts over."""
        r = rt()
        d = r.device(dev_id)
        body = json_body()
        if body.get("playCommand") not in PLAY_COMMANDS or not body.get("itemIds"):
            raise DeviceError("bad playCommand or itemIds")
        require_items_allowed(r.jf(), body["itemIds"])
        asked = body.get("startPositionTicks")
        notes = []
        if note := resolve_series(r, body):
            notes.append(note)
            body["startPositionTicks"] = 0 if asked == 0 else None        # a position given for a show means nothing: the episode has its own
        if resumed := resolve_resume(r, body):
            notes.append(resumed)
        body["_jellyfinUser"] = ((getattr(g, "profile", None) or {}).get("jellyfin_user_id")) or None
        response = play_dispatch(r, d, body)
        if not notes:
            return response
        payload = response.get_json()
        for note in notes:
            r.logbook.add("system", d.id, "play", response=note, ok=True)
        payload["steps"] = notes + list(payload.get("steps") or [])
        payload["body"] = "\n".join(notes) + "\n" + str(payload.get("body") or "")
        return jsonify(payload)

    def play_dispatch(r, d, body):
        mode = body.get("via") or d.play_mode()
        can_stream, can_link = "url_play" in d.capabilities, "deeplink" in d.capabilities
        if mode == "deeplink" and not can_link:
            raise DeviceError(f"{d.name} cannot be sent a deep link")
        if mode == "auto" and can_link:
            mode = "deeplink+"   # deep link first; stream only if the request itself fails
        if mode in ("deeplink", "deeplink+"):
            if can_stream and d.resume_mode() == "a" and (body.get("startPositionTicks") or 0) > 0:
                # option a: the app cannot start part-way, so this title is streamed from the saved place (Jellyfin then records no progress)
                r.logbook.add("system", d.id, "play", response="Resuming by streaming from the saved place (Remote Apps, 'When a title is resumed')", ok=True)
                return play_by_url(d, body, ["Resuming by streaming from the saved place; Jellyfin will not record progress for this viewing"])
            return play_by_deeplink(d, body, fallback=(mode == "deeplink+" and can_stream))
        if mode == "url" and not can_stream:
            raise DeviceError(f"{d.name} cannot play from a stream URL")
        if mode == "auto" and not can_stream:
            mode = "session"
        if mode == "url":
            return play_by_url(d, body)
        jf = r.jf()
        steps = []
        if body.get("prepare", True):
            result = orchestrator.prepare(d, jf, r.logbook)
            steps = result["steps"]
            if not result["ok"]:
                if mode == "auto" and body["playCommand"] == "PlayNow" and result.get("reason") in ("not_remote", "no_match"):
                    note = ("The Jellyfin app on this device cannot be remote controlled, so it is streamed directly instead"
                            if result["reason"] == "not_remote" else
                            "No usable Jellyfin session appeared, so the movie is streamed directly instead")
                    r.logbook.add("system", d.id, "play", response=note, ok=True)
                    if result["reason"] == "not_remote":
                        learn_url_play(d)
                    out = play_by_url(d, body, steps + [note])
                    payload = out.get_json()
                    payload["learned"] = result["reason"] == "not_remote"
                    return jsonify(payload)
                return reply(False, "\n".join(steps), steps=steps)
            session = result["session"]
        else:
            found = orchestrator.device_sessions(jf, d)
            if not found:
                if mode == "auto" and body["playCommand"] == "PlayNow":
                    return play_by_url(d, body, ["No Jellyfin session is open on this device, so the movie is streamed directly"])
                raise JellyfinError(f"No Jellyfin session for {d.name}. Tick 'prepare device first' or open the app.")
            session = summarize_session(found[0])
        command = body["playCommand"]
        ids, index = body["itemIds"], int(body.get("startIndex") or 0)
        wanted = ids[index] if index < len(ids) else ids[0]

        def log_step(message, ok=True):
            steps.append(message)
            r.logbook.add("system", d.id, "play", response=message, ok=ok)

        def attempt(session):
            """Send the play command; for Play now, wait for the session to report the item playing. -> (response, confirmed)."""
            playing = session.get("NowPlaying")
            if command == "PlayNow" and playing and orchestrator._norm(playing.get("Id")) != orchestrator._norm(wanted):
                log_step(f'"{playing.get("Name")}" is still open in the app: stopping it first')
                orchestrator.stop_playback(jf, session["Id"], log_step)
            response = jf.play(session["Id"], play_params(body))
            steps.append(f"{command} -> HTTP {response.status_code}")
            confirmed = None
            if response.ok and command == "PlayNow":
                confirmed, title, secs = orchestrator.confirm_playing(jf, session["Id"], wanted)
                log_step(f"Playback confirmed: {title} (after {secs}s)" if confirmed else
                         f"The command was accepted but the session never reported the item playing within {secs}s", bool(confirmed))
            return response, confirmed

        resp, confirmed = attempt(session)
        if resp.ok and confirmed is False and body.get("prepare", True) and d.keymap.get("home"):
            # The app took the command but did nothing (it is often stuck on a player or detail screen): go Home, reopen it, try once more.
            log_step("Nothing started, so going Home, reopening the Jellyfin app and trying once more")
            if orchestrator.go_home(d, log_step):
                again = orchestrator.prepare(d, jf, r.logbook)
                steps.extend(again["steps"])
                if again["ok"]:
                    session = again["session"]
                    resp, confirmed = attempt(session)
        return reply(resp.ok, "\n".join(steps), resp.status_code, steps=steps, sessionId=session["Id"], confirmed=confirmed)

    # ------------------------------------------------------------------ cameras (UniFi Protect, shown through the go2rtc relay)
    def camera_streams():
        try:
            return protect.streams()
        except (ApiError, ConfigError) as e:
            raise DeviceError(str(e))

    @app.get("/api/protect/relay")
    def api_protect_relay():
        """The Camera relay card in Settings: the checklist from the chosen worker to cameras being served."""
        with db.connection() as conn:
            return jsonify(ok=True, **protect.relay_status(conn))

    @app.post("/api/protect/relay/<action>")
    def api_protect_relay_action(action):
        """install: the worker's PC (its update helper) sets go2rtc up again now; resend: ask Protect afresh and give go2rtc the cameras again; test: ask the relay from SyncHub."""
        if action == "test":
            try:
                names = protect.test_relay()
            except (ApiError, ConfigError) as e:
                return jsonify(ok=False, message=str(e))
            return jsonify(ok=bool(names), message=(f"The relay answers and serves {len(names)} camera(s): {', '.join(names)}." if names
                                                    else "The relay answers but serves no cameras yet. Use Send cameras now."))
        if action in ("install", "resend"):
            wid = protect.relay_worker_id()
            if not wid:
                return jsonify(ok=False, message="Choose the relay worker above and save first.")
            protect.send_again()
            if action == "resend":
                return jsonify(ok=True, message="Asked Protect for the cameras again. The worker gives them to go2rtc within a few seconds.")
            try:
                with db.connection() as conn:
                    workers.request_host(conn, wid, "relay")
            except workers.WorkerError as e:
                return jsonify(ok=False, message=str(e))
            return jsonify(ok=True, message="Asked the worker's PC to set go2rtc up. It happens within a minute, once the worker has no running job.")
        return jsonify(ok=False, message="Unknown action."), 404

    @app.get("/api/tv/cameras")
    def api_tv_cameras():
        """The cameras the remote can put on a TV. `enabled` is false until UniFi Protect is switched on in Settings."""
        if not protect.enabled():
            return jsonify(enabled=False, cameras=[])
        try:
            found = protect.streams()
        except (ApiError, ConfigError) as e:
            return jsonify(enabled=True, cameras=[], error=str(e))
        return jsonify(enabled=True, relay=bool(protect.relay_host()), cameras=[{"name": name, "title": item["title"]} for name, item in found.items()])

    @app.get("/api/tv/devices/<dev_id>/camera")
    def api_tv_device_camera(dev_id):
        r = rt()
        r.device(dev_id)
        return jsonify(state=camera.status(r, dev_id))

    @app.post("/api/tv/devices/<dev_id>/camera")
    def api_tv_device_camera_show(dev_id):
        """Pause what the TV is playing, remember it, and show this camera full screen (or switch to it when a camera is already showing)."""
        r = rt()
        d = r.device(dev_id)
        name = str(json_body().get("camera") or "")
        found = camera_streams()
        if name not in found:
            raise DeviceError("That camera is not on the list any more.")
        try:
            url = protect.play_url(name)
        except ConfigError as e:
            raise DeviceError(str(e))
        result = camera.show(r, d, {"name": name, "title": found[name]["title"]}, url, settings.get("protect_viewer_app"))
        return reply(result["ok"], "\n".join(result["steps"]), steps=result["steps"], state=result["state"])

    @app.post("/api/tv/devices/<dev_id>/camera/close")
    def api_tv_device_camera_close(dev_id):
        """Close the camera and carry on with what the TV was playing."""
        r = rt()
        result = camera.close(r, r.device(dev_id), settings.get("protect_viewer_app"))
        return reply(result["ok"], "\n".join(result["steps"]), steps=result["steps"], state=result["state"])

    # ------------------------------------------------------------------ Jellyfin
    @app.get("/api/tv/jf/sessions")
    def api_tv_jf_sessions():
        r = rt()
        try:
            raw = r.jf().sessions(quiet=True)
        except JellyfinError as e:
            r.logbook.poll_failed("jf-sessions", "jellyfin", None, "sessions poll", "GET /Sessions", 0, str(e))
            raise
        r.logbook.poll_ok("jf-sessions", "jellyfin", None, "sessions poll")
        owners = session_owners(r, raw)
        out = []
        for s in raw:
            summary = summarize_session(s)
            summary["deviceId"] = owners[s["Id"]]
            out.append(summary)
        return jsonify(out)

    @app.get("/api/tv/now")
    def api_tv_now():
        """What each TV is playing through Jellyfin, for the dashboard strip. One call to Jellyfin and none to the TVs, so it is cheap to poll.
        Without Jellyfin set up it simply lists nothing."""
        r = rt()
        limit, cache = current_limit(), {}
        try:
            jf = r.jf()
            raw = jf.sessions(quiet=True)
        except JellyfinError:
            return jsonify(ok=True, tvs=[])
        owner = session_owners(r, raw)
        playing = []
        for d in r.devices.values():
            found = [s for s in raw if owner[s["Id"]] == d.id and s.get("NowPlayingItem") and not is_stale(s)]
            if not found:
                continue
            s = max(found, key=lambda x: x.get("LastActivityDate") or "")
            if limit is not None and not ratings.permitted(jf_rank(jf, s["NowPlayingItem"], cache), limit):
                continue
            playing.append({"id": d.id, "name": d.name, "title": item_label(s["NowPlayingItem"]), "paused": bool((s.get("PlayState") or {}).get("IsPaused"))})
        return jsonify(ok=True, tvs=playing)

    # Parental controls: someone with a rating limit (see synchub.ratings) only finds, opens and plays titles within it. Unrated titles count as too mature.
    def jf_rank(jf, item, cache):
        series_id = item.get("SeriesId") if item.get("Type") == "Episode" else None
        series = None
        if series_id:
            if series_id not in cache:
                cache[series_id] = jf.item(series_id) or {}
            series = cache[series_id]
        return ratings.of_jellyfin_item(item, series)

    def require_items_allowed(jf, item_ids):
        limit = current_limit()
        if limit is None:
            return
        cache = {}
        for item_id in item_ids:
            if not valid_id(item_id) or not ratings.permitted(jf_rank(jf, jf.item(item_id) or {}, cache), limit):
                raise DeviceError("That title is not available for your profile.")

    def viewer():
        """The Jellyfin user of the profile chosen on this device, or None (a guest): whose watch data the lists below show."""
        return (getattr(g, "profile", None) or {}).get("jellyfin_user_id") or None

    @app.get("/api/tv/jf/search")
    def api_tv_jf_search():
        jf = rt().jf()
        found, limit = jf.search(request.args.get("q", "").strip(), viewer()), current_limit()
        if limit is not None:
            cache = {}
            found = [i for i in found if ratings.permitted(jf_rank(jf, i, cache), limit)]
        return jsonify(found)

    @app.get("/api/tv/jf/episodes/<series_id>")
    def api_tv_jf_episodes(series_id):
        if not valid_id(series_id):
            raise JellyfinError("bad id")
        jf = rt().jf()
        require_items_allowed(jf, [series_id])
        return jsonify(jf.episodes(series_id, viewer()))

    def session_id(body):
        sid = body.get("sessionId")
        if not valid_id(sid):
            raise JellyfinError("bad or missing sessionId")
        return sid

    @app.post("/api/tv/setup/jf/play")
    def api_tv_setup_jf_play():
        body = json_body()
        if body.get("playCommand") not in PLAY_COMMANDS:
            raise JellyfinError("bad playCommand")
        jf = rt().jf()
        require_items_allowed(jf, body.get("itemIds") or [])
        return relay(jf.play(session_id(body), play_params(body)))

    @app.post("/api/tv/jf/playstate")
    def api_tv_jf_playstate():
        body = json_body()
        if body.get("command") not in PLAYSTATE_COMMANDS:
            raise JellyfinError("bad command")
        params = {}
        if body.get("seekPositionTicks") is not None:
            params["seekPositionTicks"] = int(body["seekPositionTicks"])
        return relay(rt().jf().playstate(session_id(body), body["command"], params))

    @app.post("/api/tv/setup/jf/command")
    def api_tv_setup_jf_command():
        body = json_body()
        if not body.get("command"):
            raise JellyfinError("missing command")
        payload = {"Name": body["command"]}
        if body.get("arguments"):
            payload["Arguments"] = body["arguments"]
        return relay(rt().jf().command(session_id(body), payload))

    @app.post("/api/tv/setup/jf/viewing")
    def api_tv_setup_jf_viewing():
        jf = rt().jf()
        body = json_body()
        item_id = body.get("itemId")
        if not valid_id(item_id):
            raise JellyfinError("bad itemId")
        # Newer servers require itemName as well as itemType; look both up from the item.
        item = jf.item(item_id)
        params = {"itemId": item_id,
                  "itemType": item.get("Type") or body.get("itemType") or "Movie",
                  "itemName": item.get("Name") or body.get("itemName") or "Item"}
        return relay(jf.viewing(session_id(body), params))

    @app.post("/api/tv/setup/jf/message")
    def api_tv_setup_jf_message():
        body = json_body()
        payload = {"Header": body.get("header") or "", "Text": body.get("text") or "",
                   "TimeoutMs": int(body.get("timeoutMs") or 5000)}
        return relay(rt().jf().message(session_id(body), payload))
