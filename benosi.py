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
# g4f replaced by Perchance AI via browser automation

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
                return "TOO_LARGE"
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
                if vpath == "TOO_LARGE":
                    return "TOO_LARGE"
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


def find_matching_tweet_index(page, target_text, search_range=10):
    """
    Timeline order can shift between the initial scan and a later reload of
    the same profile (e.g. a new post appears in between). Re-locate the
    tweet whose text matches target_text so media is pulled from the SAME
    tweet the caption was built from, instead of trusting a stale positional
    index. Returns the matching index, or None if no match is found.
    """
    try:
        target = target_text.strip()
        tweets = page.query_selector_all('article[data-testid="tweet"]')
        for i, tweet in enumerate(tweets[:search_range]):
            try:
                text_el = tweet.query_selector('div[data-testid="tweetText"]')
                txt = text_el.inner_text().strip() if text_el else ""
                if txt and txt == target:
                    return i
            except:
                continue
    except Exception as e:
        print(f"  ⚠️ Tweet re-match error: {e}")
    return None


# ──────────────────────────────────────────────
# AI
# ──────────────────────────────────────────────

def clean_text(text):
    text = re.sub(r'\*+', '', text)
    text = re.sub(r'_+', '', text)
    text = re.sub(r'#+', '', text)
    # Some AI providers occasionally over-escape quotes in plain text
    # (e.g. "strange" -> \"strange\"). Strip stray backslash-escapes
    # so they don't leak into the final caption.
    text = text.replace('\\"', '"').replace("\\'", "'")
    return text.strip()


PERCHANCE_URL = "https://perchance.org/ai-text-generator"

# Perchance instruction (system prompt) — সেট করা হবে একবার, তারপর reuse
PERCHANCE_SYSTEM_INSTRUCTION = (
    "You are a sharp, fast-paced breaking news editor on X/Twitter. "
    "Your job is to flash high-impact, urgent updates in a single, crisp sentence. "
    "When given a tweet, rewrite it and choose the best label. "
    "OUTPUT FORMAT (nothing else): LABEL|rewritten text"
)

# একটা পেজ reuse করার জন্য module-level reference
_perchance_page = None
_perchance_frame = None   # iframe frame object
_perchance_context = None


def _get_output_frame(page):
    """iframe#outputIframeEl এর ভেতরের frame object ফেরত দেয়।"""
    try:
        page.wait_for_selector("iframe#outputIframeEl", timeout=20000)
        page.wait_for_timeout(1500)
        for f in page.frames:
            if f is not page.main_frame:
                return f
    except Exception as e:
        print(f"  ⚠️ Output iframe পাওয়া যায়নি: {e}")
    return None


