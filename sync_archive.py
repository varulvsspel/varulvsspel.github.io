#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations
import argparse
import json
import math
import re
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import unquote, urljoin
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
BASE_URL = "https://www.rollspel.nu"
FORUM_PATH = "/forums/varulvsspel.81/"
USER_AGENT = "VarulvScraperBot/3.0 (damogn på forumet)"
DEFAULT_DELAY = 1.25
DEFAULT_TIMEOUT = 30
THREADS_PER_FORUM_PAGE = 20
INDEX_FILE = "_sync_index.json"
CLEAN_PATTERNS = [
    re.compile(r'data-csrf="[^"]+"'),
    re.compile(r'name="_xfToken"\s+value="[^"]+"'),
    re.compile(r"csrf:\s*'[^']+'"),
    re.compile(r"\bnow:\s*\d+\b"),
    re.compile(r'data-lb-trigger="[^"]*?_xfUid[^"]*"'),
    re.compile(r'data-lb-id="[^"]*?_xfUid[^"]*"'),
    re.compile(r'js-lbImage-_xfUid[^"\s>]*'),
    re.compile(r'_xfUid-\d+-\d+'),
    re.compile(r'data-timestamp="\d+"'),
]
VOTE_START = re.compile(r"\bröst\s*:\s*", re.I)
USER_TAG = re.compile(r'data-username="@([^"]+)"', re.I)
OLD_VOTE = re.compile(r"\bröst\s*:\s*(.+)", re.I)
NAME_CHARS = re.compile(r"[A-Za-z0-9_åäöÅÄÖ\- ]+")
class SyncError(Exception):
    pass
def clean_html(text: str) -> str:
    out = []
    for line in text.splitlines():
        for pat in CLEAN_PATTERNS:
            line = pat.sub("", line)
        out.append(line.strip())
    return "\n".join(out)
def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default
def save_json(path: Path, obj, compact: bool = False) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    if compact:
        tmp.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    else:
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
def pages_in_dir(d: Path) -> List[Tuple[int, Path]]:
    out = []
    for p in d.glob("page*.html"):
        m = re.fullmatch(r"page(\d+)\.html", p.name)
        if m:
            out.append((int(m.group(1)), p))
    out.sort(key=lambda x: x[0])
    return out
def local_last_page(thread_dir: Path) -> int:
    pages = pages_in_dir(thread_dir)
    return pages[-1][0] if pages else 0
def thread_title_from_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    t = soup.find("title")
    if not t or not t.text:
        return ""
    s = t.text.strip()
    for pfx in ("Nekromanti - ", "Varulv - "):
        if s.startswith(pfx):
            s = s[len(pfx):]
    return s.replace("| rollspel.nu", "").strip()
def build_session() -> requests.Session:
    sess = requests.Session()
    sess.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "sv-SE,sv;q=0.9,en-US;q=0.8,en;q=0.7",
        "Connection": "keep-alive",
    })
    retry = Retry(
        total=6,
        connect=6,
        read=6,
        backoff_factor=1.2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "HEAD"],
        raise_on_status=False,
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    sess.mount("https://", adapter)
    sess.mount("http://", adapter)
    return sess
def polite_sleep(delay: float) -> None:
    if delay > 0:
        time.sleep(delay)
def fetch_html(sess: requests.Session, url: str, timeout: int, delay: float) -> str:
    res = sess.get(url, timeout=timeout)
    polite_sleep(delay)
    if res.status_code != 200:
        raise SyncError(f"{url} gav status {res.status_code}")
    res.encoding = res.encoding or "utf-8"
    return res.text
def parse_forum_last_page_number(html: str) -> int:
    soup = BeautifulSoup(html, "html.parser")
    inp = soup.select_one("input.js-pageJumpPage")
    if inp and inp.has_attr("max"):
        try:
            return int(inp["max"])
        except ValueError:
            pass
    last = soup.select_one("a.pageNavSimple-el--last[href]")
    if last:
        m = re.search(r"/page-(\d+)", last.get("href", ""))
        if m:
            return int(m.group(1))
    txt = soup.get_text(" ", strip=True)
    m = re.search(r"\b(\d+)\s+of\s+(\d+)\b", txt)
    return int(m.group(2)) if m else 1
def normalize_thread_slug_id(thread_href: str) -> Optional[str]:
    m = re.search(r"/threads/([^/]+?\.\d+)", thread_href)
    return m.group(1) if m else None
def thread_base_url_from_slug(slug_id: str) -> str:
    return urljoin(BASE_URL, f"/threads/{slug_id}/")
