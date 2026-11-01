#!/usr/bin/env python3
"""
deepseek_session.py — DeepSeek-এর সেশন বের করে GitHub secret DEEPSEEK_SESSION_JSON-এর জন্য এক লাইনের JSON বানায়।

ব্যবহার (নিজের পিসিতে, একবার):
    pip install playwright
    python deepseek_session.py

ডিফল্ট পদ্ধতি (সুপারিশকৃত) — "attach":
  ১. স্ক্রিপ্ট আপনার ইনস্টল করা সাধারণ Google Chrome আলাদা একটা প্রোফাইলে খোলে (অটোমেশন-ফ্ল্যাগ ছাড়া)।
  ২. সেখানে আপনি নিজে DeepSeek-এ লগইন করেন (ইমেইল-কোড আসবে — আগের "অটোমেশন-ব্রাউজারে" আসত না)।
  ৩. টার্মিনালে Enter চাপলে স্ক্রিপ্ট ওই Chrome-এর সাথে যুক্ত হয়ে কুকি + localStorage পড়ে নেয়।
  ৪. নতুন ব্রাউজারে যাচাই করে, এক লাইনের JSON সেভ করে ও ক্লিপবোর্ডে কপি করে।
     → GitHub: Settings → Secrets and variables → Actions → DEEPSEEK_SESSION_JSON-এ পেস্ট করুন।

পুরনো পদ্ধতি: --playwright-launch (Playwright নিজে ব্রাউজার খোলে; DeepSeek এতে কোড না-ও পাঠাতে পারে)।

নিরাপত্তা: Chrome-টা debug পোর্টসহ খোলা থাকে (শুধু আপনার পিসির ভেতর, 127.0.0.1) — কাজ শেষে স্ক্রিপ্ট নিজেই বন্ধ করে।
এই JSON পাসওয়ার্ডের মতো — কাউকে দেবেন না, গিটে কমিট করবেন না।
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

DEFAULT_URL = "https://chat.deepseek.com/"
DEFAULT_PORT = 9222
DEFAULT_PROFILE = os.path.join(os.path.expanduser("~"), ".deepseek_login_profile")
SECRET_LIMIT = 48_000      # GitHub secret-এর সীমা ~48KB; একটু নিচে থাকি


# ──────────────────────────────────────────────
# সাধারণ সহায়ক
# ──────────────────────────────────────────────
def launch(p, headless):
    """Google Chrome থাকলে সেটা, না থাকলে Playwright-এর Chromium।"""
    try:
        return p.chromium.launch(headless=headless, channel="chrome")
    except Exception:
        return p.chromium.launch(headless=headless)


def looks_logged_out(page):
    return "sign_in" in page.url.lower() or "login" in page.url.lower()


# ──────────────────────────────────────────────
# পুরনো পদ্ধতি: Playwright নিজে ব্রাউজার খোলে (--playwright-launch)
# ──────────────────────────────────────────────
def export_state_playwright(url, headless=False, wait=input):
    with sync_playwright() as p:
        browser = launch(p, headless)
        ctx = browser.new_context(viewport={"width": 1280, "height": 850})
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        print("\n🌐 খোলা উইন্ডোতে DeepSeek-এ লগইন করুন।")
        print("   চ্যাট স্ক্রিন (মেসেজ লেখার বক্স) দেখা গেলে এখানে ফিরে Enter চাপুন।")
        wait("   ▶ Enter: ")
        if looks_logged_out(page):
            print("⚠️ এখনো লগইন/সাইন-ইন পেজে আছেন মনে হচ্ছে — লগইন শেষ করে আবার চেষ্টা করুন।")
        state = ctx.storage_state()
        browser.close()
    return state


# ──────────────────────────────────────────────
# ডিফল্ট পদ্ধতি: সাধারণ Chrome খুলে CDP দিয়ে যুক্ত হওয়া (--attach)
# ──────────────────────────────────────────────
def find_chrome(custom=None):
    if custom:
        return custom if os.path.exists(custom) else None
    cands = []
    if sys.platform.startswith("win"):
        for var in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(var)
            if base:
                cands.append(os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"))
    elif sys.platform == "darwin":
        cands.append("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    else:
        cands += [shutil.which(n) for n in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")]
    for c in cands:
        if c and os.path.exists(c):
            return c
    return None


def cdp_alive(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def start_chrome(port, profile_dir, url, exe=None, extra_args=()):
    """আলাদা প্রোফাইল-ফোল্ডারে সাধারণ Chrome খোলে। (Chrome নতুন ভার্সনে ডিফল্ট প্রোফাইলে
    debug পোর্ট চালু করতে দেয় না — তাই আলাদা --user-data-dir লাগে।)"""
    exe = exe or find_chrome()
    if not exe:
        return None
    os.makedirs(profile_dir, exist_ok=True)
    cmd = [exe, f"--remote-debugging-port={port}", f"--user-data-dir={profile_dir}",
           "--no-first-run", "--no-default-browser-check", *extra_args, url]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        if cdp_alive(port):
            return proc
        time.sleep(0.5)
    try:
        proc.terminate()
    except Exception:
        pass
    return None


def export_state_attach(url, port=DEFAULT_PORT, profile_dir=DEFAULT_PROFILE, chrome_path=None,
                        domain_hint="deepseek", wait=input, extra_args=()):
    started = None
    if cdp_alive(port):
        print(f"ℹ️ পোর্ট {port}-এ আগে থেকেই একটা Chrome চলছে — সেটাই ব্যবহার করছি।")
    else:
        exe = find_chrome(chrome_path)
        if not exe:
            print("❌ Google Chrome খুঁজে পাওয়া যায়নি। --chrome-path \"...\\chrome.exe\" দিন, "
                  "অথবা --playwright-launch দিয়ে পুরনো পদ্ধতিতে চেষ্টা করুন।")
            sys.exit(1)
        print(f"🚀 সাধারণ Chrome খোলা হচ্ছে (আলাদা প্রোফাইল: {profile_dir}) ...")
        started = start_chrome(port, profile_dir, url, exe=exe, extra_args=extra_args)
        if not started:
            print("❌ Chrome খুলে debug পোর্ট পাওয়া গেল না। অন্য সব Chrome উইন্ডো বন্ধ করে আবার চেষ্টা করুন, "
                  "অথবা --port বদলে দিন।")
            sys.exit(1)

    try:
        print("\n🌐 যে Chrome উইন্ডো খুলেছে সেখানে DeepSeek-এ নিজে লগইন করুন (ইমেইল-কোড এলে দিন)।")
        print("   চ্যাট স্ক্রিন (মেসেজ লেখার বক্স) দেখা গেলে এখানে ফিরে Enter চাপুন।")
        wait("   ▶ Enter: ")

        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            ctx = browser.contexts[0]
            cookies = [c for c in ctx.cookies() if domain_hint in (c.get("domain") or "")]
            page = next((pg for pg in ctx.pages if domain_hint in pg.url), None)
            if page is None:      # চ্যাট ট্যাব বন্ধ করে ফেললে নতুন ট্যাবে খুলে পড়ি
                page = ctx.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(3000)
            if looks_logged_out(page):
                print("⚠️ এখনো লগইন/সাইন-ইন পেজে আছেন মনে হচ্ছে — লগইন শেষ করে আবার চেষ্টা করুন।")
            ls = page.evaluate(
                "() => Object.entries(localStorage).map(([name, value]) => ({name, value}))")
            u = urlparse(page.url)
            origin = f"{u.scheme}://{u.netloc}" if u.scheme and u.netloc else url.rstrip("/")
            browser.close()      # শুধু সংযোগ ছাড়ে; Chrome বন্ধ হয় না
        state = {"cookies": cookies, "origins": [{"origin": origin, "localStorage": ls}]}
    finally:
        if started is not None:
            try:
                started.terminate()      # debug পোর্ট খোলা না রাখতে নিজের খোলা Chrome বন্ধ
                print("🔒 স্ক্রিপ্টের খোলা Chrome বন্ধ করা হলো।")
            except Exception:
                pass
    return state


# ──────────────────────────────────────────────
# আকার কমানো, যাচাই, ক্লিপবোর্ড
# ──────────────────────────────────────────────
def shrink(state, limit=SECRET_LIMIT):
    """JSON বেশি বড় হলে সবচেয়ে বড় localStorage মানগুলো বাদ দিই (কুকি ও ছোট টোকেন থাকে)।"""
    dropped = []
    while len(json.dumps(state, separators=(",", ":"))) > limit:
        biggest = None
        for oi, origin in enumerate(state.get("origins", [])):
            for ii, item in enumerate(origin.get("localStorage", [])):
                size = len(item.get("value", ""))
                if biggest is None or size > biggest[0]:
                    biggest = (size, oi, ii)
        if biggest is None:
            break
        _, oi, ii = biggest
        item = state["origins"][oi]["localStorage"].pop(ii)
        dropped.append(item.get("name"))
    return dropped


def verify_state(state, url, headless=True):
    """নতুন ব্রাউজারে এই সেশন দিয়ে ঢুকে দেখি লগইন টিকে আছে কিনা।"""
    with sync_playwright() as p:
        browser = launch(p, headless)
        ctx = browser.new_context(storage_state=state)
        page = ctx.new_page()
        ok = False
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(5000)
            has_box = page.locator("textarea").count() > 0
            ok = has_box and not looks_logged_out(page)
        except Exception as e:
            print(f"⚠️ যাচাইয়ে সমস্যা: {str(e).splitlines()[0][:120]}")
        browser.close()
    return ok


def copy_to_clipboard(text):
    data = text.encode("utf-8")
    try:
        if sys.platform.startswith("win"):
            subprocess.run("clip", input=data, check=True, shell=True)
            return True
        if sys.platform == "darwin":
            subprocess.run(["pbcopy"], input=data, check=True)
            return True
        for cmd in (["xclip", "-selection", "clipboard"], ["wl-copy"]):
            try:
                subprocess.run(cmd, input=data, check=True)
                return True
            except FileNotFoundError:
                continue
    except Exception:
        pass
    return False


def main():
    ap = argparse.ArgumentParser(description="DeepSeek সেশন → GitHub secret JSON")
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--out", default="deepseek_session.min.json")
    ap.add_argument("--no-verify", action="store_true", help="যাচাই ধাপ বাদ")
    ap.add_argument("--playwright-launch", action="store_true",
                    help="পুরনো পদ্ধতি: Playwright নিজে ব্রাউজার খোলে (DeepSeek কোড না-ও পাঠাতে পারে)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--profile-dir", default=DEFAULT_PROFILE)
    ap.add_argument("--chrome-path", default=None, help="chrome.exe-এর পাথ (অটো খুঁজে না পেলে)")
    args = ap.parse_args()

    if args.playwright_launch:
        state = export_state_playwright(args.url)
    else:
        state = export_state_attach(args.url, port=args.port, profile_dir=args.profile_dir,
                                    chrome_path=args.chrome_path)

    cookies = state.get("cookies", [])
    ls_names = [i.get("name") for o in state.get("origins", []) for i in o.get("localStorage", [])]
    print(f"\n📦 কুকি: {len(cookies)}টি | localStorage আইটেম: {len(ls_names)}টি")
    if "userToken" not in ls_names:
        print("⚠️ 'userToken' পাওয়া যায়নি (এটা DeepSeek-এর লগইন-টোকেনের নাম বলে আমার ধারণা) — "
              "লগইন সম্পূর্ণ হয়েছে কিনা নিশ্চিত হোন।")
    if not cookies:
        print("❌ কোনো কুকি পাওয়া যায়নি — লগইন হয়নি। আবার চালান।")
        sys.exit(1)

    dropped = shrink(state)
    if dropped:
        print(f"✂️ সাইজ কমাতে localStorage থেকে বাদ: {dropped}")

    if not args.no_verify:
        print("🔎 নতুন ব্রাউজারে সেশন যাচাই হচ্ছে...")
        if verify_state(state, args.url):
            print("✅ যাচাই সফল — এই সেশন দিয়ে লগইন টিকছে।")
        else:
            print("⚠️ যাচাই সফল হয়নি — লগইন পুরো না হলে বা সেশন যাচাই-বাধা পেলে এমন হয়। "
                  "চাইলে তবু চেষ্টা করতে পারেন, নইলে আবার লগইন করে চালান।")

    text = json.dumps(state, separators=(",", ":"))     # এক লাইন — GitHub লগে '***' ভাঙন এড়াতে
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(text)
    size = len(text.encode("utf-8"))
    print(f"\n💾 সেভ হয়েছে: {args.out} ({size} বাইট, এক লাইন)")
    if size > SECRET_LIMIT:
        print("⚠️ এখনো GitHub secret-এর সীমার কাছাকাছি/বেশি — কাজ না করলে জানাবেন।")
    if copy_to_clipboard(text):
        print("📋 ক্লিপবোর্ডে কপি করা হয়েছে — GitHub secret DEEPSEEK_SESSION_JSON-এ সরাসরি পেস্ট করুন।")
    else:
        print(f"📋 ক্লিপবোর্ডে কপি হয়নি — {args.out} ফাইলটা খুলে পুরো লেখা কপি করুন।")
    print("\n🔐 মনে রাখবেন: এই JSON পাসওয়ার্ডের মতো, কাউকে দেবেন না।")


if __name__ == "__main__":
    main()
