#!/usr/bin/env python3
"""VFS Global randevu takip botu.

Belirlenen aralıklarla VFS Global'e giriş yapar, seçilen merkez / kategori /
alt kategori için uygun randevu olup olmadığını kontrol eder ve randevu
bulunduğunda Telegram üzerinden mesaj gönderir.

Kullanım:
    python vfs_bot.py                 # sürekli çalış
    python vfs_bot.py --once          # tek kontrol yap ve çık
    python vfs_bot.py --telegram-test # Telegram ayarlarını test et
    python vfs_bot.py --telegram-chat-id  # chat id'nizi öğrenin
"""

import argparse
import logging
import os
import random
import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv
from playwright.sync_api import Page, TimeoutError as PWTimeout, sync_playwright

load_dotenv()

log = logging.getLogger("vfs-bot")

LOGIN_EMAIL = 'input[formcontrolname="username"], input#email, input[type="email"]'
LOGIN_PASSWORD = 'input[formcontrolname="password"], input#password, input[type="password"]'
OTP_INPUT = 'input[formcontrolname="otp"], input[autocomplete="one-time-code"], input#otp'
ALERT_TEXT = ".alert, .border-info, [class*='alert'], [role='alert']"

LOGIN_BUTTON = re.compile(r"sign in|oturum aç|giriş|login", re.I)
OTP_BUTTON = re.compile(r"verify|doğrula|onayla|sign in|oturum aç|giriş|submit|gönder", re.I)
NEW_BOOKING = re.compile(r"start new booking|new booking|yeni rezervasyon|yeni randevu", re.I)
COOKIE_ACCEPT = re.compile(r"accept all|accept|tümünü kabul|kabul et", re.I)
BLOCKED = re.compile(
    r"access denied|erişim engellendi|you have been blocked|too many requests|error 1015|403201",
    re.I,
)
NO_SLOT = re.compile(
    r"no appointment slots|no slots|uygun randevu (yok|bulunma)|randevu bulunmamaktadır|müsait randevu yok",
    re.I,
)
HAS_SLOT = re.compile(r"earliest available|en erken", re.I)

OUTLOOK_FOLDERS = [
    "https://outlook.live.com/mail/0/inbox",
    "https://outlook.live.com/mail/0/junkemail",
]
OUTLOOK_ITEM = 'div[role="listbox"] div[role="option"]'
OUTLOOK_BODY = 'div[role="document"], div[aria-label="Message body"], div[aria-label="İleti gövdesi"]'
OTP_CODE = re.compile(r"(?<!\d)\d{6}(?!\d)")

DEBUG_DIR = Path("debug")


class BotError(Exception):
    pass


class OutlookLoggedOut(BotError):
    pass


class Blocked(BotError):
    """VFS geçici olarak erişimi engelledi (429 / Cloudflare)."""


@dataclass
class Config:
    email: str
    password: str
    base_url: str
    centres: list
    category: str
    subcategory: str
    interval_min: float
    jitter_min: float
    backoff_min: float
    remind_min: float
    captcha_wait_s: int
    headless: bool
    channel: str
    browser_path: str
    profile_dir: str
    otp_from_outlook: bool

    @classmethod
    def load(cls):
        def need(name):
            value = os.getenv(name, "").strip()
            if not value:
                sys.exit(f"Eksik ayar: {name} (.env dosyasını kontrol edin)")
            return value

        base_url = os.getenv("VFS_BASE_URL", "").strip().rstrip("/")
        if not base_url:
            base_url = "https://visa.vfsglobal.com/{}/{}/{}".format(
                os.getenv("VFS_COUNTRY", "tur"),
                os.getenv("VFS_LANGUAGE", "tr"),
                os.getenv("VFS_MISSION", "cze"),
            )
        return cls(
            email=need("VFS_EMAIL"),
            password=need("VFS_PASSWORD"),
            base_url=base_url,
            centres=[c.strip() for c in need("VFS_CENTRES").split(",") if c.strip()],
            category=need("VFS_CATEGORY"),
            subcategory=need("VFS_SUBCATEGORY"),
            interval_min=float(os.getenv("CHECK_INTERVAL_MINUTES", "6")),
            jitter_min=float(os.getenv("CHECK_JITTER_MINUTES", "2")),
            backoff_min=float(os.getenv("BLOCKED_BACKOFF_MINUTES", "60")),
            remind_min=float(os.getenv("REMIND_MINUTES", "30")),
            captcha_wait_s=int(os.getenv("CAPTCHA_WAIT_SECONDS", "300")),
            headless=os.getenv("HEADLESS", "false").lower() in ("1", "true", "yes"),
            channel=os.getenv("BROWSER_CHANNEL", "").strip(),
            browser_path=os.getenv("BROWSER_PATH", "").strip(),
            profile_dir=os.getenv("BROWSER_PROFILE_DIR", "browser-profile"),
            otp_from_outlook=os.getenv("OTP_FROM_OUTLOOK", "true").lower() in ("1", "true", "yes"),
        )