def parse_forum_threads(html: str) -> List[Dict]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for item in soup.select("div.structItem.structItem--thread"):
        title_a = None
        for a in item.select("div.structItem-title a[href]"):
            href = a.get("href", "")
            if "/threads/" in href:
                title_a = a
                break
        if not title_a:
            continue
        slug_id = normalize_thread_slug_id(title_a.get("href", ""))
        if not slug_id:
            continue
        latest_time = item.select_one("time.structItem-latestDate[data-timestamp]")
        if not latest_time:
            latest_time = item.select_one("div.structItem-cell--latest time[data-timestamp]")
        if latest_time and latest_time.has_attr("data-timestamp"):
            try:
                latest_ts = int(latest_time["data-timestamp"])
            except ValueError:
                latest_ts = 0
        else:
            latest_ts = 0
        nums = []
        for a in item.select("span.structItem-pageJump a"):
            t = a.get_text(strip=True)
            if t.isdigit():
                nums.append(int(t))
        last_page_hint = max(nums) if nums else 1
        out.append({
            "slug_id": slug_id,
            "title": title_a.get_text(" ", strip=True),
            "base_url": thread_base_url_from_slug(slug_id),
            "latest_ts": latest_ts,
            "last_page_hint": last_page_hint,
        })
    return out
def crawl_forum(sess: requests.Session, timeout: int, delay: float, limit_threads: int) -> List[Dict]:
    first_html = fetch_html(sess, urljoin(BASE_URL, FORUM_PATH), timeout, delay)
    forum_last_page = parse_forum_last_page_number(first_html)
    if limit_threads > 0:
        pages_needed = max(1, math.ceil(limit_threads / THREADS_PER_FORUM_PAGE))
        forum_pages = min(forum_last_page, pages_needed)
    else:
        forum_pages = forum_last_page
    threads = parse_forum_threads(first_html)
    for page in range(2, forum_pages + 1):
        try:
            html = fetch_html(sess, urljoin(BASE_URL, f"{FORUM_PATH}page-{page}"), timeout, delay)
        except Exception as e:
            print(f"[VARNING] Kunde inte hämta forumsida {page}: {e}", file=sys.stderr)
            continue
        threads.extend(parse_forum_threads(html))
    uniq = {}
    for t in threads:
        prev = uniq.get(t["slug_id"])
        if not prev or t["latest_ts"] >= prev["latest_ts"]:
            uniq[t["slug_id"]] = t
    out = list(uniq.values())
    out.sort(key=lambda t: t["latest_ts"], reverse=True)
    return out[:limit_threads] if limit_threads > 0 else out
def thread_page_url(base_url: str, page_num: int) -> str:
    if page_num <= 1:
        return base_url
    if not base_url.endswith("/"):
        base_url += "/"
    return urljoin(base_url, f"page-{page_num}")
def verify_thread_identity(html: str, slug_id: str) -> bool:
    if slug_id in html:
        return True
    soup = BeautifulSoup(html, "html.parser")
    canon = soup.select_one("link[rel='canonical'][href]")
    return bool(canon and slug_id in canon.get("href", ""))
def write_if_changed(path: Path, html: str) -> bool:
    if path.exists():
        old = path.read_text(encoding="utf-8", errors="ignore")
        if clean_html(old) == clean_html(html):
            return False
    path.write_text(html, encoding="utf-8")
    return True
def sync_thread(
    sess: requests.Session,
    data_dir: Path,
    idx: Dict,
    t: Dict,
    timeout: int,
    delay: float,
) -> Tuple[bool, bool, str]:
    slug = t["slug_id"]
    thread_dir = data_dir / slug
    ensure_dir(thread_dir)
    state = idx.setdefault("threads", {}).setdefault(slug, {})
    last_seen = state.get("latest_ts")
    if last_seen is not None and t["latest_ts"] != 0 and int(last_seen) == int(t["latest_ts"]):
        return False, False, "skip"
    x = local_last_page(thread_dir)
    y = max(1, int(t["last_page_hint"]))
    if x == 0:
        page_range = list(range(1, y + 1))
        action = f"ny tråd, hämtar 1..{y}"
    elif y > x:
        page_range = [x] + list(range(x + 1, y + 1))
        action = f"nya sidor {x}->{y}"
    elif y == x:
        page_range = [x]
        action = f"kollar sista sidan {x}"
    else:
        return False, False, f"forum säger {y} sidor men lokalt finns {x}, skippar"
    wrote = False
    try:
        for page_num in page_range:
            html = fetch_html(sess, thread_page_url(t["base_url"], page_num), timeout, delay)
            if not verify_thread_identity(html, slug):
                raise SyncError(f"identitetstest misslyckades för {slug} page{page_num}")
            if write_if_changed(thread_dir / f"page{page_num}.html", html):
                wrote = True
    except Exception as e:
        return False, False, f"{action} misslyckades: {e}"
    state["latest_ts"] = t["latest_ts"]
    state["last_page"] = max(x, y)
    return True, wrote, action
