"""Whole-house remote: control TVs/streamers and their Jellyfin playback from one web UI.

  logbook.py      server-side log of every outbound call (ADB, Roku ECP, Jellyfin)
  jellyfin.py     Sessions API client
  devices/        one driver per device type (androidtv, roku); add new ones there
  orchestrator.py wake device -> launch Jellyfin app -> wait for its session
  app.py          Flask routes
  static/         the web UI
"""