class Telegram:
    def __init__(self, token, chat_id):
        self.api = f"https://api.telegram.org/bot{token}"
        self.chat_id = str(chat_id)
        self.offset = None

    def send(self, text):
        try:
            r = requests.post(
                f"{self.api}/sendMessage",
                json={"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True},
                timeout=15,
            )
            if not r.ok:
                log.warning("Telegram mesajı gönderilemedi: %s %s", r.status_code, r.text)
        except requests.RequestException as e:
            log.warning("Telegram mesajı gönderilemedi: %s", e)

    def updates(self, timeout=0):
        params = {"timeout": timeout}
        if self.offset is not None:
            params["offset"] = self.offset
        r = requests.get(f"{self.api}/getUpdates", params=params, timeout=timeout + 15)
        r.raise_for_status()
        result = r.json().get("result", [])
        if result:
            self.offset = result[-1]["update_id"] + 1
        return result

    def drain(self):
        """Eski mesajları atla ki sadece bundan sonra yazılanlar okunsun."""
        try:
            self.updates()
        except requests.RequestException as e:
            log.warning("Telegram mesajları okunamadı: %s", e)

    def wait_for_reply(self, pattern, timeout_s):
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                for upd in self.updates(timeout=20):
                    msg = upd.get("message") or {}
                    if str(msg.get("chat", {}).get("id")) != self.chat_id:
                        continue
                    m = re.search(pattern, msg.get("text", ""))
                    if m:
                        return m.group(0)
            except requests.RequestException as e:
                log.warning("Telegram mesajları okunamadı: %s", e)
                time.sleep(5)
        return None


def telegram_from_env():
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        sys.exit("TELEGRAM_BOT_TOKEN ve TELEGRAM_CHAT_ID ayarlanmalı (.env)")
    return Telegram(token, chat_id)


def print_chat_ids():
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        sys.exit("Önce .env içinde TELEGRAM_BOT_TOKEN ayarlayın.")
    r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=15)
    r.raise_for_status()
    chats = {}
    for upd in r.json().get("result", []):
        chat = (upd.get("message") or {}).get("chat")
        if chat:
            chats[chat["id"]] = chat.get("username") or chat.get("title") or chat.get("first_name")
    if not chats:
        print("Mesaj bulunamadı. Telegram'da botunuza herhangi bir mesaj yazıp tekrar deneyin.")
    for chat_id, name in chats.items():
        print(f"TELEGRAM_CHAT_ID={chat_id}   ({name})")


# --------------------------------------------------------------------------- #
# Tarayıcı adımları
# --------------------------------------------------------------------------- #

def check_blocked(page: Page):
    try:
        body = page.locator("body").inner_text(timeout=3_000)
    except PWTimeout:
        return
    if BLOCKED.search(body):
        raise Blocked("VFS erişimi geçici olarak engelledi")


def accept_cookies(page: Page):
    btn = page.get_by_role("button", name=COOKIE_ACCEPT).first
    try:
        if btn.is_visible(timeout=2_000):
            btn.click()
    except PWTimeout:
        pass


def login(page: Page, cfg: Config, tg: Telegram):
    log.info("Giriş yapılıyor...")
    old_code = None
    if cfg.otp_from_outlook:
        try:
            old_code = latest_outlook_code(page.context)
        except OutlookLoggedOut:
            pass
    page.goto(f"{cfg.base_url}/login", wait_until="domcontentloaded")
    email = page.locator(LOGIN_EMAIL).first
    try:
        email.wait_for(timeout=60_000)
    except PWTimeout:
        check_blocked(page)
        raise BotError("Giriş formu yüklenmedi")
    accept_cookies(page)
    email.fill(cfg.email)
    page.locator(LOGIN_PASSWORD).first.fill(cfg.password)

    # Cloudflare doğrulaması bitene kadar giriş butonu pasif kalır.
    button = page.get_by_role("button", name=LOGIN_BUTTON).first
    start = time.time()
    notified = False
    while not button.is_enabled():
        waited = time.time() - start
        if waited > cfg.captcha_wait_s:
            raise BotError("Captcha doğrulaması zaman aşımına uğradı")
        if waited > 30 and not notified:
            tg.send("⚠️ VFS girişinde captcha doğrulaması bekleniyor. "
                    "Bot'un çalıştığı bilgisayarda açık tarayıcıdan doğrulamayı tamamlayın.")
            notified = True
        page.wait_for_timeout(1_000)
    button.click()

    otp_done = False
    deadline = time.time() + 90
    while time.time() < deadline:
        if "/dashboard" in page.url:
            log.info("Giriş başarılı")
            return
        otp = page.locator(OTP_INPUT).first
        if not otp_done and otp.is_visible():
            submit_otp(page, otp, tg, cfg, old_code)
            otp_done = True
            deadline = time.time() + 90
        check_blocked(page)
        page.wait_for_timeout(1_000)
    alert = page.locator(ALERT_TEXT).first
    detail = alert.inner_text() if alert.count() else page.url
    raise BotError(f"Giriş başarısız: {detail.strip()[:200]}")


