"""
Keeps the logins (config.json) encrypted in the repo as config.json.enc.
The key lives only in the repo secret CONFIG_KEY (and was made on the PC).

    python cloud_config.py decrypt   config.json.enc -> config.json   (start of a run)
    python cloud_config.py encrypt   config.json -> config.json.enc   (end of a run, only if it changed,
                                     e.g. after the Instagram token refresh)
"""
import hashlib
import os
import sys

from cryptography.fernet import Fernet

HERE = os.path.dirname(os.path.abspath(__file__))
PLAIN = os.path.join(HERE, "config.json")
ENC = os.path.join(HERE, "config.json.enc")
STAMP = os.path.join(HERE, ".config.sha256")


def key():
    k = os.environ.get("CONFIG_KEY", "").strip()
    if not k:
        raise SystemExit("CONFIG_KEY secret is missing - run move_to_github.bat on the PC.")
    return Fernet(k.encode())


def digest(data):
    return hashlib.sha256(data).hexdigest()


def main(cmd):
    if cmd == "decrypt":
        if not os.path.exists(ENC):
            raise SystemExit("config.json.enc is missing - run move_to_github.bat on the PC.")
        data = key().decrypt(open(ENC, "rb").read())
        open(PLAIN, "wb").write(data)
        open(STAMP, "w").write(digest(data))
    elif cmd == "encrypt":
        if not os.path.exists(PLAIN):
            return
        data = open(PLAIN, "rb").read()
        if os.path.exists(STAMP) and open(STAMP).read().strip() == digest(data):
            print("logins unchanged")
        else:
            open(ENC, "wb").write(key().encrypt(data))
            print("logins changed - saved the new encrypted copy")
        os.remove(PLAIN)
        if os.path.exists(STAMP):
            os.remove(STAMP)
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "")
