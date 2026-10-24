#!/usr/bin/env python3
"""Whole-house remote control web UI (TVs + Jellyfin). See remote_control/ and devices.example.json.

  export JELLYFIN_URL=http://192.168.128.55:5000
  export JELLYFIN_API_KEY=xxxxxxxx
  cp devices.example.json devices.json    # then edit
  python remote_control_app.py            # listens on 0.0.0.0:5000
"""
from remote_control.app import main

if __name__ == "__main__":
    main()
