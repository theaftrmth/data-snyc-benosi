import time
import random
import re
import os
import glob
import hashlib
import requests
import subprocess
import json
from datetime import datetime, timezone
import pytz
from playwright.sync_api import sync_playwright
import g4f

BD_TZ = pytz.timezone("Asia/Dhaka")

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
TOPIC_MEMORY_FILE = "topic_memory.json"
os.makedirs(MEDIA_DIR, exist_ok=True)

# ──────────────────────────────────────────────
# DAILY POST LIMIT (40–48 posts per day)
# ──────────────────────────────────────────────
def get_daily_limit():
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if os.path.exists(DAILY_LIMIT_FILE):
        try:
            with open(DAILY_LIMIT_FILE, "r") as f:
                data = json.load(f)
            if data.get("date") == today_str:
                return data["target"], data["count"]
        except:
            pass
    target = random.randint(40, 48)
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

def load_deepseek_session():
    session_json_str = os.environ.get("DEEPSEEK_SESSION_JSON")
    if session_json_str:
        try:
            data = json.loads(session_json_str)
            if "cookies" in data:
                print(f"✅ DEEPSEEK_SESSION_JSON loaded. cookies: {len(data['cookies'])}")
                return data
        except Exception as e:
            print(f"❌ DEEPSEEK_SESSION_JSON parse error: {e}")
    if os.path.exists("deepseek_session.json"):
        try:
            with open("deepseek_session.json", "r", encoding="utf-8") as f:
                data = json.load(f)
            if "cookies" in data:
                print(f"✅ deepseek_session.json loaded. cookies: {len(data['cookies'])}")
                return data
        except Exception as e:
            print(f"❌ deepseek_session.json error: {e}")
    print("⚠️  No DeepSeek session found (DEEPSEEK_SESSION_JSON not set). DeepSeek rewrite may fail as logged-out.")
    return None

def apply_deepseek_cookies(context):
    """একই browser context-এ DeepSeek-এর কুকি ইনজেক্ট করে (X সেশনের পাশাপাশি)।"""
    ds_session = load_deepseek_session()
    if ds_session and "cookies" in ds_session:
        try:
            context.add_cookies(ds_session["cookies"])
            print(f"✅ DeepSeek cookies injected into context: {len(ds_session['cookies'])}")
        except Exception as e:
            print(f"❌ Failed to inject DeepSeek cookies: {e}")

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
    try:
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
    except:
        pass
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

SPORTS_KEYWORDS = [
    "world cup", "football match", "soccer", "premier league", "la liga",
    "serie a", "bundesliga", "champions league", "europa league",
    "fifa", "uefa", "ballon d'or", "transfer window", "hat-trick", "hattrick",
    "red card", "yellow card", "penalty shootout", "penalty kick",
    "extra time", "round of sixteen", "round of 32",
    "quarterfinal", "semifinal", "semi-final",
    "relegation", "kickoff", "kick-off", "halftime", "half-time",
    "full-time whistle", "fulltime", "nba", "nfl", "mlb", "nhl",
    "wimbledon", "grand prix", "formula 1", "f1 race", "ufc", "boxing match",
    "olympics", "playoffs", "grand slam", "test match", "t20", "ipl",
    "manager sacked", "transfer fee", "world cup qualifier",
]

def is_sports_related(text):
    return any(kw in text.lower() for kw in SPORTS_KEYWORDS)

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
    elif age_minutes > 240:          # 4-hour cutoff
        score -= 20
    if is_promotional(text):
        score -= 100
    if is_too_short(text):
        score -= 50
    return score