def split_html_lines(html_fragment: str) -> List[str]:
    frag = re.sub(r"<br\s*/?>", "\n", html_fragment, flags=re.I)
    return re.split(r"\n", frag)
def normalize_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()
def best_prefix_known(raw: str, known_cf: set) -> Optional[str]:
    parts = raw.split()
    for i in range(len(parts), 0, -1):
        pref = " ".join(parts[:i]).strip()
        if pref and pref.casefold() in known_cf:
            return pref
    return None
def build_known_and_canon(pages: List[Tuple[int, Path]]) -> Tuple[set, Dict[str, str]]:
    known_cf = set()
    canon = {}
    for _, page_path in pages:
        html = page_path.read_text(encoding="utf-8", errors="ignore")
        soup = BeautifulSoup(html, "html.parser")
        for post in soup.select("article[data-author]"):
            author = (post.get("data-author") or "").strip()
            if author:
                cf = author.casefold()
                known_cf.add(cf)
                canon.setdefault(cf, author)
        for m in USER_TAG.finditer(html):
            user = (m.group(1) or "").lstrip("@").strip()
            if user:
                cf = user.casefold()
                known_cf.add(cf)
                canon.setdefault(cf, user)
    return known_cf, canon
def extract_votes_tagmode(html: str, page_num: int) -> List[Dict]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for post in soup.select("article[data-author]"):
        from_user = (post.get("data-author") or "").strip()
        pid = (post.get("id") or "").replace("js-post-", "").strip()
        t = post.select_one("time.u-dt")
        ts = (t.get("datetime") if t else "") or ""
        for bq in post.select("blockquote"):
            bq.decompose()
        msg = post.select_one(".message-content")
        if not msg or not pid:
            continue
        for line in split_html_lines(msg.decode_contents()):
            plain = re.sub(r"<[^>]+>", " ", line)
            if not VOTE_START.search(plain):
                continue
            tail = re.sub(r"[\s\S]*?\bröst\s*:\s*", "", line, count=1, flags=re.I)
            m = USER_TAG.search(tail)
            if not m:
                continue
            to_user = (m.group(1) or "").lstrip("@").strip()
            if from_user and to_user:
                out.append({"from": from_user, "to": to_user, "ts": ts, "post": pid, "page": page_num})
    return out
def extract_votes_oldmode(html: str, page_num: int, known_cf: set, canon: Dict[str, str]) -> List[Dict]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for post in soup.select("article[data-author]"):
        from_user = (post.get("data-author") or "").strip()
        pid = (post.get("id") or "").replace("js-post-", "").strip()
        t = post.select_one("time.u-dt")
        ts = (t.get("datetime") if t else "") or ""
        for bq in post.select("blockquote"):
            bq.decompose()
        msg = post.select_one(".message-content")
        if not msg or not pid:
            continue
        for line in split_html_lines(msg.decode_contents()):
            plain = re.sub(r"<[^>]+>", " ", line)
            if not VOTE_START.search(plain):
                continue
            m = OLD_VOTE.search(plain)
            if not m:
                continue
            raw = normalize_spaces((m.group(1) or "").lstrip("@"))
            if not raw:
                continue
            best = best_prefix_known(raw, known_cf)
            if best:
                to_user = best
            else:
                nm = NAME_CHARS.search(raw)
                if not nm:
                    continue
                to_user = nm.group(0).strip()
            cf = to_user.casefold()
            to_user = canon.get(cf, to_user)
            if from_user and to_user:
                out.append({"from": from_user, "to": to_user, "ts": ts, "post": pid, "page": page_num})
    return out
