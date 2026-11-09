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

All of it is on **Settings, TV dashboard** (its own tab). The **UniFi Network** connection stays on **Settings, Connections**, because it is an integration. The tab has a status bar at the top (on the TV now or not, the last problem, whose phone is home, and the Show / Close buttons) and sections that fold open and shut: **Screen**, **People** (one card per person, closed until you click it, with a one-line summary), **Weather and extras**, **School days for everyone** and **Welcome home**.

1. Switch it on, choose the **TV** and the **browser app**, set **Show from / Until** and the **Days**. Weekdays are ticked by default; tick Sat and Sun for weekends.
2. Type your **ZIP code** (or latitude and longitude) and press **Test weather**. Choose US (°F, mph) or metric.
3. **People.** Add a *calendar person* for each adult: paste their private calendar link and press **Test**.
   - Google Calendar: *Settings, (the calendar), Integrate calendar, Secret address in iCal format*.
   - iCloud: in Calendar, share the calendar as a **public calendar** and copy the link (webcal://...).
   - Microsoft 365 / Outlook: *Settings, Calendar, Shared calendars, Publish a calendar*, choose *Can view all details*, copy the **ICS** link.
   - Add a *student* for each child at school: the leave-by, school start and end times for each school day (leave a day blank for no school).
   To save typing: fill in one day and use **Copy to...** on that row (all other weekdays, or one day); **Duplicate** makes a new person with the same school week (not the phone) to edit; **Copy the week from** copies another student's week. All of it stays editable.
   Anyone with a calendar link can read that calendar, so treat the link like a password. If your account cannot publish an ICS link (some work accounts), that person cannot be added yet.
4. **No school days**: each student has their own list and an optional **school calendar link** (an ICS address from the school or district website: all-day events named *No school*, *Holiday*, *Break*, *Staff development* and the like close that student's day, and *Minimum day* or *Early release* events show as a note on the screen). A school that only publishes a PDF calendar needs its dates typed in once a year. The list under **No school days for everyone** applies to all students. One line per date or range, with a name (`2026-11-26 Thanksgiving`, `2026-12-21 to 2027-01-03 Winter break`). Students then show the name instead of a school day.
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

## Alerts, hints, countdowns

- **Weather alerts:** active US National Weather Service alerts of moderate or worse severity for your ZIP code show as a banner (nothing shows outside the US). A line such as *Take an umbrella* or *Cold this morning: take a jacket* appears under the forecast.
- **Countdowns and birthdays:** one per line in Settings, `2026-12-21 Winter break` (once) or `03-14 Sam's birthday` (every year). The next three within 90 days are shown at the bottom.
- **Everyone has left for school** shows once every student with a watched phone has left. The option *Close the dashboard early...* then closes the screen for the morning.

## Welcome home

When someone's phone joins the home Wi-Fi after being away, the TV shows a full-screen "Welcome home, Sam!" for a couple of minutes, then goes back to what it was.

- **Who:** anyone (adult or student) with a phone set under People. Each person can switch it off, and give it a line of their own ("Dinner at 6"). Under the greeting it shows their next event (or tomorrow's school), a weather hint and the next countdown. Two people arriving together get one page. On a birthday (a countdown line that says "birthday", such as `03-14 Sam's birthday`) it says "Happy birthday".
- **What counts as arriving:** the phone was away for at least 15 minutes (setting) and is back on the Wi-Fi. SyncHub asks UniFi about once a minute, all day (one call). A phone that was never seen, or that napped for a few minutes, does not count. Quiet hours (10 pm to 6 am by default) and "not again within 60 minutes" stop repeats.
- **The TV:** it never interrupts a film, a camera or another app. If the TV is in standby it is woken (a switch turns that off), and put back to sleep afterwards. If the morning or evening screen is already up, the welcome appears over it. Display time is 2 minutes by default.
- **Seeing why:** the Welcome home section lists what happened lately ("Sam came home: quiet hours, nothing shown"). **Try it on the TV now** shows it for the first person.
- Needs each person's phone set to use its real Wi-Fi address (same note as for students). Only "this phone is on the home Wi-Fi" is known, never where anyone is. A phone on mobile data shows as home a few seconds to a minute after it joins the Wi-Fi.

## The evening screen (tomorrow)

Switch it on in Screen. Between 8 and 10 pm (setting) on Sunday to Thursday (setting) the same page shows **tomorrow**: tomorrow's weather and a hint ("Rain tomorrow: pack umbrellas"), each student's leave-by time or no school, and each calendar person's events. **Show tomorrow now** and the "tomorrow screen" link let you try it any time.

## Weather extras, birthdays and the bottom line

The weather box also shows sunrise and sunset, the moon phase (worked out in SyncHub), the UV index and the air quality (free Open-Meteo services), with a hint such as "UV high: sunscreen". On a birthday the screen has a celebration banner. The line at the bottom can be a quote, a dad joke, a fun fact, "this day in history" or a different one each day; each has a built-in list for when the internet is down.

## What happens at the start and the end

- At the start SyncHub checks the TV. If a **Jellyfin title is playing**, a **camera is showing**, or **another app is open** (and *Leave the TV alone* is ticked, the default) it does nothing and does not try again that morning. Otherwise it wakes the TV and opens the page. It tries three times, 90 seconds apart, and the last problem is shown on the Settings page.
- Pressing **Close on the TV** (or closing it by hand) stops it opening again that morning.
- At the end it closes the page only if the page is the app on screen, and puts the TV to sleep only if the dashboard woke it from standby.
- **Show on the TV now** ignores the busy check.

## What is not built yet

- Holiday handling beyond the typed *No school days* list (no school-calendar link yet).
- Family photos, a chore chart, doorbell pop-ups and the TV player PC version (the page is the same one the PC will show).

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