def _get_perchance_page(context):
    """
    Perchance এর জন্য আলাদা ট্যাব খোলে বা আগেরটা reuse করে।
    Instruction field একবার সেট করে রাখে।
    """
    global _perchance_page, _perchance_frame, _perchance_context

    # নতুন context হলে আগের পেজ invalid — reset করো
    if _perchance_context is not context:
        _perchance_page = None
        _perchance_frame = None
        _perchance_context = context

    if _perchance_page is not None:
        try:
            _ = _perchance_page.url
            return _perchance_page
        except Exception:
            _perchance_page = None
            _perchance_frame = None

    print("  🌐 Perchance: নতুন ট্যাব খুলছি...")
    page = context.new_page()
    page.wait_for_timeout(random.randint(800, 1500))
    page.goto(PERCHANCE_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(random.randint(2000, 3500))

    # iframe frame object নাও
    frame = _get_output_frame(page)
    if not frame:
        print("  ⚠️ iframe পাওয়া যায়নি।")
        frame = page  # fallback

    # Instruction field খুঁজে সেট করো — iframe এর ভেতরে
    try:
        instr_el = frame.wait_for_selector("#instructionEl", timeout=15000)
        instr_el.click()
        page.wait_for_timeout(random.randint(300, 600))
        page.keyboard.press("Control+A")
        page.wait_for_timeout(200)
        page.keyboard.press("Delete")
        page.wait_for_timeout(random.randint(200, 400))
        for char in PERCHANCE_SYSTEM_INSTRUCTION:
            page.keyboard.type(char, delay=random.randint(18, 55))
        page.wait_for_timeout(random.randint(400, 800))
        print("  ✅ Perchance instruction সেট হয়েছে।")
    except Exception as e:
        print(f"  ⚠️ Instruction field সেট করতে পারিনি: {e}")

    _perchance_page = page
    _perchance_frame = frame
    return page


def ai_call(prompt, _page_context=None):
    """
    Perchance AI তে prompt পাঠায় এবং output ফেরত দেয়।
    _page_context: playwright BrowserContext (build_final_caption থেকে দেওয়া হয়)
    """
    global _perchance_frame

    if _page_context is None:
        print("  ❌ Perchance: browser context নেই, AI call বাদ।")
        return None

    try:
        page = _get_perchance_page(_page_context)
        frame = _perchance_frame or page  # iframe frame ব্যবহার করো

        # Output textarea clear করো
        try:
            frame.evaluate("document.querySelector('#responseEl') && (document.querySelector('#responseEl').value = '')")
        except Exception:
            pass

        # Instruction আপডেট করো full prompt দিয়ে — iframe এর ভেতরে
        try:
            instr_el = frame.wait_for_selector("#instructionEl", timeout=10000)
            instr_el.click()
            page.wait_for_timeout(random.randint(200, 400))
            page.keyboard.press("Control+A")
            page.wait_for_timeout(150)
            page.keyboard.press("Delete")
            page.wait_for_timeout(random.randint(150, 300))
            for char in prompt:
                page.keyboard.type(char, delay=random.randint(8, 30))
            page.wait_for_timeout(random.randint(300, 600))
        except Exception as e:
            print(f"  ❌ Prompt type করতে পারিনি: {e}")
            return None

        # Regenerate বাটন — iframe এর ভেতরে
        try:
            gen_btn = frame.wait_for_selector("#generateBtn", timeout=10000)
            page.wait_for_timeout(random.randint(400, 800))
            gen_btn.click()
            print("  ⏳ Perchance generate করছে...")
        except Exception as e:
            print(f"  ❌ Generate বাটন ক্লিক করতে পারিনি: {e}")
            return None

        # Output আসার জন্য অপেক্ষা — iframe evaluate
        output = None
        for attempt in range(30):
            page.wait_for_timeout(1000)
            try:
                val = frame.evaluate("document.querySelector('#responseEl') ? document.querySelector('#responseEl').value.trim() : ''")
                if val and len(val) > 5:
                    output = val
                    break
            except Exception:
                pass

        if not output:
            print("  ❌ Perchance থেকে output পাইনি।")
            return None

        print(f"  ✅ Perchance output: {output[:80]}...")
        return clean_text(output)

    except Exception as e:
        print(f"  ❌ Perchance AI error: {e}")
        return None


# ──────────────────────────────────────────────
# AI SELECTION (POST)
# ──────────────────────────────────────────────

def ai_select_best_tweet(tweet_list):
    """
    Score-based tweet selection (AI call বাদ — Perchance শুধু caption এ ব্যবহার হবে)।
    সর্বোচ্চ score এর tweet return করে।
    """
    try:
        if not tweet_list:
            return None
        best = max(tweet_list, key=lambda t: t.get("score", 0))
        return best
    except Exception as e:
        print(f"  ⚠️ Tweet selection error: {e}")
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


def build_final_caption(original_text, has_video=False, browser_context=None):
    prompt = f"""You are a sharp, fast-paced breaking news editor on X/Twitter. Your job is to flash high-impact, urgent updates in a single, crisp sentence.

Task: Rewrite the tweet below into a punchy, conversational, and urgent post, then choose the best label.

RULES FOR REWRITING:
- Total character limit: MAXIMUM 220 characters for the ENTIRE text (including label). Short, fast, and to the point.
- Complete thoughts only: It must be a self-contained, fully finished sentence. Do NOT use '...' or truncation markers.
- Cut the fluff: If the text is long, strip out minor details and grab ONLY the single most explosive, important fact.
- Real-human Twitter style: Break completely free from the original sentence structure! Rearrange clauses, use active and powerful verbs, and make it sound alive. Avoid stiff, formal news jargon or robotic reporting. Write like a real person tweeting.
- Quotation Marks ("...") Rule: If the original tweet has text inside quotes, you MUST preserve a vital part of that quote word-for-word inside "..." marks. Do not paraphrase anything inside quotes. If the quote is too long for the limit, pick just one short, high-impact sentence from it. If there are no quotes, you are free to change all words (including news sources).
- No clutter: No hashtags, no markdown, no asterisks, no bold text.
- No double colons (Wrong: "Trump: says...", Correct: "Trump says...").
- Starting constraint: Never start the text with labels like BREAKING, DEVELOPING, WATCH, or INTERESTING.
- Full official titles: Use full formal titles for official positions (e.g., use "United States Central Command" instead of just "CENTCOM").
- Keep it objective: Stick strictly to the factual core event. No personal drama, no blame-game, and no opinion from the source account.

RULES FOR LABEL:
- BREAKING → urgent news, military developments, major political events (DEFAULT)
- DEVELOPING → active, fast-changing situations
- INTERESTING → surprising facts, but not immediately urgent
- WATCH → ONLY if the tweet has video footage

{"VIDEO ATTACHED — WATCH label is allowed if it fits." if has_video else "NO VIDEO ATTACHED — do NOT use WATCH. Use BREAKING instead."}

OUTPUT FORMAT (Strictly match this layout, nothing else):
LABEL|rewritten text

Examples:
BREAKING|Trump warns Iran of consequences unlike anything seen before if nuclear talks fail
INTERESTING|North Korea quietly tested a new ICBM variant — US intel confirms
DEVELOPING|Clashes ongoing near Kharkiv as ceasefire talks remain stalled

Tweet:
{original_text}"""

    result = ai_call(prompt, _page_context=browser_context)

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
                # Upload শেষ হওয়ার জন্য fixed wait
                page.wait_for_timeout(45000)

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
        # The selected tweet's position can shift on this fresh page load
        # (e.g. the source posted something new while other sources were
        # being checked). Re-find it by text so media matches the caption.
        page_source = chosen_source
        matched_idx = find_matching_tweet_index(page, original_text)
        if matched_idx is None:
            print("  ⚠️ Couldn't relocate the selected tweet after reload (timeline shifted). Posting text-only.")
        else:
            best_idx = matched_idx
            has_video = check_video_in_article(page, best_idx)
            media_paths = extract_media_urls_safely(page, best_idx)

            if media_paths == "TOO_LARGE":
                print("  ⏭ Video too large, trying next best candidate...")
                remaining = [c for c in candidates if c != best_tweet]
                if not remaining:
                    print("  ❌ No more candidates.")
                    return False
                best_tweet = max(remaining, key=lambda x: x['score'])
                original_text = best_tweet['text']
                chosen_source = best_tweet['source']
                best_idx = best_tweet['index']
                print(f"  🔄 New selection: @{chosen_source} | {original_text[:80]}...")

                if chosen_source != page_source:
                    try:
                        page.goto(f"https://x.com/{chosen_source}", wait_until="domcontentloaded", timeout=60000)
                        page.wait_for_timeout(random.randint(3000, 5000))
                        page_source = chosen_source
                    except:
                        pass

                matched_idx = find_matching_tweet_index(page, original_text)
                if matched_idx is None:
                    print("  ⚠️ Couldn't relocate next candidate either, posting text-only.")
                    has_video = False
                    media_paths = []
                else:
                    best_idx = matched_idx
                    has_video = check_video_in_article(page, best_idx)
                    media_paths = extract_media_urls_safely(page, best_idx)
                    if media_paths == "TOO_LARGE":
                        print("  ⚠️ Next candidate also too large, posting text only.")
                        media_paths = []
                        has_video = False

        print(f"  🎥 Video: {has_video}, 🖼 Media: {len(media_paths) if isinstance(media_paths, list) else 0} files")

    print("  🤖 Generating caption...")
    final_caption = build_final_caption(original_text, has_video=has_video, browser_context=page.context)
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
        browser = p.chromium.launch(
            headless=headless,
            channel="chrome",          # Real Chrome = H.264/MP4 codec support for video upload
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",  # GitHub Actions-এ shared memory crash ঠেকায়
                "--use-gl=egl",             # Video thumbnail rendering এর জন্য
            ]
        )
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
