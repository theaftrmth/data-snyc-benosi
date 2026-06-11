import time
import random
import re
import os
import hashlib
import requests
import subprocess
import json
import math
from datetime import datetime, timezone
import pytz
from playwright.sync_api import sync_playwright
import g4f

# ──────────────────────────────────────────────
# SOURCES (from env SOURCES)
# ──────────────────────────────────────────────
SOURCES_STR = os.environ.get("SOURCES")
if not SOURCES_STR:
    print("❌ SOURCES environment variable not set. Bot cannot run.")
    exit(1)

SOURCES = [s.strip() for s in SOURCES_STR.split(",") if s.strip()]
print(f"✅ Loaded {len(SOURCES)} sources from environment.")

PROMO_KEYWORDS = [
    "subscribe", "follow me", "join my", "telegram", "substack",
    "newsletter", "patreon", "buy now", "link in bio", "check out my",
    "my channel", "dm me", "sign up", "free trial", "discount",
    "coupon", "affiliate", "sponsored", "ad:", "promotion",
]

MEDIA_DIR = "downloaded_media"
POSTED_CACHE = "posted_cache.txt"
REPLIED_CACHE = "replied_cache.txt"
CAPTCHA_LOCK_FILE = "captcha_lock.txt"
DAILY_LIMIT_FILE = "daily_post_limit.json"
os.makedirs(MEDIA_DIR, exist_ok=True)

# ──────────────────────────────────────────────
# DAILY POST LIMIT
# ──────────────────────────────────────────────
def get_daily_limit():
    """Returns (target_limit, current_count) and ensures a fresh target for a new day."""
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if os.path.exists(DAILY_LIMIT_FILE):
        try:
            with open(DAILY_LIMIT_FILE, "r") as f:
                data = json.load(f)
            if data.get("date") == today_str:
                return data["target"], data["count"]
        except:
            pass
    target = random.randint(35, 55)
    data = {"date": today_str, "target": target, "count": 0}
    with open(DAILY_LIMIT_FILE, "w") as f:
        json.dump(data, f)
    print(f"📊 New daily post target: {target}")
    return target, 0

def increment_daily_counter():
    target, count = get_daily_limit()
    count += 1
    data = {"date": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "target": target, "count": count}
    with open(DAILY_LIMIT_FILE, "w") as f:
        json.dump(data, f)
    print(f"📈 Daily count: {count}/{target}")
    return count >= target

# ──────────────────────────────────────────────
# SESSION MANAGEMENT (env SESSION_JSON)
# ──────────────────────────────────────────────

def load_session():
    session_json_str = os.environ.get("SESSION_JSON")
    if session_json_str:
        try:
            data = json.loads(session_json_str)
            if "cookies" in data:
                print(f"✅ SESSION_JSON loaded. cookies: {len(data['cookies'])}")
                return data
        except Exception as e:
            print(f"❌ SESSION_JSON parse error: {e}")
    if os.path.exists("session.json"):
        try:
            with open("session.json", "r", encoding="utf-8") as f:
                data = json.load(f)
            if "cookies" in data:
                print(f"✅ session.json loaded. cookies: {len(data['cookies'])}")
                return data
        except Exception as e:
            print(f"❌ session.json error: {e}")
    return None


def validate_session():
    session = load_session()
    if session is None:
        print("❌ No session found (set SESSION_JSON or provide session.json). Bot stopped.")
        return False
    return True


# ──────────────────────────────────────────────
# CAPTCHA LOCK (with screenshot)
# ──────────────────────────────────────────────

