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
from playwright_stealth import stealth_sync
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
os.makedirs(MEDIA_DIR, exist_ok=True)


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
# CAPTCHA LOCK
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
    try:
        captcha = page.query_selector(
            'iframe[src*="captcha"], div[data-testid="captcha"], '
            '#captcha, div[class*="captcha"], iframe[title*="captcha"]'
        )
        if captcha:
            print("  ⚠️ CAPTCHA detected!")
            set_captcha_lock()
            return True
        if "challenge" in page.url.lower() or "captcha" in page.url.lower():
            print("  ⚠️ Challenge URL!")
            set_captcha_lock()
            return True
    except:
        pass
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
            if size > 50 * 1024 * 1024:
                print("  ⚠️ Video too large (50MB+), skip.")
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


def build_final_caption(original_text, has_video=False):
    prompt = f"""You are a sharp breaking news editor on X/Twitter.

Task: Rewrite the tweet below, then choose the best label.

RULES FOR REWRITING:
- Keep the same meaning, make it punchy and urgent
- **Maximum 220 characters total**
- No hashtags, no markdown, no asterisks, no bold
- Preserve direct quotes word for word
- Avoid double colon (wrong: "Trump: says...", correct: "Trump says...")
- Do NOT start the rewritten text with BREAKING, DEVELOPING, WATCH, or INTERESTING
- When mentioning official positions, use the full formal title (e.g., "Federal Reserve Chair" not just "Chair")
- Paraphrase the tweet naturally in simple and easy words without changing its meaning. Sound like a real human, not a news bot.

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

    if not result or "|" not in result:
        print("  ⚠️ AI format failed, fallback...")
        label = _fallback_label(original_text, has_video)
        caption = clean_text(original_text[:220])
        return f"{_label_emoji(label)} {label} | {caption}"

    parts = result.split("|", 1)
    label = parts[0].strip().upper()
    caption = parts[1].strip() if len(parts) > 1 else ""

    if label not in {"BREAKING", "DEVELOPING", "WATCH", "INTERESTING"}:
        label = _fallback_label(original_text, has_video)

    if label == "WATCH" and not has_video:
        label = "BREAKING"

    if not caption:
        caption = clean_text(original_text[:220])

    caption = re.sub(
        r'^(BREAKING|DEVELOPING|WATCH|INTERESTING)\s*(says|:|\|)?\s*',
        '', caption, flags=re.IGNORECASE
    ).strip()

    caption = _fix_double_colon(caption)

    if len(caption) > 217:
        caption = caption[:217] + "..."
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
# POSTING WITH MOUSE MOVEMENTS
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
        try:
            # "Add media" বাটনে ক্লিক করব
            add_media_btn = page.wait_for_selector(
                'button[aria-label="Add media"], [data-testid="mediaButton"]',
                timeout=10000
            )
            box = add_media_btn.bounding_box()
            human_mouse_move(page, box['x'] + box['width']//2, box['y'] + box['height']//2)
            
            # ফাইল চুজারের জন্য অপেক্ষা
            with page.expect_file_chooser() as fc_info:
                add_media_btn.click()
            file_chooser = fc_info.value
            file_chooser.set_files(media_paths)
            
            print(f"  📎 Attached {len(media_paths)} file(s) via file chooser.")
            
            # আপলোড শেষ হওয়ার প্রমাণ: attachment container
            try:
                page.wait_for_selector('[data-testid="attachments"]', timeout=60000)
                print("  ✅ Media attached successfully.")
            except:
                print("  ⚠️ Attachment container did not appear; media may not be attached.")
            
            # ভিডিওর জন্য বাড়তি নিশ্চিতকরণ (ভিডিও প্লেয়ার)
            if any(f.lower().endswith('.mp4') for f in media_paths):
                try:
                    page.wait_for_selector('[data-testid="videoPlayer"]', timeout=30000)
                    print("  ✅ Video player ready.")
                except:
                    print("  ⚠️ Video player did not appear; video might still be processing.")
            
        except Exception as e:
            print(f"  ⚠️ Media attachment failed: {e}")
    
    # পোস্ট বাটনে ক্লিক
    try:
        btn = page.wait_for_selector('div[data-testid="tweetButtonInline"]', timeout=8000)
    except:
        btn = page.wait_for_selector('button[data-testid="tweetButton"]', timeout=8000)
    box = btn.bounding_box()
    human_mouse_move(page, box['x'] + box['width']//2, box['y'] + box['height']//2)
    page.wait_for_timeout(random.randint(500, 1200))
    btn.click()
    page.wait_for_timeout(5000)


def open_compose_and_post(page, text, media_paths):
    for method_num, method in enumerate(["keyboard", "sidenav", "direct"], 1):
        try:
            print(f"  🔄 Method {method_num} trying...")
            if method in ["keyboard", "sidenav"]:
                page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(random.randint(4000, 7000))
                if check_captcha(page):
                    raise Exception("CAPTCHA_DETECTED")
                if method == "keyboard":
                    page.keyboard.press("n")
                else:
                    btn = page.wait_for_selector(
                        'a[data-testid="SideNav_NewTweet_Button"]', timeout=15000
                    )
                    box = btn.bounding_box()
                    human_mouse_move(page, box['x'] + box['width']//2, box['y'] + box['height']//2)
                    btn.click()
            else:
                page.goto("https://x.com/compose/post", wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(random.randint(4000, 7000))
                if check_captcha(page):
                    raise Exception("CAPTCHA_DETECTED")

            page.wait_for_timeout(random.randint(2000, 4000))
            type_and_submit(page, text, media_paths)
            print(f"  ✅ Method {method_num} success!")
            return True
        except Exception as e:
            if "CAPTCHA_DETECTED" in str(e):
                raise
            print(f"  ❌ Method {method_num} failed: {e}")

    print("  💥 All methods failed.")
    return False


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

    # Reload for media
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

        # Human-like scroll after posting
        simulate_scroll(page)
        return True
    else:
        print("❌ Post failed.")
        return False


# ──────────────────────────────────────────────
# HUMAN DELAY FUNCTION
# ──────────────────────────────────────────────

def human_delay(iteration, hour):
    """
    Aggressive but natural posting rhythm for a news aggregator.
    """
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
# MAIN LOOP
# ──────────────────────────────────────────────

def run_bot_loop():
    if not validate_session():
        return
    if is_captcha_locked():
        return

    MAX_DURATION = 6 * 3600  # 6 hours per runner
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
                "Chrome/123.0.0.0 Safari/537.36"
            ),
            viewport={'width': 1920, 'height': 1080}
        )
        page = context.new_page()
        stealth_sync(page)

        print(f"\n🤖 News Bot started (Post-Only Mode) — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        iteration = 0

        while True:
            elapsed = time.time() - start_time
            if elapsed > MAX_DURATION - 300:
                print("⏰ Approaching 6-hour limit. Exiting loop.")
                break

            if is_captcha_locked():
                print("🔒 Captcha lock active. Exiting loop.")
                break

            # Human siesta every 15-20 posts
            if iteration > 0 and iteration % random.randint(15, 20) == 0:
                siesta = random.randint(45, 90) * 60
                print(f"\n☕ Siesta for {siesta//60} minutes...")
                time.sleep(siesta)
                continue

            iteration += 1
            now = datetime.now()
            print(f"\n🔄 Post iteration {iteration} — {now.strftime('%H:%M:%S')}")

            posted_cache = load_cache(POSTED_CACHE)

            success = perform_post_only(page, posted_cache)
            if not success:
                print("⚠️ Post failed, continuing loop after delay.")

            delay = human_delay(iteration, now.hour)
            print(f"⏳ Next post in {delay//60} minutes...")
            time.sleep(delay)

        browser.close()
        print("\n🔒 Browser closed. Loop ended.")


if __name__ == "__main__":
    delay = random.randint(60, 180)
    print(f"⏱ {delay}s initial delay...")
    time.sleep(delay)
    run_bot_loop()