# ──────────────────────────────────────────────
# TOPIC MEMORY (short-term duplicate topic prevention)
# ──────────────────────────────────────────────
STOPWORDS = {
    "the", "is", "at", "which", "on", "a", "an", "and", "or", "but",
    "in", "with", "to", "for", "of", "by", "from", "as", "into",
    "about", "like", "after", "before", "between", "under", "over",
    "out", "up", "down", "off", "no", "not", "its", "it's", "that",
    "this", "was", "are", "were", "been", "be", "have", "has", "had",
    "do", "does", "did", "will", "would", "could", "should", "may",
    "can", "said", "says", "new", "just", "now", "very", "also",
    "more", "some", "BREAKING", "Iran", "US", "USA", "next", "back",
    "still", "already", "yet", "all", "both", "each", "every", "other",
    "many", "much", "such", "only", "then", "than", "too", "here",
    "there", "when", "where", "why", "how"
}

def extract_keywords(text):
    words = re.findall(r'[a-zA-Z]+', text.lower())
    return {w for w in words if w not in STOPWORDS and len(w) > 2}

def load_topic_memory():
    if not os.path.exists(TOPIC_MEMORY_FILE):
        return []
    try:
        with open(TOPIC_MEMORY_FILE, "r") as f:
            return json.load(f)
    except:
        return []

def save_topic_memory(memory):
    cutoff = time.time() - 6 * 3600   # ৬ ঘণ্টা – পুরো রান জুড়ে একই টপিক ব্লক
    memory = [m for m in memory if m["time"] > cutoff]
    with open(TOPIC_MEMORY_FILE, "w") as f:
        json.dump(memory, f)

def is_similar_topic(text, memory, min_overlap=3):
    keywords = extract_keywords(text)
    for mem in memory:
        if len(keywords & set(mem["keywords"])) >= min_overlap:
            return True
    return False

def add_to_topic_memory(text):
    memory = load_topic_memory()
    memory.append({"time": time.time(), "keywords": list(extract_keywords(text))})
    save_topic_memory(memory)

# ──────────────────────────────────────────────
# VIDEO / MEDIA (fixed: reliable multi-video + mixed support)
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

# ──────────────────────────────────────────────
# FALLBACK IMAGE (media-less পোস্টের জন্য) — run শুরুতে একবার ডাউনলোড, পুরো ৬ ঘণ্টা reuse
# ──────────────────────────────────────────────
def download_fallback_image():
    url = os.environ.get("FALLBACK_IMAGE_URL")
    if not url:
        print("ℹ️  FALLBACK_IMAGE_URL সেট করা নেই — media-less পোস্টে fallback ছবি ব্যবহার হবে না।")
        return None
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        if r.status_code == 200 and r.content:
            ext = ".jpg"
            content_type = r.headers.get("Content-Type", "").lower()
            if "gif" in content_type:
                ext = ".gif"
            elif "png" in content_type:
                ext = ".png"
            elif "webp" in content_type:
                ext = ".webp"
            elif url.lower().split("?")[0].endswith(".gif"):
                ext = ".gif"

            if ext == ".gif" and len(r.content) > 15 * 1024 * 1024:
                print(f"⚠️  Fallback GIF {len(r.content) / 1024 / 1024:.1f}MB — X-এর ~15MB লিমিট ছাড়িয়ে গেছে, ব্যবহার হবে না।")
                return None

            path = os.path.join(MEDIA_DIR, f"fallback_image{ext}")
            with open(path, "wb") as f:
                f.write(r.content)
            print(f"✅ Fallback image downloaded: {path} ({len(r.content)} bytes)")
            return path
        else:
            print(f"⚠️  Fallback image download failed: HTTP {r.status_code}")
    except Exception as e:
        print(f"⚠️  Fallback image download failed: {e}")
    return None

