# post_repost.py
"""
Safe repost bot package (conservative).
- SOURCE_USERNAMES default: shiryohost,ghostonki
- MAX_TWEETS_TO_FETCH default: 3
- workflow uses manual run only (workflow_dispatch) to avoid accidental schedule runs
- On rate-limit (429) the run exits immediately (to avoid burning quota)
- since_id caching is included
- Robust upload fallback included
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
from requests.exceptions import HTTPError, RequestException
import sys

load_dotenv()

# CONFIG / ENV VARS
BEARER_TOKEN = os.getenv("X_BEARER_TOKEN")
CONSUMER_KEY = os.getenv("X_CONSUMER_KEY")
CONSUMER_SECRET = os.getenv("X_CONSUMER_SECRET")
ACCESS_TOKEN = os.getenv("X_ACCESS_TOKEN")
ACCESS_SECRET = os.getenv("X_ACCESS_SECRET")

RAW_SOURCE_USERS = os.getenv("SOURCE_USERNAMES", "shiryohost,ghostonki")
SOURCE_USERNAMES = [u.strip() for u in RAW_SOURCE_USERS.split(",") if u.strip()]

MAX_TWEETS_TO_FETCH = int(os.getenv("MAX_TWEETS_TO_FETCH", "3"))
HISTORY_FILE = Path(os.getenv("HISTORY_FILE", "posted_history.json"))
SINCE_FILE = Path(os.getenv("SINCE_FILE", "since_ids.json"))
TWEET_PREFIX = os.getenv("TWEET_PREFIX", "Repost (via @{orig})")

# Helpers
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

def load_since_ids():
    if SINCE_FILE.exists():
        try:
            return json.loads(SINCE_FILE.read_text())
        except Exception:
            return {}
    return {}

def save_since_ids(d):
    try:
        SINCE_FILE.write_text(json.dumps(d, indent=2))
    except Exception:
        pass

# Auth
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

# Quick-stop safe_api_call: exits immediately on 429 to avoid retries that burn quota
import sys
def safe_api_call(func, *args, **kwargs):
    try:
        return func(*args, **kwargs)
    except Exception as e:
        # Try to detect TooManyRequests / 429 and print headers
        resp = getattr(e, "response", None)
        status = getattr(resp, "status_code", None) if resp is not None else None
        print("[API ERROR] exception type:", type(e), "status:", status, flush=True)
        try:
            if resp is not None:
                # Print common rate headers if present
                for h in ("x-rate-limit-limit","x-rate-limit-remaining","x-rate-limit-reset","x_rate_limit_reset","x-rate-limit-remaining","x-rate-limit-limit"):
                    val = resp.headers.get(h) if hasattr(resp, "headers") else None
                    if val:
                        print(f"[RATE HEADER] {h}: {val}", flush=True)
                # body
                try:
                    print("[HTTP BODY]", resp.text, flush=True)
                except Exception:
                    pass
        except Exception:
            pass

        # If this looks like rate-limit, exit to avoid further calls
        if status == 429 or "TooManyRequests" in repr(e) or (resp is not None and resp.status_code == 429):
            reset = None
            try:
                reset = resp.headers.get("x-rate-limit-reset") or resp.headers.get("x_rate_limit_reset")
            except Exception:
                pass
            if reset:
                try:
                    import time
                    print("[RATE LIMIT] reset epoch:", reset, "=> UTC:", time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(int(reset))), flush=True)
                except Exception:
                    pass
            print("[RATE LIMIT] Exiting run to avoid further requests.", flush=True)
            sys.exit(0)
        # otherwise re-raise for other errors
        raise


# Media download helper
def download_url_to_file(url, dest_path):
    resp = requests.get(url, stream=True, timeout=30)
    resp.raise_for_status()
    with open(dest_path, "wb") as f:
        for chunk in resp.iter_content(1024):
            if chunk:
                f.write(chunk)

# Upload with fallback
def upload_media_with_fallback(local_path):
    try:
        uploaded = api_v1.media_upload(filename=str(local_path))
        return uploaded
    except Exception as e:
        print(f"[UPLOAD] Simple upload failed: {e}", flush=True)
    try:
        uploaded = api_v1.media_upload(filename=str(local_path), chunked=True)
        return uploaded
    except Exception as e:
        print(f"[UPLOAD] Chunked upload failed: {e}", flush=True)
    try:
        from PIL import Image
        img = Image.open(local_path)
        jpg_path = str(Path(local_path).with_suffix(".jpg"))
        rgb = img.convert("RGB")
        rgb.save(jpg_path, format="JPEG", quality=88)
        print(f"[UPLOAD] Converted to JPEG: {jpg_path}", flush=True)
        uploaded = api_v1.media_upload(filename=jpg_path)
        return uploaded
    except Exception as e:
        print(f"[UPLOAD] JPEG conversion+upload failed: {e}", flush=True)
        try:
            if hasattr(e, "response") and e.response is not None:
                print("HTTP status:", e.response.status_code, flush=True)
                print("HTTP body:", e.response.text, flush=True)
        except Exception:
            pass
        raise

# Get recent media tweets (since_id supported)
def get_recent_media_tweets(username, max_results=3, since_id=None):
    try:
        user = safe_api_call(client_v2.get_user, username=username)
    except Exception as e:
        print(f"[{username}] get_user ERROR → {e}", flush=True)
        return []

    if not user or not getattr(user, "data", None):
        print(f"[{username}] no user data returned.", flush=True)
        return []

    uid = user.data.id
    params = dict(
        id=uid,
        max_results=min(max_results, 100),
        expansions=["attachments.media_keys", "author_id"],
        media_fields=["url", "type", "alt_text"],
        tweet_fields=["created_at", "attachments"]
    )
    if since_id:
        params["since_id"] = since_id

    try:
        resp = safe_api_call(client_v2.get_users_tweets, **params)
    except Exception as e:
        print(f"[{username}] get_users_tweets ERROR → {e}", flush=True)
        return []

    if not resp or not getattr(resp, "data", None):
        return []

    media_map = {}
    if getattr(resp, "includes", None) and "media" in resp.includes:
        for m in resp.includes["media"]:
            media_map[m.media_key] = m

    out = []
    for t in resp.data:
        try:
            keys = t.attachments.get("media_keys", []) if getattr(t, "attachments", None) else []
        except Exception:
            keys = []
        valid = []
        for k in keys:
            mm = media_map.get(k)
            if not mm:
                continue
            if getattr(mm, "type", None) != "photo":
                continue
            if not getattr(mm, "url", None):
                continue
            valid.append(mm)
        if valid:
            out.append((t, valid))
    return out

# Rotation slot mapping
def current_slot_index_7():
    now = datetime.now(timezone.utc)
    minutes_since_midnight = now.hour * 60 + now.minute
    slot_length = (24 * 60) / 7.0
    slot = int(minutes_since_midnight // slot_length)
    return slot

# Main flow
def pick_and_repost_from_slot():
    if not SOURCE_USERNAMES:
        print("No SOURCE_USERNAMES configured. Set SOURCE_USERNAMES as a comma-separated list in env/secrets.", flush=True)
        return

    since_ids = load_since_ids()
    history = load_history()
    print("Loaded history size:", len(history), "since_ids:", since_ids, flush=True)

    slot = current_slot_index_7()
    account_index = slot % len(SOURCE_USERNAMES)
    source_user = SOURCE_USERNAMES[account_index]
    print(f"Current slot: {slot} -> using account: {source_user}", flush=True)

    last_seen = since_ids.get(source_user)
    tweets = get_recent_media_tweets(source_user, max_results=MAX_TWEETS_TO_FETCH, since_id=last_seen)
    if not tweets:
        print(f"No new media tweets for {source_user}", flush=True)
        try:
            resp_all = get_recent_media_tweets(source_user, max_results=MAX_TWEETS_TO_FETCH, since_id=None)
            if resp_all:
                highest = max(int(t.id) for (t, _) in resp_all)
                since_ids[source_user] = str(highest)
                save_since_ids(since_ids)
        except Exception:
            pass
        return

    candidates = [(t, media_list) for (t, media_list) in tweets if str(t.id) not in history]
    if not candidates:
        print("No new candidates (all in history).", flush=True)
        try:
            highest = max(int(t.id) for (t, _) in tweets)
            since_ids[source_user] = str(highest)
            save_since_ids(since_ids)
        except Exception:
            pass
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
        local_path = local_path.with_suffix(ext if ext else ".jpg")
        try:
            download_url_to_file(media_url, local_path)
        except Exception as e:
            print("Failed to download media:", e, flush=True)
            return

        print("Downloaded to:", local_path, " — uploading...", flush=True)
        try:
            uploaded = safe_api_call(upload_media_with_fallback, local_path)
            media_id = uploaded.media_id_string if hasattr(uploaded, "media_id_string") else str(uploaded.media_id)
            print("Uploaded media_id:", media_id, flush=True)
        except Exception as e:
            print("Media upload ultimately failed:", e, flush=True)
            return

        new_text = f"{TWEET_PREFIX.format(orig=source_user)}\nOriginal: https://twitter.com/{source_user}/status/{tid}\n(Used with permission)"
        print("Posting new tweet with text preview:", new_text[:200], flush=True)
        try:
            resp = safe_api_call(client_v2.create_tweet, text=new_text, media_ids=[media_id])
            print("Posted new tweet. response:", resp, flush=True)
            history.add(tid)
            save_history(history)
            try:
                highest = max(int(t.id) for (t, _) in tweets)
                since_ids[source_user] = str(highest)
                save_since_ids(since_ids)
            except Exception:
                pass
            print("Done.", flush=True)
        except Exception as e:
            print("Failed to create tweet (v2):", e, flush=True)
            try:
                safe_api_call(api_v1.update_status, status=new_text, media_ids=[media_id])
                print("Posted via v1.1 fallback.", flush=True)
                history.add(tid)
                save_history(history)
                try:
                    highest = max(int(t.id) for (t, _) in tweets)
                    since_ids[source_user] = str(highest)
                    save_since_ids(since_ids)
                except Exception:
                    pass
            except Exception as e2:
                print("Fallback post failed:", e2, flush=True)

if __name__ == "__main__":
    time.sleep(random.random() * 8)
    pick_and_repost_from_slot()
