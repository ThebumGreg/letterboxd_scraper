# SyncHub — Notifications design

Status: **Phase 1 and most of Phase 2 built** (per-person rules, quiet hours, grouping, request-available, job, worker, share and TV alerts; not yet: request status steps, request failed, new-episode and check-in notices; migrations `011` and `014`). **Phase 1 was built** on the `feature/notifications` branch (the foundation and Pushover; nothing is sent about requests or episodes yet). Phases 2-5 below are planned. Pushover facts in section 2 were read from <https://pushover.net/api> on 2026-10-25.

Goal: tell each person about the things they asked SyncHub for (a title they wanted is available, a request failed, a new episode of a show they follow) and tell administrators when something needs attention, through whatever delivery method each person prefers. Pushover is first; email, SMS and an iMessage gateway must be addable later as one new file each, without touching the rest.

## 1. Decisions

| Topic | Decision |
|---|---|
| Pushover model | The administrator registers **one** Pushover application for this installation and pastes its token in Settings. Each person pastes only their own **user key**. (Pushover advises every installation of redistributed software to use its own token, so none ships in the repository or the image.) |
| Whose list | A person "wants" a title exactly as `catalog.demand()` already says: on their Letterboxd/Serializd watchlist and not watched, or requested from Search. |
| Administrators | The profiles the administrator picks in Settings. There is no separate role. Admin alerts go to those profiles' own delivery methods. |
| Secrets | Stored like the other API keys (plaintext in SQLite, masked in every page, never sent back to a browser, kept out of logs). Backups contain them. Encryption at rest would need a key kept outside the database and a new dependency; it is a possible later step. |
| Show "available" | Fires when the first episode of a requested show reaches the library; later episodes arrive as new-episode notifications. |
| Providers a person may add | Providers that cannot make the server connect to an address the person chose. Apprise (arbitrary URLs) is therefore limited to the administrator's own target. |

## 2. What Pushover requires (from its documentation)

