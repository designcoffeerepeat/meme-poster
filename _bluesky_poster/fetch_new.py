"""
Weekly: copy memes that are new in the Dropbox memes folder (not in queue.json and
not in its excluded list) into _inbox/ in this repo, so the Tuesday job can look at
them, write alt text and hashtags, and add them to the queue. Videos are ignored.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["MEME_SOURCE"] = "dropbox"
import post_meme  # noqa: E402
from dropbox_store import IMAGE_EXTS  # noqa: E402

INBOX = os.path.join(os.path.dirname(HERE), "_inbox")


def main():
    post_meme.load_config()
    dbx = post_meme.DBX
    queue = post_meme.load_json(post_meme.QUEUE_PATH)
    known = {img["file"].lower() for it in queue["items"] for img in it["images"]}
    known |= {f.lower() for f in (queue.get("excluded") or {}).get("files", [])}
    os.makedirs(INBOX, exist_ok=True)
    already = {f.lower() for f in os.listdir(INBOX)}
    new = [n for n in dbx.list_names()
           if n.lower().endswith(IMAGE_EXTS) and n.lower() not in known and n.lower() not in already]
    for n in new:
        dbx.download(f"{dbx.folder}/{n}", os.path.join(INBOX, n))
    post_meme.log(f"New-meme check: {len(new)} new meme(s) copied to _inbox for the Tuesday job"
                  + (f": {', '.join(new)}" if new else "."))
    if not os.listdir(INBOX):
        os.rmdir(INBOX)


if __name__ == "__main__":
    main()