def download_videos_from_tweet(tweet_url, max_attempts=3):
    if not tweet_url:
        return []
    base_name = os.path.join(MEDIA_DIR, f"video_{int(time.time())}")
    for attempt in range(1, max_attempts + 1):
        try:
            cmd = [
                "yt-dlp",
                "--format", "mp4/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
                "--merge-output-format", "mp4",
                "--output", base_name + "_%(playlist_index)s.%(ext)s",
                "--quiet", "--no-warnings",
                "--socket-timeout", "30",
                tweet_url,
            ]
            result = subprocess.run(cmd, timeout=60, capture_output=True, text=True)
            if result.returncode == 0:
                video_files = sorted(glob.glob(base_name + "_*.mp4"))
                valid_files = []
                too_large = False
                for fname in video_files:
                    size = os.path.getsize(fname)
                    if size == 0:
                        os.remove(fname)
                        continue
                    if size > 100 * 1024 * 1024:
                        print(f"  ⚠️ {os.path.basename(fname)} too large (100MB+), skip tweet.")
                        too_large = True
                        continue
                    valid_files.append(fname)
                if too_large:
                    for vf in valid_files:
                        try: os.remove(vf)
                        except: pass
                    return "TOO_LARGE"
                if valid_files:
                    print(f"  📥 Downloaded {len(valid_files)} video(s): {[os.path.basename(v) for v in valid_files]}")
                    return valid_files
                print(f"  ❌ yt-dlp ran but no video files found (attempt {attempt}/{max_attempts})")
            else:
                print(f"  ❌ yt-dlp failed (attempt {attempt}/{max_attempts}): {result.stderr[:200]}")
                if attempt < max_attempts:
                    time.sleep(random.uniform(4, 8))
        except subprocess.TimeoutExpired:
            print(f"  ❌ yt-dlp timeout (attempt {attempt}/{max_attempts}).")
            if attempt < max_attempts:
                time.sleep(random.uniform(4, 8))
        except FileNotFoundError:
            print("  ❌ yt-dlp not installed.")
            return []
        except Exception as e:
            print(f"  ❌ Video download error (attempt {attempt}/{max_attempts}): {e}")
            if attempt < max_attempts:
                time.sleep(random.uniform(4, 8))
    return []

def extract_media_urls_safely(page, tweet_index):
    media_paths = []
    try:
        # ---------- ১. ভিডিও ডাউনলোড ----------
        has_vid = check_video_in_article(page, tweet_index)
        if has_vid:
            tweet_url = get_tweet_url_from_article(page, tweet_index)
            print(f"  🎬 Video(s) detected, downloading all with yt-dlp: {tweet_url}")
            if tweet_url:
                video_paths = download_videos_from_tweet(tweet_url)
                if video_paths == "TOO_LARGE":
                    return "TOO_LARGE"
                if video_paths:
                    media_paths.extend(video_paths)
            else:
                print("  ⚠️ Tweet URL not found, skipping video download.")

        # ---------- ২. ছবি ডাউনলোড ----------
        urls = page.evaluate(f"""() => {{
            const a = document.querySelectorAll('article[data-testid="tweet"]')[{tweet_index}];
            if (!a) return [];
            const imgs = a.querySelectorAll('img[src*="pbs.twimg.com/media"]');
            return Array.from(imgs).map(i => i.src);
        }}""")

        for i, src in enumerate(urls or []):
            src = re.sub(r'name=\w+', 'name=large', src)
            path = download_media(src, f"img_{int(time.time())}_{i}.jpg")
            if path:
                media_paths.append(path)
                print(f"  📥 Image {i+1} downloaded.")

        # ---------- ৩. সেফটি ট্রিম (টুইটারের সর্বোচ্চ ৪টি মিডিয়া) ----------
        if len(media_paths) > 4:
            print(f"  ⚠️ Combined media count {len(media_paths)} exceeds 4, trimming to first 4.")
            for extra in media_paths[4:]:
                try:
                    os.remove(extra)
                except:
                    pass
            media_paths = media_paths[:4]

    except Exception as e:
        print(f"  ⚠️ Media extract error: {e}")

    return media_paths

def find_matching_tweet_index(page, target_text, search_range=10):
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
# DEEPSEEK REWRITE (caption generation)
# ──────────────────────────────────────────────
def _deepseek_select_expert_mode(page) -> None:
    try:
        expert_radio = page.query_selector('div[data-model-type="expert"][role="radio"]')
        if expert_radio:
            checked = expert_radio.get_attribute("aria-checked")
            if checked != "true":
                expert_radio.click()
                page.wait_for_timeout(random.uniform(500, 800))
        else:
            print("  ⚠️  DeepSeek Expert radio option খুঁজে পাওয়া যায়নি।")
    except Exception as e:
        print(f"  ⚠️  DeepSeek Expert mode selection error: {e}")

