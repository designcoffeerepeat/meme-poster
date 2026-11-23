#!/usr/bin/env python3
"""
Daily meme poster for Bluesky, Instagram, X, TikTok and Facebook.

Each run posts the next unposted meme from queue.json to every platform that is
set up (at most one post per platform per day), with alt text and hashtags.
Once a meme is posted everywhere it moves into the _posted folder.

Usage:
    python post_meme.py              post the next meme (once per day per platform)
    python post_meme.py --dry-run    show what would be posted; no login, no changes
    python post_meme.py --check      test the logins only; posts nothing
    python post_meme.py --force      post even if something already went out today
    python post_meme.py --only bluesky|instagram|x|tiktok|facebook   limit a run to one platform
    python post_meme.py --setup-instagram          (used by setup_instagram.bat)

Files (all next to this script, in the _bluesky_poster folder):
    config.json   logins (created by setup.bat / setup_instagram.bat)
    queue.json    the memes, alt text and hashtags, in posting order
    post_log.txt  a line for every run
The memes themselves are in the parent folder.
"""
import base64
import datetime as _dt
import hashlib
import hmac
import io
import json
import os
import shutil
import sys
import time
import secrets
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
MEME_DIR = os.path.dirname(HERE)
POSTED_DIR = os.path.join(MEME_DIR, "_posted")
QUEUE_PATH = os.path.join(HERE, "queue.json")
CONFIG_PATH = os.path.join(HERE, "config.json")
LOG_PATH = os.path.join(HERE, "post_log.txt")

MAX_BLOB_BYTES = 950_000          # Bluesky's image limit is ~1,000,000 bytes
MAX_TEXT_GRAPHEMES = 300
MAX_ALT_CHARS = 2000
IG_MIN_RATIO, IG_MAX_RATIO = 0.8, 1.91   # Instagram feed: 4:5 portrait .. 1.91:1 landscape
IG_REFRESH_AFTER_DAYS = 7               # long-lived tokens last 60 days; refresh well before

# Per-platform progress fields stored on each queue item.
DONE_FIELD = {"bluesky": "posted_at", "instagram": "ig_posted_at", "x": "x_posted_at",
              "tiktok": "tt_posted_at", "facebook": "fb_posted_at"}
X_ALT_MAX = 1000
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")


# ---------------------------------------------------------------- utilities
def log(msg):
    stamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {msg}"
    print(line)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def load_json(path):
    # utf-8-sig tolerates the byte-order mark Windows tools sometimes add
    with open(path, "r", encoding="utf-8-sig") as fh:
        return json.load(fh)


def save_json_atomic(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


DBX = None          # set when memes come from Dropbox (cloud runs: MEME_SOURCE=dropbox)


def load_config():
    global DBX
    cfg = load_json(CONFIG_PATH) if os.path.exists(CONFIG_PATH) else {}
    if os.environ.get("MEME_SOURCE") == "dropbox" and DBX is None:
        from dropbox_store import DropboxStore
        DBX = DropboxStore(cfg["dropbox"])
    return cfg


def enabled_platforms(cfg):
    out = []
    if (cfg.get("handle") or "").strip() and (cfg.get("app_password") or "").strip():
        out.append("bluesky")
    if ((cfg.get("instagram") or {}).get("access_token") or "").strip():
        out.append("instagram")
    x = cfg.get("x") or {}
    direct = all((x.get(k) or "").strip() for k in ("api_key", "api_secret", "access_token", "access_secret"))
    buffer = (x.get("buffer_api_key") or "").strip() and (x.get("channel_id") or "").strip()
    if direct or buffer:
        out.append("x")
    tt = cfg.get("tiktok") or {}
    if (tt.get("channel_id") or "").strip() and buffer_key(cfg):
        out.append("tiktok")
    if ((cfg.get("facebook") or {}).get("channel_id") or "").strip() and buffer_key(cfg):
        out.append("facebook")
    return out


def http(method, url, data=None, headers=None, timeout=90, tries=4):
    for attempt in range(tries):
        req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            raise RuntimeError(f"{method} {url.split('?')[0]} failed: HTTP {e.code} {detail}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            # No connection yet (e.g. the PC just woke up) - wait and try again.
            if attempt == tries - 1:
                raise RuntimeError(f"{method} {url.split('?')[0]} failed: no connection ({e})") from None
            time.sleep(30)


def multipart(fields, file_field, filename, file_bytes, mime):
    boundary = uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; '
                  f'filename="{filename}"\r\nContent-Type: {mime}\r\n\r\n').encode())
    parts.append(file_bytes)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


# ---------------------------------------------------------------- shared content prep
def hashtags_of(item):
    tags = []
    for t in item["hashtags"]:
        t = t.strip()
        if t:
            tags.append(t if t.startswith("#") else "#" + t)
    return tags[:5]                                   # never more than 5


