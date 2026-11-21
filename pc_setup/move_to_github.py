"""
One-time move of the meme poster from this PC to GitHub Actions.

What it does (your passwords never leave this PC unencrypted, and Claude never sees them):
  1. Signs you in to GitHub (GitHub CLI, installed if missing).
  2. Connects the poster to your Dropbox so it can read the memes folder and move
     posted memes into _posted.
  3. Stops the PC's daily posting task (GitHub takes over, so nothing double-posts).
  4. Encrypts config.json, stores the key as a GitHub secret, and uploads the
     encrypted logins + queue.json + post_log.txt to your private meme-poster repo.
  5. Runs a login test on GitHub.
"""
import base64
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
MEME_DIR = os.path.dirname(HERE)
CONFIG = os.path.join(HERE, "config.json")
QUEUE = os.path.join(HERE, "queue.json")
LOG = os.path.join(HERE, "post_log.txt")
REPO_NAME = "meme-poster"
TASK = "Bluesky Meme Poster"


def say(msg=""):
    print(msg, flush=True)


def fail(msg):
    say()
    say("PROBLEM: " + msg)
    say("Nothing else was changed. Send Claude a screenshot of this window.")
    input("\nPress Enter to close...")
    sys.exit(1)


# ------------------------------------------------------------------ GitHub CLI
def find_gh():
    gh = shutil.which("gh")
    for cand in (gh, r"C:\Program Files\GitHub CLI\gh.exe",
                 os.path.expandvars(r"%LOCALAPPDATA%\Programs\GitHub CLI\gh.exe")):
        if cand and os.path.exists(cand):
            return cand
    return None


def ensure_gh():
    gh = find_gh()
    if not gh:
        say("Installing GitHub CLI (one time)...")
        subprocess.run(["winget", "install", "--id", "GitHub.cli", "-e", "--silent",
                        "--accept-source-agreements", "--accept-package-agreements"])
        gh = find_gh()
    if not gh:
        fail("Couldn't install GitHub CLI. Install it from https://cli.github.com and run this again.")
    if subprocess.run([gh, "auth", "status"], capture_output=True).returncode != 0:
        say()
        say("Sign in to GitHub: a browser window will open. Copy the 8-character code shown")
        say("below, paste it in the browser, and approve.")
        r = subprocess.run([gh, "auth", "login", "--web", "--git-protocol", "https",
                            "--hostname", "github.com", "--scopes", "repo,workflow"])
        if r.returncode != 0:
            fail("GitHub sign-in didn't finish.")
    return gh


def gh_out(gh, *args):
    r = subprocess.run([gh, *args], capture_output=True, text=True)
    return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()


def gh_api(token, method, path, body=None):
    req = urllib.request.Request("https://api.github.com/" + path.lstrip("/"),
                                 data=json.dumps(body).encode() if body is not None else None,
                                 method=method,
                                 headers={"Authorization": "Bearer " + token,
                                          "Accept": "application/vnd.github+json",
                                          "User-Agent": "meme-poster-setup"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, {"message": e.read().decode("utf-8", "replace")[:300]}


def upload(token, repo, rel_path, data, message):
    status, cur = gh_api(token, "GET", f"repos/{repo}/contents/{rel_path}")
    body = {"message": message, "content": base64.b64encode(data).decode()}
    if status == 200 and isinstance(cur, dict) and cur.get("sha"):
        body["sha"] = cur["sha"]
    status, res = gh_api(token, "PUT", f"repos/{repo}/contents/{rel_path}", body)
    if status not in (200, 201):
        raise RuntimeError(f"upload of {rel_path} failed: {status} {res.get('message')}")


# ------------------------------------------------------------------ Dropbox
def dropbox_folder_path():
    for base in (os.environ.get("LOCALAPPDATA", ""), os.environ.get("APPDATA", "")):
        info = os.path.join(base, "Dropbox", "info.json")
        if os.path.exists(info):
            try:
                data = json.load(open(info, encoding="utf-8"))
                for acct in data.values():
                    root = acct.get("path")
                    if root and os.path.normcase(MEME_DIR).startswith(os.path.normcase(root)):
                        return _app_relative(os.path.relpath(MEME_DIR, root))
            except Exception:
                pass
    root = os.path.join(os.path.expanduser("~"), "Dropbox")
    return _app_relative(os.path.relpath(MEME_DIR, root))


def _app_relative(rel):
    """An 'App folder' Dropbox app sees Dropbox\\Apps\\<app> as its root."""
    parts = rel.replace("\\", "/").split("/")
    if len(parts) > 2 and parts[0].lower() == "apps":
        parts = parts[2:]
    return "/" + "/".join(parts)


def in_app_folder():
    """True when the memes folder sits inside Dropbox\\Apps\\<app>\\."""
    parts = [x.lower() for x in os.path.normpath(MEME_DIR).split(os.sep)]
    for i in range(len(parts) - 3):
        if parts[i].startswith("dropbox") and parts[i + 1] == "apps":
            return True
    return False


def dbx_post(url, body, headers):
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Dropbox said {e.code}: {e.read().decode('utf-8', 'replace')[:300]}")


def dropbox_test(app_key, refresh_token, folder):
    tok = dbx_post("https://api.dropboxapi.com/oauth2/token",
                   urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": refresh_token,
                                           "client_id": app_key}).encode(),
                   {"Content-Type": "application/x-www-form-urlencoded"})["access_token"]
    res = dbx_post("https://api.dropboxapi.com/2/files/list_folder",
                   json.dumps({"path": folder, "limit": 2000}).encode(),
                   {"Authorization": "Bearer " + tok, "Content-Type": "application/json"})
    return sum(1 for e in res["entries"] if e[".tag"] == "file")