def _deepseek_ensure_toggle_on(page, label_text: str) -> None:
    try:
        toggles = page.query_selector_all("div[aria-pressed]")
        for t in toggles:
            span = t.query_selector("span")
            if span and span.inner_text().strip() == label_text:
                pressed = t.get_attribute("aria-pressed")
                cls = t.get_attribute("class") or ""
                is_on = (pressed == "true") and ("ds-toggle-button--selected" in cls)
                if not is_on:
                    t.click()
                    page.wait_for_timeout(random.uniform(400, 700))
                return
    except Exception as e:
        print(f"  ⚠️  DeepSeek toggle '{label_text}' error: {e}")

def _deepseek_is_focused(page, el) -> bool:
    try:
        return bool(page.evaluate(
            """(node) => {
                let p = node;
                for (let i = 0; i < 6 && p; i++) {
                    if (p.classList && p.classList.contains('focused')) return true;
                    p = p.parentElement;
                }
                return false;
            }""",
            el,
        ))
    except Exception:
        return False

def _deepseek_find_textarea(page, timeout=8000):
    for sel in [
        'textarea[name="search"]',
        'textarea[placeholder="Message DeepSeek"]',
        'textarea.ds-scroll-area',
        'textarea',
    ]:
        try:
            el = page.wait_for_selector(sel, timeout=timeout)
            if el and el.is_visible():
                return el
        except:
            continue
    return None