def latest_outlook_code(context):
    """Outlook'taki en yeni VFS mailinden 6 haneli kodu okur (gelen kutusu + gereksiz)."""
    page = context.new_page()
    try:
        for url in OUTLOOK_FOLDERS:
            page.goto(url, wait_until="domcontentloaded")
            items = page.locator(OUTLOOK_ITEM)
            try:
                items.first.wait_for(timeout=30_000)
            except PWTimeout:
                if "/mail/" not in page.url:
                    raise OutlookLoggedOut("Outlook oturumu kapalı")
                continue
            for i in range(min(5, items.count())):
                item = items.nth(i)
                label = (item.get_attribute("aria-label") or "") + " " + item.inner_text()
                if "vfs" not in label.lower():
                    continue
                code = OTP_CODE.search(label)
                if not code:
                    item.click()
                    body = page.locator(OUTLOOK_BODY).first
                    body.wait_for(timeout=15_000)
                    code = OTP_CODE.search(body.inner_text())
                if code:
                    return code.group(0)
                break
        return None
    except OutlookLoggedOut:
        raise
    except Exception as e:  # noqa: BLE001 - Outlook okunamazsa Telegram'a düşülür
        log.warning("Outlook okunamadı: %s", str(e).splitlines()[0])
        return None
    finally:
        page.close()


def wait_outlook_code(context, old_code, tg: Telegram, timeout_s=180):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            code = latest_outlook_code(context)
        except OutlookLoggedOut:
            log.warning("Outlook oturumu kapalı: python vfs_bot.py --outlook-login çalıştırın")
            tg.send("⚠️ Outlook oturumu kapalı, kod otomatik okunamadı. Botu durdurup "
                    "bilgisayarda şunu çalıştırın: python vfs_bot.py --outlook-login")
            return None
        if code and code != old_code:
            return code
        time.sleep(10)
    return None


def submit_otp(page: Page, otp_input, tg: Telegram, cfg: Config, old_code=None):
    code = None
    if cfg.otp_from_outlook:
        log.info("OTP isteniyor, Outlook'tan kod okunuyor")
        code = wait_outlook_code(page.context, old_code, tg)
        if code:
            log.info("OTP Outlook'tan alındı")
        page.bring_to_front()
    if not code:
        log.info("OTP isteniyor, Telegram'dan kod bekleniyor")
        tg.drain()
        tg.send("🔐 VFS e-postanıza tek kullanımlık kod (OTP) gönderildi. "
                "Kodu bu sohbete yazın (5 dakika içinde).")
        code = tg.wait_for_reply(r"\b\d{4,8}\b", 300)
        if not code:
            raise BotError("OTP kodu zamanında gelmedi")
        tg.send("Kod alındı, giriş yapılıyor.")
    otp_input.fill(code)
    page.get_by_role("button", name=OTP_BUTTON).first.click()


def open_booking_form(page: Page, cfg: Config, tg: Telegram):
    page.goto(f"{cfg.base_url}/dashboard", wait_until="domcontentloaded")
    button = page.get_by_role("button", name=NEW_BOOKING).first
    try:
        button.wait_for(timeout=30_000)
    except PWTimeout:
        check_blocked(page)
        if "/login" not in page.url and not page.locator(LOGIN_EMAIL).count():
            raise BotError("Panel sayfası yüklenmedi")
        login(page, cfg, tg)
        button.wait_for(timeout=30_000)
    button.click()
    page.locator("mat-select").first.wait_for(timeout=30_000)


def fold(text: str) -> str:
    """Büyük/küçük harf ve Türkçe karakter farklarını yok sayar (İstanbul == istanbul == Istanbul)."""
    text = text.replace("İ", "i").replace("ı", "i")
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c)).casefold()


