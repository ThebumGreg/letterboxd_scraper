def _profile(db, name):
    return next(p for p in db.get_user_profile_mappings() if p["profile_name"] == name)


def test_partial_save_keeps_existing_fields(db):
    db.save_user_profile_mapping("Alice", "alice_lb", "alice_sz", "jf-old")
    db.toggle_profile_mapping(_profile(db, "Alice")["id"], 0)

    db.save_user_profile_mapping("Alice", jellyfin_user_id="jf-new")   # what the Jellyfin import posts

    p = _profile(db, "Alice")
    assert (p["letterboxd_username"], p["serializd_username"], p["jellyfin_user_id"], p["is_active"]) == ("alice_lb", "alice_sz", "jf-new", 0)


def test_full_edit_can_blank_a_field_and_does_not_reactivate(db):
    db.save_user_profile_mapping("Bob", "bob_lb", "bob_sz", "jf")
    db.toggle_profile_mapping(_profile(db, "Bob")["id"], 0)

    db.save_user_profile_mapping("Bob", "bob_lb", "", "jf")   # edit modal: serializd cleared on purpose

    p = _profile(db, "Bob")
    assert p["serializd_username"] == "" and p["is_active"] == 0


def test_new_profile_defaults(db):
    db.save_user_profile_mapping("  Carol  ", jellyfin_user_id="jf-c")
    p = _profile(db, "Carol")
    assert (p["letterboxd_username"], p["serializd_username"], p["is_active"]) == ("", "", 1)


def test_jellyfin_import_route_does_not_wipe_usernames(client, db):
    db.save_user_profile_mapping("Alice", "alice_lb", "alice_sz")
    client.post("/settings/profiles/save", data={"profile_name": "Alice", "jellyfin_user_id": "jf-1"})
    p = _profile(db, "Alice")
    assert (p["letterboxd_username"], p["serializd_username"], p["jellyfin_user_id"]) == ("alice_lb", "alice_sz", "jf-1")


def test_edit_route_overwrites_posted_fields(client, db):
    db.save_user_profile_mapping("Alice", "alice_lb", "alice_sz", "jf-1")
    client.post("/settings/profiles/save", data={"profile_name": "Alice", "letterboxd_username": "new_lb", "serializd_username": "", "jellyfin_user_id": "jf-1"})
    p = _profile(db, "Alice")
    assert (p["letterboxd_username"], p["serializd_username"]) == ("new_lb", "")


def test_deleted_profile_is_not_recreated_on_restart(db, add_request):
    db.save_user_profile_mapping("Dana", "dana_lb")
    add_request("dana_lb", "some-film")
    pid = _profile(db, "Dana")["id"]

    db.delete_user_profile_mapping(pid)
    db.init_db()   # simulates an app restart

    assert db.get_user_profile_mappings() == []
    with db.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM user_requests").fetchone()[0] == 1   # requests are kept


def test_legacy_requests_are_seeded_into_profiles_once(db, add_request):
    add_request("legacy_user", "film-a")
    add_request("legacy_user", "film-b")
    with db.get_connection() as conn:
        conn.execute("DELETE FROM app_settings WHERE key = 'profile_seed_done'")

    db.init_db()
    profiles = db.get_user_profile_mappings()
    assert [p["profile_name"] for p in profiles] == ["legacy_user"]
    with db.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM user_requests WHERE profile_id = ?", (profiles[0]["id"],)).fetchone()[0] == 2

    db.delete_user_profile_mapping(profiles[0]["id"])
    db.init_db()
    assert db.get_user_profile_mappings() == []


def test_existing_install_with_profiles_is_not_reseeded(db, add_request):
    db.save_user_profile_mapping("Kept", "kept_lb")
    add_request("orphan_user", "film-x")
    with db.get_connection() as conn:
        conn.execute("DELETE FROM app_settings WHERE key = 'profile_seed_done'")   # DB created before this fix

    db.init_db()

    assert [p["profile_name"] for p in db.get_user_profile_mappings()] == ["Kept"]


def test_update_film_identifiers_keeps_dismissed_flag(db, add_request):
    add_request("u", "film-a")
    with db.get_connection() as conn:
        conn.execute("UPDATE user_requests SET ignore_match = 1")
        conn.execute("INSERT INTO service_cache (service, movie_slug, title, ignore_match) VALUES ('radarr', 'film-a', 'Film', 1)")

    db.update_film_identifiers("film-a", tvdb_id="555")

    with db.get_connection() as conn:
        assert conn.execute("SELECT ignore_match, tvdb_id FROM user_requests").fetchone()[:] == (1, "555")
        assert conn.execute("SELECT ignore_match FROM service_cache").fetchone()[0] == 1


def test_update_film_identifiers_can_set_ignore(db, add_request):
    add_request("u", "film-a")
    db.update_film_identifiers("film-a", set_ignore=True)
    with db.get_connection() as conn:
        assert conn.execute("SELECT ignore_match FROM user_requests").fetchone()[0] == 1
