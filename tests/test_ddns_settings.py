import app as app_module


def _seed(db):
    db.set_setting("dynu_domain", "home.dynu.net")
    db.set_setting("dynu_api_key", "super-secret-key")
    db.set_setting("dynu_dns_id", "12345")
    db.set_setting("dynu_record_id", "Root Domain")


def test_saving_with_blank_key_keeps_key_and_dns_ids(client, db, monkeypatch):
    _seed(db)
    monkeypatch.setattr(app_module, "fetch_and_cache_dynu_metadata", lambda *a: (_ for _ in ()).throw(AssertionError("should not refetch")))

    client.post("/ddns/update-settings", data={"dynu_hostname": "home.dynu.net", "dynu_api_key": "", "dynu_enabled": "1"})

    assert db.get_setting("dynu_api_key") == "super-secret-key"
    assert db.get_setting("dynu_dns_id") == "12345"
    assert db.get_setting("dynu_enabled") == "1"


def test_changing_domain_with_blank_key_uses_saved_key_and_refetches(client, db, monkeypatch):
    _seed(db)
    fetched = []
    monkeypatch.setattr(app_module, "fetch_and_cache_dynu_metadata", lambda domain, key: fetched.append((domain, key)))

    client.post("/ddns/update-settings", data={"dynu_hostname": "other.dynu.net", "dynu_api_key": ""})

    assert db.get_setting("dynu_api_key") == "super-secret-key"
    assert db.get_setting("dynu_domain") == "other.dynu.net"
    assert db.get_setting("dynu_dns_id") == ""
    assert fetched == [("other.dynu.net", "super-secret-key")]


def test_new_key_replaces_old_and_clears_cached_ids(client, db, monkeypatch):
    _seed(db)
    monkeypatch.setattr(app_module, "fetch_and_cache_dynu_metadata", lambda *a: None)

    client.post("/ddns/update-settings", data={"dynu_hostname": "home.dynu.net", "dynu_api_key": "brand-new-key"})

    assert db.get_setting("dynu_api_key") == "brand-new-key"
    assert db.get_setting("dynu_dns_id") == ""