def choose(page: Page, index: int, wanted: str):
    select = page.locator("mat-select").nth(index)
    select.wait_for(timeout=30_000)
    # Bir önceki seçimden sonra listenin yenilenmesine zaman tanı.
    page.wait_for_timeout(1_500)
    select.click()
    options = page.locator("mat-option")
    options.first.wait_for(timeout=20_000)
    texts = [t.strip() for t in options.all_inner_texts()]
    for i, text in enumerate(texts):
        if fold(wanted) in fold(text):
            options.nth(i).click()
            return text
    page.keyboard.press("Escape")
    raise BotError(f"'{wanted}' seçeneği bulunamadı. Mevcut seçenekler: {texts}")


def parse_slot_response(data: dict):
    """CheckIsSlotAvailable yanıtını (uygun_mu, en_erken_tarih, mesaj) olarak döndürür."""
    slots = data.get("earliestSlotLists") or []
    earliest = data.get("earliestDate") or (slots[0].get("date") if slots else None)
    if earliest:
        return True, earliest, ""
    error = data.get("error") or {}
    return False, None, error.get("description") or "Uygun randevu yok"


def read_slot_message(page: Page):
    alert = page.locator(ALERT_TEXT).filter(has_text=re.compile(r"\w")).last
    try:
        text = alert.inner_text(timeout=15_000).strip()
    except PWTimeout:
        check_blocked(page)
        raise BotError("Randevu durumu okunamadı")
    if NO_SLOT.search(text):
        return False, None, text
    if HAS_SLOT.search(text):
        m = re.search(r"\d{1,2}[./-]\d{1,2}[./-]\d{2,4}", text)
        return True, m.group(0) if m else text, text
    raise BotError(f"Randevu mesajı anlaşılamadı: {text[:200]}")


def check_centre(page: Page, cfg: Config, tg: Telegram, centre: str):
    open_booking_form(page, cfg, tg)
    centre_name = choose(page, 0, centre)
    choose(page, 1, cfg.category)
    try:
        with page.expect_response(
            lambda r: "checkisslotavailable" in r.url.lower(), timeout=45_000
        ) as info:
            choose(page, 2, cfg.subcategory)
        resp = info.value
        if resp.status in (403, 429):
            raise Blocked(f"Randevu sorgusu HTTP {resp.status} döndü")
        available, date, message = parse_slot_response(resp.json())
    except PWTimeout:
        log.debug("Ağ yanıtı yakalanamadı, sayfadaki mesaj okunuyor")
        available, date, message = read_slot_message(page)
    return centre_name, available, date, message


def save_debug(page: Page, label: str):
    DEBUG_DIR.mkdir(exist_ok=True)
    path = DEBUG_DIR / f"{datetime.now():%Y%m%d-%H%M%S}-{label}.png"
    try:
        page.screenshot(path=str(path), full_page=True)
        log.info("Ekran görüntüsü kaydedildi: %s", path)
    except Exception as e:  # noqa: BLE001 - debug yardımcısı, asıl hatayı gizlememeli
        log.warning("Ekran görüntüsü alınamadı: %s", e)


# --------------------------------------------------------------------------- #
# Ana döngü
# --------------------------------------------------------------------------- #

