#!/usr/bin/env python3
"""
IPTV playlist builder v2 (public streams from the iptv-org database).

What's new vs v1
  * Deep health check: fetches the HLS manifest, a variant playlist AND the
    first video segment, so "link answers" no longer counts as "channel plays".
  * Memory (playlists/state.json): a stream is only dropped after N failed
    checks in a row (--fail-limit), so one hiccup doesn't delete a channel,
    and a dead stream that comes back is re-added automatically.
  * Ranked backups: every channel keeps its best sources, ranked by
    resolution then speed.
  * Time budget: oldest-checked streams are re-checked first, so big runs
    rotate through the whole list instead of timing out.

Outputs (./playlists/)
  all.m3u                 best working stream per channel
  all_sources.m3u         best + backups (same tvg-id; apps like TiviMate
                          merge these into one channel with fallback)
  countries/<CC>.m3u      per country   (best stream per channel)
  categories/<cat>.m3u    per category  (best stream per channel)
  sources.json            ranked sources per channel (used by failover_server.py)
  STATUS.md               health summary
  state.json              check history (commit it, it is the memory)
  catalog.csv             with --catalog: every channel, even with no stream

Usage
  pip install requests
  python build_playlists.py
  python build_playlists.py --country KE US GB --category news sports
  python build_playlists.py --no-check          # skip health checks (fast)
  python build_playlists.py --catalog --include-closed
"""
import argparse
import csv
import json
import re
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests

API = "https://iptv-org.github.io/api"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
SKIP = object()
_local = threading.local()


# --------------------------------------------------------------------------
# Health checking
# --------------------------------------------------------------------------
def _session():
    if not hasattr(_local, "s"):
        s = requests.Session()
        ad = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=4)
        s.mount("http://", ad)
        s.mount("https://", ad)
        _local.s = s
    return _local.s


def _get_head(url, headers, timeout, limit):
    """GET url, read at most `limit` bytes. -> (status, content_type, data, final_url)"""
    r = _session().get(url, headers=headers, timeout=(6, timeout), stream=True)
    try:
        data = b""
        if r.status_code < 400:
            for chunk in r.iter_content(8192):
                data += chunk
                if len(data) >= limit:
                    break
        return r.status_code, r.headers.get("Content-Type", "").lower(), data, r.url
    finally:
        r.close()


def _check_hls(text, base, headers, timeout, depth=0):
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    uris = [l for l in lines if not l.startswith("#")]
    if not uris:
        return False
    target = urljoin(base, uris[0])
    if "#EXT-X-STREAM-INF" in text:            # master playlist -> variant
        if depth >= 2:
            return False
        code, _, data, final = _get_head(target, headers, timeout, 300_000)
        if code >= 400 or not data:
            return False
        if data.lstrip().startswith(b"#EXTM3U"):
            return _check_hls(data.decode("utf-8", "ignore"), final,
                              headers, timeout, depth + 1)
        return True
    code, _, data, _ = _get_head(target, headers, timeout, 2048)  # first segment
    return code < 400 and len(data) > 0


def probe(st, timeout=10):
    """Return latency in seconds if the stream really delivers data, else None."""
    url = st["url"]
    if not url.lower().startswith(("http://", "https://")):
        return None                              # rtmp/rtsp etc. can't be verified
    headers = {"User-Agent": st.get("user_agent") or UA, "Accept": "*/*"}
    if st.get("referrer"):
        headers["Referer"] = st["referrer"]
    t0 = time.time()
    try:
        code, ctype, data, final = _get_head(url, headers, timeout, 300_000)
        if code >= 400 or not data:
            return None
        is_m3u = data.lstrip().startswith(b"#EXTM3U")
        if "text/html" in ctype and not is_m3u:
            return None                          # login / error page
        if is_m3u and not _check_hls(data.decode("utf-8", "ignore"), final,
                                     headers, timeout):
            return None
        return time.time() - t0
    except Exception:
        return None


def _check_one(st, timeout, deadline):
    if time.time() > deadline:
        return SKIP
    return probe(st, timeout)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def fetch(name):
    r = requests.get(f"{API}/{name}.json", timeout=30)
    r.raise_for_status()
    return r.json()


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe(name):
    return re.sub(r"[^\w\-]+", "_", name).strip("_") or "unknown"


