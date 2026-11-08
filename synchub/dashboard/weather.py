"""Current weather and the forecast from Open-Meteo (free, no key). A ZIP code is turned into a place once by zippopotam.us and remembered."""
import json
import re
import time

from synchub import http, settings
from synchub.dashboard import config

TTL = 900
_cache = {"at": 0, "key": None, "data": None}

# WMO weather codes -> (words, symbol)
CODES = {0: ("Clear", "☀️"), 1: ("Mostly clear", "🌤️"), 2: ("Partly cloudy", "⛅"), 3: ("Cloudy", "☁️"), 45: ("Fog", "🌫️"), 48: ("Fog", "🌫️"),
         51: ("Light drizzle", "🌦️"), 53: ("Drizzle", "🌦️"), 55: ("Heavy drizzle", "🌧️"), 56: ("Freezing drizzle", "🌧️"), 57: ("Freezing drizzle", "🌧️"),
         61: ("Light rain", "🌦️"), 63: ("Rain", "🌧️"), 65: ("Heavy rain", "🌧️"), 66: ("Freezing rain", "🌧️"), 67: ("Freezing rain", "🌧️"),
         71: ("Light snow", "🌨️"), 73: ("Snow", "🌨️"), 75: ("Heavy snow", "❄️"), 77: ("Snow grains", "🌨️"),
         80: ("Showers", "🌦️"), 81: ("Showers", "🌧️"), 82: ("Heavy showers", "⛈️"), 85: ("Snow showers", "🌨️"), 86: ("Snow showers", "❄️"),
         95: ("Thunderstorm", "⛈️"), 96: ("Thunderstorm, hail", "⛈️"), 99: ("Thunderstorm, hail", "⛈️")}


class WeatherError(Exception):
    pass


def describe(code):
    return CODES.get(code, ("", "🌡️"))


def locate(place):
    """-> {"lat", "lon", "name"} for a US ZIP code or 'latitude, longitude'. Remembered in a setting so the lookup happens once."""
    place = (place or "").strip()
    try:
        saved = json.loads(settings.get("dash_geo") or "{}")
    except ValueError:
        saved = {}
    if saved.get("for") == place and saved.get("lat") is not None:
        return saved
    pair = re.fullmatch(r"(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)", place)
    if pair:
        found = {"for": place, "lat": float(pair.group(1)), "lon": float(pair.group(2)), "name": ""}
    elif re.fullmatch(r"\d{5}", place):
        try:
            res = http.request("weather", "zip", "GET", f"https://api.zippopotam.us/us/{place}", timeout=10)
        except Exception:
            raise WeatherError("Could not look up the ZIP code (no internet?).")
        if res.status_code == 404:
            raise WeatherError(f"{place} is not a ZIP code zippopotam.us knows. Type your latitude and longitude instead, like 40.71, -74.00.")
        if res.status_code != 200:
            raise WeatherError(f"The ZIP code lookup failed: HTTP {res.status_code}")
        spot = (res.json().get("places") or [{}])[0]
        found = {"for": place, "lat": float(spot.get("latitude")), "lon": float(spot.get("longitude")), "name": f'{spot.get("place name", "")}, {spot.get("state abbreviation", "")}'.strip(", ")}
    else:
        raise WeatherError("Enter a 5 digit US ZIP code, or latitude and longitude like 40.71, -74.00.")
    settings.set_value("dash_geo", json.dumps(found))
    return found


SEVERE = {"Moderate", "Severe", "Extreme"}


def alerts(where):
    """Active US National Weather Service alerts of moderate or higher severity for this spot: [{"event", "severity", "headline"}]. Never raises (outside the US it just has none)."""
    try:
        res = http.request("weather", "alerts", "GET", "https://api.weather.gov/alerts/active", params={"point": f'{where["lat"]:.4f},{where["lon"]:.4f}'},
                           headers={"User-Agent": "SyncHub family dashboard", "Accept": "application/geo+json"}, timeout=8)
        if res.status_code != 200:
            return []
        found = [f.get("properties") or {} for f in (res.json().get("features") or [])]
    except Exception:
        return []
    order = {"Extreme": 0, "Severe": 1, "Moderate": 2}
    keep = sorted((a for a in found if a.get("severity") in SEVERE), key=lambda a: order[a["severity"]])
    return [{"event": a.get("event") or "Weather alert", "severity": a["severity"], "headline": (a.get("headline") or "")[:160]} for a in keep[:2]]


