#!/usr/bin/env python3
"""
Self-healing playlist server.

Your TV loads ONE playlist whose channel URLs point at this server. When you
press play, the server tests that channel's ranked sources and redirects the
player to the first one that works RIGHT NOW. A dead source no longer matters.

Run (same folder as build_playlists.py):
    python failover_server.py                          # uses playlists/sources.json
    python failover_server.py --sources https://raw.githubusercontent.com/Valmoe/TV/master/playlists/sources.json
    python failover_server.py --port 8000

Use on your TV / VLC:
    http://<this-pc-ip>:8000/playlist.m3u
    http://<this-pc-ip>:8000/playlist.m3u?country=KE
    http://<this-pc-ip>:8000/playlist.m3u?category=news&country=US

Notes
  * Sources that need a custom User-Agent/Referer can't be redirected, so they
    are skipped here (they still appear in the normal .m3u files).
  * The sources list is refreshed from its origin every 30 minutes.
"""
import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

import requests

from build_playlists import probe

DATA = {}
CACHE = {}          # channel id -> (expires_at, url)
CACHE_TTL = 90      # seconds a working pick is reused
LOCK = threading.Lock()


def load_sources(src):
    if src.startswith("http"):
        return requests.get(src, timeout=30).json()
    with open(src, encoding="utf-8") as f:
        return json.load(f)


def refresher(src, every=1800):
    global DATA
    while True:
        time.sleep(every)
        try:
            DATA = load_sources(src)
            print("sources refreshed")
        except Exception as e:
            print("refresh failed:", e)


def pick(cid):
    with LOCK:
        hit = CACHE.get(cid)
        if hit and hit[0] > time.time():
            return hit[1]
    ch = DATA.get(cid)
    if not ch:
        return None
    for s in ch["sources"]:
        if s.get("user_agent") or s.get("referrer"):
            continue
        if probe(s, timeout=6) is not None:
            with LOCK:
                CACHE[cid] = (time.time() + CACHE_TTL, s["url"])
            return s["url"]
    return None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/playlist.m3u":
            cc = {c.upper() for c in q.get("country", [])}
            cats = {c.lower() for c in q.get("category", [])}
            base = f"http://{self.headers.get('Host')}"
            lines = ["#EXTM3U"]
            for cid, ch in DATA.items():
                if cc and ch["country"] not in cc:
                    continue
                if cats and not (cats & {c.lower() for c in ch["categories"]}):
                    continue
                if not any(not (s.get("user_agent") or s.get("referrer"))
                           for s in ch["sources"]):
                    continue
                grp = (ch["categories"][0] if ch["categories"] else "general").title()
                name = ch["name"].replace('"', "'")
                lines.append(f'#EXTINF:-1 tvg-id="{cid}" tvg-name="{name}" '
                             f'tvg-logo="{ch["logo"]}" tvg-country="{ch["country"]}" '
                             f'group-title="{grp}",{name}')
                lines.append(f"{base}/ch/{quote(cid)}")
            body = ("\n".join(lines) + "\n").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "audio/x-mpegurl; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif u.path.startswith("/ch/"):
            url = pick(unquote(u.path[4:]))
            if url:
                self.send_response(302)
                self.send_header("Location", url)
                self.end_headers()
            else:
                self.send_error(503, "no working source right now")
        else:
            self.send_error(404)


def main():
    global DATA
    ap = argparse.ArgumentParser()
    ap.add_argument("--sources", default="playlists/sources.json")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    DATA = load_sources(args.sources)
    threading.Thread(target=refresher, args=(args.sources,), daemon=True).start()
    print(f"{len(DATA)} channels loaded. Playlist: http://localhost:{args.port}/playlist.m3u")
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()