def parse_thread(thread_dir: Path) -> Optional[Dict]:
    pages = pages_in_dir(thread_dir)
    if not pages:
        return None

    slug_raw = thread_dir.name
    slug = unquote(slug_raw)

    html1 = pages[0][1].read_text(encoding="utf-8", errors="ignore")
    title = thread_title_from_html(html1) or slug

    # Först: testa tagmode för hela tråden
    cached_pages: List[Tuple[int, str]] = []
    votes: List[Dict] = []

    for page_num, page_path in pages:
        html = page_path.read_text(encoding="utf-8", errors="ignore")
        cached_pages.append((page_num, html))
        votes.extend(extract_votes_tagmode(html, page_num))

    # Bara om inga taggröster hittades alls: fall back till oldmode
    if not votes:
        known_cf, canon = build_known_and_canon(pages)
        for page_num, html in cached_pages:
            votes.extend(extract_votes_oldmode(html, page_num, known_cf, canon))

    votes.sort(key=lambda v: v.get("ts") or "")
    players = sorted(set([v["from"] for v in votes] + [v["to"] for v in votes]), key=lambda s: s.lower())
    tss = sorted(v["ts"] for v in votes if v.get("ts"))

    return {
        "slug": slug,
        "slug_raw": slug_raw,
        "name": title,
        "players": players,
        "range": {"min": tss[0] if tss else None, "max": tss[-1] if tss else None},
        "votes": votes,
    }
def rebuild_threads_list(by_slug: Dict[str, Dict]) -> List[Dict]:
    threads = [{"slug": slug, "name": obj.get("name", slug)} for slug, obj in by_slug.items()]
    threads.sort(key=lambda x: x["name"].lower())
    return threads
def archive_equal(a: Dict, b: Dict) -> bool:
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    keys = ["name", "players", "range", "votes", "slug", "slug_raw"]
    return all(a.get(k) == b.get(k) for k in keys)
def main() -> int:
    ap = argparse.ArgumentParser(description="Snål sync + build av varulvsarkivet")
    ap.add_argument("--data", default="data", help="katalog för lokala trådsidor")
    ap.add_argument("--out", default="archive.json", help="utfil för archive.json")
    ap.add_argument("--limit-threads", type=int, default=0, help="hämta bara N senaste trådar (0 = alla)")
    ap.add_argument("--delay", type=float, default=DEFAULT_DELAY, help="delay mellan requests")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="HTTP-timeout i sekunder")
    args = ap.parse_args()
    data_dir = Path(args.data)
    out_path = Path(args.out)
    ensure_dir(data_dir)
    old_idx = load_json(data_dir / INDEX_FILE, {"threads": {}})
    idx = json.loads(json.dumps(old_idx))
    old_archive = load_json(out_path, {"threads": [], "bySlug": {}})
    by_slug = old_archive.get("bySlug", {}) if isinstance(old_archive.get("bySlug"), dict) else {}
    sess = build_session()
    print("SL: läser forumlistan...")
    try:
        forum_threads = crawl_forum(sess, args.timeout, args.delay, args.limit_threads)
    except Exception as e:
        print(f"[FEL] Kunde inte läsa forumlistan: {e}", file=sys.stderr)
        return 2
    changed_slugs = []
    sync_touched = False
    for i, t in enumerate(forum_threads, start=1):
        print(f"[{i}/{len(forum_threads)}] {t['title']}")
        synced, wrote, msg = sync_thread(sess, data_dir, idx, t, args.timeout, args.delay)
        print(f"  {t['slug_id']}: {msg}")
        if synced:
            sync_touched = True
        if wrote:
            changed_slugs.append(t["slug_id"])
    local_dirs = [p for p in data_dir.iterdir() if p.is_dir() and not p.name.startswith("_")]
    if out_path.exists():
        targets = set(changed_slugs)
        for td in local_dirs:
            slug = unquote(td.name)
            if slug not in by_slug:
                targets.add(td.name)
    else:
        targets = {td.name for td in local_dirs}
    archive_changed = False
    votes_changed = False
    for slug_raw in sorted(targets):
        info = parse_thread(data_dir / slug_raw)
        if not info:
            continue
        prev = by_slug.get(info["slug"])
        if prev is None or not archive_equal(prev, info):
            by_slug[info["slug"]] = info
            archive_changed = True
            if prev is None:
                if info["votes"]:
                    votes_changed = True
            elif prev.get("votes") != info.get("votes"):
                votes_changed = True
    new_archive = {"bySlug": by_slug, "threads": rebuild_threads_list(by_slug)}
    if not out_path.exists() or old_archive != new_archive:
        save_json(out_path, new_archive, compact=True)
        archive_changed = True
    if old_idx != idx:
        save_json(data_dir / INDEX_FILE, idx, compact=False)
        sync_touched = True
    if votes_changed:
        result = "votes_changed"
    elif sync_touched or archive_changed:
        result = "sync_only"
    else:
        result = "none"
    print(f"RESULT={result}")
    return 0
if __name__ == "__main__":
    raise SystemExit(main())
