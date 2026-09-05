#!/usr/bin/env python3
"""
scrape.py — 抓取 GitHub Trending 数据（daily / weekly / monthly）
输出格式：trending.json
"""
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone, timedelta

# 北京时间
CST = timezone(timedelta(hours=8))
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
}


# Known-good IP pool for github.com. The DNS-published IP (20.205.243.166)
# is unreachable from this Windows host's egress; we probe a pool of GitHub's
# announced IPs at startup and pick the first that accepts TCP. SNI stays
# set to req.host so the TLS handshake validates.
GITHUB_IP_POOL = [
    "20.200.245.247",
    "20.201.28.151",
    "140.82.112.3",
    "140.82.114.4",
    "140.82.112.4",
    "20.27.177.113",  # was good on 2026-07-08; may rotate out
]


def _probe_github_ip(timeout: float = 4.0) -> str | None:
    """Return the first IP in GITHUB_IP_POOL that accepts a TCP/443 connection,
    or None if every candidate times out. Cached per-process."""
    if hasattr(_probe_github_ip, "_cached"):
        return _probe_github_ip._cached
    import socket
    for ip in GITHUB_IP_POOL:
        try:
            s = socket.create_connection((ip, 443), timeout=timeout)
            s.close()
            _probe_github_ip._cached = ip
            print(f"[scrape] github.com IP probe: using {ip}")
            return ip
        except Exception:
            continue
    return None


class _GithubIpHandler(urllib.request.HTTPSHandler):
    """HTTPS handler that connects to a probed-good IP for github.com domains,
    while sending the correct SNI (req.host) so TLS handshake succeeds."""

    def https_open(self, req):
        host = req.host
        if host in ("github.com", "www.github.com"):
            ip = _probe_github_ip()
            if not ip:
                return super().https_open(req)
            import socket
            original_create_connection = socket.create_connection

            def patched_create_connection(address, *a, **kw):
                h, p = address
                if h == host:
                    h = ip
                return original_create_connection((h, p), *a, **kw)

            socket.create_connection = patched_create_connection
            try:
                return super().https_open(req)
            finally:
                socket.create_connection = original_create_connection
        return super().https_open(req)


def _local_proxy_alive() -> str | None:
    """If the user's dev proxy (Clash / V2RayN etc.) at 127.0.0.1:7897 is up,
    return its URL. We use it as a fallback only when the IP pool is exhausted
    because every GitHub IP is being silently dropped at the TCP-then-HTTP layer
    (egress firewall quirk)."""
    import socket as _s
    p = _s.socket()
    p.settimeout(1.5)
    try:
        p.connect(("127.0.0.1", 7897))
        p.close()
        return "http://127.0.0.1:7897"
    except Exception:
        return None


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    # First try the direct IP-pool path (short timeout — fall back fast if blocked)
    try:
        opener = urllib.request.build_opener(_GithubIpHandler())
        with opener.open(req, timeout=8) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception:
        pass
    # Fallback: route via the user's local dev proxy if it's listening
    proxy = _local_proxy_alive()
    if proxy:
        proxy_handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        opener = urllib.request.build_opener(proxy_handler)
        with opener.open(req, timeout=30) as r:
            return r.read().decode("utf-8", errors="replace")
    raise RuntimeError("github.com unreachable: IP pool exhausted and no local proxy")


def parse_article(block: str) -> dict | None:
    m = re.search(r'<h2[^>]*>\s*<a[^>]+href="/([^"]+)"', block)
    if not m:
        return None
    full_path = m.group(1).strip().replace("\n", "").replace(" ", "")

    desc_m = re.search(r'<p class="col-9[^"]*"[^>]*>([\s\S]*?)</p>', block)
    desc = re.sub(r"\s+", " ", desc_m.group(1).strip()) if desc_m else ""

    lang_m = re.search(r'itemprop="programmingLanguage">([^<]+)</span>', block)
    lang = lang_m.group(1).strip() if lang_m else ""

    stars_today_m = re.search(r"(\d[\d,]*)\s*stars\s*(today|this\s+week|this\s+month)", block)
    stars_today = stars_today_m.group(1).replace(",", "") if stars_today_m else "0"

    links = re.findall(r'<a[^>]+href="([^"]+)"[^>]*>([\s\S]*?)</a>', block)
    total_stars, forks = "", ""
    for href, text in links:
        clean = re.sub(r"<[^>]+>", "", text).strip().replace(",", "")
        if "/stargazers" in href and clean.isdigit():
            total_stars = clean
        elif "/forks" in href and clean.isdigit():
            forks = clean

    return {
        "full_name": full_path,
        "url": f"https://github.com/{full_path}",
        "description": desc,
        "stars_today": int(stars_today) if stars_today else 0,
        "total_stars": int(total_stars) if total_stars else 0,
        "forks": int(forks) if forks else 0,
        "language": lang,
    }


def scrape_range(date_range: str) -> list[dict]:
    """date_range: daily | weekly | monthly"""
    # Use www. subdomain — bare github.com hangs from this IP (likely geo/firewall quirk)
    url = f"https://www.github.com/trending?since={date_range}"
    html = fetch(url)
    articles = re.findall(r'<article class="Box-row">(.*?)</article>', html, re.DOTALL)
    return [r for r in (parse_article(a) for a in articles) if r]


def main() -> int:
    now = datetime.now(CST).strftime("%Y-%m-%d %H:%M")
    data = {
        "updated_at": now,
        "daily": scrape_range("daily")[:10],
        "weekly": scrape_range("weekly")[:10],
        "monthly": scrape_range("monthly")[:10],
    }
    # 统计
    all_repos = data["daily"] + data["weekly"] + data["monthly"]
    unique_repos = {r["full_name"] for r in all_repos}
    languages = {r["language"] for r in all_repos if r["language"]}
    data["stats"] = {
        "total_repos": len(unique_repos),
        "languages_count": len(languages),
    }
    out = "trending.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"[scrape] {now} 写入 {out}")
    print(f"  daily={len(data['daily'])}  weekly={len(data['weekly'])}  monthly={len(data['monthly'])}")
    print(f"  unique_repos={data['stats']['total_repos']}  languages={data['stats']['languages_count']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