def build_text_and_facets(hashtags):
    tags = hashtags_of({"hashtags": hashtags})
    text = " ".join(tags)
    facets, pos = [], 0
    for t in tags:
        start = len(text[:pos].encode("utf-8"))
        facets.append({
            "index": {"byteStart": start, "byteEnd": start + len(t.encode("utf-8"))},
            "features": [{"$type": "app.bsky.richtext.facet#tag", "tag": t[1:]}],
        })
        pos += len(t) + 1
    if len(text) > MAX_TEXT_GRAPHEMES:
        raise ValueError("hashtags are longer than 300 characters")
    return text, facets


def find_image(name):
    if DBX is not None:
        return DBX.fetch(name)
    for folder in (MEME_DIR, POSTED_DIR):
        p = os.path.join(folder, name)
        if os.path.exists(p):
            return p
    return None


def next_item(queue, platform):
    field = DONE_FIELD[platform]
    for item in queue["items"]:
        if item.get("skip") or item.get(field) or item.get("missing"):
            continue
        return item
    return None


def posted_today(queue, platform):
    today = _dt.date.today().isoformat()
    field = DONE_FIELD[platform]
    return any((i.get(field) or "").startswith(today) and not i.get(f"{platform}_catchup")
               for i in queue["items"])


def resolve_item(queue, platform, dry):
    """Next item for this platform whose image files exist (marks missing ones)."""
    while True:
        item = next_item(queue, platform)
        if item is None:
            return None, None
        paths = [find_image(img["file"]) for img in item["images"]]
        if all(paths):
            return item, paths
        missing = [img["file"] for img, p in zip(item["images"], paths) if not p]
        log(f"Item {item['id']}: image not found ({', '.join(missing)}) - skipping it.")
        item["missing"] = True
        if not dry:
            save_json_atomic(QUEUE_PATH, queue)


# ---------------------------------------------------------------- bluesky
def bsky_prepare_image(path):
    """Return (bytes, mime, width, height), shrinking/converting if needed."""
    from PIL import Image, ImageOps

    with Image.open(path) as im:
        im.seek(0)
        im = ImageOps.exif_transpose(im)
        width, height = im.size
        ext = os.path.splitext(path)[1].lower()
        raw = open(path, "rb").read()
        if ext in (".jpg", ".jpeg") and len(raw) <= MAX_BLOB_BYTES:
            return raw, "image/jpeg", width, height
        if ext == ".png" and len(raw) <= MAX_BLOB_BYTES:
            return raw, "image/png", width, height
        rgb = im.convert("RGB")
        scale, quality = 1.0, 90
        while True:
            w, h = max(1, int(width * scale)), max(1, int(height * scale))
            img = rgb if scale == 1.0 else rgb.resize((w, h), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=quality, optimize=True)
            if buf.tell() <= MAX_BLOB_BYTES:
                return buf.getvalue(), "image/jpeg", w, h
            if quality > 70:
                quality -= 10
            else:
                scale *= 0.85


class Bsky:
    def __init__(self, pds):
        self.pds = pds.rstrip("/")
        self.jwt = None
        self.did = None

    def _call(self, nsid, body=None, data=None, content_type=None):
        headers = {}
        if self.jwt:
            headers["Authorization"] = f"Bearer {self.jwt}"
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            content_type = "application/json"
        if content_type:
            headers["Content-Type"] = content_type
        raw = http("POST", f"{self.pds}/xrpc/{nsid}", data=data, headers=headers)
        return json.loads(raw) if raw else {}

    def login(self, handle, password):
        out = self._call("com.atproto.server.createSession",
                         body={"identifier": handle, "password": password})
        self.jwt, self.did = out["accessJwt"], out["did"]

    def upload_blob(self, data, mime):
        return self._call("com.atproto.repo.uploadBlob", data=data, content_type=mime)["blob"]

    def create_post(self, record):
        return self._call("com.atproto.repo.createRecord", body={
            "repo": self.did, "collection": "app.bsky.feed.post", "record": record})


def bsky_login(cfg):
    api = Bsky(cfg.get("pds") or "https://bsky.social")
    api.login(cfg["handle"].strip().lstrip("@"), cfg["app_password"].strip())
    return api


def bsky_post(cfg, item, paths):
    text, facets = build_text_and_facets(item["hashtags"])
    prepared = [bsky_prepare_image(p) for p in paths]
    api = bsky_login(cfg)
    images = []
    for img, (data, mime, w, h) in zip(item["images"], prepared):
        images.append({"alt": img["alt"][:MAX_ALT_CHARS],
                       "image": api.upload_blob(data, mime),
                       "aspectRatio": {"width": w, "height": h}})
    record = {
        "$type": "app.bsky.feed.post",
        "text": text,
        "facets": facets,
        "langs": ["en"],
        "createdAt": _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "embed": {"$type": "app.bsky.embed.images", "images": images},
    }
    out = api.create_post(record)
    item["posted_at"] = _dt.datetime.now().isoformat(timespec="seconds")
    item["uri"] = out.get("uri")
    return item["uri"]


