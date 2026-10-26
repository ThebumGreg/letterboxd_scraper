"""A person's own notification settings: the delivery methods they add (such as Pushover), a test message, and what was sent to them."""
from flask import jsonify, redirect, request, url_for

from synchub import access, db
from synchub.notifications import dispatch, providers, store
from synchub.notifications.providers.base import BAD_SETUP, QUOTA, RETRY, ProviderError
from synchub.notifications.throttle import Throttle
from synchub.web import current_profile

CHECKS = Throttle(limit=6, per_seconds=60)      # asking Pushover about a key, and adding one (which asks again)
TESTS = Throttle(limit=5, per_seconds=60)
SERVICE_STATUS = {RETRY: 502, QUOTA: 502, BAD_SETUP: 503}


def _fail(message, status=400):
    response = jsonify(ok=False, message=message)
    response.status_code = status
    return response


def _person(conn):
    """(the profile using this request, None), or (None, why they cannot manage notifications).

    Anyone on the network can choose a profile that has no PIN, and notification settings decide where a person's messages go; so once any profile
    has a PIN, a profile without one is not allowed to change them (until then nothing is restricted, as everywhere else in SyncHub)."""
    profile = current_profile(conn)
    if profile is None:
        return None, "Choose your profile first (the link at the top right). Notifications belong to a profile."
    has_pin = conn.execute("SELECT pin_hash IS NOT NULL FROM profile WHERE id = ?", (profile["id"],)).fetchone()[0]
    if access.locked_down(conn) and not has_pin:
        return None, "This profile has no PIN, and anyone on the network can choose a profile without one. Ask whoever manages People to give it a PIN before it sets up notifications."
    return profile, None


def _forms():
    """What the Add form is built from: each method a person may add, with the fields it needs."""
    return [{"key": p.key, "label": p.label, "fields": [{"name": f.name, "label": f.label, "secret": f.secret, "required": f.required, "help": f.help} for f in p.fields]}
            for p in providers.for_people()]


def _provider_and_config(data):
    """The provider and its cleaned config from a request body, or a ProviderError saying what is wrong with what was typed."""
    provider = providers.get(str(data.get("provider") or ""))
    if provider.scope != "user":
        raise ProviderError("That delivery method is not available.")
    config = data.get("config")
    config = {k: v for k, v in config.items() if isinstance(k, str) and isinstance(v, (str, int))} if isinstance(config, dict) else {}
    return provider, provider.validate(config)


def panels_context(conn):
    """What the notification panels (shown on a person's own page under People) are built from, for the person using this request."""
    profile, problem = _person(conn)
    channels = store.list_channels(conn, profile["id"]) if profile else []
    history = store.history(conn, profile["id"]) if profile else []
    is_admin = bool(profile) and profile["id"] in store.admin_ids(conn)
    rules = [r for r in store.rules(conn, profile["id"]) if is_admin or r["audience"] == "user"] if profile else []
    pref = store.pref(conn, profile["id"]) if profile else store.DEFAULT_PREF
    return {"profile": profile, "problem": problem, "channels": channels, "history": history, "forms": _forms(), "state": dispatch.state(),
            "rules": rules, "pref": pref, "is_admin": is_admin}


