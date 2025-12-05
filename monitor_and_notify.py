
# -*- coding: utf-8 -*-
"""
ふもとっぱら予約カレンダー監視 + LINE Messaging API通知（Selenium最適化版）
- <table> のヘッダー(<tr>/<th>)から対象日列インデックスを取得
- 「キャンプ宿泊」行(<tr><th>キャンプ宿泊</th>...)の同じ列<td>を直接読む
- セルのテキスト（○／△／×／残n／ー）で判定
- 対象セルの innerHTML と スクリーンショットを保存（Artifacts用）
- ×/○→△へ変化した時のみ通知するオプションあり
"""

import os
import sys
import time
import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


# --------- ユーティリティ ---------
def env(name: str, default: str | None = None):
    val = os.environ.get(name)
    if val is None or (isinstance(val, str) and val.strip() == ""):
        return default
    return val


# --------- 監視設定 ---------
CALENDAR_URL       = env("FUMO_CALENDAR_URL", "https://reserve.fumotoppara.net/reserved/reserved-calendar-list")
TARGET_CATEGORY    = env("TARGET_CATEGORY_LABEL", "キャンプ宿泊")   # この行だけを見る
TARGET_DATE_LABEL  = env("TARGET_DATE_LABEL", "12/31")             # ヘッダー表記に合わせる
NOTIFY_DIFF_ONLY   = env("NOTIFY_DIFF_ONLY", "0") == "1"           # "1"なら ×/○→△への変化時のみ通知

# --------- LINE設定 ---------
CHANNEL_TOKEN      = env("LINE_CHANNEL_TOKEN")
SEND_MODE          = env("LINE_SEND_MODE", "push")                 # push|broadcast|multicast
TO_USER_ID         = env("LINE_TO_USER_ID", None)
TO_GROUP_ID        = env("LINE_TO_GROUP_ID", None)
USER_IDS_CSV       = env("LINE_USER_IDS", "")
LINE_MESSAGE       = env("LINE_MESSAGE", f"🚨 ふもとっぱら（{TARGET_CATEGORY}）{TARGET_DATE_LABEL} に空き（△）が出ました！\n{CALENDAR_URL}")

HEADERS = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {CHANNEL_TOKEN}" if CHANNEL_TOKEN else "",
}

# --------- 保存（Artifacts用） ---------
DUMP_DIR  = "html_dump"
SHOT_DIR  = "shots"
CACHE_FILE = "last_status.txt"


# --------- LINE送信 ---------
def notify_push(target_id: str, text: str):
    r = requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers=HEADERS,
        json={"to": target_id, "messages": [{"type": "text", "text": text}]},
        timeout=20,
    )
    r.raise_for_status()
    print(f"[LINE] Push sent to {target_id}: {r.status_code}")


def notify_broadcast(text: str):
    r = requests.post(
        "https://api.line.me/v2/bot/message/broadcast",
        headers=HEADERS,
        json={"messages": [{"type": "text", "text": text}]},
        timeout=20,
    )
    r.raise_for_status()
    print(f"[LINE] Broadcast sent: {r.status_code}")


def notify_multicast(user_ids, text: str):
    r = requests.post(
        "https://api.line.me/v2/bot/message/multicast",
        headers=HEADERS,
        json={"to": user_ids, "messages": [{"type": "text", "text": text}]},
        timeout=20,
    )
    r.raise_for_status()
    print(f"[LINE] Multicast sent({len(user_ids)}): {r.status_code}")


# --------- ブラウザ起動 ---------
def setup_driver() -> webdriver.Chrome:
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1280,2200")
    opts.add_argument("--lang=ja-JP")
    return webdriver.Chrome(options=opts)