* Two credentials: an **application token** (30 characters, `[A-Za-z0-9]`, registered once, free) and the recipient's **user key** (30 characters, same alphabet). A **group key** looks identical and works the same. The documentation describes no call that creates a token or finds one from a user key.
* Send: `POST https://api.pushover.net/1/messages.json`, form-encoded, with `token`, `user`, `message`. Optional: `title`, `device` (up to 25 characters of `[A-Za-z0-9_-]`; a missing or disabled device falls back to all of the person's devices), `url` + `url_title`, `priority` (-2 to 2), `sound`, `timestamp`, `ttl`, `html`.
* Limits: message 1024 characters, title 250, URL 512, URL title 100.
* Priority: -2 no notification, -1 no sound, 0 normal, 1 bypasses the person's Pushover quiet hours, 2 emergency (needs `retry` of at least 30 s and `expire` of at most 10800 s; returns a `receipt`). A normal message that arrives during the person's Pushover quiet hours is delivered as -1.
* `ttl` deletes the message from the devices after that many seconds (ignored for priority 2). `timestamp` shows the original time of a message that was held or retried.
* Answers: HTTP 200 with `"status":1` and a `request` id means queued. **Any 4xx, or a `status` other than 1, means the input was wrong and repeating it can never work.** 429 means the monthly quota is used up. 5xx or no answer means a temporary problem: retry, but no sooner than 5 seconds later.
* Be kind to the API: at most **2 concurrent connections**; many 4xx answers within a few minutes get the server's IP **blocked** for a while.
* Quota: **10,000 messages a month for the whole Pushover account**, shared by all its applications. One message is one successful call to one user (a group key counts once per member). It resets at 00:00 Central Time on the 1st. Each answer carries `X-Limit-App-Limit`, `X-Limit-App-Remaining` and `X-Limit-App-Reset`; `GET /1/apps/limits.json?token=...` returns the same.
* Validation: `POST /1/users/validate.json` with `token`, `user` and an optional `device` answers `status` 1 when the account is valid and has an active device, with `devices` (names) and `licenses` (platforms).

What this means for SyncHub: check key shapes locally before any call (a typo must not cost a 4xx), never retry a 4xx, switch a recipient off when Pushover says the user is invalid, run one sender at a time, watch the quota and hold non-urgent messages when it is nearly used, and never show an exception's text (a failed `requests` call quotes the URL, and the limits call carries the token in its query string).

## 3. Architecture

`synchub/notifications/`

| Module | Role |
|---|---|
| `events.py` | The event registry (in code). An event type has a code, label, audience (user or admin), default priority and a `render(payload)` that builds the message. Adding an event is adding an entry. |
| `providers/base.py` | `Message`, `Result` and the `Provider` base class: declared config fields (the Settings form is built from them), `validate`, `check`, `send`. |
| `providers/pushover.py`, `providers/apprise_provider.py` | The providers. A new method (email, SMS, an iMessage gateway) is one file and one `register` line. |
| `store.py` | Delivery methods ("channels") per profile, their masking, the history, retention. |
| `emit.py` | `enqueue()`: records an event once (duplicates are refused) and one delivery row per recipient channel. Database writes only, never the network. |
| `dispatch.py` | Sends due deliveries outside any transaction, one at a time, with retry and backoff, expiry, the quota guard and the "this channel is dead" rule. Runs as a scheduler job, so `SYNCHUB_SCHEDULER=0` silences it. |

Rules that follow from the rest of SyncHub: no network call while a write transaction is open (`db.assert_not_in_write_transaction`); the scheduler job is the only sender of real notifications; tests mock `synchub.http.request` and never reach the network.

## 4. Database

Migration `011_notifications.sql` (Phase 1):

| Table | Holds |
|---|---|
| `notification_channel` | A person's delivery method: `profile_id` (cascade-deleted with the profile), `provider`, `label`, `config` JSON (Pushover: `user_key`, `device`), `enabled`, `verified_at`, `last_ok_at`, `last_error`, `failures`. |
| `notification_event` | What happened, once: `event_type`, `dedupe_key`, `payload` JSON. `UNIQUE (event_type, dedupe_key)` is the duplicate guard. |
| `notification` | One row per recipient channel and event: the rendered `title`, `body`, `url`, `priority`, `ttl_seconds`; `status` (`pending`, `held`, `sent`, `failed`, `dropped`); `attempts`, `next_attempt_at`, `expires_at`, `sent_at`, `provider_ref`, `error`. `UNIQUE (event_id, channel_id)`. This is the outbox and the history. |

Phase 2 adds `notification_rule` (profile, event type, channel, options) and `notification_profile_pref` (quiet hours).

Settings (`app_setting`): `enable_notifications` (the administrator's master switch), `pushover_app_token` (secret), `pushover_quota` (last seen limit/remaining/reset), `notify_history_keep_days`.

## 5. Events and where they come from (Phases 2-4)

| Event | Source |
|---|---|
| Request available / status (queued, downloading, processing) | `states.compute()` will record each item's old and new state; transitions go to `emit`. A first-ever state is not a transition, so the first sync does not notify for the whole library. |
| Request failed or needs attention | Failed and import-blocked Radarr/Sonarr queue entries (currently dropped by `states.QUEUE_IGNORED`) and auto-add errors from `automation`. |
| New episode available / aired | Episode transitions to `in_library`; a periodic check of `episode.air_date`. |
| Job failed, service unreachable, DDNS failed | `tasks.run()` failures, `tasks.poll_queue()` failure streaks, `ddns.run_update()`, each with a cooldown and a "recovered" message. |
| Worker offline, job needs a decision or failed | `workers.is_online()` changes, `jobs.fail()`, `jobs.reap()`. |
| Periodic check-in | A scheduled summary. |

Recipients' rating limits apply: a profile never gets a message naming a title above its limit.

## 6. Access

A new permission, **Notifications**, opens the page where a person manages their own delivery methods. Because anyone on the network can choose a profile that has no PIN, once any profile has a PIN a profile **without** one cannot manage notifications either; and stored keys are never sent back to a browser (a masked tail only), so choosing someone's profile reveals nothing. Everything is open until some profile has a PIN, exactly as for the rest of SyncHub.

## 7. Trying phase 1 with a real Pushover account

The tests use a pretend Pushover, so the first real send needs you:

1. At pushover.net/apps/build create an application ("SyncHub"). Copy its API token.
2. **Settings, Connections, Notifications**: paste the token, press **Test token** (it should say how many messages are left this month), turn on **Send notifications** and save the tab.
3. **Notifications**: paste your own user key (top of the Pushover app), press **Check** (your devices should be offered), **Add**, then **Send test**. The message should arrive within a few seconds and the history should say Sent.
4. Try the failures: a key with one character changed (refused: invalid), the token cleared in Settings (the page says Pushover is not set up), a test with the container offline (a clear error, nothing repeated).
5. With a PIN set on one profile, choose a profile without a PIN: Notifications explains why it cannot be changed.

## 8. Phases

1. **Foundation** (this phase): migration, registry, providers, outbox and dispatcher, the Pushover token and its test, "Notifications" page (add and verify a delivery method, send a test, history).
2. Request events, rules per person, quiet hours, batching, rating filter.
3. Episode events.
4. Administrator events, check-ins, recovery messages, security alerts.
5. Digests, further ideas, polish.
