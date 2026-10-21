"""Builds frame.json for the photo-frame dashboard.

Reads Apple Calendars over CalDAV (login from environment secrets), gets the
weather from Open-Meteo, and writes site/<FEED_TOKEN>/frame.json.
Only titles and times are published. No locations, notes or attendees.
"""
import datetime as dt
import json
import os
import random
import socket
import sys
import urllib.error
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

B62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def album_token(url):
    """The token is the part of the share link after '#'."""
    tok = url.strip().split("#")[-1].split("/")[-1]
    if not tok:
        raise ValueError("no album token in link")
    return tok


def album_host(tok):
    """Best guess at which Apple server holds the album (a 330 reply corrects it)."""
    try:
        if tok[0] == "A":
            n = B62.index(tok[1])
        else:
            n = B62.index(tok[1]) * 62 + B62.index(tok[2])
        return "p%02d-sharedstreams.icloud.com" % n
    except Exception:
        return "p01-sharedstreams.icloud.com"


def _post(host, tok, path, body):
    """One POST; follow Apple's 330 'use this host' reply once."""
    for _ in range(2):
        url = "https://%s/%s/sharedstreams/%s" % (host, tok, path)
        req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json",
                                              "User-Agent": "frame-feed"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return host, json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 330:
                data = json.loads(e.read().decode() or "{}")
                new = data.get("X-Apple-MMe-Host")
                if new and new != host:
                    host = new
                    continue
            raise
    raise RuntimeError("album host redirect loop")


def post_json(host, tok, path, body):
    """Try the guessed server first, then p01..p99, skipping names that do not exist."""
    seen = set()
    for h in [host] + ["p%02d-sharedstreams.icloud.com" % n for n in range(1, 100)]:
        if h in seen:
            continue
        seen.add(h)
        try:
            return _post(h, tok, path, body)
        except urllib.error.HTTPError:
            raise
        except urllib.error.URLError as e:
            if isinstance(e.reason, socket.gaierror):
                continue          # that server name does not exist; try the next
            raise
    raise RuntimeError("no album server answered")


def pick_derivative(derivs):
    """Smallest picture at least ~1000 px wide, else the biggest one available."""
    rows = []
    for key, d in (derivs or {}).items():
        try:
            w = int(d.get("width") or key)
        except Exception:
            continue
        rows.append((w, d))
    if not rows:
        return None
    rows.sort(key=lambda x: x[0])
    for w, d in rows:
        if w >= 1000:
            return d
    return rows[-1][1]


def photos(limit=60):
    url = os.environ.get("ALBUM_URL", "").strip()
    if not url:
        return None
    try:
        tok = album_token(url)
        host, stream = post_json(album_host(tok), tok, "webstream", {"streamCtag": None})
        items = []
        for ph in stream.get("photos", []):
            if ph.get("mediaAssetType") == "video":
                continue
            d = pick_derivative(ph.get("derivatives"))
            if d and d.get("checksum") and ph.get("photoGuid"):
                items.append((ph["photoGuid"], d["checksum"], str(ph.get("caption") or "")[:60]))
        random.Random(dt.date.today().toordinal()).shuffle(items)
        items = items[:limit]
        if not items:
            print("album: no photos found", file=sys.stderr)
            return None
        host, assets = post_json(host, tok, "webasseturls", {"photoGuids": [i[0] for i in items]})
        urls = assets.get("items", {})
        out = []
        for guid, checksum, caption in items:
            u = urls.get(checksum)
            if u and u.get("url_location") and u.get("url_path"):
                out.append({"src": "https://" + u["url_location"] + u["url_path"], "caption": caption})
        print("album: %d photos (server %s)" % (len(out), host))
        return out or None
    except Exception as e:
        print("album failed:", repr(e), file=sys.stderr)
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
    pics = photos()
    if pics:
        feed["photos"] = pics
    folder = os.path.join(HERE, "site", os.environ["FEED_TOKEN"])
    os.makedirs(folder, exist_ok=True)
    json.dump(feed, open(os.path.join(folder, "frame.json"), "w"), separators=(",", ":"))
    open(os.path.join(HERE, "site", "index.html"), "w").write("")
    print("wrote", sum(len(p["events"]) for p in people), "events")


if __name__ == "__main__":
    main()