def ensure_app_folder(access_token):
    """Dropbox only creates Dropbox\\Apps\\<app> once the app writes something, so write a note."""
    note = ("Move the whole memes folder into this folder (next to this note).\r\n"
            "The meme poster can only see what is inside this folder.\r\n").encode()
    req = urllib.request.Request(
        "https://content.dropboxapi.com/2/files/upload", data=note, method="POST",
        headers={"Authorization": "Bearer " + access_token,
                 "Content-Type": "application/octet-stream",
                 "Dropbox-API-Arg": json.dumps({"path": "/MOVE THE MEMES FOLDER HERE.txt",
                                                "mode": "overwrite", "mute": True})})
    with urllib.request.urlopen(req, timeout=60) as resp:
        resp.read()


def access_token_for(app_key, refresh_token):
    return dbx_post("https://api.dropboxapi.com/oauth2/token",
                    urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": refresh_token,
                                            "client_id": app_key}).encode(),
                    {"Content-Type": "application/x-www-form-urlencoded"})["access_token"]


def connect_dropbox(cfg):
    folder = dropbox_folder_path()
    old = cfg.get("dropbox") or {}
    if old.get("app_key") and old.get("refresh_token"):
        try:
            n = dropbox_test(old["app_key"], old["refresh_token"], folder)
            if old.get("folder") != folder:
                old["folder"] = folder
                save_config(cfg)
            say(f"Dropbox connected ({n} files in the memes folder).")
            return
        except Exception as e:
            try:
                tok_ok = access_token_for(old["app_key"], old["refresh_token"])
            except Exception:
                tok_ok = None          # app deleted / access removed: set it up again below
            if tok_ok and not in_app_folder():
                ensure_app_folder(tok_ok)
                fail("The memes folder isn't inside Dropbox\\Apps\\<your app's name> yet. Move the whole "
                     f"'{os.path.basename(MEME_DIR)}' folder there (it's the folder with a note called "
                     "'MOVE THE MEMES FOLDER HERE'; Dropbox may take a minute to sync it to this PC), "
                     "then run this again from its new place.")
            say(f"The saved Dropbox connection didn't work ({e}) - let's redo it.")
    say()
    say("=== Connect Dropbox (so GitHub can read the memes and move them into _posted) ===")
    say("1. A page will open: https://www.dropbox.com/developers/apps/create")
    say("   - Choose 'Scoped access', then 'App folder' (it can ONLY see its own folder)")
    say("   - Name it e.g. memeposter-2goat (must be unique), then 'Create app'")
    say("2. Open the 'Permissions' tab, tick files.metadata.read, files.content.read and")
    say("   files.content.write, then click 'Submit' at the bottom.")
    say("3. Back on the 'Settings' tab, copy the 'App key'.")
    say("   (Already made an 'App folder' app? Skip 1 - open it at dropbox.com/developers/apps,")
    say("    check step 2, and copy its App key.)")
    webbrowser.open("https://www.dropbox.com/developers/apps/create")
    app_key = input("\nPaste the App key here and press Enter: ").strip()
    if not app_key:
        fail("No App key was entered.")
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    url = ("https://www.dropbox.com/oauth2/authorize?" + urllib.parse.urlencode({
        "client_id": app_key, "response_type": "code", "token_access_type": "offline",
        "code_challenge": challenge, "code_challenge_method": "S256"}))
    say("\n4. A Dropbox page will open - click 'Continue' / 'Allow', then copy the code it shows.")
    say("   (If it doesn't open, paste this into the browser:)\n   " + url)
    webbrowser.open(url)
    code = input("\nPaste the code here and press Enter: ").strip()
    try:
        tok = dbx_post("https://api.dropboxapi.com/oauth2/token",
                       urllib.parse.urlencode({"grant_type": "authorization_code", "code": code,
                                               "client_id": app_key, "code_verifier": verifier}).encode(),
                       {"Content-Type": "application/x-www-form-urlencoded"})
        if not in_app_folder():
            # Make sure Dropbox has created Dropbox\\Apps\\<app>, save the login, and have the
            # user move the memes folder in there (can't be done while this window is open in it).
            ensure_app_folder(tok["access_token"])
            cfg["dropbox"] = {"app_key": app_key, "refresh_token": tok["refresh_token"],
                              "folder": "/" + os.path.basename(MEME_DIR)}
            save_config(cfg)
            say()
            say("Dropbox is connected. Last step for Dropbox - move the memes folder into the app's folder:")
            say("  1. Close this window.")
            say(f"  2. In File Explorer, move the whole '{os.path.basename(MEME_DIR)}' folder")
            say(f"     ({MEME_DIR})")
            say("     into  Dropbox\\Apps\\<your app's name>  - the folder with a note called")
            say("     'MOVE THE MEMES FOLDER HERE' (give Dropbox a minute to sync it to this PC).")
            say("  3. Open the _bluesky_poster folder at its new place, type cmd in the address bar,")
            say("     and run  py move_to_github.py  again. It picks up from here.")
            input("\nPress Enter to close...")
            sys.exit(0)
        n = dropbox_test(app_key, tok["refresh_token"], folder)
    except SystemExit:
        raise
    except Exception as e:
        fail(f"Dropbox connection failed: {e}\n(Did you tick the 3 permissions and click Submit "
             "BEFORE clicking Allow? If not, tick them, then run this again.)")
    cfg["dropbox"] = {"app_key": app_key, "refresh_token": tok["refresh_token"], "folder": folder}
    save_config(cfg)
    say(f"Dropbox connected: {n} files found in {folder}.")


