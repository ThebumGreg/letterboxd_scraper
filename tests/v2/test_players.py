"""TV players: tokens, the command queue, the player API and the Players tab."""
import json
import threading
import time

import pytest

from synchub import players

T0 = "2026-01-01 12:00:00"


def add(conn, name="living-room", tv=""):
    player_id, token = players.create(conn, name, tv)
    return players.get(conn, player_id), token


# ---------------------------------------------------------------- tokens and identity
def test_a_token_is_shown_once_and_only_its_hash_is_stored(conn):
    player, token = add(conn)
    assert token.startswith("sp_") and len(token) == 51
    stored = conn.execute("SELECT * FROM player").fetchone()
    assert stored["token_hash"] == players.hash_token(token) and token not in " ".join(str(v) for v in dict(stored).values())
    assert player["token_hash"] is True
    found, problem = players.authenticate(conn, token)
    assert problem is None and found["id"] == player["id"]


def test_a_worker_token_does_not_open_the_player_api(conn):
    from synchub import workers
    _, worker_token = workers.create(conn, "w1")
    assert players.authenticate(conn, worker_token) == (None, "unknown")


@pytest.mark.parametrize("token,problem", [("", "missing"), (None, "missing"), ("sp_nope", "unknown")])
def test_bad_tokens_are_refused(conn, token, problem):
    add(conn)
    assert players.authenticate(conn, token) == (None, problem)


def test_disabled_revoked_and_rotated(conn):
    player, token = add(conn)
    players.set_enabled(conn, player["id"], False)
    assert players.authenticate(conn, token) == (None, "disabled")
    players.set_enabled(conn, player["id"], True)
    new = players.rotate_token(conn, player["id"])
    assert players.authenticate(conn, token)[0] is None and players.authenticate(conn, new)[0]["id"] == player["id"]
    players.revoke(conn, player["id"])
    assert players.authenticate(conn, new) == (None, "unknown") and players.get(conn, player["id"])["revoked"]


def test_names_are_validated_and_unique(conn):
    with pytest.raises(players.PlayerError, match="needs a name"):
        players.create(conn, "  ")
    with pytest.raises(players.PlayerError, match="60"):
        players.create(conn, "x" * 61)
    add(conn, "a")
    with pytest.raises(players.PlayerError, match="already a player"):
        players.create(conn, "a")
    other, _ = add(conn, "b", "Den TV")
    with pytest.raises(players.PlayerError, match="already a player"):
        players.update(conn, other["id"], name="a")
    players.update(conn, other["id"], name="b2", tv_name="Bedroom")
    assert players.get(conn, other["id"])["name"] == "b2" and players.get(conn, other["id"])["tv_name"] == "Bedroom"


# ---------------------------------------------------------------- registering and being seen
def test_register_stores_what_the_player_says_and_refuses_another_protocol(conn):
    player, _ = add(conn)
    players.register(conn, player["id"], {"protocol": players.PROTOCOL, "version": "1.4.0", "os": "Ubuntu 24.04", "hostname": "nuc1", "capabilities": {"mpv": "0.37"}}, now=T0)
    got = players.get(conn, player["id"], now="2026-01-01 12:00:30")
    assert got["version"] == "1.4.0" and got["capabilities"] == {"mpv": "0.37"} and got["online"]
    assert not players.get(conn, player["id"], now="2026-01-01 12:02:00")["online"]
    with pytest.raises(players.ProtocolError):
        players.register(conn, player["id"], {"protocol": 99})


def test_touch_keeps_status_and_now_playing_when_they_are_small_enough(conn):
    player, _ = add(conn)
    players.touch(conn, player["id"], {"load": 0.5}, {"title": "Heat"}, now=T0)
    got = players.get(conn, player["id"])
    assert got["status"] == {"load": 0.5} and got["now_playing"] == {"title": "Heat"} and got["last_seen"] == T0
    players.touch(conn, player["id"], {"blob": "x" * 9000}, None, now=T0)
    assert players.get(conn, player["id"])["status"] == {"load": 0.5}


