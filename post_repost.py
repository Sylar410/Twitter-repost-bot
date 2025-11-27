# post_repost.py
"""
Mode B: Multi-source combined pool repost bot with ALTERNATING accounts.
- Fetches media from one source account per run (alternates across 7 daily slots).
- Picks a random media tweet from that account, downloads the first image, uploads to your account and posts it.
- Keeps posted_history.json to avoid reposting the same original tweet locally in the runner.
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
from datetime import datetime, timezone

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
RAW_SOURCE_USERS = os.getenv("SOURCE_USERNAMES", "shiyohost,goodgirlxsz,ghostonki")
SOURCE_USERNAMES = [u.strip() for u in RAW_SOURCE_USERS.split(",") if u.strip()]

MAX_TWEETS_TO_FETCH = int(os.getenv("MAX_TWEETS_TO_FETCH", "10"))
HISTORY_FILE = Path(os.getenv("HISTORY_FILE", "posted_history.json"))
TWEET_PREFIX = os.getenv("TWEET_PREFIX", "Repost (via @{orig})")  # use {{orig}} in string

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
    try:
        HISTORY_FILE.write_text(json.dumps(list(s), indent=2))
    except Exception:
        pass

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
# Rate limit safe wrapper
# -----------------------
from requests.exceptions import HTTPError, RequestException
def exponential_backoff(attempt, base=2.0, cap=300):
    wait = base ** attempt
    return min(wait, cap)

def safe_api_call(func, *args, max_retries=6, **kwargs):
    attempt = 0
    while True:
        try:
            return func(*args, **kwargs)
        except tweepy.TooManyRequests as e:
            attempt += 1
            if attempt > max_retries:
                raise
            sleep_for = exponential_backoff(attempt)
            print(f"Rate limit (TooManyRequests). Backing off {sleep_for}s (attempt {attempt}).", flush=True)
            time.sleep(sleep_for)
        except HTTPError as e:
            resp = getattr(e, "response", None)
            code = resp.status_code if resp is not None else None
            if code == 429:
                attempt += 1
                if attempt > max_retries:
                    raise
                reset = None
                if resp is not None:
                    reset = resp.headers.get("x-rate-limit-reset") or resp.headers.get("x-rate_limit_reset")
                if reset:
                    try:
                        wait_for = max(0, int(reset) - int(time.time()))
                        print(f"429 with reset header; sleeping until reset (~{wait_for}s).", flush=True)
                        time.sleep(wait_for + 2)
                        continue
                    except Exception:
                        pass
                sleep_for = exponential_backoff(attempt)
                print(f"HTTP 429. Backing off {sleep_for}s (attempt {attempt}).", flush=True)
                time.sleep(sleep_for)
            else:
                raise
        except RequestException as e:
            attempt += 1
            if attempt > max_retries:
                raise
            sleep_for = exponential_backoff(attempt)
            print(f"Network error {e}. Backing off {sleep_for}s (attempt {attempt}).", flush=True)
            time.sleep(sleep_for)
        except Exception as e:
            raise

# -----------------------
# Find media tweets for a single user
# -----------------------
def get_recent_media_tweets(username, max_results=10):
    try:
        user = safe_api_call(client_v2.get_user, username=username)
    except Exception as e:
        print(f"[{username}] get_user error: {e}", flush=True)
        return []

    if not user or not getattr(user, "data", None):
        print(f"[{username}] user not found or no data.", flush=True)
        return []
    uid = user.data.id

    try:
        resp = safe_api_call(client_v2.get_users_tweets,
                             id=uid,
                             max_results=min(max_results, 100),
                             expansions=["attachments.media_keys", "author_id"],
                             media_fields=["url","type","alt_text"],
                             tweet_fields=["created_at","attachments","entities"])
    except Exception as e:
        print(f"[{username}] get_users_tweets error: {e}", flush=True)
        return []

    if not resp or not getattr(resp, "data", None):
        return []

    media_map = {}
    if getattr(resp, "includes", None) and "media" in resp.includes:
        for m in resp.includes["media"]:
            media_map[m.media_key] = m

    tweets_with_media = []
    for t in resp.data:
        try:
            keys = t.attachments.get("media_keys", []) if getattr(t, "attachments", None) else []
        except Exception:
            keys = []
        valid_keys = [k for k in keys if k in media_map and getattr(media_map[k], "url", None) and getattr(media_map[k], "type", None)=="photo"]
        if valid_keys:
            tweets_with_media.append((t, [media_map[k] for k in valid_keys]))
    return tweets_with_media

# -----------------------
# Rotation logic: map current time to one of 7 daily slots (0..6)
# -----------------------
def current_slot_index_7():
    now = datetime.now(timezone.utc)
    minutes_since_midnight = now.hour * 60 + now.minute
    slot_length = (24 * 60) / 7.0
    slot = int(minutes_since_midnight // slot_length)
    return slot  # 0..6

# -----------------------
# Main flow: pick account based on slot, then repost from that account
# -----------------------
def pick_and_repost_from_slot():
    if not SOURCE_USERNAMES:
        print("No SOURCE_USERNAMES configured. Set SOURCE_USERNAMES as a comma-separated list in env/secrets.", flush=True)
        return

    history = load_history()
    print("Loaded history entries:", len(history), flush=True)

    slot = current_slot_index_7()
    account_index = slot % len(SOURCE_USERNAMES)
    source_user = SOURCE_USERNAMES[account_index]
    print(f"Current slot: {slot} -> using account: {source_user}", flush=True)

    tweets = get_recent_media_tweets(source_user, MAX_TWEETS_TO_FETCH)
    if not tweets:
        print(f"No candidate media tweets found for {source_user}", flush=True)
        return

    candidates = [(t, media_list) for (t, media_list) in tweets if str(t.id) not in history]
    if not candidates:
        print("No new media tweets to repost (all already in history).", flush=True)
        return

    chosen_tweet, media_list = random.choice(candidates)
    tid = str(chosen_tweet.id)
    print("Chosen tweet id:", tid, "created at:", getattr(chosen_tweet, "created_at", "unknown"), flush=True)

    media_obj = media_list[0]
    media_url = getattr(media_obj, "url", None)
    if not media_url:
        print("No direct media URL available for tweet", tid, flush=True)
        return

    print("Downloading media:", media_url, flush=True)
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
            print("Failed to download media:", e, flush=True)
            return

        print("Downloaded to:", local_path, " — uploading to your account...", flush=True)
        try:
            uploaded = safe_api_call(api_v1.media_upload, filename=str(local_path))
            media_id = uploaded.media_id_string if hasattr(uploaded, "media_id_string") else str(uploaded.media_id)
            print("Uploaded media_id:", media_id, flush=True)
        except Exception as e:
            print("Media upload failed:", e, flush=True)
            return

        new_text = f"{TWEET_PREFIX.format(orig=source_user)}\\nOriginal: https://twitter.com/{source_user}/status/{tid}\\n(Used with permission)"
        print("Posting new tweet with text:\\n", new_text[:300], flush=True)
        try:
            resp = safe_api_call(client_v2.create_tweet, text=new_text, media_ids=[media_id])
            print("Posted new tweet. response:", resp, flush=True)
            history.add(tid)
            save_history(history)
            print("Saved history; done.", flush=True)
        except Exception as e:
            print("Failed to create tweet (v2):", e, flush=True)
            try:
                safe_api_call(api_v1.update_status, status=new_text, media_ids=[media_id])
                print("Posted via v1.1 fallback.", flush=True)
                history.add(tid)
                save_history(history)
            except Exception as e2:
                print("Fallback post failed:", e2, flush=True)

if __name__ == "__main__":
    time.sleep(random.random() * 8)
    pick_and_repost_from_slot()
