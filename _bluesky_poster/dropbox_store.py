"""
Reads memes straight from the Dropbox memes folder and moves posted ones into its
_posted subfolder, so the poster can run in the cloud (GitHub Actions) with the
PC switched off.

Needs config["dropbox"] = {"app_key": ..., "refresh_token": ..., "folder": "/xVault Temp/memes"}
(written by move_to_github.bat on the PC).
"""
import json
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.dropboxapi.com/2/"
CONTENT = "https://content.dropboxapi.com/2/"
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic")


class DropboxError(RuntimeError):
    pass


class DropboxStore:
    def __init__(self, dbx_cfg):
        self.app_key = dbx_cfg["app_key"]
        self.refresh_token = dbx_cfg["refresh_token"]
        self.folder = "/" + dbx_cfg["folder"].strip("/")
        self.posted = self.folder + "/_posted"
        self._token = None
        self._token_until = 0
        self.tmp = tempfile.mkdtemp(prefix="memes_")

    # ------------------------------------------------------------ plumbing
    def _access_token(self):
        if self._token and time.time() < self._token_until - 60:
            return self._token
        body = urllib.parse.urlencode({"grant_type": "refresh_token",
                                       "refresh_token": self.refresh_token,
                                       "client_id": self.app_key}).encode()
        data = self._raw("https://api.dropboxapi.com/oauth2/token", body,
                         {"Content-Type": "application/x-www-form-urlencoded"}, auth=False)
        tok = json.loads(data)
        self._token = tok["access_token"]
        self._token_until = time.time() + int(tok.get("expires_in", 14400))
        return self._token

    def _raw(self, url, body, headers, auth=True, tries=4):
        for attempt in range(tries):
            h = dict(headers)
            if auth:
                h["Authorization"] = "Bearer " + self._access_token()
            req = urllib.request.Request(url, data=body, headers=h, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    return resp.read()
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")
                if e.code == 409:
                    raise DropboxError(detail)
                if e.code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                    time.sleep(15 * (attempt + 1))
                    continue
                raise DropboxError(f"HTTP {e.code}: {detail[:300]}")
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if attempt < tries - 1:
                    time.sleep(30)
                    continue
                raise DropboxError(f"network error: {e}")

    def _rpc(self, endpoint, args):
        return json.loads(self._raw(API + endpoint, json.dumps(args).encode(),
                                    {"Content-Type": "application/json"}))

    # ------------------------------------------------------------ public
    def list_names(self, folder=None):
        """File names (not subfolders) directly inside the memes folder (or `folder`)."""
        res = self._rpc("files/list_folder", {"path": folder or self.folder, "limit": 2000})
        entries = res["entries"]
        while res.get("has_more"):
            res = self._rpc("files/list_folder/continue", {"cursor": res["cursor"]})
            entries += res["entries"]
        return [e["name"] for e in entries if e[".tag"] == "file"]

    def exists(self, path):
        try:
            self._rpc("files/get_metadata", {"path": path})
            return True
        except DropboxError as e:
            if "not_found" in str(e):
                return False
            raise

    def download(self, path, dest):
        # Dropbox-API-Arg must be ASCII; json.dumps escapes anything else.
        data = self._raw(CONTENT + "files/download", b"",
                         {"Dropbox-API-Arg": json.dumps({"path": path}),
                          "Content-Type": "text/plain"})
        with open(dest, "wb") as fh:
            fh.write(data)
        return dest

    def fetch(self, name):
        """Local copy of a meme from the memes folder (or _posted). None if it's gone."""
        local = os.path.join(self.tmp, name)
        if os.path.exists(local):
            return local
        for folder in (self.folder, self.posted):
            try:
                return self.download(f"{folder}/{name}", local)
            except DropboxError as e:
                if "not_found" in str(e):
                    continue
                raise
        return None

    def move_to_posted(self, name):
        """Move a meme from the memes folder into _posted. Returns True if it moved."""
        src, dst = f"{self.folder}/{name}", f"{self.posted}/{name}"
        try:
            self._rpc("files/move_v2", {"from_path": src, "to_path": dst, "autorename": True})
            return True
        except DropboxError as e:
            if "from_lookup/not_found" in str(e):
                return False          # already moved (or deleted)
            raise