def is_captcha_locked():
    if not os.path.exists(CAPTCHA_LOCK_FILE):
        return False
    with open(CAPTCHA_LOCK_FILE, "r") as f:
        lock_time = float(f.read().strip())
    elapsed = time.time() - lock_time
    remaining = (12 * 3600) - elapsed
    if remaining > 0:
        hours = int(remaining // 3600)
        mins = int((remaining % 3600) // 60)
        print(f"🔒 Captcha lock active. {hours}h {mins}m remaining.")
        return True
    os.remove(CAPTCHA_LOCK_FILE)
    print("✅ Captcha lock ended.")
    return False


def set_captcha_lock():
    with open(CAPTCHA_LOCK_FILE, "w") as f:
        f.write(str(time.time()))
    print("🔒 Captcha lock set for 12h.")


def check_captcha(page):
    """
    Returns True if a captcha/challenge is detected (and locks the bot).
    Takes a screenshot for debugging.
    Fixed: removed div[class*="captcha"] to prevent false positives from
    X's "Get Verified" popup and other internal elements.
    """
    try:
        # Only match specific, unambiguous captcha elements (NOT div[class*="captcha"] — too broad)
        captcha = page.query_selector(
            'iframe[src*="captcha"], '
            'div[data-testid="captcha"], '
            '#captcha, '
            'iframe[title*="captcha"]'
        )
        if captcha and captcha.is_visible():
            print("  ⚠️ CAPTCHA element detected!")
            page.screenshot(path=f"captcha_debug_elem_{int(time.time())}.png")
            set_captcha_lock()
            return True
    except:
        pass

    # Check body text AND URL together to avoid false positives
    try:
        page_text = page.inner_text('body').lower()
        if any(phrase in page_text for phrase in [
            "verify your identity", "are you human", "unusual activity",
            "prove you're not a bot", "security challenge", "complete the challenge"
        ]):
            current_url = page.url.lower()
            if "challenge" in current_url or "captcha" in current_url or "suspended" in current_url:
                print("  ⚠️ Challenge text + suspicious URL detected!")
                page.screenshot(path=f"captcha_debug_text_{int(time.time())}.png")
                set_captcha_lock()
                return True
            else:
                print("  ℹ️ Challenge phrase found but URL looks normal — skipping lock.")
    except:
        pass

    # Check URL only
    current_url = page.url.lower()
    if "challenge" in current_url or "captcha" in current_url:
        print("  ⚠️ Challenge/Captcha URL detected!")
        page.screenshot(path=f"captcha_debug_url_{int(time.time())}.png")
        set_captcha_lock()
        return True

    return False


# ──────────────────────────────────────────────
# CACHE
# ──────────────────────────────────────────────

def text_hash(text):
    t = text.lower().strip()
    t = re.sub(r'https?://\S+', '', t)
    t = re.sub(r'@\w+', '', t)
    t = re.sub(r'[^\w\s]', '', t)
    t = re.sub(r'\s+', ' ', t).strip()[:250]
    return hashlib.sha256(t.encode()).hexdigest()[:16]


def load_cache(filepath):
    if not os.path.exists(filepath):
        return set()
    with open(filepath, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f if line.strip())


def save_to_cache(text, filepath):
    h = text_hash(text)
    with open(filepath, "a", encoding="utf-8") as f:
        f.write(h + "\n")


def is_duplicate(text, cache):
    return text_hash(text) in cache


def trim_cache(filepath, limit=500):
    if not os.path.exists(filepath):
        return
    with open(filepath, "r", encoding="utf-8") as f:
        lines = f.readlines()
    if len(lines) > limit:
        with open(filepath, "w", encoding="utf-8") as f:
            f.writelines(lines[-limit:])


# ──────────────────────────────────────────────
# FILTERS
# ──────────────────────────────────────────────

def is_promotional(text):
    return any(kw in text.lower() for kw in PROMO_KEYWORDS)


def is_too_short(text, min_chars=40):
    return len(text.strip()) < min_chars


def is_pinned_tweet(tweet_element):
    try:
        social_context = tweet_element.query_selector('[data-testid="socialContext"]')
        if social_context and "pinned" in social_context.inner_text().lower():
            return True
        outer = tweet_element.inner_html().lower()
        if "pinned" in outer and "pinnedtweet" in outer.replace(" ", ""):
            return True
    except:
        pass
    return False


def is_thread_continuation(tweet_element):
    try:
        outer = tweet_element.inner_html()
        if 'data-testid="tweet_reply_context"' in outer:
            return True
        if tweet_element.query_selector('[data-testid="tweet_reply_context"]'):
            return True
    except:
        pass
    return False


def is_retweet(tweet_element):
    try:
        ctx = tweet_element.query_selector('[data-testid="socialContext"]')
        if ctx:
            t = ctx.inner_text().lower()
            if "retweet" in t or "retweeted" in t:
                return True
    except:
        pass
    return False


def get_tweet_age_minutes(tweet_element):
    try:
        time_el = tweet_element.query_selector('time')
        if time_el:
            dt_str = time_el.get_attribute("datetime")
            if dt_str:
                tweet_time = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                now = datetime.now(timezone.utc)
                return int((now - tweet_time).total_seconds() / 60)
    except:
        pass
    return 9999


# ──────────────────────────────────────────────
# SCORING (fallback)
# ──────────────────────────────────────────────

def has_news_keywords(text):
    keywords = [
        "breaking", "war", "attack", "missile", "troops", "nato",
        "russia", "china", "ukraine", "iran", "israel", "gaza",
        "brics", "sanctions", "military", "killed", "turkey", "ceasefire",
        "explosion", "strike", "conflict", "coup", "election",
        "president", "minister", "nuclear", "uae", "saudi", "drone", "border",
        "crisis", "emergency", "peace", "deal", "treaty",
    ]
    return any(kw in text.lower() for kw in keywords)


def parse_count(text):
    if not text:
        return 0
    text = text.strip().upper().replace(",", "")
    try:
        if "K" in text:
            return int(float(text.replace("K", "")) * 1000)
        elif "M" in text:
            return int(float(text.replace("M", "")) * 1_000_000)
        return int(text)
    except:
        return 0


def get_tweet_engagement(tweet):
    total = 0
    try:
        for testid in ["like", "reply", "retweet"]:
            btn = tweet.query_selector(f'button[data-testid="{testid}"]')
            if btn:
                total += parse_count(btn.inner_text())
    except:
        pass
    return total


def get_tweet_view_count(tweet):
    try:
        view_btn = tweet.query_selector('a[href*="/analytics"], [data-testid="analyticsButton"]')
        if view_btn:
            label = view_btn.get_attribute("aria-label") or view_btn.inner_text()
            return parse_count(label)
        stats = tweet.query_selector_all('div[data-testid$="count"]')
        return sum(parse_count(s.inner_text()) for s in stats) * 50
    except:
        return 0


def score_tweet(text, likes, views=0, age_minutes=9999):
    score = 0.0
    score += min(likes, 50000) * 0.001
    score += min(views, 500000) * 0.0001
    if has_news_keywords(text):
        score += 20
    if "breaking" in text.lower():
        score += 10
    if age_minutes <= 30:
        score += 25
    elif age_minutes <= 60:
        score += 15
    elif age_minutes <= 120:
        score += 5
    elif age_minutes > 360:
        score -= 20
    if is_promotional(text):
        score -= 100
    if is_too_short(text):
        score -= 50
    return score


# ──────────────────────────────────────────────
# VIDEO / MEDIA
# ──────────────────────────────────────────────

def check_video_in_article(page, tweet_index):
    try:
        return bool(page.evaluate(f"""() => {{
            const a = document.querySelectorAll('article[data-testid="tweet"]')[{tweet_index}];
            if (!a) return false;
            return !!(a.querySelector('video') || a.querySelector('div[data-testid="videoPlayer"]'));
        }}"""))
    except:
        return False


def get_tweet_url_from_article(page, tweet_index):
    try:
        url = page.evaluate(f"""() => {{
            const a = document.querySelectorAll('article[data-testid="tweet"]')[{tweet_index}];
            if (!a) return null;
            const link = a.querySelector('a[href*="/status/"]');
            return link ? 'https://x.com' + link.getAttribute('href') : null;
        }}""")
        return url
    except:
        return None


def download_media(url, filename):
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        if r.status_code == 200:
            path = os.path.join(MEDIA_DIR, filename)
            with open(path, "wb") as f:
                f.write(r.content)
            return path
    except Exception as e:
        print(f"  ❌ Image download failed: {e}")
    return None


def download_video_with_ytdlp(tweet_url):
    if not tweet_url:
        return None
    try:
        out_path = os.path.join(MEDIA_DIR, f"video_{int(time.time())}.mp4")
        cmd = [
            "yt-dlp", "--no-playlist",
            "--format", "mp4/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "--merge-output-format", "mp4",
            "--output", out_path,
            "--quiet", "--no-warnings",
            "--socket-timeout", "30",
            tweet_url,
        ]
        result = subprocess.run(cmd, timeout=60, capture_output=True, text=True)
        if result.returncode == 0 and os.path.exists(out_path):
            size = os.path.getsize(out_path)
            print(f"  📥 Video downloaded: {size // 1024}KB")
            if size == 0:
                print("  ⚠️ Downloaded file is 0 bytes, skipping.")
                os.remove(out_path)
                return None
            if size > 100 * 1024 * 1024:   # 100 MB limit
                print("  ⚠️ Video too large (100MB+), skip.")
                os.remove(out_path)
                return None
            return out_path
        else:
            print(f"  ❌ yt-dlp failed: {result.stderr[:200]}")
    except subprocess.TimeoutExpired:
        print("  ❌ yt-dlp timeout.")
    except FileNotFoundError:
        print("  ❌ yt-dlp not installed.")
    except Exception as e:
        print(f"  ❌ Video download error: {e}")
    return None


def extract_media_urls_safely(page, tweet_index):
    media_paths = []
    try:
        has_vid = check_video_in_article(page, tweet_index)
        if has_vid:
            tweet_url = get_tweet_url_from_article(page, tweet_index)
            print(f"  🎬 Video detected, yt-dlp: {tweet_url}")
            if tweet_url:
                vpath = download_video_with_ytdlp(tweet_url)
                if vpath:
                    return [vpath]
            print("  ⚠️ Video failed, trying images...")

        urls = page.evaluate(f"""() => {{
            const a = document.querySelectorAll('article[data-testid="tweet"]')[{tweet_index}];
            if (!a) return [];
            const imgs = a.querySelectorAll('img[src*="pbs.twimg.com/media"]');
            return Array.from(imgs).slice(0, 4).map(i => i.src);
        }}""")

        for i, src in enumerate(urls or []):
            src = re.sub(r'name=\w+', 'name=large', src)
            path = download_media(src, f"img_{int(time.time())}_{i}.jpg")
            if path:
                media_paths.append(path)
                print(f"  📥 Image {i+1} downloaded.")

    except Exception as e:
        print(f"  ⚠️ Media extract error: {e}")
    return media_paths


# ──────────────────────────────────────────────
# AI
# ──────────────────────────────────────────────

def clean_text(text):
    text = re.sub(r'\*+', '', text)
    text = re.sub(r'_+', '', text)
    text = re.sub(r'#+', '', text)
    return text.strip()


def ai_call(prompt):
    try:
        response = g4f.ChatCompletion.create(
            model=g4f.models.default,
            messages=[{"role": "user", "content": prompt}],
        )
        if response:
            return clean_text(str(response).strip())
        return None
    except Exception as e:
        print(f"  ❌ AI error: {e}")
        return None


# ──────────────────────────────────────────────
# AI SELECTION (POST)
# ──────────────────────────────────────────────

def ai_select_best_tweet(tweet_list):
    try:
        shortlist = []
        for t in tweet_list:
            shortlist.append({
                "source": t["source"],
                "text": t["text"][:150],
                "likes": t.get("likes", 0),
                "age_min": t.get("age_min", 0)
            })
        prompt = f"""You are a sharp geopolitical news editor for X/Twitter.
Below are tweets from breaking news sources. Pick the ONE tweet that is the most newsworthy, urgent, and likely to get high engagement.
**Avoid tweets that are primarily opinion, editorializing, or advocacy.** Prefer strictly factual, neutral reporting.
Consider:
- Global impact, surprise, conflict, diplomatic moves.
- Uniqueness (not just a reaction).
- Relevance right now.

Tweets:
{json.dumps(shortlist, indent=2, ensure_ascii=False)}

Return ONLY the index (0-based) of the best tweet. Nothing else.
Example: 2"""
        result = ai_call(prompt)
        if result and result.strip().isdigit():
            idx = int(result.strip())
            if 0 <= idx < len(tweet_list):
                return tweet_list[idx]
    except Exception as e:
        print(f"  ⚠️ AI post selection error: {e}")
    return None


# ──────────────────────────────────────────────
# CAPTION GENERATION
# ──────────────────────────────────────────────

LABEL_KEYWORDS = {
    "DEVELOPING": ["developing", "ongoing", "unfolding", "continues", "still"],
    "INTERESTING": ["interesting", "surprising", "unexpected", "unusual", "curious", "remarkable"],
}


def _fallback_label(text, has_video=False):
    lower = text.lower()
    for label, keywords in LABEL_KEYWORDS.items():
        if any(kw in lower for kw in keywords):
            return label
    if has_video and any(kw in lower for kw in ["watch", "footage", "video", "seen"]):
        return "WATCH"
    return "BREAKING"


def _label_emoji(label):
    return {
        "BREAKING": "🚨",
        "DEVELOPING": "🔄",
        "WATCH": "⚠️",
        "INTERESTING": "🔍",
    }.get(label, "🚨")


def _fix_double_colon(caption):
    match = re.match(r'^([A-Z][a-zA-Z\s]{1,30}):\s+(.+)$', caption)
    if match:
        name_part = match.group(1).strip()
        rest = match.group(2).strip()
        return f"{name_part} {rest}" if rest.startswith('"') else f"{name_part} says {rest}"
    return caption


def _trim_no_ellipsis(caption: str, max_chars=217) -> str:
    if len(caption) <= max_chars:
        return caption
    portion = caption[:max_chars]
    last_space = portion.rfind(' ')
    if last_space > 0:
        return portion[:last_space]
    return portion


def build_final_caption(original_text, has_video=False):
    prompt = f"""You are a sharp breaking news editor on X/Twitter.

Task: Rewrite the tweet below, then choose the best label.

RULES FOR REWRITING:
- **Your ENTIRE rewritten text must be 220 characters or fewer. Do NOT exceed this limit.**
- **Do NOT use '...' or any truncation markers. Your output must be a complete, self-contained sentence.**
- If the original is too long, pick ONLY the single most important fact and express it fully.
- Keep the same meaning, make it punchy and urgent.
- No hashtags, no markdown, no asterisks, no bold.
- Preserve direct quotes word for word.
- Avoid double colon (wrong: "Trump: says...", correct: "Trump says...").
- Do NOT start the rewritten text with BREAKING, DEVELOPING, WATCH, or INTERESTING.
- When mentioning official positions, use the full formal title (e.g., "Federal Reserve Chair" not just "Chair").
- Maintain a strictly neutral, factual tone. Do not take sides.
- Avoid any language that labels a group as "terrorist", "freedom fighter", "militant" etc. unless it's a direct quote from an official.
- Paraphrase naturally in simple words. Sound like a real human, not a news bot.

RULES FOR LABEL:
- BREAKING → urgent news, military action, major political event (DEFAULT)
- DEVELOPING → situation still unfolding
- WATCH → ONLY if this tweet has VIDEO showing the event
- INTERESTING → surprising fact, not urgent

{"VIDEO IS ATTACHED — WATCH label allowed if content fits." if has_video else "NO VIDEO — do NOT use WATCH, use BREAKING instead."}

OUTPUT FORMAT (exactly, nothing else):
LABEL|rewritten text

Examples:
BREAKING|Trump warns Iran of consequences unlike anything seen before if nuclear talks fail
INTERESTING|North Korea quietly tested a new ICBM variant — US intel confirms
DEVELOPING|Clashes ongoing near Kharkiv as ceasefire talks remain stalled
WATCH|Russian Su-35 engages Ukrainian drone — footage now circulating

Tweet:
{original_text}"""

    result = ai_call(prompt)

    if result:
        if "|" in result:
            parts = result.split("|", 1)
            label = parts[0].strip().upper()
            caption = parts[1].strip() if len(parts) > 1 else ""
        else:
            label = ""
            caption = result.strip()

        caption = re.sub(
            r'^(BREAKING|DEVELOPING|WATCH|INTERESTING)\s*(says|:|\|)?\s*',
            '', caption, flags=re.IGNORECASE
        ).strip()

        if label not in {"BREAKING", "DEVELOPING", "WATCH", "INTERESTING"}:
            label = _fallback_label(original_text, has_video)
        if label == "WATCH" and not has_video:
            label = "BREAKING"

        if not caption:
            caption = clean_text(original_text[:220])

        caption = _fix_double_colon(caption)
        caption = _trim_no_ellipsis(caption, max_chars=217)
        return f"{_label_emoji(label)} {label} | {caption}"

    print("  ⚠️ AI format failed, using fallback...")
    label = _fallback_label(original_text, has_video)
    caption = re.sub(
        r'^(BREAKING|DEVELOPING|WATCH|INTERESTING)\s*(says|:|\|)?\s*',
        '', original_text, flags=re.IGNORECASE
    ).strip()
    caption = clean_text(caption[:220])
    caption = _fix_double_colon(caption)
    caption = _trim_no_ellipsis(caption, max_chars=217)
    return f"{_label_emoji(label)} {label} | {caption}"


# ──────────────────────────────────────────────
# HUMAN-LIKE MOUSE MOVEMENT & TYPING
# ──────────────────────────────────────────────

def human_mouse_move(page, target_x, target_y, steps=15):
    start_x, start_y = random.randint(100, 300), random.randint(100, 300)
    cp_x = (start_x + target_x) / 2 + random.randint(-80, 80)
    cp_y = (start_y + target_y) / 2 + random.randint(-80, 80)
    for i in range(steps + 1):
        t = i / steps
        x = (1-t)**2 * start_x + 2*(1-t)*t * cp_x + t**2 * target_x
        y = (1-t)**2 * start_y + 2*(1-t)*t * cp_y + t**2 * target_y
        page.mouse.move(x, y)
        time.sleep(random.uniform(0.005, 0.015))


def human_type(element, text):
    element.click()
    time.sleep(random.uniform(0.3, 0.8))
    for char in text:
        element.type(char, delay=random.randint(40, 120))
        if random.random() < 0.05:
            time.sleep(random.uniform(0.3, 0.9))
    time.sleep(random.uniform(0.5, 1.2))


# ──────────────────────────────────────────────
# POSTING (fixed: manual stealth, correct video selector)
# ──────────────────────────────────────────────

def type_and_submit(page, text, media_paths):
    viewport = page.viewport_size
    human_mouse_move(page, viewport['width']//2, viewport['height']//2)
    textarea = page.wait_for_selector(
        'div[data-testid="tweetTextarea_0"]', timeout=25000
    )
    box = textarea.bounding_box()
    human_mouse_move(page, box['x'] + box['width']//2, box['y'] + box['height']//2)
    human_type(textarea, text)
    page.wait_for_timeout(random.randint(800, 1500))

    if media_paths:
        has_video = False
        for mp in media_paths:
            try:
                # expect_file_chooser ব্যবহার করো — stealth mode-এ React event ঠিকমতো fire হয়
                attach_btn = page.query_selector('button[aria-label="Add photos or video"]')
                if attach_btn:
                    with page.expect_file_chooser(timeout=10000) as fc_info:
                        attach_btn.click()
                    file_chooser = fc_info.value
                    file_chooser.set_files(mp)
                    if mp.lower().endswith('.mp4'):
                        has_video = True
                        print(f"  🎞 Video file queued: {os.path.basename(mp)}")
                    else:
                        print(f"  📎 Image queued: {os.path.basename(mp)}")
                else:
                    print(f"  ⚠️ Attach button not found.")
            except Exception as e:
                print(f"  ⚠️ Media attach error: {e}")

        if has_video:
            # React upload pipeline শুরু হওয়ার সময় দাও
            page.wait_for_timeout(3000)

            attached = False
            try:
                page.wait_for_selector(
                    'div[data-testid="attachments"]',
                    timeout=30000
                )
                attached = True
                print("  ✅ Attachment container found.")
            except:
                print("  ⚠️ Attachment container not found.")
                page.screenshot(path=f"attach_fail_{int(time.time())}.png")

            if attached:
                # div[aria-live="polite"][role="status"] এ "Uploaded" text আসা পর্যন্ত wait
                try:
                    page.wait_for_function(
                        """() => {
                            const s = document.querySelector('div[aria-live="polite"][role="status"]');
                            return s && s.innerText.includes('Uploaded');
                        }""",
                        timeout=120000
                    )
                    print("  ✅ Upload complete.")
                except:
                    print("  ⚠️ Upload status not detected, waiting fallback...")
                    page.wait_for_timeout(8000)

                # Video preview confirm
                try:
                    page.wait_for_selector(
                        'div[data-testid="attachments"] video',
                        timeout=15000
                    )
                    print("  ✅ Video preview confirmed.")
                except:
                    print("  ⚠️ Preview not confirmed, continuing anyway.")

            else:
                print("  ⚠️ Skipping media, posting text only.")

            page.wait_for_timeout(2000)

        else:
            # Image হলে ছোট wait
            page.wait_for_timeout(random.randint(3000, 5000))

    try:
        btn = page.wait_for_selector('div[data-testid="tweetButtonInline"]', timeout=8000)
    except:
        btn = page.wait_for_selector('button[data-testid="tweetButton"]', timeout=8000)
    box = btn.bounding_box()
    human_mouse_move(page, box['x'] + box['width']//2, box['y'] + box['height']//2)
    page.wait_for_timeout(random.randint(500, 1200))
    btn.click()
    page.wait_for_timeout(5000)


# ──────────────────────────────────────────────
# TWITTER INTERNAL API — REQUESTS-BASED POSTING
# Playwright UI দিয়ে video attach করলে stealth mode-এ React pipeline
# silently fail হয়। তাই session cookies extract করে সরাসরি
# Twitter-এর upload API ব্যবহার করা হচ্ছে।
# ──────────────────────────────────────────────

TWITTER_WEB_BEARER = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs"
    "%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)

# Twitter GraphQL queryId for CreateTweet.
# DevTools → Network → CreateTweet request থেকে নেওয়া।
# যদি ভবিষ্যতে বদলায়: DevTools-এ আবার দেখে এই একটা value update করো।
_CT_QUERY_ID = "zWBsbUW6mqkNJv25Yrp-_Q"
_CT_URL = f"https://x.com/i/api/graphql/{_CT_QUERY_ID}/CreateTweet"

_CT_FEATURES = {
    "premium_content_api_read_enabled": False,
    "communities_web_enable_tweet_community_results_fetch": True,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "responsive_web_grok_analyze_button_fetch_trends_enabled": False,
    "responsive_web_grok_analyze_post_followups_enabled": True,
    "rweb_cashtags_composer_attachment_enabled": True,
    "responsive_web_jetfuel_frame": True,
    "responsive_web_grok_share_attachment_enabled": True,
    "responsive_web_grok_annotations_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "rweb_conversational_replies_downvote_enabled": False,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "content_disclosure_indicator_enabled": True,
    "content_disclosure_ai_generated_indicator_enabled": True,
    "responsive_web_grok_show_grok_translated_post": True,
    "responsive_web_grok_analysis_button_from_backend": True,
    "post_ctas_fetch_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": False,
    "profile_label_improvements_pcf_label_in_post_enabled": True,
    "responsive_web_profile_redirect_enabled": False,
    "rweb_tipjar_consumption_enabled": False,
    "verified_phone_label_enabled": False,
    "articles_preview_enabled": True,
    "rweb_cashtags_enabled": True,
    "responsive_web_grok_community_note_auto_translation_is_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "responsive_web_grok_image_annotation_enabled": True,
    "responsive_web_grok_imagine_annotation_enabled": True,
    "responsive_web_graphql_timeline_navigation_enabled": True,
}


def _build_api_headers(page):
    """
    Playwright page context থেকে session cookies extract করে
    Twitter API-র জন্য headers dict তৈরি করে।
    """
    try:
        cookies = page.context.cookies()
        ct0 = next((c["value"] for c in cookies if c["name"] == "ct0"), None)
        auth_token = next((c["value"] for c in cookies if c["name"] == "auth_token"), None)
        if not ct0 or not auth_token:
            print("  ❌ [API] ct0 বা auth_token cookie পাওয়া যায়নি।")
            return None
        # সব cookie একসাথে string-এ
        cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
        return {
            "Authorization": f"Bearer {TWITTER_WEB_BEARER}",
            "x-csrf-token": ct0,
            "Cookie": cookie_str,
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/136.0.0.0 Safari/537.36"
            ),
            "x-twitter-active-user": "yes",
            "x-twitter-auth-type": "OAuth2Session",
            "x-twitter-client-language": "en",
            "Origin": "https://x.com",
            "Referer": "https://x.com/",
        }
    except Exception as e:
        print(f"  ❌ [API] Header build error: {e}")
        return None


def _upload_media_api(api_headers, file_path, is_video=True):
    """
    Twitter-এর chunked media upload API দিয়ে video বা image upload করে।
    INIT → APPEND (4MB chunks) → FINALIZE → POLL processing
    Returns: media_id_string অথবা None
    """
    try:
        file_size = os.path.getsize(file_path)
        mime = "video/mp4" if is_video else "image/jpeg"
        category = "tweet_video" if is_video else "tweet_image"
        # multipart request-এ Content-Type requests নিজেই set করে
        h = {k: v for k, v in api_headers.items() if k.lower() != "content-type"}

        # ── INIT ──
        print(f"  📡 [API] INIT {category} ({file_size // 1024}KB)...")
        r = requests.post(
            "https://upload.twitter.com/1.1/media/upload.json",
            headers=h,
            data={
                "command": "INIT",
                "total_bytes": file_size,
                "media_type": mime,
                "media_category": category,
            },
            timeout=30,
        )
        init_data = r.json()
        media_id = init_data.get("media_id_string")
        if not media_id:
            print(f"  ❌ [API] INIT failed ({r.status_code}): {init_data}")
            return None
        print(f"  ✅ [API] INIT OK → media_id: {media_id}")

        # ── APPEND (4MB chunks) ──
        CHUNK_SIZE = 4 * 1024 * 1024
        seg = 0
        with open(file_path, "rb") as f:
            while True:
                chunk = f.read(CHUNK_SIZE)
                if not chunk:
                    break
                r = requests.post(
                    "https://upload.twitter.com/1.1/media/upload.json",
                    headers=h,
                    data={
                        "command": "APPEND",
                        "media_id": media_id,
                        "segment_index": seg,
                    },
                    files={"media": ("blob", chunk, "application/octet-stream")},
                    timeout=60,
                )
                print(f"  📤 [API] APPEND seg {seg} → {r.status_code}")
                if r.status_code not in (200, 204):
                    print(f"      ⚠️ Body: {r.text[:150]}")
                seg += 1

        # ── FINALIZE ──
        print("  📡 [API] FINALIZE...")
        r = requests.post(
            "https://upload.twitter.com/1.1/media/upload.json",
            headers=h,
            data={"command": "FINALIZE", "media_id": media_id},
            timeout=30,
        )
        final = r.json()
        proc = final.get("processing_info", {})

        # ── POLL processing (video-র জন্য দরকার) ──
        deadline = time.time() + 180
        while proc.get("state") in ("pending", "in_progress") and time.time() < deadline:
            wait = max(proc.get("check_after_secs", 5), 3)
            pct = proc.get("progress_percent", 0)
            print(f"  ⏳ [API] Video processing {pct}%... {wait}s wait")
            time.sleep(wait)
            r = requests.get(
                "https://upload.twitter.com/1.1/media/upload.json",
                headers=h,
                params={"command": "STATUS", "media_id": media_id},
                timeout=15,
            )
            proc = r.json().get("processing_info", {})

        if proc.get("state") == "failed":
            print(f"  ❌ [API] Processing failed: {proc.get('error', {})}")
            return None

        print(f"  ✅ [API] Media ready → media_id: {media_id}")
        return media_id

    except Exception as e:
        print(f"  ❌ [API] Upload error: {e}")
        return None


def _post_tweet_api(api_headers, text, media_id=None):
    """
    Twitter-এর internal GraphQL API দিয়ে tweet post করে।
    URL: DevTools থেকে নেওয়া সঠিক endpoint।
    media structure: media_entities (সঠিক) — আগের media_ids ভুল ছিল।
    """
    variables = {
        "tweet_text": text,
        "semantic_annotation_ids": [],
        "disallowed_reply_options": None,
        "semantic_annotation_options": {"source": "Htl"},
    }
    if media_id:
        variables["media"] = {
            "media_entities": [{"media_id": media_id, "tagged_users": []}],
            "possibly_sensitive": False,
        }

    payload = {
        "variables": variables,
        "features": _CT_FEATURES,
        "queryId": _CT_QUERY_ID,
    }
    headers = {**api_headers, "Content-Type": "application/json"}

    try:
        r = requests.post(_CT_URL, headers=headers, json=payload, timeout=20)
        data = r.json()

        if r.status_code == 200 and "data" in data:
            tweet_id = (
                data["data"]
                .get("create_tweet", {})
                .get("tweet_results", {})
                .get("result", {})
                .get("rest_id")
            )
            if tweet_id:
                print(f"  ✅ [API] Tweet posted! id: {tweet_id}")
                return True
            print(f"  ❌ [API] Response OK কিন্তু tweet_id নেই: {str(data)[:200]}")
            return False

        errs = data.get("errors", [])
        if errs:
            code = errs[0].get("code", 0)
            msg = errs[0].get("message", "")[:150]
            print(f"  ❌ [API] Error {code}: {msg}")
        else:
            print(f"  ❌ [API] HTTP {r.status_code}: {str(data)[:200]}")
        return False

    except Exception as e:
        print(f"  ❌ [API] Exception: {e}")
        return False


def open_compose_and_post(page, text, media_paths):
    """
    Tweet post করে media সহ — সম্পূর্ণ API-based।
    Playwright UI compose box আর ব্যবহার হয় না।
    """
    # x.com-এ থাকলে session cookies page context-এ loaded থাকে
    try:
        if "x.com" not in page.url and "twitter.com" not in page.url:
            page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)
    except:
        pass

    api_headers = _build_api_headers(page)
    if not api_headers:
        print("  ❌ Session থেকে API headers বানানো যায়নি। Post বাতিল।")
        return False

    media_id = None
    videos = [p for p in media_paths if p.lower().endswith(".mp4")]
    images = [p for p in media_paths if not p.lower().endswith(".mp4")]

    if videos:
        print("  🎬 [API] Video uploading...")
        media_id = _upload_media_api(api_headers, videos[0], is_video=True)
        if not media_id:
            print("  ⚠️ Video upload failed — text only post হবে।")
    elif images:
        print("  🖼 [API] Image uploading...")
        media_id = _upload_media_api(api_headers, images[0], is_video=False)

    return _post_tweet_api(api_headers, text, media_id)


# ──────────────────────────────────────────────
# TIMELINE SCROLL (human-like)
# ──────────────────────────────────────────────

def simulate_scroll(page):
    try:
        page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(random.randint(2000, 3000))
        for _ in range(random.randint(2, 5)):
            page.mouse.wheel(0, random.randint(300, 800))
            time.sleep(random.uniform(0.5, 1.5))
        print("  📜 Scrolled timeline naturally.")
    except Exception as e:
        print(f"  ⚠️ Scroll error: {e}")


# ──────────────────────────────────────────────
# POST-ONLY FUNCTION
# ──────────────────────────────────────────────

def perform_post_only(page, posted_cache):
    candidates = []

    for source in random.sample(SOURCES, len(SOURCES)):
        print(f"\n📡 @{source} checking...")
        try:
            page.goto(f"https://x.com/{source}", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(random.randint(5000, 8000))
        except:
            continue

        if check_captcha(page):
            return False

        tweets = page.query_selector_all('article[data-testid="tweet"]')
        if not tweets:
            continue

        for i, tweet in enumerate(tweets[:6]):
            try:
                if is_pinned_tweet(tweet) or is_retweet(tweet) or is_thread_continuation(tweet):
                    continue
                text_el = tweet.query_selector('div[data-testid="tweetText"]')
                txt = text_el.inner_text() if text_el else ""
                if not txt or is_duplicate(txt, posted_cache) or is_promotional(txt) or is_too_short(txt):
                    continue
                age = get_tweet_age_minutes(tweet)
                if age > 180:
                    continue
                like_btn = tweet.query_selector('button[data-testid="like"]')
                likes = parse_count(like_btn.inner_text()) if like_btn else 0
                views = get_tweet_view_count(tweet)
                sc = score_tweet(txt, likes, views, age)
                print(f"  ✅ {i+1} | Age:{age}m Likes:{likes} Score:{sc:.1f} | {txt[:60]}...")
                candidates.append({
                    'index': i, 'text': txt, 'source': source, 'likes': likes,
                    'views': views, 'age_min': age, 'score': sc,
                })
            except:
                pass

    if not candidates:
        print("\n⚠️ No new posts found.")
        return False

    top_candidates = sorted(candidates, key=lambda x: x['likes'], reverse=True)[:5]
    best_tweet = ai_select_best_tweet(top_candidates)
    if best_tweet is None:
        best_tweet = max(candidates, key=lambda x: x['score'])

    original_text = best_tweet['text']
    chosen_source = best_tweet['source']
    best_idx = best_tweet['index']
    print(f"\n🏆 Selected: @{chosen_source} | {original_text[:100]}...")

    print(f"\n📡 Reloading @{chosen_source} for media...")
    reloaded = False
    try:
        page.goto(f"https://x.com/{chosen_source}", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(random.randint(3000, 5000))
        reloaded = True
    except:
        pass

    has_video = False
    media_paths = []
    if reloaded:
        has_video = check_video_in_article(page, best_idx)
        media_paths = extract_media_urls_safely(page, best_idx)
        print(f"  🎥 Video: {has_video}, 🖼 Media: {len(media_paths)} files")

    print("  🤖 Generating caption...")
    final_caption = build_final_caption(original_text, has_video=has_video)
    if len(final_caption) > 250:
        final_caption = final_caption[:247] + "..."
    print(f"  ✅ Caption: {final_caption}")

    print("\n📤 Posting...")
    posted = open_compose_and_post(page, final_caption, media_paths)
    for path in media_paths:
        try:
            os.remove(path)
        except:
            pass

    if posted:
        save_to_cache(original_text, POSTED_CACHE)
        trim_cache(POSTED_CACHE)
        print("✅ Post successful!")

        limit_reached = increment_daily_counter()
        simulate_scroll(page)
        if limit_reached:
            print(f"🎯 Daily post limit reached. Stopping further posts today.")
        return True
    else:
        print("❌ Post failed.")
        return False


# ──────────────────────────────────────────────
# HUMAN DELAY FUNCTION
# ──────────────────────────────────────────────

def human_delay(iteration, hour):
    if 6 <= hour < 10:
        base = random.randint(15, 25) * 60
    elif 10 <= hour < 16:
        base = random.randint(10, 18) * 60
    elif 16 <= hour < 22:
        base = random.randint(15, 25) * 60
    else:
        base = random.randint(40, 90) * 60

    if iteration > 40:
        base = int(base * 1.6)
    elif iteration > 25:
        base = int(base * 1.3)

    return base


# ──────────────────────────────────────────────
# MAIN LOOP (manual stealth, full fingerprint)
# ──────────────────────────────────────────────

def run_bot_loop():
    if not validate_session():
        return
    if is_captcha_locked():
        return

    target, current = get_daily_limit()
    print(f"📊 Daily limit: {current}/{target}")
    if current >= target:
        print("🎯 Today's post limit already reached. Exiting.")
        return

    MAX_DURATION = 6 * 3600
    start_time = time.time()

    with sync_playwright() as p:
        headless = os.environ.get("HEADLESS", "false").lower() == "true"
        browser = p.chromium.launch(headless=headless)
        session_data = load_session()
        context = browser.new_context(
            storage_state=session_data,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/136.0.0.0 Safari/537.36"
            ),
            viewport={'width': 1920, 'height': 1080}
        )
        page = context.new_page()

        # পূর্ণাঙ্গ ফিঙ্গারপ্রিন্ট স্পুফিং
        page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
            window.chrome = {runtime: {}, loadTimes: function(){}, csi: function(){}, app: {}};
            Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
            Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});
            Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
            Object.defineProperty(navigator, 'platform', {get: () => 'Win32'});

            const getParameter = WebGLRenderingContext.prototype.getParameter;
            WebGLRenderingContext.prototype.getParameter = function(parameter) {
                if (parameter === 37445) return 'Google Inc. (Intel)';
                if (parameter === 37446) return 'ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)';
                return getParameter.call(this, parameter);
            };

            // Canvas toDataURL modification REMOVED — it broke X's video thumbnail
            // generation in the compose box, causing videos to be dropped silently.

            const originalCreateOscillator = AudioContext.prototype.createOscillator;
            AudioContext.prototype.createOscillator = function() {
                const osc = originalCreateOscillator.apply(this, arguments);
                const originalStart = osc.start;
                osc.start = function() {
                    setTimeout(() => originalStart.apply(this, arguments), Math.random() * 2);
                };
                return osc;
            };

            const originalQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (parameters) => (
                parameters.name === 'notifications' ?
                    Promise.resolve({state: Notification.permission}) :
                    originalQuery(parameters)
            );
        """)

        print(f"\n🤖 News Bot started (Post-Only Mode) — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        iteration = 0
        SIESTA_EVERY = random.randint(15, 20)

        while True:
            target, current = get_daily_limit()
            if current >= target:
                print("🎯 Daily limit reached. Stopping.")
                break

            elapsed = time.time() - start_time
            if elapsed > MAX_DURATION - 300:
                print("⏰ Approaching 6-hour limit. Exiting loop.", flush=True)
                break

            if is_captcha_locked():
                print("🔒 Captcha lock active. Exiting loop.", flush=True)
                break

            if iteration > 0 and iteration % SIESTA_EVERY == 0:
                siesta = random.randint(45, 90) * 60
                print(f"\n☕ Siesta for {siesta//60} minutes...", flush=True)
                time.sleep(siesta)
                continue

            iteration += 1
            now = datetime.now()
            print(f"\n🔄 Post iteration {iteration} — {now.strftime('%H:%M:%S')}", flush=True)

            posted_cache = load_cache(POSTED_CACHE)

            success = perform_post_only(page, posted_cache)
            if not success:
                print("⚠️ Post failed, continuing loop after delay.", flush=True)

            delay = human_delay(iteration, now.hour)
            print(f"⏳ Next post in {delay//60} minutes...", flush=True)
            time.sleep(delay)

        browser.close()
        print("\n🔒 Browser closed. Loop ended.", flush=True)


if __name__ == "__main__":
    delay = random.randint(60, 180)
    print(f"⏱ {delay}s initial delay...")
    time.sleep(delay)
    run_bot_loop()