def deepseek_rewrite(context, prompt: str) -> str | None:
    ds_session = load_deepseek_session()
    if not ds_session:
        print("  ❌ DeepSeek session not available — cannot rewrite.")
        return None
    ds_context = context.browser.new_context(storage_state=ds_session)
    page = ds_context.new_page()
    try:
        print("  🌐 DeepSeek page loading...")
        page.goto("https://chat.deepseek.com/", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)

        _deepseek_select_expert_mode(page)
        # DeepThink টগলও textarea-র handle নেওয়ার আগেই সেরে ফেলা হচ্ছে —
        # মোড/টগল ক্লিক কম্পোজ-এরিয়া re-render করতে পারে, যার ফলে আগে নেওয়া
        # handle stale হয়ে যেতে পারত (fill() তখন সাইলেন্টলি খালি বক্সে গিয়ে পড়তো)
        _deepseek_ensure_toggle_on(page, "DeepThink")

        textarea = _deepseek_find_textarea(page)
        if not textarea:
            print("  ❌ DeepSeek textarea not found.")
            page.screenshot(path=f"deepseek_debug_{int(time.time())}.png")
            return None

        textarea.click()
        page.wait_for_timeout(500)
        if not _deepseek_is_focused(page, textarea):
            textarea.click()
            page.wait_for_timeout(500)
            if not _deepseek_is_focused(page, textarea):
                print("  ⚠️  DeepSeek: ক্লিকের পরও ইনপুট বক্স focused হয়নি, তবু টাইপ করার চেষ্টা চলছে...")

        textarea.fill(prompt)
        page.wait_for_timeout(random.uniform(500, 800))

        # ── fill() আসলে টেক্সট বসিয়েছে কিনা read-back করে ভেরিফাই করা হচ্ছে —
        # stale handle বা অন্য কোনো কারণে বক্স খালি থেকে গেলে একবার নতুন করে
        # textarea খুঁজে রিট্রাই করা হয়, তাও ব্যর্থ হলে খালি বক্সে সাবমিট না করেই থামা
        try:
            current_value = textarea.input_value()
        except Exception:
            current_value = ""
        if not current_value.strip():
            print("  ⚠️  DeepSeek: fill()-এর পর বক্স খালি (stale handle সন্দেহ), textarea নতুন করে খুঁজে রিট্রাই করছি...")
            textarea = _deepseek_find_textarea(page, timeout=5000)
            if textarea:
                textarea.click()
                page.wait_for_timeout(500)
                textarea.fill(prompt)
                page.wait_for_timeout(random.uniform(500, 800))
                try:
                    current_value = textarea.input_value()
                except Exception:
                    current_value = ""
            if not textarea or not current_value.strip():
                print("  ❌ DeepSeek: রিট্রাইতেও বক্স খালি — প্রম্পট সাবমিট না করেই থামছে।")
                return None

        sent = False
        try:
            btn = page.wait_for_selector(
                'div[role="button"].ds-button--primary.ds-button--circle', timeout=5000
            )
            if btn and btn.is_visible() and btn.is_enabled():
                btn.click()
                sent = True
        except:
            pass
        if not sent:
            page.keyboard.press("Enter")

        print("  ⏳ Waiting for DeepSeek response (Expert mode + DeepThink, ~90s)...")
        page.wait_for_timeout(90000)

        response_text = ""
        last_text = ""
        stable_count = 0
        for _ in range(20):
            page.wait_for_timeout(2000)
            try:
                blocks = page.query_selector_all(
                    "div.ds-markdown.ds-assistant-message-main-content"
                )
                if blocks:
                    last_block = blocks[-1]
                    page.evaluate(
                        """(el) => {
                            el.querySelectorAll('a').forEach(a => a.remove());
                            el.querySelectorAll('span[style]').forEach(sp => {
                                const st = (sp.getAttribute('style') || '').replace(/\\s+/g, '');
                                if (st.includes('cursor:pointer')) {
                                    sp.remove();
                                }
                            });
                        }""",
                        last_block,
                    )
                    txt = last_block.inner_text().strip()
                    lines = [" ".join(line.split()) for line in txt.splitlines()]
                    txt = "\n".join(lines)
                    txt = re.sub(r'\n{3,}', '\n\n', txt).strip()
                    if txt:
                        if txt == last_text:
                            stable_count += 1
                        else:
                            stable_count = 0
                            last_text = txt
                        if stable_count >= 2:
                            response_text = txt
                            break
            except Exception:
                pass

        if not response_text and last_text:
            response_text = last_text

        REFUSAL_PHRASES = ["beyond my current scope"]
        if response_text and any(p in response_text.lower() for p in REFUSAL_PHRASES):
            print(f"  🚫 DeepSeek refused: {response_text[:80]}...")
            response_text = ""

        if response_text:
            print(f"  ✅ DeepSeek response: {response_text[:100]}...")
            return response_text
        else:
            print("  ⚠️  DeepSeek no response.")
    except Exception as e:
        print(f"  ⚠️  DeepSeek error: {e}")
    finally:
        page.close()
        ds_context.close()
    return None

# ──────────────────────────────────────────────
# g4f AI call (tweet selection only)
# ──────────────────────────────────────────────
def clean_text(text):
    text = re.sub(r'\*+', '', text)
    text = re.sub(r'_+', '', text)
    text = re.sub(r'#+', '', text)
    text = text.replace('\\"', '"').replace("\\'", "'")
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
# AI SELECTION (g4f, no disqualifiers, source + text only)
# ──────────────────────────────────────────────
def ai_select_best_tweet(tweet_list):
    try:
        shortlist = []
        for t in tweet_list:
            shortlist.append({
                "source": t["source"],
                "text": t["text"][:4000]
            })
        prompt = f"""You are a sharp geopolitical news editor for X/Twitter.
Below are tweets from breaking news sources. Pick the ONE tweet that is the most newsworthy, urgent, and likely to get high engagement.

Consider:
- Global geopolitical significance and urgency.
- High public interest and potential engagement.
- No reaction tweets.

STRICT EXCLUSION: Do NOT select any sports-related tweet.

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
# CAPTION GENERATION (DeepSeek, updated prompt)
# ──────────────────────────────────────────────
def build_final_caption(original_text, context=None):
    prompt = f"""You run an alternative news aggregator twitter account

Casually rewrite this sentence in simple words within 280 characters. 
No Emoji.
If a quote is present and it adds value, feel free to include the most important part of it.