def register(app):
    @app.route("/notifications")
    def notifications_page():
        """Kept for old links and bookmarks: a person's notifications are now on their own page under People."""
        with db.connection() as conn:
            profile = current_profile(conn)
        return redirect(url_for("profile_page", profile_id=profile["id"], _anchor="notifications") if profile else url_for("who", next="/notifications"))

    @app.route("/api/notifications/verify", methods=["POST"])
    def api_notifications_verify():
        """Checks a key with the service and lists the choices it offers (such as the person's devices). Saves nothing."""
        with db.connection() as conn:
            profile, problem = _person(conn)
        if problem:
            return _fail(problem, 403)
        try:
            provider, config = _provider_and_config(request.get_json(silent=True) or {})
        except ProviderError as exc:
            return _fail(str(exc))
        if not CHECKS.allow(profile["id"]):
            return _fail("That is a lot of checks. Wait a minute and try again.", 429)
        result = provider.check(config)
        if not result.ok:
            return _fail(result.detail, SERVICE_STATUS.get(result.outcome, 400))
        return jsonify(ok=True, message="That works.", choices=provider.choices(result))

    @app.route("/api/notifications/channels", methods=["POST"])
    def api_notifications_add():
        """Adds a delivery method for the person using this request. It is checked with the service again here: the browser's earlier check is not trusted."""
        with db.connection() as conn:
            profile, problem = _person(conn)
        if problem:
            return _fail(problem, 403)
        data = request.get_json(silent=True) or {}
        try:
            provider, config = _provider_and_config(data)
        except ProviderError as exc:
            return _fail(str(exc))
        if not CHECKS.allow(profile["id"]):
            return _fail("That is a lot of checks. Wait a minute and try again.", 429)
        result = provider.check(config)
        if not result.ok:
            return _fail(result.detail, SERVICE_STATUS.get(result.outcome, 400))
        try:
            with db.connection() as conn:
                channel_id = store.add_channel(conn, profile["id"], provider.key, data.get("label"), config)
        except ProviderError as exc:
            return _fail(str(exc))
        return jsonify(ok=True, message="Added. Send a test to see it arrive.", id=channel_id)

    def _owned(action):
        """Runs action(conn, profile) for the person using this request; their own channels only."""
        with db.connection() as conn:
            profile, problem = _person(conn)
            if problem:
                return _fail(problem, 403)
            return action(conn, profile)

    @app.route("/api/notifications/channels/<int:channel_id>/test", methods=["POST"])
    def api_notifications_test(channel_id):
        with db.connection() as conn:
            profile, problem = _person(conn)
        if problem:
            return _fail(problem, 403)
        if not TESTS.allow(profile["id"]):
            return _fail("That is a lot of tests. Wait a minute and try again.", 429)
        try:
            got = dispatch.send_test(profile["id"], channel_id)
        except LookupError as exc:
            return _fail(str(exc), 404)
        return jsonify(ok=got["ok"], message=got["message"], status=got["status"])

    @app.route("/api/notifications/channels/<int:channel_id>/enabled", methods=["POST"])
    def api_notifications_enable(channel_id):
        wanted = (request.get_json(silent=True) or {}).get("enabled")
        if not isinstance(wanted, bool):
            return _fail("Say whether it should be on or off.")
        return _owned(lambda conn, profile: jsonify(ok=True, message="Switched on." if wanted else "Switched off.") if store.set_enabled(conn, channel_id, profile["id"], wanted)
                      else _fail("That delivery method no longer exists.", 404))

    @app.route("/api/notifications/channels/<int:channel_id>/delete", methods=["POST"])
    def api_notifications_delete(channel_id):
        return _owned(lambda conn, profile: jsonify(ok=True, message="Removed.") if store.delete_channel(conn, channel_id, profile["id"])
                      else _fail("That delivery method no longer exists.", 404))

    @app.route("/api/notifications/history")
    def api_notifications_history():
        return _owned(lambda conn, profile: jsonify(ok=True, history=store.history(conn, profile["id"])))

    @app.route("/api/notifications/rules", methods=["POST"])
    def api_notifications_rule():
        """Switches one kind of notification on or off for the person using this request."""
        data = request.get_json(silent=True) or {}
        if not isinstance(data.get("enabled"), bool):
            return _fail("Say whether it should be on or off.")

        def save(conn, profile):
            try:
                store.set_rule(conn, profile["id"], str(data.get("event_type") or ""), data["enabled"])
            except ValueError as exc:
                return _fail(str(exc), 404)
            return jsonify(ok=True, message="Saved.")
        return _owned(save)

    @app.route("/api/notifications/quiet", methods=["POST"])
    def api_notifications_quiet():
        """Saves the quiet hours of the person using this request."""
        data = request.get_json(silent=True) or {}
        if not isinstance(data.get("quiet_on"), bool):
            return _fail("Say whether quiet hours should be on or off.")
        batch = data.get("batch_minutes", 0)
        if not isinstance(batch, int) or isinstance(batch, bool):
            return _fail("Choose how often to group notices from the list.")

        def save(conn, profile):
            try:
                store.set_pref(conn, profile["id"], data["quiet_on"], data.get("quiet_start"), data.get("quiet_end"), data.get("timezone"), batch)
            except ValueError as exc:
                return _fail(str(exc))
            return jsonify(ok=True, message="Saved.")
        return _owned(save)

    @app.route("/api/notifications/episodes", methods=["POST"])
    def api_notifications_episodes():
        """Saves when the person using this request hears about new episodes, and for which shows."""
        data = request.get_json(silent=True) or {}

        def save(conn, profile):
            try:
                store.set_episode_pref(conn, profile["id"], data.get("episode_when"), data.get("episode_source"))
            except ValueError as exc:
                return _fail(str(exc))
            return jsonify(ok=True, message="Saved.")
        return _owned(save)

    @app.route("/api/notifications/shows/<int:media_id>/mute", methods=["POST"])
    def api_notifications_mute_show(media_id):
        """Stops (or resumes) new-episode notices about one show for the person using this request."""
        muted = (request.get_json(silent=True) or {}).get("muted")
        if not isinstance(muted, bool):
            return _fail("Say whether the show should be muted.")

        def save(conn, profile):
            if conn.execute("SELECT 1 FROM media WHERE id = ? AND kind = 'show'", (media_id,)).fetchone() is None:
                return _fail("That show does not exist.", 404)
            store.set_muted(conn, profile["id"], media_id, muted)
            return jsonify(ok=True, muted=muted, message="Muted." if muted else "Unmuted.")
        return _owned(save)
