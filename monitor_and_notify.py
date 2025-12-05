
# -*- coding: utf-8 -*-
"""
ふもとっぱら予約カレンダー監視 + LINE Messaging API通知（Selenium版・カテゴリ「キャンプ宿泊」限定）
- ページ表示後に「キャンプ宿泊」カテゴリをクリックしてから解析
- カレンダー本体（例: .calendar-area）配下で日付セルを特定
- セル内のテキスト／img alt/title/class で △/○/× を判定
- セルの innerHTML と スクリーンショットを保存（Artifactsで確認）
- ×/○→△に変化したときだけ通知のオプションあり
"""

import os
import sys
import json
import time
import requests

# Selenium
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


def env(name: str, default: str | None = None):
    val = os.environ.get(name)
    if val is None or (isinstance(val, str) and val.strip() == ""):
        return default
    return val


# ===== 監視対象設定 =====
CALENDAR_URL       = env("FUMO_CALENDAR_URL", "https://reserve.fumotoppara.net/reserved/reserved-calendar-list")
TARGET_CATEGORY    = env("TARGET_CATEGORY_LABEL", "キャンプ宿泊")   # ← カテゴリ名（可視テキスト）
TARGET_DATE_LABEL  = env("TARGET_DATE_LABEL", "12/31")              # 画面表示どおり（例：12/31 / 12月31日）
TARGET_DATE_ISO    = env("TARGET_DATE_ISO", None)                   # 例：2025-12-31（data-date属性があれば推奨）
NOTIFY_DIFF_ONLY   = env("NOTIFY_DIFF_ONLY", "0") == "1"            # "1"なら ×/○→△ の変化時のみ通知

# ===== LINE設定 =====
CHANNEL_TOKEN      = env("LINE_CHANNEL_TOKEN")
SEND_MODE          = env("LINE_SEND_MODE", "push")                  # push|broadcast|multicast
TO_USER_ID         = env("LINE_TO_USER_ID", None)
TO_GROUP_ID        = env("LINE_TO_GROUP_ID", None)
USER_IDS_CSV       = env("LINE_USER_IDS", "")
LINE_MESSAGE       = env("LINE_MESSAGE", f"🚨 ふもとっぱら（{TARGET_CATEGORY}）{TARGET_DATE_LABEL} に空き（△）が出ました！\n{CALENDAR_URL}")

HEADERS = {"Content-Type": "application/json",
           "Authorization": f"Bearer {CHANNEL_TOKEN}" if CHANNEL_TOKEN else ""}

# ===== 保存先（Artifacts用） =====
DUMP_DIR  = "html_dump"
SHOT_DIR  = "shots"
CACHE_FILE = "last_status.txt"


# ===== LINE送信 =====
def notify_push(target_id: str, text: str):
    url = "https://api.line.me/v2/bot/message/push"
    payload = {"to": target_id, "messages": [{"type": "text", "text": text}]}
    r = requests.post(url, headers=HEADERS, json=payload, timeout=20)
    r.raise_for_status()
    print(f"[LINE] Push sent to {target_id}: {r.status_code}")

def notify_broadcast(text: str):
    url = "https://api.line.me/v2/bot/message/broadcast"
    payload = {"messages": [{"type": "text", "text": text}]}
    r = requests.post(url, headers=HEADERS, json=payload, timeout=20)
    r.raise_for_status()
    print(f"[LINE] Broadcast sent: {r.status_code}")

def notify_multicast(user_ids, text: str):
    url = "https://api.line.me/v2/bot/message/multicast"
    payload = {"to": user_ids, "messages": [{"type": "text", "text": text}]}
    r = requests.post(url, headers=HEADERS, json=payload, timeout=20)
    r.raise_for_status()
    print(f"[LINE] Multicast sent({len(user_ids)} users): {r.status_code}")


# ===== ブラウザ起動 =====
def setup_driver() -> webdriver.Chrome:
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1280,2200")
    opts.add_argument("--lang=ja-JP")
    return webdriver.Chrome(options=opts)