BRAVE_PATHS = [
    Path(os.getenv("PROGRAMFILES", r"C:\Program Files")) / "BraveSoftware/Brave-Browser/Application/brave.exe",
    Path(os.getenv("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "BraveSoftware/Brave-Browser/Application/brave.exe",
    Path(os.getenv("LOCALAPPDATA", "")) / "BraveSoftware/Brave-Browser/Application/brave.exe",
    Path("/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"),
]


def launch_browser(p, cfg: Config, options: dict):
    """Sırasıyla dener: BROWSER_PATH, BROWSER_CHANNEL (ör. chrome), kurulu Brave, yerleşik Chromium."""
    attempts = []
    if cfg.browser_path:
        attempts.append((cfg.browser_path, {"executable_path": cfg.browser_path}))
    if cfg.channel:
        attempts.append((cfg.channel, {"channel": cfg.channel}))
    brave = next((b for b in BRAVE_PATHS if b.is_file()), None)
    if brave:
        attempts.append(("Brave", {"executable_path": str(brave)}))
    attempts.append(("yerleşik Chromium", {}))
    for name, extra in attempts:
        try:
            context = p.chromium.launch_persistent_context(cfg.profile_dir, **extra, **options)
            log.info("Tarayıcı: %s", name)
            return context
        except Exception as e:  # noqa: BLE001 - sıradaki tarayıcıyı dene
            if not extra:
                raise
            log.warning("'%s' açılamadı: %s", name, str(e).splitlines()[0])


def outlook_login(cfg: Config):
    with sync_playwright() as p:
        context = launch_browser(p, cfg, dict(headless=False, viewport={"width": 1366, "height": 900}))
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(OUTLOOK_FOLDERS[0])
        print("\nAçılan pencerede Outlook hesabınıza giriş yapın ve 'Oturumu açık tut' seçeneğine EVET deyin.")
        print("Gelen kutunuzu görünce buraya dönüp Enter'a basın.")
        input()
        ok = page.locator(OUTLOOK_ITEM).count() > 0 or "live.com/mail" in page.url
        context.close()
        print("Outlook bağlandı. ✅" if ok else "Gelen kutusu görünmedi, tekrar deneyin.")
        return 0 if ok else 1


def run(cfg: Config, tg: Telegram, once: bool):
    notified = {}  # merkez -> (tarih, gönderim zamanı)
    errors = 0
    error_alerted = False

    with sync_playwright() as p:
        options = dict(
            headless=cfg.headless,
            viewport={"width": 1366, "height": 900},
            locale="tr-TR",
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = launch_browser(p, cfg, options)
        page = context.pages[0] if context.pages else context.new_page()
        if not once:
            tg.send(f"🤖 VFS randevu botu başladı.\nMerkezler: {', '.join(cfg.centres)}\n"
                    f"Kategori: {cfg.category} / {cfg.subcategory}\n"
                    f"Kontrol aralığı: ~{cfg.interval_min:g} dk")

        while True:
            delay = cfg.interval_min + random.uniform(0, cfg.jitter_min)
            try:
                for centre in cfg.centres:
                    name, available, date, message = check_centre(page, cfg, tg, centre)
                    if available:
                        log.info("✅ %s: uygun randevu var, en erken %s", name, date)
                        last = notified.get(centre)
                        if (not last or last[0] != date
                                or time.time() - last[1] > cfg.remind_min * 60):
                            tg.send(f"🟢 RANDEVU BULUNDU!\nMerkez: {name}\n"
                                    f"Kategori: {cfg.category} / {cfg.subcategory}\n"
                                    f"En erken tarih: {date}\n\nHemen giriş yapın: {cfg.base_url}/login")
                            notified[centre] = (date, time.time())
                    else:
                        log.info("❌ %s: %s", name, message)
                        notified.pop(centre, None)
                    page.wait_for_timeout(random.randint(3_000, 8_000))
                errors = 0
                if error_alerted:
                    tg.send("✅ Bot tekrar normal çalışıyor.")
                    error_alerted = False
            except Blocked as e:
                log.warning("%s; %g dakika bekleniyor", e, cfg.backoff_min)
                save_debug(page, "blocked")
                tg.send(f"⛔ {e}. {cfg.backoff_min:g} dakika sonra tekrar denenecek.")
                delay = cfg.backoff_min
                if once:
                    return 1
            except Exception as e:  # noqa: BLE001 - döngü her hatadan sonra devam etmeli
                errors += 1
                log.exception("Kontrol başarısız (%d. ardışık hata)", errors)
                save_debug(page, "error")
                if errors >= 3 and not error_alerted:
                    tg.send(f"⚠️ Bot art arda {errors} kez kontrol yapamadı.\nSon hata: {e}\n"
                            "debug/ klasöründeki ekran görüntülerine bakın.")
                    error_alerted = True
                if once:
                    return 1
            if once:
                return 0
            log.info("Sonraki kontrol %.1f dakika sonra", delay)
            time.sleep(delay * 60)


def main():
    parser = argparse.ArgumentParser(description="VFS Global randevu takip botu")
    parser.add_argument("--once", action="store_true", help="tek kontrol yap ve çık")
    parser.add_argument("--telegram-test", action="store_true", help="test mesajı gönder")
    parser.add_argument("--telegram-chat-id", action="store_true", help="chat id'leri listele")
    parser.add_argument("--outlook-login", action="store_true",
                        help="OTP kodlarının okunacağı Outlook hesabına bir kez giriş yap")
    parser.add_argument("--debug", action="store_true", help="ayrıntılı log")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler("vfs_bot.log", encoding="utf-8")],
    )

    if args.telegram_chat_id:
        print_chat_ids()
        return 0
    if args.outlook_login:
        return outlook_login(Config.load())
    tg = telegram_from_env()
    if args.telegram_test:
        tg.send("✅ VFS randevu botu: Telegram bağlantısı çalışıyor.")
        print("Test mesajı gönderildi.")
        return 0
    try:
        return run(Config.load(), tg, args.once)
    except KeyboardInterrupt:
        log.info("Durduruldu")
        return 0


if __name__ == "__main__":
    sys.exit(main())
