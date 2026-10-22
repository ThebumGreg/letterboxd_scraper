#!/usr/bin/env python3
"""Check whether a Jellyfin client (e.g. Swiftfin on Apple TV) accepts remote control.

Steps:
  1. List sessions and print each one's remote-control capabilities.
  2. Pick the target session (by --device / --client substring, or --session-id).
  3. Optionally send Pause/Unpause (--pause-test) and Play (--play ITEM_ID) to it.

Usage:
  export JELLYFIN_URL=http://192.168.1.10:8096
  export JELLYFIN_API_KEY=xxxxxxxx
  python jellyfin_remote_test.py                      # list sessions only
  python jellyfin_remote_test.py --device "Apple TV" --pause-test
  python jellyfin_remote_test.py --device "Apple TV" --play <itemId>

Start something playing in Swiftfin first so its session shows up.
"""
import argparse
import os
import sys
import time

import requests

TIMEOUT = 10


def headers(token):
    return {
        "Accept": "application/json",
        "Authorization": (
            'MediaBrowser Client="RemoteTest", Device="Script", '
            f'DeviceId="remote-test-01", Version="1.0.0", Token="{token}"'
        ),
    }


def list_sessions(url, token, user_id=None):
    params = {"controllableByUserId": user_id} if user_id else {}
    r = requests.get(f"{url}/Sessions", headers=headers(token), params=params, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def describe(s):
    cmds = s.get("SupportedCommands") or []
    return (
        f"- id={s.get('Id')}\n"
        f"    client={s.get('Client')}  device={s.get('DeviceName')}  user={s.get('UserName')}\n"
        f"    SupportsRemoteControl={s.get('SupportsRemoteControl')}  "
        f"SupportsMediaControl={s.get('SupportsMediaControl')}\n"
        f"    SupportedCommands={cmds or '[]'}\n"
        f"    NowPlaying={(s.get('NowPlayingItem') or {}).get('Name')}  "
        f"Paused={(s.get('PlayState') or {}).get('IsPaused')}"
    )


def pick(sessions, session_id, device, client):
    if session_id:
        return next((s for s in sessions if s.get("Id") == session_id), None)
    for s in sessions:
        if device and device.lower() not in (s.get("DeviceName") or "").lower():
            continue
        if client and client.lower() not in (s.get("Client") or "").lower():
            continue
        if device or client:
            return s
    return None


def send(url, token, label, path, params=None):
    r = requests.post(f"{url}{path}", headers=headers(token), params=params, timeout=TIMEOUT)
    print(f"  {label}: HTTP {r.status_code}")
    return r.status_code


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default=os.environ.get("JELLYFIN_URL"))
    p.add_argument("--api-key", default=os.environ.get("JELLYFIN_API_KEY"))
    p.add_argument("--user-id", help="only sessions controllable by this user")
    p.add_argument("--session-id")
    p.add_argument("--device", help="substring of DeviceName, e.g. 'Apple TV'")
    p.add_argument("--client", help="substring of Client, e.g. 'Swiftfin'")
    p.add_argument("--pause-test", action="store_true", help="send Pause then Unpause")
    p.add_argument("--play", metavar="ITEM_ID", help="send PlayNow for this item")
    args = p.parse_args()

    if not args.url or not args.api_key:
        sys.exit("Set JELLYFIN_URL and JELLYFIN_API_KEY (or pass --url/--api-key).")
    url = args.url.rstrip("/")

    sessions = list_sessions(url, args.api_key, args.user_id)
    print(f"{len(sessions)} session(s):")
    for s in sessions:
        print(describe(s))

    if not (args.pause_test or args.play):
        return

    target = pick(sessions, args.session_id, args.device, args.client)
    if not target:
        sys.exit("\nNo matching session. Use --device/--client/--session-id.")
    sid = target["Id"]
    print(f"\nTarget: {target.get('Client')} on {target.get('DeviceName')} ({sid})")
    if not target.get("SupportsRemoteControl"):
        print("  WARNING: SupportsRemoteControl is false; the server will likely drop commands.")

    if args.pause_test:
        print("Pause/Unpause (watch the TV; HTTP 204 only means the server accepted it):")
        send(url, args.api_key, "Pause", f"/Sessions/{sid}/Playing/Pause")
        time.sleep(3)
        send(url, args.api_key, "Unpause", f"/Sessions/{sid}/Playing/Unpause")

    if args.play:
        print("PlayNow:")
        send(url, args.api_key, "Play", f"/Sessions/{sid}/Playing",
             {"playCommand": "PlayNow", "itemIds": args.play, "startPositionTicks": 0})

    print("\nIf the server returned 204 but nothing happened on the TV, the client ignores remote control.")


if __name__ == "__main__":
    main()