# ===== カレンダールート待機 =====
def wait_calendar_root(driver):
    WebDriverWait(driver, 30).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
    time.sleep(1.2)
    selectors = [
        ".calendar-area", "#calendar",
        "[class*='calendar']", "[class*='reserve']"
    ]
    for sel in selectors:
        elems = driver.find_elements(By.CSS_SELECTOR, sel)
        if elems:
            print(f"[Root] Found calendar root by '{sel}' ({len(elems)} nodes)")
            return elems[0]
    print("[Root] Calendar root not found. Fallback to <body>.")
    return driver.find_element(By.TAG_NAME, "body")


# ===== カテゴリ選択（「キャンプ宿泊」をクリック） =====
def select_category(driver):
    """
    ページ上のボタン/タブのうち、可視テキストに TARGET_CATEGORY が含まれるものをクリック。
    見つからない場合は aria-label / title / data-* も試す。
    """
    # 候補セレクタ群：button, a, div など
    candidates = []
    for by, sel in [
        (By.XPATH, f"//button[contains(normalize-space(.), '{TARGET_CATEGORY}')]"),
        (By.XPATH, f"//a[contains(normalize-space(.), '{TARGET_CATEGORY}')]"),
        (By.XPATH, f"//*[self::div or self::span][contains(normalize-space(.), '{TARGET_CATEGORY}')]"),
    ]:
        found = driver.find_elements(by, sel)
        if found:
            candidates = found
            break

    # aria-label / title での一致もトライ
    if not candidates:
        for by, sel in [
            (By.XPATH, f"//*[@aria-label='{TARGET_CATEGORY}']"),
            (By.XPATH, f"//*[@title='{TARGET_CATEGORY}']"),
        ]:
            found = driver.find_elements(by, sel)
            if found:
                candidates = found
                break

    if candidates:
        btn = candidates[0]
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
        time.sleep(0.3)
        print(f"[Category] Click: tag={btn.tag_name} text={(btn.text or '').strip()}")
        btn.click()
        # カテゴリ切り替え後の再描画を待つ（カレンダー本体が変わる想定）
        time.sleep(1.5)
        return True
    else:
        print(f"[Category] '{TARGET_CATEGORY}' not found. Continue without clicking.")
        return False


# ===== 日付セル候補の抽出 =====
def get_candidate_day_cells(root, driver):
    cells = []
    if TARGET_DATE_ISO:
        cells = root.find_elements(By.CSS_SELECTOR, f'[data-date="{TARGET_DATE_ISO}"]')
        if not cells:
            cells = root.find_elements(By.XPATH, f'.//*[@data-date="{TARGET_DATE_ISO}"]')
    if not cells:
        for xp in [
            f".//table//td[contains(normalize-space(.), '{TARGET_DATE_LABEL}')]",
            f".//*[self::div or self::span][contains(normalize-space(.), '{TARGET_DATE_LABEL}')]",
            f".//*[self::button or self::li][contains(normalize-space(.), '{TARGET_DATE_LABEL}')]",
        ]:
            found = root.find_elements(By.XPATH, xp)
            if found:
                cells = found
                break
    print(f"[Candidates] {len(cells)} nodes under calendar root for '{TARGET_DATE_LABEL or TARGET_DATE_ISO}'.")
    return cells


def normalize_to_day_cell(node, root):
    target = node
    for _ in range(6):
        clazz = (target.get_attribute("class") or "").lower()
        if any(k in clazz for k in ["day", "date", "cell", "item", "slot", "card", "reserve"]):
            return target
        if target.tag_name.lower() in ["td", "li", "div", "button"]:
            return target
        ancestors = target.find_elements(By.XPATH, "..")
        if ancestors:
            target = ancestors[0]
        else:
            break
    return node


