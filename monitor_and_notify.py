
# -*- coding: utf-8 -*-
"""
ふもとっぱら予約カレンダー監視 + LINE Messaging API（Flex）通知
— A案固定：×→○／×→△ に変化した時だけ通知（単日）
"""

import os
import sys
import time
import re
import requests

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


# --------- 環境値ユーティリティ ---------
def env(name: str, default: str | None = None):
    v = os.environ.get(name)
    if v is None or (isinstance(v, str) and v.strip() == ""):
        return default
    return v


# --------- 監視設定 ---------
CALENDAR_URL       = env("FUMO_CALENDAR_URL", "https://reserve.fumotoppara.net/reserved/reserved-calendar-list")
TARGET_CATEGORY    = env("TARGET_CATEGORY_LABEL", "キャンプ宿泊")  # この行だけを見る
TARGET_DATE_LABEL  = env("TARGET_DATE_LABEL", "12/31")            # ヘッダー表記に部分一致（例：12/31）

# --------- LINE設定 ---------
CHANNEL_TOKEN      = env("LINE_CHANNEL_TOKEN")
SEND_MODE          = env("LINE_SEND_MODE", "push")                # push|broadcast|multicast
TO_USER_ID         = env("LINE_TO_USER_ID", None)
TO_GROUP_ID        = env("LINE_TO_GROUP_ID", None)
USER_IDS_CSV       = env("LINE_USER_IDS", "")
LINE_MESSAGE       = env("LINE_MESSAGE", f"ふもとっぱら（{TARGET_CATEGORY}）{TARGET_DATE_LABEL} に変化あり！\n{CALENDAR_URL}")

HEADERS = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {CHANNEL_TOKEN}" if CHANNEL_TOKEN else "",
}

# --------- 保存（Artifacts用） ---------
DUMP_DIR   = "html_dump"
SHOT_DIR   = "shots"
CACHE_FILE = "last_status.txt"


# --------- Flex Message（Bubble：構文簡潔版） ---------
def make_flex_bubble(category: str, date_label: str, status: str, reserve_url: str, prev_status: str) -> dict:
    """Flex MessageのBubble JSON。status: '○' | '△' | '×' | 'UNKNOWN'"""
    color_map = {"○": "#22c55e", "△": "#f59e0b", "×": "#ef4444", "UNKNOWN": "#6b7280"}
    label_map = {"○": "空きあり", "△": "残りわずか", "×": "満席", "UNKNOWN": "不明"}
    color = color_map.get(status, "#6b7280")
    label = label_map.get(status, "不明")
    prev_label = label_map.get(prev_status, prev_status or "不明")

    return {
        "type": "bubble",
        "size": "mega",
        "header": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {"type": "text", "text": "ふもとっぱら 予約監視", "weight": "bold", "size": "md"},
                {"type": "text", "text": category, "size": "sm", "color": "#6b7280"}
            ],
            "backgroundColor": "#f8fafc",
            "paddingAll": "12px"
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "md",
            "contents": [
                {"type": "box", "layout": "horizontal", "contents": [
                    {"type": "text", "text": "対象日", "size": "sm", "color": "#6b7280"},
                    {"type": "text", "text": date_label, "size": "sm", "align": "end", "weight": "bold"}
                ]},
                {"type": "box", "layout": "horizontal", "contents": [
                    {"type": "text", "text": "今回", "size": "sm", "color": "#6b7280"},
                    {"type": "text", "text": f"{status} {label}", "size": "sm", "weight": "bold", "color": color, "align": "end"}
                ]},
                {"type": "box", "layout": "horizontal", "contents": [
                    {"type": "text", "text": "前回", "size": "sm", "color": "#6b7280"},
                    {"type": "text", "text": f"{prev_status or '-'} {prev_label}", "size": "sm", "color": "#6b7280", "align": "end"}
                ]}
            ]
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {"type": "button", "style": "primary", "color": color,
                 "action": {"type": "uri", "label": "予約ページを開く", "uri": reserve_url}}
            ]
        },
        "styles": {"footer": {"separator": True}}
    }


# --------- Flex送信 ---------
def push_flex(target_id: str, bubble: dict, alt_text: str):
    url = "https://api.line.me/v2/bot/message/push"
    payload = {"to": target_id, "messages": [{"type": "flex", "altText": alt_text, "contents": bubble}]}
    r = requests.post(url, headers=HEADERS, json=payload, timeout=20)
    r.raise_for_status()
    print(f"[LINE] Flex Push sent to {target_id}: {r.status_code}")

def broadcast_flex(bubble: dict, alt_text: str):
    url = "https://api.line.me/v2/bot/message/broadcast"
    payload = {"messages": [{"type": "flex", "altText": alt_text, "contents": bubble}]}
    r = requests.post(url, headers=HEADERS, json=payload, timeout=20)
    r.raise_for_status()
    print(f"[LINE] Flex Broadcast sent: {r.status_code}")

