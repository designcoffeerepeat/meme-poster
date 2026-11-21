# Meme poster

Posts one meme a day at 4 PM New York time to Bluesky, Instagram, X, TikTok and the Facebook Page.
It runs on GitHub, so the PC can be off.

- **Memes** come straight from the Dropbox memes folder. Once a meme has posted everywhere, it moves into that folder's `_posted` subfolder.
- **New memes:** drop them into the Dropbox folder. Every Tuesday they get alt text and hashtags and join the end of the queue. Videos are ignored.
- **Queue:** `_bluesky_poster/queue.json` sets the posting order, alt text and hashtags.
- **Log:** `_bluesky_poster/post_log.txt` gets a line for every run.
- **Logins** are stored encrypted in `_bluesky_poster/config.json.enc`. The key is the repo secret `CONFIG_KEY`.

## Doing things by hand

Go to **Actions → Post daily meme → Run workflow** and pick a mode:

| mode | what it does |
|---|---|
| post | posts today's meme now, unless it already went out today |
| check | tests every login and the Dropbox connection; posts nothing |
| catchup | posts what any account missed, one meme an hour, until all accounts match |
| dryrun | shows what's next |
| fetchnew | looks for new memes in Dropbox right away |

**To pause posting:** go to Actions → Post daily meme → **⋯** → Disable workflow.
