# Backend changes the new templates rely on

I only had the templates, not your Flask routes, so these are sketches. Adapt names to your code.

## 1. REQUIRED before deploying `settings.html`: keep saved secrets when a field is blank

The settings page no longer puts API keys or Apprise URLs into the HTML. Secret fields come back **blank**
unless the user typed a new value. If `/tools/update-settings` writes every submitted field, saving the form
would wipe all your keys. Skip blank secrets:

```python
SECRET_FIELDS = {"tmdb_api_key", "jellyfin_api_key", "radarr_api_key", "sonarr_api_key", "apprise_urls"}

@app.route("/tools/update-settings", methods=["POST"])
def update_settings():
    for key, value in request.form.items():      # .items() returns the FIRST value per key
        if key == "csrf_token":
            continue
        if key in SECRET_FIELDS and not value.strip():
            continue                              # blank = keep what is saved
        save_setting(key, value.strip())          # your existing persistence call
    ...
```

Checkboxes: each switch posts the checkbox ("1") followed by a hidden "0". `request.form.get(name)` returns
"1" when checked and "0" when not, so you can drop any "missing means off" handling.

Username boxes still post a JSON list string (`["a","b"]`), same as before; the browser converts it.

## 2. CSRF protection (recommended)

```bash
pip install flask-wtf
```
```python
from flask_wtf import CSRFProtect
app.secret_key = "..."          # must be set, and kept stable
CSRFProtect(app)
```
The templates already emit the token (`{{ csrf_field() }}`, plus a `<meta name="csrf-token">` for fetch calls).
They render nothing until CSRFProtect is on, so this is safe to enable later.

## 3. Optional endpoints (the UI degrades quietly without them)

**`GET /api/logs?tail=N`**: return only the last N lines (and redact secrets):
```python
tail = min(int(request.args.get("tail", 500)), 5000)
return jsonify(logs=[redact(l) for l in read_log_lines()[-tail:]])
```

**`GET /api/tasks`**: powers live status and disabled buttons on the Tools page:
```python
return jsonify(tasks=task_statuses)   # {"scraper": {"status": "...", "message": "...", "last_run": "..."}, ...}
```

**`POST /api/test-connection/<service>`**: for the "Test connection" buttons. Body is `{"fields": {...}}`;
a blank key means "use the saved one". Respond `{"ok": true|false, "message": "..."}`.
Services: `tmdb`, `jellyfin`, `radarr`, `sonarr`, `notifications`. Typical checks: Jellyfin `GET {url}/System/Info`
with `X-Emby-Token`; Radarr/Sonarr `GET {url}/api/v3/system/status` with `X-Api-Key`; TMDB
`GET https://api.themoviedb.org/3/configuration?api_key=...`; notifications: send one Apprise test message.

## 4. Optional data fields

- `film.queued` (truthy when a missing item is already in Radarr/Sonarr) enables the dashboard's "Queued" status.
  Without it everything non-Jellyfin shows as Missing, as before.
- `item.media_type` on `unmatched_films` lets the Fix & Match chips focus the TVDB field for TV shows.

## 5. Flash categories

`base.html` maps `error` to `danger`, `message` to `info` and `warn` to `warning`, so existing `flash(...)` calls work.
