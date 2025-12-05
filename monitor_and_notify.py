
# -*- coding: utf-8 -*-
"""
ふもとっぱら予約カレンダー監視 + LINE Messaging API通知（Selenium版）
----------------------------------------------------------------------
・JavaScript描画後のDOMを Selenium で取得し、日付セルの「△/○/×」や
  画像アイコンの alt/title/class を見て正しく判定します。
・対象セルの innerHTML をログ出力し、html_dump/ にファイル保存します
  （Artifactsでダウンロードして構造確認できます）。
・重複通知防止（×/○ → △ に変化したときだけ通知）のオプション付き。

必要なSecrets / Variables の例：
- LINE_CHANNEL_TOKEN（必須）
- LINE_TO_USER_ID または LINE_TO_GROUP_ID（push宛先のいずれか）
- FUMO_CALENDAR_URL（未設定なら既定URLを使用）
- TARGET_DATE_LABEL（例：12/31、12月31日）
- TARGET_DATE_ISO（例：2025-12-31。data-date 属性がある場合は推奨）
- NOTIFY_DIFF_ONLY（"1"で×/○→△変化時だけ通知）
- LINE_SEND_MODE（push|broadcast|multicast。既定は push）
- LINE_USER_IDS（multicast用カンマ区切り）
- LINE_MESSAGE（任意の通知文。未設定なら既定文）
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


# ========= ユーティリティ =========
def env(name: str, default: str | None = None):
    """空文字は未設定扱いにして default を返す"""
    val = os.environ.get(name)
    if val is None or (isinstance(val, str) and val.strip() == ""):
        return default
    return val


# ========= 監視対象設定 =========
CALENDAR_URL      = env("FUMO_CALENDAR_URL", "https://reserve.fumotoppara.net/reserved/reserved-calendar-list")
TARGET_DATE_LABEL = env("TARGET_DATE_LABEL", "12/31")      # 画面表記に合わせる（例：12/31／12月31日）
TARGET_DATE_ISO   = env("TARGET_DATE_ISO", None)           # 例：2025-12-31（data-date があるDOMなら推奨）
NOTIFY_DIFF_ONLY  = env("NOTIFY_DIFF_ONLY", "0") == "1"    # "1" なら ×/○→△ の変化時だけ通知

# ========= LINE設定 =========
CHANNEL_TOKEN     = env("LINE_CHANNEL_TOKEN")
SEND_MODE         = env("LINE_SEND_MODE", "push")          # push|broadcast|multicast
TO_USER_ID        = env("LINE_TO_USER_ID", None)
TO_GROUP_ID       = env("LINE_TO_GROUP_ID", None)
USER_IDS_CSV      = env("LINE_USER_IDS", "")
LINE_MESSAGE      = env("LINE_MESSAGE", f"🚨 ふもとっぱら {TARGET_DATE_LABEL} に空き（△）が出ました！\n{CALENDAR_URL}")

HEADERS = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {CHANNEL_TOKEN}" if CHANNEL_TOKEN else "",
}

# ログ／Artifacts用のダンプ先
DUMP_DIR = "html_dump"
CACHE_FILE = "last_status.txt"


# ========= LINE送信 =========
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


# ========= ブラウザ起動 =========
def setup_driver() -> webdriver.Chrome:
    """ubuntu-latest + Google Chrome（headless）で動作。selenium-manager利用。"""
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1280,2200")
    opts.add_argument("--lang=ja-JP")

    # そのまま起動（chromedriverはselenium-managerが解決することが多い）
    driver = webdriver.Chrome(options=opts)
    return driver


# ========= ステータス判定 =========
def detect_status_with_selenium() -> str:
    """
    1) data-date="YYYY-MM-DD" があれば最優先でそのセルを拾う
    2) なければラベル（12/31 等）を含む td/div/span を候補にして、
       同一セル内のテキスト／img alt／title／class を総当たりで評価
    3) innerHTML をログ／ファイル保存（Artifacts用）
    """
    os.makedirs(DUMP_DIR, exist_ok=True)
    driver = setup_driver()
    try:
        print(f"[Selenium] GET {CALENDAR_URL}")
        driver.get(CALENDAR_URL)

        # 本文が描画されるまで待機（必要に応じて対象コンテナに変更）
        WebDriverWait(driver, 25).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        time.sleep(2)  # JS描画余裕

        # --- 1) ISO属性優先 ---
        cells = []
        if TARGET_DATE_ISO:
            # CSS と XPath の両方で拾う（実装差異対策）
            css_elems = driver.find_elements(By.CSS_SELECTOR, f'[data-date="{TARGET_DATE_ISO}"]')
            xpath_elems = driver.find_elements(By.XPATH, f'//*[@data-date="{TARGET_DATE_ISO}"]')
            cells = css_elems if css_elems else xpath_elems

        # --- 2) ラベルで候補抽出（td優先 → なければdiv/span） ---
        if not cells:
            cells = driver.find_elements(By.XPATH, f"//table//td[contains(normalize-space(.), '{TARGET_DATE_LABEL}')]")
            if not cells:
                cells = driver.find_elements(By.XPATH, f"//*[self::div or self::span][contains(normalize-space(.), '{TARGET_DATE_LABEL}')]")

        print(f"[Detect] Candidate cells: {len(cells)}")

        # 候補セルを順に精査（最初の一致で返す）
        for idx, cell in enumerate(cells[:12]):
            cell_text = (cell.text or "").strip()
            inner = cell.get_attribute("innerHTML") or ""
            # ログ＆ファイル保存（Artifacts）
            print("[Debug] Cell text:", cell_text)
            print("[Debug] Cell innerHTML:", (inner[:2000] + ("... (trim)" if len(inner) > 2000 else "")))
            with open(os.path.join(DUMP_DIR, f"cell_{idx}.html"), "w", encoding="utf-8") as f:
                f.write(inner)

            # 直テキストに記号があるなら即返す
            for m in ("△", "○", "×"):
                if m in cell_text:
                    return m

            # 子要素（img/span/i）を総当たりで評価
            child_elems = cell.find_elements(
                By.XPATH,
                ".//img | .//span | .//i | .//*[contains(@class,'status') or contains(@class,'icon') or contains(@class,'reserve') or contains(@class,'availability') or contains(@class,'full') or contains(@class,'few') or contains(@class,'available')]"
            )
            for el in child_elems:
                t      = (el.text or "").strip()
                alt    = (el.get_attribute("alt") or "").strip()
                title  = (el.get_attribute("title") or "").strip()
                clazz  = (el.get_attribute("class") or "").strip()
                aria   = (el.get_attribute("aria-label") or "").strip()

                joined = " ".join([t, alt, title, clazz, aria]).lower()
                print(f"[Inspect] child: text={t} alt={alt} title={title} class={clazz} aria={aria}")

                # 記号優先
                if any(m in t for m in ("△", "○", "×")):
                    for m in ("△", "○", "×"):
                        if m in t:
                            return m

                # 文言・クラス名で判定（必要に応じて語彙を追加）
                # ×（満席・受付終了など）
                if ("満席" in joined) or ("満室" in joined) or ("受付終了" in joined) or ("予約不可" in joined) or ("soldout" in joined) or ("full" in joined):
                    return "×"
                # △（残りわずか・limited）
                if ("残りわずか" in joined) or ("残少" in joined) or ("few" in joined) or ("limited" in joined):
                    return "△"
                # ○（空きあり・available）
                if ("空きあり" in joined) or ("空き" in joined) or ("available" in joined) or ("open" in joined) or ("受付中" in joined):
                    return "○"

        # 候補があっても判定できない
        return "UNKNOWN"

    finally:
        driver.quit()


# ========= 重複通知キャッシュ =========
def read_last() -> str:
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def write_last(s: str) -> None:
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        f.write(s)


# ========= メイン =========
def main():
    if not CHANNEL_TOKEN:
        print("ERROR: LINE_CHANNEL_TOKEN is not set.")
        sys.exit(2)

    os.makedirs(DUMP_DIR, exist_ok=True)

    last = read_last()
    status = detect_status_with_selenium()
    print(f"[Result] {TARGET_DATE_LABEL} status: {status}")

    # 通知判定
    should_notify = False
    if status == "△":
        if NOTIFY_DIFF_ONLY:
            should_notify = (last != "△")
        else:
            should_notify = True

    # 送信
    if should_notify:
        if SEND_MODE == "broadcast":
            notify_broadcast(LINE_MESSAGE)  # 友だち全員へ
        elif SEND_MODE == "multicast":
            ids = [s for s in USER_IDS_CSV.split(",") if s.strip()]
            if not ids:
                print("ERROR: LINE_USER_IDS is empty for multicast.")
                sys.exit(3)
            notify_multicast(ids, LINE_MESSAGE)
        else:
            target = TO_GROUP_ID or TO_USER_ID
            if not target:
                print("ERROR: push mode requires LINE_TO_GROUP_ID or LINE_TO_USER_ID.")
                sys.exit(3)
            notify_push(target, LINE_MESSAGE)

    # キャッシュ更新
    write_last(status)
    sys.exit(0)


if __name__ == "__main__":
    main()