CRITICAL FORMAT RULES:
- Use as many sentences as the content naturally needs (1, 2, or 3).
- Separate each sentence with exactly one blank line.


Tweet

{original_text}"""

    if context:
        result = deepseek_rewrite(context, prompt)
    else:
        result = None

    if result:
        parts = [p.strip() for p in re.split(r'\n\s*\n', result) if p.strip()]
        if len(parts) >= 2:
            caption = f"{parts[-2]}\n\n{parts[-1]}"
        elif len(parts) == 1:
            caption = parts[0]
        else:
            caption = clean_text(original_text)
        return caption

    print("  ⚠️ DeepSeek failed, posting original tweet text as fallback...")
    return clean_text(original_text)

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
# POSTING — attaches multiple media files in a single operation
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
        attach_btn = page.query_selector('button[aria-label="Add photos or video"]')
        if attach_btn:
            try:
                with page.expect_file_chooser(timeout=15000) as fc_info:
                    attach_btn.click()
                file_chooser = fc_info.value
                file_chooser.set_files(media_paths)
                print(f"  🎞 {len(media_paths)} media file(s) queued.")
                is_video = any(mp.lower().endswith('.mp4') for mp in media_paths)
                is_gif = any(mp.lower().endswith('.gif') for mp in media_paths)
                if is_video:
                    page.wait_for_timeout(3000)
                    try:
                        page.wait_for_selector('div[data-testid="attachments"]', timeout=30000)
                        print("  ✅ Attachment container found.")
                    except:
                        print("  ⚠️ Attachment container not found.")
                        page.screenshot(path=f"attach_fail_{int(time.time())}.png")
                    page.wait_for_timeout(45000)
                    try:
                        page.wait_for_selector('div[data-testid="attachments"] video', timeout=15000)
                        print("  ✅ Video preview confirmed.")
                    except:
                        print("  ⚠️ Preview not confirmed, continuing anyway.")
                elif is_gif:
                    try:
                        page.wait_for_selector('div[data-testid="attachments"]', timeout=15000)
                        print("  ✅ Attachment container found (GIF).")
                    except:
                        print("  ⚠️ Attachment container not found.")
                        page.screenshot(path=f"attach_fail_{int(time.time())}.png")
                    page.wait_for_timeout(random.randint(2500, 4000))
                else:
                    page.wait_for_timeout(random.randint(3000, 5000))
            except Exception as e:
                print(f"  ⚠️ Media attach error: {e}")
        else:
            print("  ⚠️ Attach button not found.")

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
# STOCHASTIC FAIR TOP-N SELECTION
# ──────────────────────────────────────────────
def _weighted_pick_one(cands):
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0]
    weights = [max(c['score'], 1.0) for c in cands]
    return random.choices(cands, weights=weights, k=1)[0]

def _weighted_sample_without_replacement(cands, k):
    pool = list(cands)
    chosen = []
    for _ in range(min(k, len(pool))):
        weights = [max(c['score'], 1.0) for c in pool]
        pick = random.choices(pool, weights=weights, k=1)[0]
        chosen.append(pick)
        pool.remove(pick)
    return chosen

def select_shortlist_for_ai(candidates, top_n=15):   # increased to 15 for more sources
    by_source = {}
    for c in candidates:
        by_source.setdefault(c['source'], []).append(c)

    per_source_picks = []
    for source, cands in by_source.items():
        pick = _weighted_pick_one(cands)
        if pick:
            per_source_picks.append(pick)

    if len(per_source_picks) <= top_n:
        shortlist = list(per_source_picks)
        if len(shortlist) < top_n:
            chosen_ids = {id(c) for c in shortlist}
            remaining = [c for c in candidates if id(c) not in chosen_ids]
            need = top_n - len(shortlist)
            shortlist.extend(_weighted_sample_without_replacement(remaining, need))
    else:
        shortlist = _weighted_sample_without_replacement(per_source_picks, top_n)

    random.shuffle(shortlist)
    return shortlist

# ──────────────────────────────────────────────
# POST-ONLY FUNCTION (with topic memory filter + 280‑char safety + fallback image)
# ──────────────────────────────────────────────
def perform_post_only(page, posted_cache, fallback_image_path=None):
    context = page.context
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
                if not txt or is_duplicate(txt, posted_cache) or is_promotional(txt) or is_too_short(txt) or is_sports_related(txt):
                    continue
                age = get_tweet_age_minutes(tweet)
                if age > 240:          # 4-hour cutoff
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

    # ────── Topic memory filtering (6-hour window, min 3 common keywords) ──────
    topic_memory = load_topic_memory()
    filtered_candidates = []
    for c in candidates:
        if not is_similar_topic(c["text"], topic_memory, min_overlap=3):
            filtered_candidates.append(c)
    if not filtered_candidates:
        print("\n⚠️ All candidates are on recently posted topics — skipping this round.")
        return False
    candidates = filtered_candidates
    # ──────────────────────────────────────────────────────────────────────────

    top_candidates = select_shortlist_for_ai(candidates, top_n=15)   # pass 15
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

    # ── কোনো নেটিভ মিডিয়া না পাওয়া গেলে fallback ছবি ব্যবহার (run-এর জন্য একবার ডাউনলোড করা কপি) ──
    if not media_paths and fallback_image_path and os.path.exists(fallback_image_path):
        print("  🖼️ No native media found — using fallback image.")
        media_paths = [fallback_image_path]

    print("  🤖 Generating caption...")
    final_caption = build_final_caption(original_text, context=context)

    # ── 280-char safety for free tier ──
    if len(final_caption) > 280:
        parts = final_caption.split("\n\n")
        if len(parts) > 1:
            final_caption = parts[0].strip()
            print(f"  ✂️ Caption too long, using first sentence only.")
        else:
            final_caption = final_caption[:280].rsplit(".", 1)[0].strip()
            print(f"  ✂️ Caption too long, truncated to 280 chars.")
    print(f"  ✅ Caption: {final_caption}")

    print("\n📤 Posting...")
    posted = open_compose_and_post(page, final_caption, media_paths)
    for path in media_paths:
        if path == fallback_image_path:
            continue  # fallback ছবিটা পুরো run জুড়ে reuse হবে, এখানে ডিলিট করা যাবে না
        try:
            os.remove(path)
        except:
            pass

    if posted:
        save_to_cache(original_text, POSTED_CACHE)
        add_to_topic_memory(original_text)
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
# HUMAN DELAY FUNCTION (adjusted for 40-48 posts/day)
# ──────────────────────────────────────────────
def human_delay(iteration, hour):
    if 6 <= hour < 10:
        base = random.randint(22, 35) * 60      # ~28 min avg -> ~12.8 posts in 6h
    elif 10 <= hour < 16:
        base = random.randint(25, 38) * 60      # ~31 min avg -> ~11.6
    elif 16 <= hour < 22:
        base = random.randint(22, 35) * 60      # ~28 min
    else:
        base = random.randint(30, 45) * 60      # ~37 min -> ~9.7
    return base

# ──────────────────────────────────────────────
# MAIN LOOP
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
            channel="chrome",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--use-gl=egl",
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

        # ── run শুরুতে একবারই fallback ছবি ডাউনলোড, পুরো ৬ ঘণ্টা reuse হবে ──
        fallback_image_path = download_fallback_image()

        print(f"\n🤖 News Bot started (Post-Only Mode) — {datetime.now(BD_TZ).strftime('%Y-%m-%d %H:%M:%S')} (BD time)")
        iteration = 0
        SIESTA_EVERY = 1000

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
            now = datetime.now(BD_TZ)
            print(f"\n🔄 Post iteration {iteration} — {now.strftime('%H:%M:%S')} (BD time)", flush=True)

            posted_cache = load_cache(POSTED_CACHE)

            success = perform_post_only(page, posted_cache, fallback_image_path)
            if not success:
                print("⚠️ Post failed, continuing after delay.", flush=True)

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
