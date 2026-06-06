"""
One-shot bot: fetches new tweets and sends Telegram messages.
State (seen tweets, chat IDs, user IDs) is stored in a GitHub Gist.
Designed to be run via GitHub Actions cron every 15 minutes.
"""
import asyncio
import json
import logging
import os
import urllib.request
import urllib.error

import twikit
from twikit.utils import find_dict

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN     = os.environ["TELEGRAM_TOKEN"]
TWITTER_AUTH_TOKEN = os.environ["TWITTER_AUTH_TOKEN"]
TWITTER_CT0        = os.environ["TWITTER_CT0"]
TWITTER_ACCOUNTS   = [a.strip().lstrip("@") for a in os.environ["TWITTER_ACCOUNTS"].split(",") if a.strip()]
GIST_TOKEN         = os.environ["GIST_TOKEN"]
GIST_ID            = os.environ["GIST_ID"]


# ── Gist helpers ──────────────────────────────────────────────────────────────

def gist_request(method: str, path: str, data: bytes | None = None) -> dict:
    url = f"https://api.github.com{path}"
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"token {GIST_TOKEN}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def load_state() -> dict:
    gist = gist_request("GET", f"/gists/{GIST_ID}")
    files = gist.get("files", {})
    state = {}
    for fname in ("seen_tweets.json", "chat_ids.json", "user_ids.json"):
        if fname in files:
            raw = files[fname].get("content", "")
            try:
                state[fname] = json.loads(raw) if raw.strip() else {}
            except Exception:
                state[fname] = {}
        else:
            state[fname] = {} if fname != "chat_ids.json" else []
    return state


def save_state(seen: dict, chat_ids: list, user_ids: dict):
    payload = json.dumps({
        "files": {
            "seen_tweets.json": {"content": json.dumps(seen, indent=2)},
            "chat_ids.json":    {"content": json.dumps(chat_ids, indent=2)},
            "user_ids.json":    {"content": json.dumps(user_ids, indent=2)},
        }
    }).encode()
    gist_request("PATCH", f"/gists/{GIST_ID}", payload)


# ── Telegram ──────────────────────────────────────────────────────────────────

def tg_send(chat_id: str, text: str):
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False,
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        data=payload,
    )
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        logger.error(f"Telegram error {e.code}: {e.read().decode()}")


# ── Twitter ───────────────────────────────────────────────────────────────────

def make_client() -> twikit.Client:
    client = twikit.Client("en-US")
    client.set_cookies({"auth_token": TWITTER_AUTH_TOKEN, "ct0": TWITTER_CT0})
    return client


async def get_user_id(client: twikit.Client, account: str, cache: dict) -> str | None:
    if account in cache:
        return cache[account]
    try:
        response, _ = await client.gql.user_by_screen_name(account)
        user_data = find_dict(response, "result")[0]
        uid = user_data["rest_id"]
        cache[account] = uid
        return uid
    except Exception as e:
        logger.error(f"Could not get user ID for @{account}: {e}")
        return None


async def fetch_tweets(client: twikit.Client, user_id: str) -> list[dict]:
    response, _ = await client.gql.user_tweets(user_id, count=20, cursor=None)
    entries = find_dict(response, "entries")[0]
    tweets = []
    for entry in entries:
        tweet_results = find_dict(entry, "tweet_results")
        if not tweet_results:
            continue
        legacy = find_dict(tweet_results[0], "legacy")
        if not legacy:
            continue
        tid = find_dict(tweet_results[0], "rest_id")
        text = legacy[0].get("full_text", "")
        if tid and text and not text.startswith("RT "):
            tweets.append({"id": tid[0], "text": text})
    return tweets


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    logger.info("Loading state from Gist...")
    state = load_state()
    seen     = state["seen_tweets.json"]
    chat_ids = state["chat_ids.json"]
    user_ids = state["user_ids.json"]

    if not chat_ids:
        logger.warning("No subscribers in Gist — nothing to send")
        return

    client = make_client()
    to_send = []

    for account in TWITTER_ACCOUNTS:
        try:
            uid = await get_user_id(client, account, user_ids)
            if not uid:
                continue

            tweets = await fetch_tweets(client, uid)
            if not tweets:
                logger.warning(f"@{account}: no tweets returned")
                continue

            last_id = seen.get(account)
            if last_id is None:
                seen[account] = tweets[0]["id"]
                logger.info(f"@{account}: initialized at {tweets[0]['id']}")
                continue

            new = [t for t in tweets if int(t["id"]) > int(last_id)]
            if new:
                seen[account] = str(max(int(t["id"]) for t in new))
                logger.info(f"@{account}: {len(new)} new tweet(s)")
                for t in sorted(new, key=lambda x: int(x["id"])):
                    to_send.append(
                        f"🐦 *@{account}*\n\n{t['text']}\n\n"
                        f"[View tweet](https://twitter.com/{account}/status/{t['id']})"
                    )
            else:
                logger.info(f"@{account}: no new tweets")

        except Exception as e:
            logger.error(f"Error fetching @{account}: {e}")

    logger.info("Saving state to Gist...")
    save_state(seen, chat_ids, user_ids)

    for msg in to_send:
        for chat_id in chat_ids:
            tg_send(str(chat_id), msg)
            await asyncio.sleep(0.3)

    logger.info(f"Done — sent {len(to_send)} message(s)")


if __name__ == "__main__":
    asyncio.run(main())