def evaluate_cell_status(cell, idx: int, driver) -> str:
    os.makedirs(DUMP_DIR, exist_ok=True)
    os.makedirs(SHOT_DIR, exist_ok=True)

    inner = cell.get_attribute("innerHTML") or ""
    with open(os.path.join(DUMP_DIR, f"cell_{idx}.html"), "w", encoding="utf-8") as f:
        f.write(inner)
    print("[Debug] Cell innerHTML:", (inner[:2000] + ("... (trim)" if len(inner) > 2000 else "")))

    try:
        cell.screenshot(os.path.join(SHOT_DIR, f"cell_{idx}.png"))
        print(f"[Shot] Saved shots/cell_{idx}.png")
    except Exception as e:
        print(f"[Shot] Failed: {e}")

    cell_text = (cell.text or "").strip()
    for m in ("△", "○", "×"):
        if m in cell_text:
            return m

    child_elems = cell.find_elements(
        By.XPATH,
        ".//img | .//span | .//i | .//*[contains(@class,'status') or contains(@class,'icon') or contains(@class,'reserve') or contains(@class,'availability') or contains(@class,'full') or contains(@class,'few') or contains(@class,'available') or contains(@class,'soldout') or contains(@class,'close') or contains(@class,'open')]"
    )
    for el in child_elems:
        t      = (el.text or "").strip()
        alt    = (el.get_attribute("alt") or "").strip()
        title  = (el.get_attribute("title") or "").strip()
        clazz  = (el.get_attribute("class") or "").strip().lower()
        aria   = (el.get_attribute("aria-label") or "").strip().lower()
        joined = " ".join([t, alt, title, clazz, aria]).lower()
        print(f"[Inspect] child: text={t} alt={alt} title={title} class={clazz} aria={aria}")

        if any(m in t for m in ("△", "○", "×")):
            for m in ("△", "○", "×"):
                if m in t:
                    return m
        if ("満席" in joined) or ("満室" in joined) or ("受付終了" in joined) or ("予約不可" in joined) or ("soldout" in joined) or ("full" in joined) or ("close" in joined):
            return "×"
        if ("残りわずか" in joined) or ("残少" in joined) or ("few" in joined) or ("limited" in joined):
            return "△"
        if ("空きあり" in joined) or ("空き" in joined) or ("available" in joined) or ("open" in joined) or ("受付中" in joined):
            return "○"

    return "UNKNOWN"


def detect_status_with_selenium() -> str:
    driver = setup_driver()
    try:
        print(f"[Selenium] GET {CALENDAR_URL}")
        driver.get(CALENDAR_URL)

        # カテゴリを「キャンプ宿泊」に絞る
        selected = select_category(driver)
        root = wait_calendar_root(driver)  # カテゴリ切替後の本体を待機

        body_text = driver.find_element(By.TAG_NAME, "body").text
        print("[Detect] Body text sample:", body_text[:400].replace("\n", " | "))
        if selected:
            print(f"[Detect] Category '{TARGET_CATEGORY}' likely applied (post-click).")

        cells_raw = get_candidate_day_cells(root, driver)
        if not cells_raw:
            print("[Detect] No candidate cells found under calendar root.")
            return "UNKNOWN"

        for i, node in enumerate(cells_raw[:12]):
            day_cell = normalize_to_day_cell(node, root)
            print(f"[Candidate] {i}: tag={day_cell.tag_name} class={(day_cell.get_attribute('class') or '')}")
            status = evaluate_cell_status(day_cell, i, driver)
            if status in ("△", "○", "×"):
                return status

        return "UNKNOWN"
    finally:
        driver.quit()


# ===== キャッシュ =====
def read_last() -> str:
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""

def write_last(s: str) -> None:
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        f.write(s)


# ===== メイン =====
def main():
    if not CHANNEL_TOKEN:
        print("ERROR: LINE_CHANNEL_TOKEN is not set."); sys.exit(2)

    os.makedirs(DUMP_DIR, exist_ok=True)
    os.makedirs(SHOT_DIR, exist_ok=True)

    last = read_last()
    status = detect_status_with_selenium()
    print(f"[Result] ({TARGET_CATEGORY}) {TARGET_DATE_LABEL} status: {status}")

    should_notify = False
    if status == "△":
        should_notify = (last != "△") if NOTIFY_DIFF_ONLY else True

    if should_notify:
        if SEND_MODE == "broadcast":
            notify_broadcast(LINE_MESSAGE)
        elif SEND_MODE == "multicast":
            ids = [s for s in USER_IDS_CSV.split(",") if s.strip()]
            if not ids:
                print("ERROR: LINE_USER_IDS is empty for multicast."); sys.exit(3)
            notify_multicast(ids, LINE_MESSAGE)
        else:
            target = TO_GROUP_ID or TO_USER_ID
            if not target:
                print("ERROR: push mode requires LINE_TO_GROUP_ID or LINE_TO_USER_ID."); sys.exit(3)
            notify_push(target, LINE_MESSAGE)

    write_last(status)
    sys.exit(0)

if __name__ == "__main__":
    main()