# ---------------------------------------------------------------- commands
def test_commands_go_out_oldest_first_once_and_are_answered_once(conn):
    player, _ = add(conn)
    first, second = players.send(conn, player["id"], "ping"), players.send(conn, player["id"], "restart")
    one = players.take(conn, player["id"])
    assert one["id"] == first and one["kind"] == "ping"
    assert players.take(conn, player["id"])["id"] == second
    assert players.take(conn, player["id"]) is None
    players.finish(conn, player["id"], first, True, "pong")
    with pytest.raises(players.PlayerError):
        players.finish(conn, player["id"], first, True)                         # already answered
    assert [c["status"] for c in players.recent_commands(conn, player["id"])] == ["sent", "done"]


def test_the_same_command_is_not_queued_twice_but_a_ping_can_be(conn):
    player, _ = add(conn)
    assert players.send(conn, player["id"], "update") == players.send(conn, player["id"], "update")
    assert players.send(conn, player["id"], "ping") != players.send(conn, player["id"], "ping")


def test_a_player_cannot_answer_another_players_command(conn):
    (a, _), (b, _) = add(conn, "a"), add(conn, "b")
    command = players.send(conn, a["id"], "ping")
    players.take(conn, a["id"])
    with pytest.raises(players.PlayerError):
        players.finish(conn, b["id"], command, True)


def test_old_commands_expire(conn):
    player, _ = add(conn)
    conn.execute("INSERT INTO player_command (player_id, kind, created_at) VALUES (?, 'restart', ?)", (player["id"], T0))
    assert players.take(conn, player["id"], now="2026-01-01 12:30:00") is None
    assert players.recent_commands(conn, player["id"])[0]["status"] == "expired"
    command = players.send(conn, player["id"], "ping")
    players.take(conn, player["id"], now=T0)
    conn.execute("UPDATE player_command SET sent_at = ? WHERE id = ?", (T0, command))
    players.expire(conn, now="2026-01-01 12:10:00")
    assert conn.execute("SELECT status FROM player_command WHERE id = ?", (command,)).fetchone()[0] == "expired"


def test_unknown_commands_and_revoked_players_are_refused(conn):
    player, _ = add(conn)
    with pytest.raises(players.PlayerError, match="no command"):
        players.send(conn, player["id"], "format-disk")
    players.revoke(conn, player["id"])
    with pytest.raises(players.PlayerError, match="revoked"):
        players.send(conn, player["id"], "ping")


def test_log_is_kept_latest_only_and_closes_the_request(conn):
    player, _ = add(conn)
    players.request_log(conn, player["id"])
    assert players.get(conn, player["id"])["log_requested"] == 1
    players.store_log(conn, player["id"], "line 1", now=T0)
    players.store_log(conn, player["id"], "line 2", now=T0)
    assert players.stored_log(conn, player["id"])["content"] == "line 2" and players.get(conn, player["id"])["log_requested"] == 0


# ---------------------------------------------------------------- the player API
def call(client, path, token, body=None):
    return client.post(f"/api/player/v1{path}", json=body or {}, headers={"Authorization": f"Bearer {token}"} if token else {})


def test_the_api_needs_a_valid_player_token(client, conn):
    player, token = add(conn)
    conn.commit()
    assert call(client, "/poll", None).status_code == 401
    assert call(client, "/poll", "sp_nope").status_code == 401
    players.set_enabled(conn, player["id"], False)
    conn.commit()
    assert call(client, "/poll", token).status_code == 403


def test_register_then_poll_delivers_a_command_and_the_answer_is_recorded(client, conn):
    player, token = add(conn)
    conn.commit()
    assert call(client, "/register", token, {"protocol": 99}).status_code == 426
    out = call(client, "/register", token, {"protocol": players.PROTOCOL, "version": "1.4.0", "capabilities": {"mpv": "0.37"}}).get_json()
    assert out["ok"] and out["wait_seconds"] > 0
    empty = call(client, "/poll", token, {"wait": 0, "status": {"load": 0.1}}).get_json()
    assert empty["command"] is None and empty["send_log"] is False
    conn.execute("COMMIT") if conn.in_transaction else None
    from synchub import db
    with db.connection() as c:
        command_id = players.send(c, player["id"], "ping")
    got = call(client, "/poll", token, {"wait": 0}).get_json()
    assert got["command"] == {"id": command_id, "kind": "ping", "payload": {}}
    assert call(client, f"/commands/{command_id}/answer", token, {"ok": True, "result": "pong"}).get_json()["ok"]
    assert call(client, f"/commands/{command_id}/answer", token, {"ok": True}).status_code == 409
    with db.connection() as c:
        assert players.get(c, player["id"])["status"] == {"load": 0.1}
        assert players.recent_commands(c, player["id"])[0]["result"] == "pong"


