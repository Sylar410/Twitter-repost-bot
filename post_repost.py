# post_repost.py
"""
Mode B: Multi-source combined pool repost bot.
- Fetches recent media tweets from multiple source accounts.
- Combines candidates, picks one at random, downloads the media and reposts as a NEW tweet.
- Keeps a posted_history.json to avoid reposting the same original tweet.
- IMPORTANT: Only repost when you have permission or when the media is licensed for reuse.
"""

import os
import random
import json
import tempfile
import requests
from pathlib import Path
from dotenv import load_dotenv
import tweepy
import time

# load .env locally if present
load_dotenv()

# -----------------------
# CONFIG / ENV VARS
# -----------------------
BEARER_TOKEN = os.getenv("X_BEARER_TOKEN")
CONSUMER_KEY = os.getenv("X_CONSUMER_KEY")
CONSUMER_SECRET = os.getenv("X_CONSUMER_SECRET")
ACCESS_TOKEN = os.getenv("X_ACCESS_TOKEN")
ACCESS_SECRET = os.getenv("X_ACCESS_SECRET")

# SOURCE_USERNAMES is comma-separated, e.g. "archillect,nasa,earthpix"
RAW_SOURCE_USERS = os.getenv("SOURCE_USERNAMES", "")
SOURCE_USERNAMES = [u.strip() for u in RAW_SOURCE_USERS.split(",") if u.strip()]

MAX_TWEETS_TO_FETCH = int(os.getenv("MAX_TWEETS_TO_FETCH", "50"))
HISTORY_FILE = Path(os.getenv("HISTORY_FILE", "posted_history.json"))
TWEET_PREFIX = os.getenv("TWEET_PREFIX", "Repost (via @{orig})")  # use {orig} in string

# -----------------------
# Helpers
# -----------------------
def load_history():
    if HISTORY_FILE.exists():
        try:
            return set(json.loads(HISTORY_FILE.read_text()))
        except Exception:
            return set()
    return set()

def save_history(s):
    HISTORY_FILE.write_text(json.dumps(list(s), indent=2))

def download_url_to_file(url, dest_path):
    resp = requests.get(url, stream=True, timeout=30)
    resp.raise_for_status()
    with open(dest_path, "wb") as f:
        for chunk in resp.iter_content(1024):
            if chunk:
                f.write(chunk)

# -----------------------
# Auth: build clients
# -----------------------
client_v2 = tweepy.Client(
    bearer_token=BEARER_TOKEN,
    consumer_key=CONSUMER_KEY,
    consumer_secret=CONSUMER_SECRET,
    access_token=ACCESS_TOKEN,
    access_token_secret=ACCESS_SECRET,
    wait_on_rate_limit=True
)

auth = tweepy.OAuth1UserHandler(
    consumer_key=CONSUMER_KEY,
    consumer_secret=CONSUMER_SECRET,
    access_token=ACCESS_TOKEN,
    access_token_secret=ACCESS_SECRET
)
api_v1 = tweepy.API(auth, wait_on_rate_limit=True)

# -----------------------
# Find media tweets for a single user
# -----------------------
def get_recent_media_tweets(username, max_results=50):
    try:
        user = client_v2.get_user(username=username)
    except Exception as e:
        print(f"[{username}] get_user error:", e)
        return []

    if not user or not getattr(user, "data", None):
        print(f"[{username}] user not found or no data.")
        return []
    uid = user.data.id

    try:
        resp = client_v2.get_users_tweets(
            id=uid,
            max_results=min(max_results, 100),
            expansions=["attachments.media_keys", "author_id"],
            media_fields=["url","type","alt_text"],
            tweet_fields=["created_at","attachments","entities"]
        )
    except Exception as e:
        print(f"[{username}] get_users_tweets error:", e)
        return []

    if not resp or not getattr(resp, "data", None):
        return []

    media_map = {}
    if getattr(resp, "includes", None) and "media" in resp.includes:
        for m in resp.includes["media"]:
            # store Media objects keyed by media_key
            media_map[m.media_key] = m

    tweets_with_media = []
    for t in resp.data:
        # robustly fetch media keys
        try:
            keys = t.attachments.get("media_keys", []) if getattr(t, "attachments", None) else []
        except Exception:
            keys = []
        valid_keys = [k for k in keys if k in media_map and getattr(media_map[k], "url", None)]
        if valid_keys:
            tweets_with_media.append((t, [media_map[k] for k in valid_keys]))
    return tweets_with_media

