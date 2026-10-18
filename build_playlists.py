#!/usr/bin/env python3
"""
Build M3U playlists (all / per country / per category) from the open
iptv-org database of publicly available TV streams.

Usage:
    pip install requests
    python build_playlists.py                      # everything
    python build_playlists.py --country US GB      # only some countries
    python build_playlists.py --category news sports
    python build_playlists.py --check              # drop dead streams (slow)
    python build_playlists.py --include-nsfw       # off by default
    python build_playlists.py --catalog --include-closed   # full channel list

Output goes to ./playlists/ :
    all.m3u
    countries/<CC>.m3u
    categories/<category>.m3u
Load any of them in VLC (Media > Open File) or in an IPTV player on your TV.
"""
import argparse
import csv
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

API = "https://iptv-org.github.io/api"
TIMEOUT = 15


def fetch(name):
    r = requests.get(f"{API}/{name}.json", timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def is_alive(stream):
    """Light check: does the URL respond without an error?"""
    headers = {"User-Agent": stream.get("user_agent") or "Mozilla/5.0"}
    if stream.get("referrer"):
        headers["Referer"] = stream["referrer"]
    try:
        r = requests.get(stream["url"], headers=headers, timeout=8, stream=True)
        ok = r.status_code < 400
        r.close()
        return ok
    except requests.RequestException:
        return False


def safe(name):
    return re.sub(r"[^\w\-]+", "_", name).strip("_") or "unknown"


def entry(ch, st, country_names):
    cats = ch.get("categories") or []
    group = (cats[0] if cats else "general").title()
    cc = ch.get("country", "")
    attrs = (
        f'tvg-id="{ch["id"]}" '
        f'tvg-name="{ch["name"]}" '
        f'tvg-logo="{ch.get("logo", "") or ""}" '
        f'tvg-country="{cc}" '
        f'group-title="{group}"'
    )
    quality = f' [{st["quality"]}]' if st.get("quality") else ""
    lines = [f'#EXTINF:-1 {attrs},{ch["name"]}{quality}']
    if st.get("user_agent"):
        lines.append(f'#EXTVLCOPT:http-user-agent={st["user_agent"]}')
    if st.get("referrer"):
        lines.append(f'#EXTVLCOPT:http-referrer={st["referrer"]}')
    lines.append(st["url"])
    return "\n".join(lines)


def write_m3u(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#EXTM3U\n" + "\n".join(entries) + "\n", encoding="utf-8")
    print(f"  wrote {path} ({len(entries)} streams)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--country", nargs="*", help="ISO codes, e.g. US GB DE")
    ap.add_argument("--category", nargs="*", help="e.g. news sports movies")
    ap.add_argument("--check", action="store_true", help="remove dead streams")
    ap.add_argument("--include-nsfw", action="store_true")
    ap.add_argument("--out", default="playlists")
    ap.add_argument("--catalog", action="store_true",
                    help="also write catalog.csv listing EVERY channel, "
                         "including ones with no public stream")
    ap.add_argument("--include-closed", action="store_true",
                    help="include channels marked as closed/defunct")
    args = ap.parse_args()

    print("Downloading database...")
    channels = {c["id"]: c for c in fetch("channels")}
    streams = fetch("streams")
    countries = {c["code"]: c["name"] for c in fetch("countries")}

    want_cc = {c.upper() for c in args.country} if args.country else None
    want_cat = {c.lower() for c in args.category} if args.category else None

    # Join streams with channel metadata and apply filters
    items = []
    for st in streams:
        ch = channels.get(st.get("channel"))
        if not ch or (ch.get("closed") and not args.include_closed):
            continue
        if ch.get("is_nsfw") and not args.include_nsfw:
            continue
        if want_cc and ch.get("country") not in want_cc:
            continue
        cats = [c.lower() for c in (ch.get("categories") or [])]
        if want_cat and not (want_cat & set(cats)):
            continue
        items.append((ch, st))

    print(f"{len(items)} streams matched filters.")

    if args.catalog:
        stream_count = defaultdict(int)
        for st in streams:
            stream_count[st.get("channel")] += 1
        path = Path(args.out) / "catalog.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = 0
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["id", "name", "country", "country_name", "categories",
                        "network", "owners", "website", "closed", "nsfw",
                        "public_streams_found"])
            for ch in channels.values():
                if want_cc and ch.get("country") not in want_cc:
                    continue
                cats = [c.lower() for c in (ch.get("categories") or [])]
                if want_cat and not (want_cat & set(cats)):
                    continue
                if ch.get("is_nsfw") and not args.include_nsfw:
                    continue
                if ch.get("closed") and not args.include_closed:
                    continue
                w.writerow([ch["id"], ch["name"], ch.get("country", ""),
                            countries.get(ch.get("country"), ""),
                            ";".join(ch.get("categories") or []),
                            ch.get("network") or "",
                            ";".join(ch.get("owners") or []),
                            ch.get("website") or "",
                            bool(ch.get("closed")), bool(ch.get("is_nsfw")),
                            stream_count.get(ch["id"], 0)])
                rows += 1
        print(f"  wrote {path} ({rows} channels)")

    if args.check:
        print("Checking streams (this can take a while)...")
        with ThreadPoolExecutor(max_workers=40) as pool:
            alive = list(pool.map(lambda x: is_alive(x[1]), items))
        items = [i for i, ok in zip(items, alive) if ok]
        print(f"{len(items)} streams are alive.")

    out = Path(args.out)
    by_country, by_cat, all_entries = defaultdict(list), defaultdict(list), []

    for ch, st in sorted(items, key=lambda x: (x[0].get("country", ""), x[0]["name"])):
        e = entry(ch, st, countries)
        all_entries.append(e)
        by_country[ch.get("country") or "XX"].append(e)
        for c in (ch.get("categories") or ["general"]):
            by_cat[c].append(e)

    print("Writing playlists...")
    write_m3u(out / "all.m3u", all_entries)
    for cc, es in by_country.items():
        write_m3u(out / "countries" / f"{safe(cc)}.m3u", es)
    for cat, es in by_cat.items():
        write_m3u(out / "categories" / f"{safe(cat)}.m3u", es)
    print("Done.")


if __name__ == "__main__":
    main()