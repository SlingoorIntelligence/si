"""
Builds callouts_data.json for the site from the links in callouts.json.
Runs every 6 hours from .github/workflows/callouts.yml (or by hand).

callouts.json is a list, newest first. Each entry needs a "url" (the Pump.fun callout share link).
Optional per entry: "text" (callout text to show), "who" (caller name), "img" (path to a screenshot).
For each link the script reads the page's public link-preview tags (og:title / og:description /
og:image) and saves the preview image into callouts/auto/ so the site never hotlinks.
Anything it can't read falls back to what you typed in callouts.json.
"""
import json, hashlib, html, re, time, urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
SRC, OUT, IMG = ROOT / "callouts.json", ROOT / "callouts_data.json", ROOT / "callouts" / "auto"
UA = "Mozilla/5.0 (compatible; SI-site-bot/1.0; +link-preview)"
KEEP = 3

def fetch(url, binary=False, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read(4_000_000)
        return data if binary else data.decode("utf-8", "replace")

def meta(page, *names):
    for n in names:
        m = re.search(r'<meta[^>]+(?:property|name)=["\']%s["\'][^>]*content=["\']([^"\']*)' % re.escape(n), page, re.I) \
            or re.search(r'<meta[^>]+content=["\']([^"\']*)["\'][^>]*(?:property|name)=["\']%s["\']' % re.escape(n), page, re.I)
        if m and m.group(1).strip():
            return html.unescape(m.group(1).strip())
    return ""

def main():
    items = [c for c in json.loads(SRC.read_text()) if isinstance(c, dict) and c.get("url")][:KEEP]
    IMG.mkdir(parents=True, exist_ok=True)
    out = []
    for c in items:
        e = {"url": c["url"], "text": c.get("text", ""), "who": c.get("who", ""), "img": c.get("img", "")}
        try:
            page = fetch(c["url"])
            title = meta(page, "og:title", "twitter:title")
            desc = meta(page, "og:description", "twitter:description", "description")
            image = meta(page, "og:image", "twitter:image", "og:image:url")
            if not e["text"] and desc: e["text"] = desc
            if not e["who"] and title: e["who"] = title
            if image and not e["img"]:
                if image.startswith("/"):
                    from urllib.parse import urljoin; image = urljoin(c["url"], image)
                raw = fetch(image, binary=True)
                ext = ".png" if raw[:4] == b"\x89PNG" else ".jpg"
                name = hashlib.sha1(c["url"].encode()).hexdigest()[:12] + ext
                (IMG / name).write_bytes(raw); e["img"] = f"callouts/auto/{name}"
        except Exception as err:
            print("could not read", c["url"], "-", err)
        out.append(e)
    OUT.write_text(json.dumps({"updated": int(time.time()), "items": out}, indent=2))
    print(f"wrote {len(out)} callouts")

if __name__ == "__main__":
    main()