# --------- 判定ロジック ---------
def detect_status_with_selenium() -> str:
    """
    1) <table> を待機
    2) ヘッダー<tr>[1]/<th> のテキストから TARGET_DATE_LABEL を含む列インデックスを取得
       - 先頭の空<th>があるため、ヘッダーの列とデータ<td>の列はオフセットずれに注意
    3) 「キャンプ宿泊」行を特定 → 同列<td>のテキストで判定
    """
    os.makedirs(DUMP_DIR, exist_ok=True)
    os.makedirs(SHOT_DIR, exist_ok=True)

    drv = setup_driver()
    try:
        print(f"[Selenium] GET {CALENDAR_URL}")
        drv.get(CALENDAR_URL)

        # <table> を待機
        WebDriverWait(drv, 30).until(EC.presence_of_element_located((By.TAG_NAME, "table")))
        time.sleep(1.0)

        table = drv.find_element(By.TAG_NAME, "table")

        # 1) ヘッダー<th>配列を取得
        header_ths = table.find_elements(By.XPATH, "./tr[1]/th")
        header_texts = [th.text.strip().replace("\n", " ") for th in header_ths]
        print("[Header] sample:", header_texts[:12])

        # 2) 対象日列インデックスを検索（部分一致）
        date_idx = -1
        for i, txt in enumerate(header_texts):
            if TARGET_DATE_LABEL in txt:
                date_idx = i
                break
        if date_idx < 0:
            print(f"[Error] TARGET_DATE_LABEL '{TARGET_DATE_LABEL}' not found in header.")
            return "UNKNOWN"

        # ヘッダーの先頭<th>は空欄なので、tdの添字は (date_idx - 1)
        td_idx = date_idx - 1
        if td_idx < 0:
            print("[Error] td index became negative. Header layout mismatch.")
            return "UNKNOWN"

        # 3) 「キャンプ宿泊」行を検索（左端<th>がカテゴリ名）
        #    normalize-space(.) で改行・空白を揃える
        camp_row = table.find_element(
            By.XPATH,
            ".//tr[normalize-space(th[1])='キャンプ宿泊' or th[contains(normalize-space(.), 'キャンプ宿泊')]]"
        )

        # 行内の td を列配列として取得
        tds = camp_row.find_elements(By.XPATH, "./td")
        if td_idx >= len(tds):
            print(f"[Error] td_idx({td_idx}) >= len(tds)({len(tds)})")
            return "UNKNOWN"

        cell = tds[td_idx]
        cell_text = cell.text.strip().replace("\n", " ")
        print(f"[Cell] ({TARGET_CATEGORY} / {TARGET_DATE_LABEL}) text:", cell_text)

        # Artifacts保存
        inner = cell.get_attribute("innerHTML") or ""
        with open(os.path.join(DUMP_DIR, "camp_target_cell.html"), "w", encoding="utf-8") as f:
            f.write(inner)
        try:
            cell.screenshot(os.path.join(SHOT_DIR, "camp_target_cell.png"))
        except Exception as se:
            print(f"[Shot] Failed: {se}")

        # 4) テキストでステータス判定（全角記号と「残n」対応）
        txt = cell_text  # 例: "△ 残1" / "〇" / "×" / "ー"
        circle_variants = ["〇", "○"]  # 環境差吸収

        if any(c in txt for c in circle_variants):
            return "○"
        if ("△" in txt) or ("残" in txt):
            return "△"
        if "×" in txt:
            return "×"
        return "UNKNOWN"

    except Exception as e:
        # 例外はログに流し、UNKNOWNで返す（ワークフロー継続のため）
        print(f"[Exception] detect_status_with_selenium: {e}")
        return "UNKNOWN"

    finally:
        drv.quit()


# --------- キャッシュ（重複通知抑止） ---------
def read_last() -> str:
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""

def write_last(s: str) -> None:
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        f.write(s)


# --------- メイン ---------
def main():
    if not CHANNEL_TOKEN:
        print("ERROR: LINE_CHANNEL_TOKEN is not set.")
        sys.exit(2)

    last = read_last()
    status = detect_status_with_selenium()
    print(f"[Result] ({TARGET_CATEGORY}) {TARGET_DATE_LABEL} status: {status}")

    # 通知判定
    should_notify = False
    if status == "△":
        should_notify = (last != "△") if NOTIFY_DIFF_ONLY else True

    # 送信
    if should_notify:
        if SEND_MODE == "broadcast":
            notify_broadcast(LINE_MESSAGE)
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

    write_last(status)
    sys.exit(0)


if __name__ == "__main__":
    main()
