# Morning TV dashboard

Every morning in a window you choose (for example 6:30 to 8:30) SyncHub wakes a TV and opens a full-screen page on it with:

- the time and date,
- the weather now and the next five days (Open-Meteo, no account needed),
- a quote of the day,
- a column for each person: their calendar for today (Google, iCloud or Microsoft 365 / Outlook), or for a student the school day with a "leave by" countdown and **Left for school** when their phone drops off the home Wi-Fi.

At the end of the window SyncHub closes the page, and puts the TV back to sleep if the dashboard was what woke it.

The page itself is made by SyncHub and loads nothing from the internet (no CDN, no fonts). SyncHub fetches the weather, quote and calendars and the TV only talks to SyncHub, so the screen still shows the time, school times and last-known data if the internet is down. Weather and quotes need the internet to refresh; a built-in list of quotes is used when the quote service is unreachable.

## What you need

- A **Google TV or Android TV** added on the TVs page, with **ADB on** (see `docs/TV_REMOTE.md`).
- A **TV browser app** on that TV that opens a web address full screen. Try **TV Bro** (`com.phlox.tvwebbrowser`, free); another browser works if you put its package name in Settings. This is the one part that depends on your TV: if the browser shows its own toolbar, try another one, or wait for the TV player PC (`plans/tv-control-nuc-plan.md`), which will show the same page without a browser.
- The **SyncHub address** set in Settings (the TV must be able to open it).
- A **time zone** on the SyncHub computer that matches yours (the page shows this computer's time zone under Days).

## Setting it up

All of it is in **Settings, Connections, Morning TV dashboard** (and **UniFi Network** just above it).

1. Switch it on, choose the **TV** and the **browser app**, set **Show from / Until** and the **Days**. Weekdays are ticked by default; tick Sat and Sun for weekends.
2. Type your **ZIP code** (or latitude and longitude) and press **Test weather**. Choose US (°F, mph) or metric.
3. **People.** Add a *calendar person* for each adult: paste their private calendar link and press **Test**.
   - Google Calendar: *Settings, (the calendar), Integrate calendar, Secret address in iCal format*.
   - iCloud: in Calendar, share the calendar as a **public calendar** and copy the link (webcal://...).
   - Microsoft 365 / Outlook: *Settings, Calendar, Shared calendars, Publish a calendar*, choose *Can view all details*, copy the **ICS** link.
   - Add a *student* for each child at school: the leave-by, school start and end times for each school day (leave a day blank for no school).
   To save typing: fill in one day and use **Copy to...** on that row (all other weekdays, or one day); **Duplicate** makes a new person with the same school week (not the phone) to edit; **Copy the week from** copies another student's week. All of it stays editable.
   Anyone with a calendar link can read that calendar, so treat the link like a password. If your account cannot publish an ICS link (some work accounts), that person cannot be added yet.
4. **No school days**: one line per date or range, with a name (`2026-11-26 Thanksgiving`, `2026-12-21 to 2027-01-03 Winter break`). Students then show the name instead of a school day.
5. Press **Save** (the *Save* button of the Connections tab), then **Show on the TV now** to try it. **Close on the TV** takes it off. The link under the settings opens the same page in your browser, so you can see it on a phone or computer first.

### Wi-Fi presence ("Left for school")

SyncHub asks the UniFi Network application which devices are on the Wi-Fi once a minute during the morning and shows a student as *Left for school* when their phone has gone.

1. In the **Network** application open *Settings, Control Plane, Integrations* and create an API key. The Network application has its own key; the one made in Protect does not work here. Network 9 or newer is needed.
2. In **UniFi Network (Wi-Fi presence)** paste the console address and the key, and press **Test connection**. It lists the Wi-Fi devices it can see.
3. On the student, press **Pick from UniFi** and choose their phone.

Rules, so a sleeping phone is not mistaken for someone leaving:
- The phone must first be **seen at home** that day.
- It must then be gone for **3 minutes** (setting), and only after **6:30** (setting).
- Once someone has left they stay *Left for school* until the next morning, even if the phone comes back.
- Only home Wi-Fi present or not is used. Nothing tracks where anyone is.
- Phones use a **private Wi-Fi address** per network by default, so UniFi may show a different address from the phone's own. For your home Wi-Fi either turn private addresses off on the phone, or pick the address UniFi shows and leave it.
- If UniFi cannot be reached the dashboard keeps working without presence and the Settings page shows the reason.

## What happens at the start and the end

- At the start SyncHub checks the TV. If a **Jellyfin title is playing**, a **camera is showing**, or **another app is open** (and *Leave the TV alone* is ticked, the default) it does nothing and does not try again that morning. Otherwise it wakes the TV and opens the page. It tries three times, 90 seconds apart, and the last problem is shown on the Settings page.
- Pressing **Close on the TV** (or closing it by hand) stops it opening again that morning.
- At the end it closes the page only if the page is the app on screen, and puts the TV to sleep only if the dashboard woke it from standby.
- **Show on the TV now** ignores the busy check.

## What is not built yet

- Holiday handling beyond the typed *No school days* list (no school-calendar link yet).
- Doorbell pop-ups and the TV player PC version (the page is the same one the PC will show).
- Quote categories (the daily quote comes from ZenQuotes).

## If something does not work

| Symptom | Likely cause |
|---|---|
| "Set the SyncHub address" | Fill in *SyncHub address* in Settings so the TV knows where to open the page. |
| The TV wakes but the browser shows a toolbar or the wrong page | Try another browser app, or open the page link in a browser on a phone to check it first. |
| Test connection for UniFi Network: "rejected the API key" | Make a key on the **Network** application's Integrations page. |
| "no UniFi Network API at that address" | Network 9 or newer is needed, and the address must be the console's own. |
| A calendar says "not a calendar (ICS) link" | The link is a web page, not the calendar file. Use the secret/public iCal address. |
| Wrong times | The SyncHub computer's time zone is wrong. |

> Written without a UniFi console, a TV browser or real calendar links to try. The UniFi Network calls follow UniFi's published Network API 11.0.97 (`GET /v1/sites`, `GET /v1/sites/{siteId}/clients`, client details for the MAC address when the list leaves it out), with a fallback to the older controller path. The first run on your hardware is the real test; the Test buttons and the TV log say which step failed.
