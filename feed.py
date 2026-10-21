"""Builds frame.json for the photo-frame dashboard.

Reads Apple Calendars over CalDAV (login from environment secrets), gets the
weather from Open-Meteo, and writes site/<FEED_TOKEN>/frame.json.
Only titles and times are published. No locations, notes or attendees.
"""
import datetime as dt
import json
import os
import sys
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

import caldav

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(HERE, "config.json")))
TZ = ZoneInfo(CFG.get("timezone", "America/New_York"))

WMO = {0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Cloudy", 45: "Fog", 48: "Fog",
       51: "Drizzle", 53: "Drizzle", 55: "Drizzle", 56: "Freezing drizzle", 57: "Freezing drizzle",
       61: "Rain", 63: "Rain", 65: "Heavy rain", 66: "Freezing rain", 67: "Freezing rain",
       71: "Snow", 73: "Snow", 75: "Heavy snow", 77: "Snow", 80: "Showers", 81: "Showers",
       82: "Heavy showers", 85: "Snow showers", 86: "Snow showers", 95: "Thunderstorm",
       96: "Thunderstorm", 99: "Thunderstorm"}


def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "frame-feed"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def weather():
    try:
        loc = CFG["location"]
        if "latitude" in loc:
            lat, lon = loc["latitude"], loc["longitude"]
        else:
            q = urllib.parse.urlencode({"name": loc["city"], "count": 1})
            g = get_json("https://geocoding-api.open-meteo.com/v1/search?" + q)["results"][0]
            lat, lon = g["latitude"], g["longitude"]
        q = urllib.parse.urlencode({
            "latitude": lat, "longitude": lon, "temperature_unit": "fahrenheit",
            "timezone": CFG.get("timezone", "America/New_York"), "forecast_days": 1,
            "current": "temperature_2m,weather_code",
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max"})
        d = get_json("https://api.open-meteo.com/v1/forecast?" + q)
        rain = d["daily"]["precipitation_probability_max"][0]
        w = {"temp": str(round(d["current"]["temperature_2m"])),
             "cond": WMO.get(d["current"]["weather_code"], ""),
             "hi": str(round(d["daily"]["temperature_2m_max"][0])),
             "lo": str(round(d["daily"]["temperature_2m_min"][0]))}
        if rain is not None and rain >= 40:
            w["note"] = "%d%% chance of rain today." % rain
        return w
    except Exception as e:
        print("weather failed:", e, file=sys.stderr)
        return None

def to_local(v):
    """Return (date, 'HH:MM' or '') in the family's time zone."""
    if isinstance(v, dt.datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=TZ)
        v = v.astimezone(TZ)
        return v.date(), v.strftime("%H:%M")
    return v, ""


def events_for(cals, start, end):
    out, seen = [], set()
    for cal in cals:
        try:
            found = cal.date_search(start=start, end=end, expand=True)
        except Exception as e:
            print("calendar failed:", e, file=sys.stderr)
            continue
        for ev in found:
            for comp in ev.icalendar_instance.walk("VEVENT"):
                title = str(comp.get("summary", "Busy"))[:60]
                s = comp.get("dtstart")
                if s is None:
                    continue
                day, when = to_local(s.dt)
                end_str = ""
                e = comp.get("dtend")
                if e is not None and when:
                    eday, etime = to_local(e.dt)
                    if eday == day:
                        end_str = etime
                key = (day, when, title)
                if key in seen:
                    continue
                seen.add(key)
                item = {"date": day.isoformat(), "when": when, "text": title}
                if end_str:
                    item["end"] = end_str
                out.append(item)
    out.sort(key=lambda x: (x["date"], x["when"]))
    return out


def main():
    client = caldav.DAVClient(url="https://caldav.icloud.com/",
                              username=os.environ["APPLE_ID"],
                              password=os.environ["APPLE_APP_PASSWORD"])
    all_cals = client.principal().calendars()
    names = {str(c.name): c for c in all_cals}
    print("calendars found:", sorted(names))
    today = dt.datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    start, end = today - dt.timedelta(days=3), today + dt.timedelta(days=60)
    people = []
    for p in CFG["people"]:
        cals = [names[n] for n in p["calendars"] if n in names]
        missing = [n for n in p["calendars"] if n not in names]
        if missing:
            print("not found for %s: %s" % (p["name"], missing), file=sys.stderr)
        people.append({"name": p["name"], "events": events_for(cals, start, end)})
    feed = {"generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
            "weather": weather(), "people": people}
    folder = os.path.join(HERE, "site", os.environ["FEED_TOKEN"])
    os.makedirs(folder, exist_ok=True)
    json.dump(feed, open(os.path.join(folder, "frame.json"), "w"), separators=(",", ":"))
    open(os.path.join(HERE, "site", "index.html"), "w").write("")
    print("wrote", sum(len(p["events"]) for p in people), "events")


if __name__ == "__main__":
    main()