# -----------------------
# Main flow: combine across source accounts and repost one
# -----------------------
def pick_and_repost_from_all_sources():
    if not SOURCE_USERNAMES:
        print("No SOURCE_USERNAMES configured. Set SOURCE_USERNAMES as a comma-separated list in env/secrets.")
        return

    history = load_history()
    print("Loaded history entries:", len(history))

    all_candidates = []  # each item: (source_username, tweet_obj, [media_objs])

    for user in SOURCE_USERNAMES:
        try:
            tweets = get_recent_media_tweets(user, MAX_TWEETS_TO_FETCH)
            # extend combined list with tuples including source username
            for (t, media_list) in tweets:
                all_candidates.append((user, t, media_list))
            print(f"[{user}] found {len(tweets)} media tweets.")
        except Exception as e:
            print(f"[{user}] error while collecting:", e)

    # Filter out those already reposted
    new_candidates = [(user, t, m) for (user, t, m) in all_candidates if str(t.id) not in history]

    if not new_candidates:
        print("No new media tweets to repost from any source (all in history or none found).")
        return

    # pick a random candidate from combined pool
    source_user, chosen_tweet, media_list = random.choice(new_candidates)
    tid = str(chosen_tweet.id)
    print("Chosen tweet id:", tid, "from:", source_user, "created at:", getattr(chosen_tweet, "created_at", "unknown"))

    # Download first media item (can be extended to multiple)
    media_obj = media_list[0]
    media_url = getattr(media_obj, "url", None)
    if not media_url:
        print("No direct media URL available for tweet", tid)
        return

    print("Downloading media:", media_url)
    with tempfile.TemporaryDirectory() as tmpdir:
        local_path = Path(tmpdir) / "image"
        ext = os.path.splitext(media_url.split("?")[0])[1]
        if ext:
            local_path = local_path.with_suffix(ext)
        else:
            local_path = local_path.with_suffix(".jpg")
        try:
            download_url_to_file(media_url, local_path)
        except Exception as e:
            print("Failed to download media:", e)
            return

        print("Downloaded to:", local_path, " — uploading to your account...")
        try:
            uploaded = api_v1.media_upload(filename=str(local_path))
            media_id = uploaded.media_id_string if hasattr(uploaded, "media_id_string") else str(uploaded.media_id)
            print("Uploaded media_id:", media_id)
        except Exception as e:
            print("Media upload failed:", e)
            return

        # Compose new tweet text; include credit and source link
        new_text = f"{TWEET_PREFIX.format(orig=source_user)}\nOriginal: https://twitter.com/{source_user}/status/{tid}\n(Used with permission)"
        print("Posting new tweet with text:\n", new_text[:300])
        try:
            resp = client_v2.create_tweet(text=new_text, media_ids=[media_id])
            print("Posted new tweet. response:", resp)
            # update history
            history.add(tid)
            save_history(history)
            print("Saved history; done.")
        except Exception as e:
            print("Failed to create tweet (v2):", e)
            # fallback to v1.1 update_status
            try:
                api_v1.update_status(status=new_text, media_ids=[media_id])
                print("Posted via v1.1 fallback.")
                history.add(tid)
                save_history(history)
            except Exception as e2:
                print("Fallback post failed:", e2)

if __name__ == "__main__":
    # small random delay to avoid perfect cron-sync patterns
    time.sleep(random.random() * 8)
    pick_and_repost_from_all_sources()