def test_a_held_poll_returns_as_soon_as_a_command_is_queued(app, client, conn, monkeypatch):
    from synchub.web import player_api
    monkeypatch.setattr(player_api, "STEP_SECONDS", 0.05)
    player, token = add(conn)
    conn.commit()
    from synchub import db

    def queue_later():
        time.sleep(0.3)
        with db.connection() as c:
            players.send(c, player["id"], "restart")
    threading.Thread(target=queue_later, daemon=True).start()
    started = time.monotonic()
    got = call(client, "/poll", token, {"wait": 10}).get_json()
    assert got["command"]["kind"] == "restart" and time.monotonic() - started < 5


def test_a_log_request_reaches_the_player_and_its_upload_is_stored(client, conn):
    player, token = add(conn)
    players.request_log(conn, player["id"])
    conn.commit()
    assert call(client, "/poll", token, {"wait": 0}).get_json()["send_log"] is True
    assert call(client, "/log", token, {"log": "hello"}).get_json()["ok"]
    assert call(client, "/poll", token, {"wait": 0}).get_json()["send_log"] is False
    from synchub import db
    with db.connection() as c:
        assert players.stored_log(c, player["id"])["content"] == "hello"


# ---------------------------------------------------------------- the Players tab
def test_the_tab_lists_players_and_is_in_the_menu(client, conn):
    player, token = add(conn, "den", "Den TV")
    conn.commit()
    page = client.get("/players").get_data(as_text=True)
    assert 'href="/players"' in page and "Add player" in page and "den" in page and "Den TV" in page and "Never connected" in page
    assert token not in page
    assert client.get("/players/fragment").get_data(as_text=True).count('id="playerTable"') == 1


def test_an_empty_tab_says_what_to_do(client):
    assert "No players yet" in client.get("/players").get_data(as_text=True)


def test_creating_a_player_shows_the_setup_command_with_the_token_once(client, v2):
    out = client.post("/api/players", json={"name": "den", "tv_name": "Den TV"}).get_json()
    assert out["ok"] and out["token"].startswith("sp_") and f"--token {out['token']}" in out["setup"] and "setup-player.sh" in out["setup"]
    assert out["token"] not in client.get("/players").get_data(as_text=True)
    assert client.post("/api/players", json={"name": "den"}).get_json()["ok"] is False


def test_actions_from_the_tab(client, v2):
    player_id = client.post("/api/players", json={"name": "den"}).get_json()["id"]
    assert client.post(f"/api/players/{player_id}/ping").get_json()["ok"]
    assert client.post(f"/api/players/{player_id}/update").get_json()["ok"]
    assert client.post(f"/api/players/{player_id}/restart").get_json()["ok"]
    page = client.get("/players").get_data(as_text=True)
    assert "3 waiting" in page
    rotated = client.post(f"/api/players/{player_id}/rotate").get_json()
    assert rotated["token"].startswith("sp_") and rotated["token"] in rotated["setup"]
    assert client.post(f"/api/players/{player_id}/log").get_json()["ok"]
    assert client.get(f"/api/players/{player_id}/log").get_json()["pending"] is True
    assert client.post(f"/api/players/{player_id}/edit", json={"name": "den2", "tv_name": "x"}).get_json()["ok"]
    assert client.post(f"/api/players/{player_id}/revoke").get_json()["ok"]
    assert client.post(f"/api/players/{player_id}/ping").get_json()["ok"] is False
    assert client.post(f"/api/players/{player_id}/nonsense").status_code == 404
    assert client.post(f"/api/players/{player_id}/delete").get_json()["ok"]
    assert client.post(f"/api/players/{player_id}/ping").status_code == 404


def test_the_player_api_is_not_behind_the_access_token_or_csrf(v2, monkeypatch):
    from synchub.web import create_app
    monkeypatch.setenv("SYNCHUB_ACCESS_TOKEN", "secret")
    app = create_app(csrf=True)
    app.config["TESTING"] = True
    assert app.test_client().post("/api/player/v1/poll", json={}).status_code == 401           # asked for the player token, not blocked earlier