def hint(now_temp, rain_chances, today_code, metric):
    """One short line of advice, or ''."""
    snow = today_code in (71, 73, 75, 77, 85, 86)
    if snow:
        return "Snow today: bundle up"
    if max(rain_chances or [0]) >= 50:
        return "Take an umbrella"
    if now_temp <= (7 if metric else 45):
        return "Cold this morning: take a jacket"
    if now_temp >= (32 if metric else 90):
        return "Hot today: bring water"
    return ""


def fetch(place, metric):
    where = locate(place)
    params = {"latitude": where["lat"], "longitude": where["lon"], "timezone": "auto", "forecast_days": 5,
              "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
              "hourly": "temperature_2m,precipitation_probability,weather_code",
              "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,sunrise,sunset",
              "temperature_unit": "celsius" if metric else "fahrenheit", "wind_speed_unit": "kmh" if metric else "mph"}
    try:
        res = http.request("weather", "forecast", "GET", "https://api.open-meteo.com/v1/forecast", params=params, timeout=12)
    except Exception:
        raise WeatherError("Could not reach the weather service (no internet?).")
    if res.status_code != 200:
        raise WeatherError(f"The weather service answered HTTP {res.status_code}.")
    body = res.json()
    cur, daily, hourly = body.get("current") or {}, body.get("daily") or {}, body.get("hourly") or {}
    words, symbol = describe(cur.get("weather_code"))
    days = []
    for i, day in enumerate(daily.get("time") or []):
        w, s = describe((daily.get("weather_code") or [None] * 9)[i])
        days.append({"date": day, "high": round(daily["temperature_2m_max"][i]), "low": round(daily["temperature_2m_min"][i]), "rain": (daily.get("precipitation_probability_max") or [None] * 9)[i],
                     "words": w, "symbol": s})
    now_hour = (cur.get("time") or "")[:13]
    times = hourly.get("time") or []
    start = next((i for i, t in enumerate(times) if t[:13] >= now_hour), 0) if now_hour else 0
    hours = []
    for i in range(start, min(start + 12, len(times)), 2):
        w, s = describe(hourly["weather_code"][i])
        hours.append({"time": times[i], "temp": round(hourly["temperature_2m"][i]), "rain": hourly["precipitation_probability"][i], "symbol": s})
    rain = [h["rain"] for h in hours if h["rain"] is not None] + [d["rain"] for d in days[:1] if d["rain"] is not None]
    return {"alerts": alerts(where), "hint": hint(round(cur["temperature_2m"]), rain, (daily.get("weather_code") or [None])[0], metric),
            "place": where.get("name") or "", "unit": "°C" if metric else "°F", "speed": "km/h" if metric else "mph",
            "now": {"temp": round(cur["temperature_2m"]), "feels": round(cur["apparent_temperature"]), "words": words, "symbol": symbol, "wind": round(cur["wind_speed_10m"])},
            "days": days, "hours": hours, "sunrise": (daily.get("sunrise") or [""])[0], "sunset": (daily.get("sunset") or [""])[0]}


def get():
    """The weather, from the cache when it is fresh. When the service is down the last good answer is returned marked stale; with none, {"error": ...}."""
    place, metric = settings.get("dash_zip").strip(), config.metric()
    if not place:
        return {"error": "Set a ZIP code in Settings."}
    key = (place, metric)
    if _cache["key"] == key and _cache["data"] and time.time() - _cache["at"] < TTL:
        return _cache["data"]
    try:
        data = fetch(place, metric)
    except WeatherError as exc:
        if _cache["key"] == key and _cache["data"]:
            return {**_cache["data"], "stale": True}
        return {"error": str(exc)}
    except Exception as exc:                          # an unexpected answer must not blank the whole screen
        return {"error": f"The weather answer could not be read ({exc.__class__.__name__})."}
    _cache.update(at=time.time(), key=key, data=data)
    return data
