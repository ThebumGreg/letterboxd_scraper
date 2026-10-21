import pytest


@pytest.mark.parametrize("path", ["/", "/tools", "/settings", "/resolution", "/ddns", "/payloads", "/logs"])
def test_pages_render(client, path):
    assert client.get(path).status_code == 200


def test_settings_page_lists_profiles_after_changes(client, db):
    db.save_user_profile_mapping("Alice", "alice_lb")
    assert "Alice" in client.get("/settings").get_data(as_text=True)