def quality_num(st):
    m = re.match(r"(\d+)", st.get("quality") or "")
    return int(m.group(1)) if m else 0


def clean(s):
    return (s or "").replace('"', "'").replace("\n", " ")


def entry(ch, st):
    cats = ch.get("categories") or []
    group = (cats[0] if cats else "general").title()
    attrs = (f'tvg-id="{clean(ch["id"])}" tvg-name="{clean(ch["name"])}" '
             f'tvg-logo="{clean(ch.get("logo"))}" '
             f'tvg-country="{clean(ch.get("country"))}" group-title="{group}"')
    q = f' [{st["quality"]}]' if st.get("quality") else ""
    lines = [f'#EXTINF:-1 {attrs},{clean(ch["name"])}{q}']
    if st.get("user_agent"):
        lines.append(f'#EXTVLCOPT:http-user-agent={st["user_agent"]}')
    if st.get("referrer"):
        lines.append(f'#EXTVLCOPT:http-referrer={st["referrer"]}')
    lines.append(st["url"])
    return "\n".join(lines)


def write_m3u(path, entries, quiet=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#EXTM3U\n" + "\n".join(entries) + "\n", encoding="utf-8")
    if not quiet:
        print(f"  wrote {path} ({len(entries)} entries)")


def load_state(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--country", nargs="*", help="ISO codes, e.g. KE US GB")
    ap.add_argument("--category", nargs="*", help="e.g. news sports movies")
    ap.add_argument("--check", action="store_true", help=argparse.SUPPRESS)  # v1 compat
    ap.add_argument("--no-check", action="store_true",
                    help="skip health checks and include every stream")
    ap.add_argument("--include-nsfw", action="store_true")
    ap.add_argument("--include-closed", action="store_true")
    ap.add_argument("--catalog", action="store_true",
                    help="also write catalog.csv with EVERY channel")
    ap.add_argument("--out", default="playlists")
    ap.add_argument("--max-sources", type=int, default=3,
                    help="backups kept per channel in all_sources.m3u")
    ap.add_argument("--fail-limit", type=int, default=2,
                    help="consecutive failed checks before a stream is dropped")
    ap.add_argument("--workers", type=int, default=120)
    ap.add_argument("--timeout", type=int, default=10)
    ap.add_argument("--max-minutes", type=float, default=60,
                    help="time budget for health checks")
    args = ap.parse_args()

    out = Path(args.out)
    print("Downloading database...")
    channels = {c["id"]: c for c in fetch("channels")}
    streams = fetch("streams")
    countries = {c["code"]: c["name"] for c in fetch("countries")}

    want_cc = {c.upper() for c in args.country} if args.country else None
    want_cat = {c.lower() for c in args.category} if args.category else None

    def wanted(ch):
        if ch.get("closed") and not args.include_closed:
            return False
        if ch.get("is_nsfw") and not args.include_nsfw:
            return False
        if want_cc and ch.get("country") not in want_cc:
            return False
        cats = {c.lower() for c in (ch.get("categories") or [])}
        if want_cat and not (want_cat & cats):
            return False
        return True

    # ---- catalog of every channel (even with no public stream) ----
    if args.catalog:
        count = defaultdict(int)
        for st in streams:
            count[st.get("channel")] += 1
        path = out / "catalog.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        n = 0
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["id", "name", "country", "country_name", "categories",
                        "network", "owners", "website", "closed", "nsfw",
                        "public_streams_found"])
            for ch in channels.values():
                if not wanted(ch):
                    continue
                w.writerow([ch["id"], ch["name"], ch.get("country", ""),
                            countries.get(ch.get("country"), ""),
                            ";".join(ch.get("categories") or []),
                            ch.get("network") or "",
                            ";".join(ch.get("owners") or []),
                            ch.get("website") or "", bool(ch.get("closed")),
                            bool(ch.get("is_nsfw")), count.get(ch["id"], 0)])
                n += 1
        print(f"  wrote {path} ({n} channels)")

    items = []
    for st in streams:
        ch = channels.get(st.get("channel"))
        if ch and wanted(ch):
            items.append((ch, st))
    print(f"{len(items)} candidate streams.")

    # ---- health checks with memory ----
    state_path = out / "state.json"
    all_urls = {s["url"] for s in streams}
    state = {u: v for u, v in load_state(state_path).items() if u in all_urls}
    do_check = not args.no_check

    if do_check:
        by_url = {}
        for _, st in items:
            by_url.setdefault(st["url"], st)
        order = sorted(by_url, key=lambda u: state.get(u, {}).get("checked", ""))
        deadline = time.time() + args.max_minutes * 60
        print(f"Checking {len(order)} unique streams "
              f"(budget {args.max_minutes:.0f} min, {args.workers} workers)...")
        skipped = ok = bad = 0
        with ThreadPoolExecutor(args.workers) as pool:
            futs = {pool.submit(_check_one, by_url[u], args.timeout, deadline): u
                    for u in order}
            for i, fut in enumerate(as_completed(futs), 1):
                u = futs[fut]
                res = fut.result()
                if res is SKIP:
                    skipped += 1
                    continue
                s = state.setdefault(u, {"fails": 0})
                s["checked"] = now_iso()
                if res is None:
                    s["fails"] = s.get("fails", 0) + 1
                    bad += 1
                else:
                    s.update(fails=0, last_ok=now_iso(), latency=round(res, 2))
                    ok += 1
                if i % 500 == 0:
                    print(f"  {i}/{len(order)}  ok={ok} failed={bad}")
        print(f"Checked: {ok} ok, {bad} failed, {skipped} deferred to next run.")
        out.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state, separators=(",", ":")),
                              encoding="utf-8")

    # ---- pick live streams, rank them ----
    live, seen = defaultdict(list), set()
    for ch, st in items:
        key = (ch["id"], st["url"])
        if key in seen:
            continue
        seen.add(key)
        latency = 0.0
        if do_check:
            s = state.get(st["url"])
            if not s or "last_ok" not in s or s.get("fails", 0) >= args.fail_limit:
                continue
            latency = s.get("latency", 99.0)
        live[ch["id"]].append((-quality_num(st), latency, ch, st))
    for cid in live:
        live[cid].sort(key=lambda x: (x[0], x[1]))

    chans = sorted(live, key=lambda c: (live[c][0][2].get("country") or "",
                                        live[c][0][2]["name"]))
    best, multi = [], []
    by_country, by_cat, sources = defaultdict(list), defaultdict(list), {}
    for cid in chans:
        cands = live[cid][:args.max_sources]
        ch = cands[0][2]
        e = entry(ch, cands[0][3])
        best.append(e)
        multi += [entry(c[2], c[3]) for c in cands]
        by_country[ch.get("country") or "XX"].append(e)
        for cat in ch.get("categories") or ["general"]:
            by_cat[cat].append(e)
        sources[cid] = {
            "name": ch["name"], "country": ch.get("country", ""),
            "categories": ch.get("categories") or [], "logo": ch.get("logo") or "",
            "sources": [{"url": c[3]["url"], "user_agent": c[3].get("user_agent"),
                         "referrer": c[3].get("referrer"),
                         "quality": c[3].get("quality")} for c in cands]}

    print("Writing playlists...")
    write_m3u(out / "all.m3u", best)
    write_m3u(out / "all_sources.m3u", multi)
    for cc, es in by_country.items():
        write_m3u(out / "countries" / f"{safe(cc)}.m3u", es, quiet=True)
    for cat, es in by_cat.items():
        write_m3u(out / "categories" / f"{safe(cat)}.m3u", es, quiet=True)
    print(f"  wrote {len(by_country)} country and {len(by_cat)} category playlists")
    (out / "sources.json").write_text(json.dumps(sources, separators=(",", ":")),
                                      encoding="utf-8")

    # ---- status report ----
    top = sorted(by_country.items(), key=lambda kv: -len(kv[1]))[:25]
    lines = [f"# Playlist status", "", f"Updated: {now_iso()}", "",
             f"- Working channels: **{len(best)}**",
             f"- Working streams (with backups kept): **{len(multi)}**",
             f"- Countries: **{len(by_country)}**", "",
             "| Country | Working channels |", "|---|---|"]
    lines += [f"| {countries.get(cc, cc)} ({cc}) | {len(es)} |" for cc, es in top]
    (out / "STATUS.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Done: {len(best)} working channels.")


if __name__ == "__main__":
    main()