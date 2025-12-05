
# -*- coding: utf-8 -*-
"""
ふもとっぱら予約カレンダー監視 + LINE Messaging API通知（Selenium最適化版）
- HTML構造（ヘッダー行+カテゴリ行）に合わせて「キャンプ宿泊」行の対象日セルだけを判定
- ヘッダー(<tr>/<th>)から列インデックスを取得 → 「キャンプ宿泊」行の同じ列の<td>を読む
- セルのテキスト（○／△／×／残○）でステータスを判定
- セルの innerHTML と スクリーンショットを保存（Artifactsで確認）
- ×/○→△へ変化した時のみ通知するオプションあり
"""

import os
import sys
import json
import time
import requests

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


# ========= ユーティリティ =========
def env(name: str, default: str | None = None):
    v = os.environ.get(name)
    if v is None or (isinstance(v, str) and v.strip() == ""):
        return default
    return v


# ========= 監視設定 =========
CALENDAR_URL       = env("FUMO_CALENDAR_URL", "https://reserve.fumotoppara.net/reserved/reserved-calendar-list")
TARGET_CATEGORY    = env("TARGET_CATEGORY_LABEL", "キャンプ宿泊")  # この行だけを見る
TARGET_DATE_LABEL  = env("TARGET_DATE_LABEL", "12/31")            # 見出しの表記に合わせる（例：12/31）
NOTIFY_DIFF_ONLY   = env("NOTIFY_DIFF_ONLY", "0") == "1"          # "1"なら ×/○→△へ変化時のみ通知

# ========= LINE設定 =========
CHANNEL_TOKEN      = env("LINE_CHANNEL_TOKEN")
SEND_MODE          = env("LINE_SEND_MODE", "push")                # push|broadcast|multicast
TO_USER_ID         = env("LINE_TO_USER_ID", None)
TO_GROUP_ID        = env("LINE_TO_GROUP_ID", None)
USER_IDS_CSV       = env("LINE_USER_IDS", "")
LINE_MESSAGE       = env("LINE_MESSAGE", f"🚨 ふもとっぱら（{TARGET_CATEGORY}）{TARGET_DATE_LABEL} に空き（△）が出ました！\n{CALENDAR_URL}")

HEADERS = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {CHANNEL_TOKEN}" if CHANNEL_TOKEN else "",
}

# ========= 保存（Artifacts用） =========
DUMP_DIR  = "html_dump"
SHOT_DIR  = "shots"
CACHE_FILE = "last_status.txt"


# ========= LINE送信 =========
def notify_push(target_id: str, text: str):
    r = requests.post("https://api.line.me/v2/bot/message/push",
                      headers=HEADERS,
                      json={"to": target_id, "messages": [{"type": "text", "text": text}]},
                      timeout=20)
    r.raise_for_status()
    print(f"[LINE] Push sent to {target_id}: {r.status_code}")

def notify_broadcast(text: str):
    r = requests.post("https://api.line.me/v2/bot/message/broadcast",
                      headers=HEADERS,
                      json={"messages": [{"type": "text", "text": text}]},
                      timeout=20)
    r.raise_for_status()
    print(f"[LINE] Broadcast sent: {r.status_code}")

def notify_multicast(user_ids, text: str):
    r = requests.post("https://api.line.me/v2/bot/message/multicast",
                      headers=HEADERS,
                      json={"to": user_ids, "messages": [{"type": "text", "text": text}]},
                      timeout=20)
    r.raise_for_status()
    print(f"[LINE] Multicast sent({len(user_ids)}): {r.status_code}")


# ========= ブラウザ起動 =========
def setup_driver() -> webdriver.Chrome:
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1280,2200")
    opts.add_argument("--lang=ja-JP")
    return webdriver.Chrome(options=opts)


# ========= 判定ロジック（テーブル構造前提） =========
def detect_status_with_selenium() -> str:
    """
    1) <table> のヘッダー行(<tr>/<th>)を読み、TARGET_DATE_LABEL の列インデックスを特定
       - 先頭の空<th>を含めたインデックス（0-based）。データ列は th[1]→ td[0] に対応
    2) 「キャンプ宿泊」行(<tr><th>キャンプ宿泊</th>...)を特定
    3) 同インデックスの <td> を取得 → テキストで ○／△／×／残○ を判定
    """
    os.makedirs(DUMP_DIR, exist_ok=True)
    os.makedirs(SHOT_DIR, exist_ok=True)

    drv = setup_driver()
    try:
        print(f"[Selenium] GET {CALENDAR_URL}")
        drv.get(CALENDAR_URL)

        # <table> が描画されるまで待機
        WebDriverWait(drv, 30).until(EC.presence_of_element_located((By.TAG_NAME, "table")))
        time.sleep(1.0)

        table = drv.find_element(By.TAG_NAME, "table")

        # 1) ヘッダー行（1行目）の <th> をすべて取得
        header_ths = table.find_elements(By.XPATH, "./tr[1]/th")
        header_texts = [th.text.strip().replace("\n", " ") for th in header_ths]
        print("[Header] texts:", header_texts[:12], "...")

        # 日付ラベルに一致する列インデックスを探す（「12/31 水」のように曜日を含むので部分一致）
        date_idx = -1
        for i, txt in enumerate(header_texts):
            if TARGET_DATE_LABEL in txt:
                date_idx = i
                break

        if date_idx < 0:
            print(f"[Error] TARGET_DATE_LABEL '{TARGET_DATE_LABEL}' not found in header.")
            return "UNKNOWN"

        # データ列のインデックス：ヘッダーの先頭は空<th>なので、tdの添字は (date_idx - 1)
        td_idx = date_idx - 1
        if td_idx < 0:
            print("[Error] Calculated td index is negative. Header may not match expected layout.")
            return "UNKNOWN"

        # 2) 「キャンプ宿泊」行を特定（左端<th>がカテゴリ名）
        camp_row = table.find_element(By.XPATH, ".//tr[th[contains(normalize-space(.), 'キャンプ宿泊')]]")
