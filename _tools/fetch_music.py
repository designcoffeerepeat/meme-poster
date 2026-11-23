"""Download a list of Pixabay music tracks (track page URLs) and zip them."""
import re, sys, time, zipfile, urllib.request
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36",
      "Accept-Language": "en-US,en;q=0.9"}
TRACKS = [l.split("|") for l in open(sys.argv[1]).read().strip().splitlines()]
z = zipfile.ZipFile("tiktok_music.zip", "w", zipfile.ZIP_STORED)
ok = 0
for num, name, url in TRACKS:
    try:
        html = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60).read().decode("utf-8", "replace")
        m = re.findall(r'https://cdn\.pixabay\.com/(?:download/)?audio/[^"\'\s<>]+?\.mp3[^"\'\s<>]*', html.replace("\\/", "/"))
        if not m:
            print(f"{num} {name}: no mp3 link found on page ({len(html)} bytes)"); continue
        mp3 = m[0].replace("&amp;", "&")
        data = urllib.request.urlopen(urllib.request.Request(mp3, headers=dict(UA, Referer=url)), timeout=120).read()
        z.writestr(f"{int(num):02d} - {name}.mp3", data); ok += 1
        print(f"{num} {name}: {len(data)//1024} KB")
    except Exception as e:
        print(f"{num} {name}: FAILED {e}")
    time.sleep(2)
z.close()
print(f"{ok} of {len(TRACKS)} downloaded")
