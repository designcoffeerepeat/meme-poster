"""Same as fetch_music.py but loads each Pixabay page in a real browser (Playwright)."""
import re, sys, time, zipfile
from playwright.sync_api import sync_playwright
TRACKS = [l.split("|") for l in open(sys.argv[1]).read().strip().splitlines()]
z = zipfile.ZipFile("tiktok_music.zip", "w", zipfile.ZIP_STORED)
ok = 0
with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
    ctx = b.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36",
                        locale="en-US", viewport={"width": 1366, "height": 900})
    page = ctx.new_page()
    seen = []
    page.on("request", lambda r: seen.append(r.url) if ".mp3" in r.url else None)
    for num, name, url in TRACKS:
        try:
            seen.clear()
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            for _ in range(15):
                html = page.content()
                if "Just a moment" not in html[:5000]:
                    break
                time.sleep(2)
            html = page.content().replace("\\/", "/")
            m = re.findall(r'https://cdn\.pixabay\.com/(?:download/)?audio/[^"\'\s<>]+?\.mp3[^"\'\s<>]*', html)
            if not m:
                try:
                    page.click("button[aria-label*='Play'], [class*='play']", timeout=5000); time.sleep(3)
                except Exception:
                    pass
                m = [u for u in seen if "cdn.pixabay.com" in u]
            if not m:
                print(f"{num} {name}: no mp3 link (title: {page.title()[:60]})"); continue
            mp3 = m[0].replace("&amp;", "&")
            resp = ctx.request.get(mp3, headers={"Referer": url}, timeout=120000)
            if resp.status != 200:
                print(f"{num} {name}: mp3 HTTP {resp.status}"); continue
            data = resp.body()
            z.writestr(f"{int(num):02d} - {name}.mp3", data); ok += 1
            print(f"{num} {name}: {len(data)//1024} KB")
        except Exception as e:
            print(f"{num} {name}: FAILED {str(e)[:150]}")
        time.sleep(2)
    b.close()
z.close()
print(f"{ok} of {len(TRACKS)} downloaded")