# ---------------------------------------------------------------- instagram
def ig_prepare_image(path):
    """JPEG within Instagram's aspect-ratio and size limits (pads, never crops)."""
    from PIL import Image, ImageOps

    with Image.open(path) as im:
        im.seek(0)
        im = ImageOps.exif_transpose(im).convert("RGB")
    w, h = im.size

    # Pad colour = median of the border pixels, so bars blend with the meme.
    border = ([im.getpixel((x, 0)) for x in range(0, w, max(1, w // 50))] +
              [im.getpixel((x, h - 1)) for x in range(0, w, max(1, w // 50))] +
              [im.getpixel((0, y)) for y in range(0, h, max(1, h // 50))] +
              [im.getpixel((w - 1, y)) for y in range(0, h, max(1, h // 50))])
    fill = tuple(sorted(c[i] for c in border)[len(border) // 2] for i in range(3))

    ratio = w / h
    if ratio < IG_MIN_RATIO:
        new_w, new_h = int(round(h * IG_MIN_RATIO)) + 1, h
    elif ratio > IG_MAX_RATIO:
        new_w, new_h = w, int(round(w / IG_MAX_RATIO)) + 1
    else:
        new_w, new_h = w, h
    if (new_w, new_h) != (w, h):
        canvas = Image.new("RGB", (new_w, new_h), fill)
        canvas.paste(im, ((new_w - w) // 2, (new_h - h) // 2))
        im, w, h = canvas, new_w, new_h

    target_w = 1440 if w > 1440 else (1080 if w < 600 else w)
    if target_w != w:
        im = im.resize((target_w, max(1, round(h * target_w / w))), Image.LANCZOS)

    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=92, optimize=True)
    return buf.getvalue(), im.size


def host_temporarily(jpeg_bytes, cfg, keep="1h"):
    """Instagram fetches images from a public URL, so park the file on a
    temporary host for a few hours (it deletes itself afterwards)."""
    name = f"meme_{uuid.uuid4().hex[:10]}.jpg"
    hosts = cfg.get("_test_hosts") or ["litterbox", "uguu", "tmpfiles"]
    errors = []
    for attempt in range(2):                         # two passes over all hosts
        for host in hosts:
            try:
                if host == "litterbox":
                    body, ctype = multipart({"reqtype": "fileupload", "time": keep},
                                            "fileToUpload", name, jpeg_bytes, "image/jpeg")
                    url = http("POST", "https://litterbox.catbox.moe/resources/internals/api.php",
                               data=body, headers={"Content-Type": ctype, "User-Agent": BROWSER_UA}
                               ).decode().strip()
                    if url.startswith("https://"):
                        return url
                    errors.append(f"litterbox: {url[:120]}")
                elif host == "uguu":
                    body, ctype = multipart({}, "files[]", name, jpeg_bytes, "image/jpeg")
                    out = json.loads(http("POST", "https://uguu.se/upload", data=body,
                                          headers={"Content-Type": ctype, "User-Agent": BROWSER_UA}))
                    return out["files"][0]["url"]
                elif host == "tmpfiles":
                    body, ctype = multipart({}, "file", name, jpeg_bytes, "image/jpeg")
                    out = json.loads(http("POST", "https://tmpfiles.org/api/v1/upload", data=body,
                                          headers={"Content-Type": ctype, "User-Agent": BROWSER_UA}))
                    url = out["data"]["url"].replace("http://", "https://")
                    return url.replace("tmpfiles.org/", "tmpfiles.org/dl/", 1)
                elif host.startswith("http"):           # test hook: a direct URL
                    return host
            except Exception as e:                      # try the next host
                errors.append(f"{host}: {str(e)[:150]}")
        time.sleep(20)
    raise RuntimeError("could not host the image for Instagram: " + "; ".join(errors))


class Insta:
    def __init__(self, cfg):
        ig = cfg["instagram"]
        self.base = (cfg.get("_test_graph") or "https://graph.instagram.com").rstrip("/")
        self.token = ig["access_token"].strip()

    def get(self, path, **params):
        params["access_token"] = self.token
        return json.loads(http("GET", f"{self.base}/{path}?{urllib.parse.urlencode(params)}"))

    def post(self, path, **params):
        params["access_token"] = self.token
        return json.loads(http("POST", f"{self.base}/{path}",
                               data=urllib.parse.urlencode(params).encode(),
                               headers={"Content-Type": "application/x-www-form-urlencoded"}))

    def me(self):
        return self.get("me", fields="user_id,username")


def ig_maybe_refresh_token(cfg):
    ig = cfg["instagram"]
    last = ig.get("token_refreshed_at")
    if last:
        age = _dt.datetime.now() - _dt.datetime.fromisoformat(last)
        if age < _dt.timedelta(days=IG_REFRESH_AFTER_DAYS):
            return
    try:
        api = Insta(cfg)
        out = api.get("refresh_access_token", grant_type="ig_refresh_token")
        ig["access_token"] = out["access_token"]
        ig["token_refreshed_at"] = _dt.datetime.now().isoformat(timespec="seconds")
        save_json_atomic(CONFIG_PATH, cfg)
        log("Instagram token refreshed (good for another 60 days).")
    except Exception as e:
        log(f"Instagram token refresh skipped: {e}")


def ig_post(cfg, item, paths):
    ig_maybe_refresh_token(cfg)
    api = Insta(cfg)
    user_id = cfg["instagram"].get("user_id") or api.me()["user_id"]
    caption = " ".join(hashtags_of(item))
    if len(paths) > 1:
        log(f"Item {item['id']}: Instagram gets the first image only.")
    jpeg, _ = ig_prepare_image(paths[0])
    url = host_temporarily(jpeg, cfg)

    container = api.post(f"{user_id}/media", image_url=url, caption=caption,
                         alt_text=item["images"][0]["alt"][:MAX_ALT_CHARS])["id"]
    for _ in range(30):                              # wait until Instagram has the image
        status = api.get(container, fields="status_code").get("status_code")
        if status == "FINISHED":
            break
        if status in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram rejected the image (status {status}).")
        time.sleep(3)
    media = api.post(f"{user_id}/media_publish", creation_id=container)["id"]
    item["ig_posted_at"] = _dt.datetime.now().isoformat(timespec="seconds")
    item["ig_id"] = media
    return media


# ---------------------------------------------------------------- x (twitter)
def _pct(v):
    return urllib.parse.quote(str(v), safe="-._~")


class XApi:
    """X API v2 with OAuth 1.0a user keys from the developer console."""

    def __init__(self, cfg):
        x = cfg["x"]
        self.base = (cfg.get("_test_x") or "https://api.x.com").rstrip("/")
        self.ck, self.cs = x["api_key"].strip(), x["api_secret"].strip()
        self.tk, self.ts = x["access_token"].strip(), x["access_secret"].strip()

    def _auth(self, method, url):
        oauth = {
            "oauth_consumer_key": self.ck, "oauth_nonce": secrets.token_hex(16),
            "oauth_signature_method": "HMAC-SHA1", "oauth_timestamp": str(int(time.time())),
            "oauth_token": self.tk, "oauth_version": "1.0",
        }
        parsed = urllib.parse.urlsplit(url)
        params = dict(urllib.parse.parse_qsl(parsed.query), **oauth)
        norm = "&".join(f"{_pct(k)}={_pct(v)}" for k, v in sorted(params.items()))
        base_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        base = "&".join([method.upper(), _pct(base_url), _pct(norm)])
        key = f"{_pct(self.cs)}&{_pct(self.ts)}".encode()
        oauth["oauth_signature"] = base64.b64encode(
            hmac.new(key, base.encode(), hashlib.sha1).digest()).decode()
        return "OAuth " + ", ".join(f'{_pct(k)}="{_pct(v)}"' for k, v in sorted(oauth.items()))

    def call(self, method, path, body=None):
        url = f"{self.base}{path}"
        headers = {"Authorization": self._auth(method, url)}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        return json.loads(http(method, url, data=data, headers=headers) or b"{}")

    def me(self):
        return self.call("GET", "/2/users/me")["data"]


# ---- X through Buffer (free: Buffer's free plan includes API access) ----
def buffer_key(cfg):
    return ((cfg.get("buffer") or {}).get("api_key") or (cfg.get("x") or {}).get("buffer_api_key") or "").strip()


class BufferApi:
    def __init__(self, cfg):
        self.url = (cfg.get("_test_buffer") or "https://api.buffer.com").rstrip("/")
        self.key = buffer_key(cfg)

    def gql(self, query, variables=None):
        body = json.dumps({"query": query, "variables": variables or {}}).encode()
        out = json.loads(http("POST", self.url, data=body, headers={
            "Authorization": f"Bearer {self.key}", "Content-Type": "application/json"}))
        if out.get("errors"):
            raise RuntimeError("Buffer: " + "; ".join(e.get("message", "?") for e in out["errors"]))
        return out["data"]

    def organizations(self):
        last = None
        for q in ("query { account { organizations { id name } } }",
                  "query { organizations { id name } }"):
            try:
                d = self.gql(q)
                orgs = (d.get("account") or {}).get("organizations") if "account" in d else d.get("organizations")
                if orgs:
                    return orgs
            except Exception as e:
                last = e
        raise RuntimeError(f"Buffer: couldn't list your organizations ({last})")

    def x_channels(self):
        return self.channels_for(("twitter", "x"))

    def channels_for(self, services):
        found = []
        for org in self.organizations():
            d = self.gql("query($o: OrganizationId!) { channels(input: {organizationId: $o}) "
                         "{ id name service } }", {"o": org["id"]})
            for ch in d["channels"]:
                if str(ch.get("service", "")).lower() in services:
                    found.append(ch)
        return found

    def alt_field(self):
        try:
            d = self.gql('query { __type(name: "ImageAssetInput") { inputFields { name } } }')
            names = [f["name"] for f in (d.get("__type") or {}).get("inputFields") or []]
            for cand in ("altText", "alt", "description"):
                if cand in names:
                    return cand
        except Exception:
            pass
        return None


def to_jpeg(path, max_bytes=4_500_000):
    from PIL import Image, ImageOps
    with Image.open(path) as im:
        im.seek(0)
        im = ImageOps.exif_transpose(im).convert("RGB")
    quality = 92
    while True:
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=quality, optimize=True)
        if buf.tell() <= max_bytes or quality <= 50:
            return buf.getvalue()
        quality -= 10


def tiktok_prepare(path):
    """TikTok photo posts are 9:16; pad (never crop) to 1080x1920 in a matching colour."""
    from PIL import Image, ImageOps
    with Image.open(path) as im:
        im.seek(0)
        im = ImageOps.exif_transpose(im).convert("RGB")
    w, h = im.size
    border = ([im.getpixel((x, 0)) for x in range(0, w, max(1, w // 50))] +
              [im.getpixel((x, h - 1)) for x in range(0, w, max(1, w // 50))])
    fill = tuple(sorted(c[i] for c in border)[len(border) // 2] for i in range(3))
    W, H = 1080, 1920
    scale = min(W / w, H / h)
    im = im.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
    canvas = Image.new("RGB", (W, H), fill)
    canvas.paste(im, ((W - im.width) // 2, (H - im.height) // 2))
    buf = io.BytesIO()
    canvas.save(buf, "JPEG", quality=92, optimize=True)
    return buf.getvalue()


def tiktok_post(cfg, item, paths):
    api = BufferApi(cfg)
    urls = [host_temporarily(tiktok_prepare(p), cfg, keep="12h") for p in paths[:35]]
    mutation = ("mutation($input: CreatePostInput!) { createPost(input: $input) { __typename "
                "... on PostActionSuccess { post { id } } ... on MutationError { message } } }")
    variables = {"input": {"text": " ".join(hashtags_of(item)), "channelId": cfg["tiktok"]["channel_id"],
                           "schedulingType": "automatic", "mode": "shareNow",
                           "assets": [{"image": {"url": u}} for u in urls]}}
    res = api.gql(mutation, variables)["createPost"]
    if res.get("__typename") != "PostActionSuccess":
        raise RuntimeError(f"Buffer refused the TikTok post: {res.get('message')}")
    item["tt_posted_at"] = _dt.datetime.now().isoformat(timespec="seconds")
    item["tt_id"] = f"buffer:{res['post']['id']}"
    return f"sent to TikTok via Buffer (post {res['post']['id']})"


def buffer_post_with_alt(cfg, item, paths, section, label):
    api = BufferApi(cfg)
    xcfg = cfg[section]
    urls = [host_temporarily(to_jpeg(p), cfg, keep="12h")
            for _, p in list(zip(item["images"], paths))[:4]]
    alts = [img["alt"][:X_ALT_MAX] for img in item["images"][:4]]
    mutation = ("mutation($input: CreatePostInput!) { createPost(input: $input) { __typename "
                "... on PostActionSuccess { post { id } } ... on MutationError { message } } }")

    # Alt text field name isn't documented; try likely names, then fall back to none.
    known = xcfg.get("alt_field")
    candidates = [known] if known and known != "none" else (["altText", "alt"] if known != "none" else [])
    candidates.append(None)
    res, last_err = None, None
    for field in candidates:
        assets = []
        for url, alt in zip(urls, alts):
            image = {"url": url}
            if field:
                image[field] = alt
            assets.append({"image": image})
        variables = {"input": {"text": " ".join(hashtags_of(item)), "channelId": xcfg["channel_id"],
                               "schedulingType": "automatic", "mode": "shareNow", "assets": assets}}
        if section == "facebook":                 # Buffer requires a Facebook post type
            variables["input"]["metadata"] = {"facebook": {"type": "post"}}
        try:
            res = api.gql(mutation, variables)["createPost"]
        except RuntimeError as e:
            last_err = e
            if field and field in str(e):        # Buffer rejected this alt-text field name
                continue
            raise
        if field != known:                       # remember what works for next time
            xcfg["alt_field"] = field or "none"
            save_json_atomic(CONFIG_PATH, cfg)
            log(f"{label} via Buffer: alt text {'sent as ' + repr(field) if field else 'not supported - posting without it'}.")
        break
    if res is None:
        raise last_err
    if res.get("__typename") != "PostActionSuccess":
        raise RuntimeError(f"Buffer refused the {label} post: {res.get('message')}")
    return res['post']['id']


def x_post_via_buffer(cfg, item, paths):
    pid = buffer_post_with_alt(cfg, item, paths, "x", "X")
    item["x_posted_at"] = _dt.datetime.now().isoformat(timespec="seconds")
    item["x_id"] = f"buffer:{pid}"
    return f"sent to X via Buffer (post {pid})"


def facebook_post(cfg, item, paths):
    pid = buffer_post_with_alt(cfg, item, paths, "facebook", "Facebook")
    item["fb_posted_at"] = _dt.datetime.now().isoformat(timespec="seconds")
    item["fb_id"] = f"buffer:{pid}"
    return f"sent to Facebook via Buffer (post {pid})"


def x_post(cfg, item, paths):
    if (cfg["x"].get("buffer_api_key") or "").strip():
        return x_post_via_buffer(cfg, item, paths)
    api = XApi(cfg)
    media_ids = []
    for img, path in list(zip(item["images"], paths))[:4]:        # X allows 4 images
        data, mime, _, _ = bsky_prepare_image(path)                 # <1 MB JPEG/PNG
        up = api.call("POST", "/2/media/upload", {
            "media": base64.b64encode(data).decode(), "media_category": "tweet_image"})
        mid = str(up["data"]["id"])
        api.call("POST", "/2/media/metadata", {
            "id": mid, "metadata": {"alt_text": {"text": img["alt"][:X_ALT_MAX]}}})
        media_ids.append(mid)
    out = api.call("POST", "/2/tweets", {"text": " ".join(hashtags_of(item)),
                                         "media": {"media_ids": media_ids}})
    tweet_id = out["data"]["id"]
    item["x_posted_at"] = _dt.datetime.now().isoformat(timespec="seconds")
    item["x_id"] = tweet_id
    user = (cfg["x"].get("username") or "i")
    return f"https://x.com/{user}/status/{tweet_id}"


POSTERS = {"bluesky": bsky_post, "instagram": ig_post, "x": x_post, "tiktok": tiktok_post, "facebook": facebook_post}


# ---------------------------------------------------------------- housekeeping
def move_if_done(item, paths, platforms):
    if not all(item.get(DONE_FIELD[p]) for p in platforms):
        return
    if DBX is not None:
        for p in paths:
            if p:
                try:
                    if DBX.move_to_posted(os.path.basename(p)):
                        log(f"Moved {os.path.basename(p)} into _posted (Dropbox).")
                except Exception as e:
                    log(f"Posted, but could not move {os.path.basename(p)} into _posted: {e}")
        return
    os.makedirs(POSTED_DIR, exist_ok=True)
    for p in paths:
        if p and os.path.dirname(p) != POSTED_DIR and os.path.exists(p):
            try:
                shutil.move(p, os.path.join(POSTED_DIR, os.path.basename(p)))
            except OSError as e:
                log(f"Posted, but could not move {p} into _posted: {e}")


def setup_bluesky():
    handle = os.environ.get("BSKY_HANDLE", "").strip().lstrip("@")
    password = os.environ.get("BSKY_PW", "").strip()
    if not handle or not password:
        raise SystemExit("No handle/app password given.")
    cfg = load_config()                       # keep any Instagram settings
    cfg.update({"handle": handle, "app_password": password,
                "pds": cfg.get("pds") or "https://bsky.social"})
    api = bsky_login(cfg)
    save_json_atomic(CONFIG_PATH, cfg)
    log(f"Bluesky connected: {handle} ({api.did}).")
    return 0


def _start_in_step(platform="x"):
    field = DONE_FIELD[platform]
    # Start X in step with Bluesky: memes Bluesky already posted are not re-posted on X.
    if os.path.exists(QUEUE_PATH):
        queue = load_json(QUEUE_PATH)
        n = 0
        for item in queue["items"]:
            if item.get("posted_at") and not item.get(field):
                item[field] = f"skipped (posted before {platform} was set up)"
                n += 1
        save_json_atomic(QUEUE_PATH, queue)
        log(f"{platform}: starting with the same meme as Bluesky ({n} earlier memes skipped).")


def setup_x_buffer():
    key = os.environ.get("BUFFER_KEY", "").strip()
    if not key:
        raise SystemExit("No Buffer API key given.")
    cfg = load_config()
    cfg["x"] = {"buffer_api_key": key}
    api = BufferApi(cfg)
    chans = api.x_channels()
    if not chans:
        raise RuntimeError("The Buffer key works, but no X account is connected in Buffer yet.")
    ch = chans[0]
    cfg["x"].update({"channel_id": ch["id"], "username": ch.get("name"),
                     "alt_field": api.alt_field()})
    save_json_atomic(CONFIG_PATH, cfg)
    if not cfg["x"]["alt_field"]:
        log("Note: Buffer didn't list its alt-text field; the first X post will test the likely names.")
    _start_in_step("x")
    log(f"X connected through Buffer: {ch.get('name')} (channel {ch['id']}).")
    return 0


def setup_tiktok_buffer():
    cfg = load_config()
    key = os.environ.get("BUFFER_KEY", "").strip() or buffer_key(cfg)
    if not key:
        raise SystemExit("No Buffer API key found - run setup_x_free.bat first or paste the key.")
    cfg.setdefault("buffer", {})["api_key"] = key
    chans = BufferApi(cfg).channels_for(("tiktok",))
    if not chans:
        raise RuntimeError("Buffer works, but no TikTok account is connected in Buffer yet.")
    ch = chans[0]
    cfg["tiktok"] = {"channel_id": ch["id"], "username": ch.get("name")}
    save_json_atomic(CONFIG_PATH, cfg)
    _start_in_step("tiktok")
    log(f"TikTok connected through Buffer: {ch.get('name')} (channel {ch['id']}).")
    return 0


def setup_facebook_buffer():
    cfg = load_config()
    key = os.environ.get("BUFFER_KEY", "").strip() or buffer_key(cfg)
    if not key:
        raise SystemExit("No Buffer API key found - run setup_x_free.bat first or paste the key.")
    cfg.setdefault("buffer", {})["api_key"] = key
    chans = BufferApi(cfg).channels_for(("facebook",))
    if not chans:
        raise RuntimeError("Buffer works, but no Facebook Page is connected in Buffer yet.")
    ch = chans[0]
    cfg["facebook"] = {"channel_id": ch["id"], "username": ch.get("name"), "alt_field": None}
    save_json_atomic(CONFIG_PATH, cfg)
    _start_in_step("facebook")
    log(f"Facebook connected through Buffer: {ch.get('name')} (channel {ch['id']}).")
    return 0


def setup_x():
    keys = {k: os.environ.get(e, "").strip() for k, e in (
        ("api_key", "X_API_KEY"), ("api_secret", "X_API_SECRET"),
        ("access_token", "X_ACCESS_TOKEN"), ("access_secret", "X_ACCESS_SECRET"))}
    if not all(keys.values()):
        raise SystemExit("One of the four X keys is missing.")
    cfg = load_config()
    cfg["x"] = keys
    me = XApi(cfg).me()
    cfg["x"]["username"] = me.get("username")
    save_json_atomic(CONFIG_PATH, cfg)
    _start_in_step("x")
    log(f"X connected: @{me.get('username')}.")
    return 0


def setup_instagram():
    token = os.environ.get("IG_TOKEN", "").strip()
    if not token:
        raise SystemExit("No token given.")
    cfg = load_config()
    cfg["instagram"] = {"access_token": token}
    me = Insta(cfg).me()
    cfg["instagram"].update({"user_id": str(me["user_id"]), "username": me.get("username"),
                             "token_refreshed_at": _dt.datetime.now().isoformat(timespec="seconds")})
    save_json_atomic(CONFIG_PATH, cfg)
    log(f"Instagram connected: @{me.get('username')} (id {me['user_id']}).")
    return 0


# ---------------------------------------------------------------- catch-up
def _is_real_post(value):
    return bool(value) and not str(value).startswith("skipped")


def catch_up(targets, every_seconds):
    """Post the memes the `targets` accounts missed, one round every `every_seconds`
    (each round posts one meme per lagging account), until every account has reached
    the same point in the queue as the furthest-ahead one."""
    cfg = load_config()
    platforms = enabled_platforms(cfg)
    targets = [t for t in targets if t in platforms] if targets != ["all"] else platforms
    queue = load_json(QUEUE_PATH)
    items = queue["items"]
    lead = -1
    for p in platforms:
        for pos, it in enumerate(items):
            if _is_real_post(it.get(DONE_FIELD[p])):
                lead = max(lead, pos)
    backlogs = {}
    for p in targets:
        f = DONE_FIELD[p]
        ids = [it["id"] for it in items[:lead + 1]
               if not (it.get("skip") or it.get(f) or it.get("missing"))]
        if ids:
            backlogs[p] = ids
            log(f"{p}: catching up {len(ids)} missed memes (items {ids[0]}-{ids[-1]}), "
                f"one every {every_seconds // 60} minutes.")
    if not backlogs:
        log("Catch-up: every account is already in step - nothing to do.")
        return 0
    rounds = max(len(v) for v in backlogs.values())
    for r in range(rounds):
        if r:
            time.sleep(every_seconds)
        for p, ids in backlogs.items():
            if r >= len(ids):
                continue
            item_id, field = ids[r], DONE_FIELD[p]
            queue = load_json(QUEUE_PATH)               # re-read: the daily run may have saved
            item = next((i for i in queue["items"] if i["id"] == item_id), None)
            if not item or item.get(field) or item.get("skip"):
                continue
            paths = [find_image(img["file"]) for img in item["images"]]
            if not all(paths):
                log(f"{p}: catch-up item {item_id} image not found - skipping it.")
                continue
            try:
                ref = POSTERS[p](cfg, item, paths)
            except Exception as e:
                log(f"{p}: catch-up ERROR on item {item_id}: {e}")
                continue
            item[f"{p}_catchup"] = True
            save_json_atomic(QUEUE_PATH, queue)
            log(f"{p}: catch-up posted item {item_id} ({r + 1}/{len(ids)}) -> {ref}")
            move_if_done(item, paths, enabled_platforms(cfg))
    log("Catch-up finished - all accounts are in step.")
    return 0


# ---------------------------------------------------------------- main
def main(argv):
    if "--setup-instagram" in argv:
        return setup_instagram()
    if "--catchup" in argv:
        every = int(argv[argv.index("--every") + 1]) if "--every" in argv else 3600
        return catch_up(argv[argv.index("--catchup") + 1].lower().split(","), every)
    if "--setup-bluesky" in argv:
        return setup_bluesky()
    if "--setup-x" in argv:
        return setup_x()
    if "--setup-x-buffer" in argv:
        return setup_x_buffer()
    if "--setup-tiktok-buffer" in argv:
        return setup_tiktok_buffer()
    if "--setup-facebook-buffer" in argv:
        return setup_facebook_buffer()
    dry, check, force = "--dry-run" in argv, "--check" in argv, "--force" in argv
    cfg = load_config()
    platforms = enabled_platforms(cfg)
    if "--only" in argv:
        want = argv[argv.index("--only") + 1].lower()
        platforms = [p for p in platforms if p == want]
    if not platforms and not dry:
        raise SystemExit("Nothing is set up yet - run setup.bat (Bluesky), setup_instagram.bat or setup_x.bat.")

    if check:
        if DBX is not None:
            names = [n for n in DBX.list_names() if n.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic"))]
            log(f"Dropbox OK: {len(names)} images waiting in {DBX.folder}. Nothing was moved.")
        for p in platforms:
            if p == "bluesky":
                api = bsky_login(cfg)
                log(f"Bluesky login OK ({cfg['handle']}, {api.did}). Nothing was posted.")
            elif p == "facebook":
                chans = BufferApi(cfg).channels_for(("facebook",))
                log(f"Facebook (via Buffer) OK: {', '.join(c.get('name', '?') for c in chans)}. Nothing was posted.")
            elif p == "tiktok":
                chans = BufferApi(cfg).channels_for(("tiktok",))
                log(f"TikTok (via Buffer) OK: {', '.join(c.get('name', '?') for c in chans)}. Nothing was posted.")
            elif p == "x" and (cfg["x"].get("buffer_api_key") or "").strip():
                chans = BufferApi(cfg).x_channels()
                log(f"X (via Buffer) OK: {', '.join(c.get('name', '?') for c in chans)}. Nothing was posted.")
            elif p == "x":
                me = XApi(cfg).me()
                log(f"X login OK (@{me.get('username')}). Nothing was posted.")
            else:
                me = Insta(cfg).me()
                log(f"Instagram login OK (@{me.get('username')}). Nothing was posted.")
        return 0

    queue = load_json(QUEUE_PATH)

    if dry:
        for p in platforms or ["bluesky"]:
            item, paths = resolve_item(queue, p, dry=True)
            if not item:
                print(f"\n{p}: queue is empty.")
                continue
            print(f"\nNext on {p}: item {item['id']}")
            print(f"  text:  {' '.join(hashtags_of(item))}")
            for img, path in zip(item["images"], paths):
                print(f"  image: {img['file']}")
                print(f"  alt:   {img['alt'][:MAX_ALT_CHARS]}")
            if p == "instagram":
                _, size = ig_prepare_image(paths[0])
                print(f"  instagram version: {size[0]}x{size[1]} JPEG")
        return 0

    failures = 0
    for p in platforms:
        # An "extra" item posts whenever it's next. If it goes out at/after 4 PM, the
        # day's regular meme follows right after it, so the extra never replaces it.
        for _pass in range(2):
            nxt = next_item(queue, p)
            if posted_today(queue, p) and not force and not (nxt and nxt.get("extra")):
                if not _pass:
                    log(f"{p}: already posted today - nothing to do.")
                break
            item, paths = resolve_item(queue, p, dry=False)
            if item is None:
                log(f"{p}: queue is empty - every meme has been posted. Add more to queue.json.")
                break
            try:
                ref = POSTERS[p](cfg, item, paths)
                if item.get("extra"):
                    item[f"{p}_catchup"] = True     # an extra post doesn't count as the day's meme
            except Exception as e:
                failures += 1
                log(f"{p}: ERROR posting item {item['id']} ({item['images'][0]['file']}): {e}")
                with open(LOG_PATH, "a", encoding="utf-8") as fh:
                    traceback.print_exc(file=fh)
                break
            save_json_atomic(QUEUE_PATH, queue)
            log(f"{p}: posted item {item['id']} ({item['images'][0]['file']}) -> {ref}"
                + (" (extra post)" if item.get("extra") else ""))
            move_if_done(item, paths, enabled_platforms(cfg))
            left = sum(1 for i in queue["items"]
                       if not (i.get("skip") or i.get(DONE_FIELD[p]) or i.get("missing")))
            log(f"{p}: {left} memes left in the queue.")
            if not item.get("extra") or _dt.datetime.now().hour < 16:
                break
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except Exception as exc:  # keep a record when running unattended
        log(f"ERROR: {exc}")
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            traceback.print_exc(file=fh)
        sys.exit(1)