def multicast_flex(user_ids: list[str], bubble: dict, alt_text: str):
    url = "https://api.line.me/v2/bot/message/multicast"
    payload = {"to": user_ids, "messages": [{"type": "flex", "altText": alt_text, "contents": bubble}]}
    r = requests.post(url, headers=HEADERS, json=payload, timeout=20)
    r.raise_for_status()
    print(f"[LINE] Flex Multicast sent({len(user_ids)}): {r.status_code}")


# --------- Seleniumセットアップ ---------
def setup_driver() -> webdriver.Chrome:
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1440,2400")
    opts.add_argument("--lang=ja-JP")
    # UAと自動化検出の緩和（描画安定化）
    opts.add_argument("--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    return webdriver.Chrome(options=opts)


# --------- 保存ユーティリティ ---------
def save_text(text: str, path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


# --------- ヘッダー抽出（text/innerText/textContentフォールバック） ---------
def header_texts_from_table(table) -> list[str]:
    ths = table.find_elements(By.XPATH, ".//thead/tr/th | ./tr[1]/th")
    texts = []
    for th in ths:
        t = th.text.strip().replace("\n", " ")
        if not t:
            t = (th.get_attribute("innerText") or "").strip().replace("\n", " ")
        if not t:
            t = (th.get_attribute("textContent") or "").strip().replace("\n", " ")
        texts.append(t)
    return texts


# --------- カレンダーらしい<table>を選ぶ ---------
def choose_calendar_table(drv):
    tables = drv.find_elements(By.TAG_NAME, "table")
    print(f"[Tables] found: {len(tables)}")
    chosen = None
    best_score = -1
    for idx, t in enumerate(tables):
        outer = t.get_attribute("outerHTML") or ""
        save_text(outer, os.path.join(DUMP_DIR, f"table_{idx}.html"))
        texts = header_texts_from_table(t)
        print(f"[Table {idx}] header sample:", texts[:10])
        # 「月日らしさ」のスコア（例: 12/31, 1/1 を含む個数）
        score = sum(1 for x in texts if re.search(r"\d{1,2}/\d{1,2}", x))
        if score > best_score:
            best_score = score
            chosen = t
    return chosen


# --------- ステータス検出（単日） ---------
def detect_status_with_selenium() -> str:
    """
    1) ページの<table>を選択
    2) ヘッダー(<th>)から TARGET_DATE_LABEL の列インデックスを特定（部分一致）
    3) 「キャンプ宿泊」行の同列<td>を取得 → ○/△/×/ー を判定
    """
    os.makedirs(DUMP_DIR, exist_ok=True)
    os.makedirs(SHOT_DIR, exist_ok=True)

    # URLが空なら既定へ
    url = CALENDAR_URL or "https://reserve.fumotoppara.net/reserved/reserved-calendar-list"

    drv = setup_driver()
    try:
        print(f"[Selenium] GET {url}")
        drv.get(url)
        WebDriverWait(drv, 30).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        time.sleep(2.0)

        # ページHTML保存（調査用）
        save_text(drv.page_source or "", os.path.join(DUMP_DIR, "page_source.html"))

        # テーブル待機（最大5回リトライ）
        for _ in range(5):
            if drv.find_elements(By.TAG_NAME, "table"):
                break
            time.sleep(1.0)

        table = choose_calendar_table(drv)
        if not table:
            print("[Error] No calendar-like table chosen.")
            return "UNKNOWN"
        time.sleep(1.0)  # 遅延描画余裕

        header_texts = header_texts_from_table(table)
        print("[Header] count:", len(header_texts))
        print("[Header] first 12:", header_texts[:12])

        # ヘッダーで日付列インデックス（部分一致）
        date_idx = -1
        for i, txt in enumerate(header_texts):
            if txt and (TARGET_DATE_LABEL in txt):
                date_idx = i
                break
        if date_idx < 0:
            print(f"[Error] TARGET_DATE_LABEL '{TARGET_DATE_LABEL}' not found in header.")
            save_text(table.get_attribute("outerHTML") or "", os.path.join(DUMP_DIR, "chosen_table.html"))
            return "UNKNOWN"

        # 左端<th>がカテゴリ見出しなので、tdの添字 = (date_idx - 1)
        td_idx = date_idx - 1
        if td_idx < 0:
            print("[Error] td_idx negative. Header layout may differ.")
            save_text(table.get_attribute("outerHTML") or "", os.path.join(DUMP_DIR, "chosen_table.html"))
            return "UNKNOWN"

        # 「キャンプ宿泊」行（左端<th>がカテゴリ名）
        camp_row = table.find_element(
            By.XPATH,
            ".//tr[th[contains(normalize-space(.), 'キャンプ宿泊')] or normalize-space(th[1])='キャンプ宿泊']"
        )
        tds = camp_row.find_elements(By.XPATH, "./td")
        print("[Row] td count:", len(tds))
        if td_idx >= len(tds):
            print(f"[Error] td_idx({td_idx}) >= len(tds)({len(tds)})")
            save_text(camp_row.get_attribute("outerHTML") or "", os.path.join(DUMP_DIR, "camp_row.html"))
            return "UNKNOWN"

        cell = tds[td_idx]
        # セルテキスト（text/innerText/textContent フォールバック）
        txt = (cell.text or "").strip().replace("\n", " ")
        if not txt:
            txt = (cell.get_attribute("innerText") or "").strip().replace("\n", " ")
        if not txt:
            txt = (cell.get_attribute("textContent") or "").strip().replace("\n", " ")
        print(f"[Cell] ({TARGET_CATEGORY}/{TARGET_DATE_LABEL}) text:", txt)

        # 保存（Artifacts）
        save_text(cell.get_attribute("innerHTML") or "", os.path.join(DUMP_DIR, "camp_target_cell.html"))
        try:
            cell.screenshot(os.path.join(SHOT_DIR, "camp_target_cell.png"))
        except Exception as se:
            print(f"[Shot] Failed: {se}")

        # 判定：○／△（または「残」）／×／その他
        if ("〇" in txt) or ("○" in txt):
            return "○"
        if ("△" in txt) or ("残" in txt):
            return "△"
        if ("×" in txt):
            return "×"
        return "UNKNOWN"

    except Exception as e:
        print(f"[Exception] detect_status_with_selenium: {e}")
        return "UNKNOWN"
    finally:
        drv.quit()


# --------- キャッシュ（前回ステータス） ---------
def read_last() -> str:
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""

def write_last(s: str) -> None:
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        f.write(s)


# --------- 比較ユーティリティ（A案固定） ---------
def norm(s: str) -> str:
    """比較用にステータスを正規化（×→'x'／UNKNOWN→'unknown'／他は記号のまま）"""
    s = (s or "").strip()
    if s.lower() in ("x", "unknown"):
        return s.lower()
    if s == "×":
        return "x"
    if s == "UNKNOWN":
        return "unknown"
    return s  # ○／△はそのまま

def is_notifiable(prev: str, curr: str) -> bool:
    """A案固定： (×→○) または (×→△) を通知"""
    return (prev, curr) in {("x", "○"), ("x", "△")}


# --------- メイン ---------
def main():
    if not CHANNEL_TOKEN:
        print("ERROR: LINE_CHANNEL_TOKEN is not set.")
        sys.exit(2)

    last = read_last()            # 例: "×" / "△" / "○" / "UNKNOWN" / ""
    status = detect_status_with_selenium()
    print(f"[Result] ({TARGET_CATEGORY}) {TARGET_DATE_LABEL} status: {status}")

    prev = norm(last)
    curr = norm(status)

    # --- A案（×→○／×→△ のときだけ通知）---
    should_notify = is_notifiable(prev, curr)

    # --- 通知（Flex）---
    if should_notify:
        bubble = make_flex_bubble(
            category=TARGET_CATEGORY,
            date_label=TARGET_DATE_LABEL,
            status=status,
            reserve_url=CALENDAR_URL,
            prev_status=last or "UNKNOWN"
        )
        # 遷移別 altText（通知バナー）
        if prev == "x" and curr == "○":
            alt = f"ふもとっぱら（{TARGET_CATEGORY}）{TARGET_DATE_LABEL} が『満席→空きあり』に変わりました"
        elif prev == "x" and curr == "△":
            alt = f"ふもとっぱら（{TARGET_CATEGORY}）{TARGET_DATE_LABEL} が『満席→残りわずか』に変わりました"
        else:
            alt = f"ふもとっぱら（{TARGET_CATEGORY}）{TARGET_DATE_LABEL} が『{last}→{status}』に変化しました"

        if SEND_MODE == "broadcast":
            broadcast_flex(bubble, alt)
        elif SEND_MODE == "multicast":
            ids = [s for s in USER_IDS_CSV.split(",") if s.strip()]
            if not ids:
                print("ERROR: LINE_USER_IDS is empty for multicast."); sys.exit(3)
            multicast_flex(ids, bubble, alt)
        else:
            target = TO_GROUP_ID or TO_USER_ID
            if not target:
                print("ERROR: push mode requires LINE_TO_GROUP_ID or LINE_TO_USER_ID."); sys.exit(3)
            push_flex(target, bubble, alt)

    # --- キャッシュ更新 ---
    write_last(status)
    sys.exit(0)


if __name__ == "__main__":
    main()