def save_config(cfg):
    tmp = CONFIG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, CONFIG)


# ------------------------------------------------------------------ main
def set_task(enabled):
    subprocess.run(["schtasks", "/Change", "/TN", TASK, "/ENABLE" if enabled else "/DISABLE"],
                   capture_output=True)


def main():
    say("Moving the meme poster to GitHub (one time). Keep this window open.\n")
    if not os.path.exists(CONFIG) or not os.path.exists(QUEUE):
        fail("config.json or queue.json isn't next to this file. Put move_to_github.bat and "
             "move_to_github.py in the _bluesky_poster folder.")
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        say("Installing the encryption library (one time)...")
        subprocess.run([sys.executable, "-m", "pip", "install", "--user", "--quiet", "cryptography"])
        try:
            from cryptography.fernet import Fernet
        except ImportError:
            fail("Couldn't install the 'cryptography' Python package.")

    gh = ensure_gh()
    rc, login, _ = gh_out(gh, "api", "user", "--jq", ".login")
    if rc != 0 or not login:
        fail("Couldn't read your GitHub username.")
    repo = f"{login}/{REPO_NAME}"
    say(f"GitHub: signed in as {login}.")
    if gh_out(gh, "repo", "view", repo)[0] != 0:
        rc, _, err = gh_out(gh, "repo", "create", repo, "--private",
                            "--description", "Daily meme poster")
        if rc != 0:
            fail(f"Couldn't create {repo}: {err}")
        say(f"Created the private repo {repo}.")
    rc, token, _ = gh_out(gh, "auth", "token")

    with open(CONFIG, encoding="utf-8-sig") as fh:
        cfg = json.load(fh)
    connect_dropbox(cfg)

    say("\nStopping the PC's daily posting task (GitHub takes over)...")
    set_task(False)
    try:
        key = Fernet.generate_key()
        enc = Fernet(key).encrypt(open(CONFIG, "rb").read())
        r = subprocess.run([gh, "secret", "set", "CONFIG_KEY", "--repo", repo],
                           input=key.decode(), text=True, capture_output=True)
        if r.returncode != 0:
            raise RuntimeError("couldn't save the CONFIG_KEY secret: " + r.stderr.strip())
        upload(token, repo, "_bluesky_poster/config.json.enc", enc, "Encrypted logins")
        upload(token, repo, "_bluesky_poster/queue.json", open(QUEUE, "rb").read(), "Meme queue from the PC")
        if os.path.exists(LOG):
            upload(token, repo, "_bluesky_poster/post_log.txt", open(LOG, "rb").read(), "Post log from the PC")
    except Exception as e:
        set_task(True)
        fail(f"{e}\nThe PC's daily posting task was switched back on, so posting continues as before.")

    with open(os.path.join(HERE, "MOVED_TO_GITHUB.txt"), "w", encoding="utf-8") as fh:
        fh.write(f"Posting moved to GitHub on {time.strftime('%Y-%m-%d %H:%M')}.\n"
                 f"Runs, queue and log: https://github.com/{repo}\n"
                 "The PC's daily task is switched off. queue.json and post_log.txt here are no longer\n"
                 "updated - the live copies are on GitHub. Keep adding memes to this Dropbox folder.\n")

    rc, _, err = gh_out(gh, "workflow", "run", "post.yml", "--repo", repo, "-f", "mode=check")
    say()
    say("DONE. Posting now runs on GitHub every day at 4 PM ET - your PC can be off.")
    say("Keep dropping memes into the Dropbox memes folder; posted ones still move into _posted.")
    if rc == 0:
        say(f"A login test is running now: https://github.com/{repo}/actions")
    else:
        say("Tell Claude 'GitHub move done' so it can finish the last step.")
    input("\nPress Enter to close...")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        fail(str(exc))
