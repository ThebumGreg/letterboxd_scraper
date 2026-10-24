"""What's-new notices shown inside the remote. Add an entry when a feature ships; each browser shows it once
(until dismissed) and the Settings page keeps the full list."""

NOTICES = [
    {"id": "2026-10-my-apps", "date": "2026-10-24", "href": "/settings", "action": "Choose apps",
     "title": "Choose which apps appear on your remote",
     "body": "Open Settings to pick the apps you want to see for each TV. Anything you hide stays installed; "
             "it just no longer crowds your remote."},
    {"id": "2026-10-tv-management", "date": "2026-10-24", "href": "/tvs", "action": "Manage TVs",
     "title": "Add TVs from the app",
     "body": "The new TVs page finds Roku and Google TV devices on your network and adds them with one tap, "
             "so devices.json no longer has to be edited by hand."},
]
