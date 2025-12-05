
# -*- coding: utf-8 -*-
"""
ふもとっぱら予約カレンダー監視 + LINE Messaging API通知（Selenium最適化＋フォールバック強化版）
- <table> の候補から「ヘッダーに月日が並ぶもの」を選択
- ヘッダー<th>の取得は text / innerText / textContent の三段でフォールバック
- ページ全体HTML・選択テーブルHTML・対象セルHTML/スクショをArtifactsへ保存
- 「キャンプ宿泊」行の同列<td>を読んで ○／△（残n）／× を判定
- ×/○→△への変化時のみ通知するオプションあり
"""

import os, sys, time, re, requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# --------- 環境値 ---------
def env(name: str, default: str | None = None):
    v = os.environ.get(name)
    if v is None or (isinstance(v, str) and v.strip() == ""):
        return default
    return v

# 監視設定
CALENDAR_URL      = env("FUMO_CALENDAR_URL", "https://reserve.fumotoppara.net/reserved/reserved-calendar-list")
TARGET_CATEGORY   = env("TARGET_CATEGORY_LABEL", "キャンプ宿泊")
TARGET_DATE_LABEL = env("TARGET_DATE_LABEL", "12/31")  # 例: 12/31（曜日は含めなくてOK）
NOTIFY_DIFF_ONLY  = env("NOTIFY_DIFF_ONLY", "0") == "1"

# LINE
CHANNEL_TOKEN = env("LINE_CHANNEL_TOKEN")
SEND_MODE     = env("LINE_SEND_MODE", "push")
TO_USER_ID    = env("LINE_TO_USER_ID", None)
TO_GROUP_ID   = env("LINE_TO_GROUP_ID", None)
USER_IDS_CSV  = env("LINE_USER_IDS", "")
LINE_MESSAGE  = env("LINE_MESSAGE", f"🚨 ふもとっぱら（{TARGET_CATEGORY}）{TARGET_DATE_LABEL} に空き（△）が出ました！\n{CALENDAR_URL}")

HEADERS = {"Content-Type":"application/json","Authorization":f"Bearer {CHANNEL_TOKEN}" if CHANNEL_TOKEN else ""}

# Artifacts
DUMP_DIR  = "html_dump"
SHOT_DIR  = "shots"
CACHE_FILE = "last_status.txt"

# --------- LINE送信 ---------
def notify_push(to, text):
    r = requests.post("https://api.line.me/v2/bot/message/push", headers=HEADERS,
                      json={"to":to,"messages":[{"type":"text","text":text}]}, timeout=20)
    r.raise_for_status(); print(f"[LINE] Push sent to {to}: {r.status_code}")

def notify_broadcast(text):
    r = requests.post("https://api.line.me/v2/bot/message/broadcast", headers=HEADERS,
                      json={"messages":[{"type":"text","text":text}]}, timeout=20)
    r.raise_for_status(); print(f"[LINE] Broadcast sent: {r.status_code}")

def notify_multicast(ids, text):
    r = requests.post("https://api.line.me/v2/bot/message/multicast", headers=HEADERS,
                      json={"to":ids,"messages":[{"type":"text","text":text}]}, timeout=20)
    r.raise_for_status(); print(f"[LINE] Multicast sent({len(ids)}): {r.status_code}")

# --------- ユーティリティ ---------
def setup_driver():
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1440,2400")
    opts.add_argument("--lang=ja-JP")
    # ユーザーエージェントを付けて、ヘッドレス検出による描画問題の回避
    opts.add_argument("--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36")
    # 自動化検出の緩和
    opts.add_argument("--disable-blink-features=AutomationControlled")
    drv = webdriver.Chrome(options=opts)
    return drv

def save_text(text: str, path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)

def read_last():
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f: return f.read().strip()
    except FileNotFoundError: return ""

def write_last(s):
    with open(CACHE_FILE, "w", encoding="utf-8") as f: f.write(s)

def header_texts_from_table(table):
    """
    ヘッダー<th>のテキストを text / innerText / textContent で総当たり取得
    """
    ths = table.find_elements(By.XPATH, ".//thead/tr/th | ./tr[1]/th")
    texts = []
    for th in ths:
        t = th.text.strip().replace("\n", " ")
        if not t:
            t = (th.get_attribute("innerText") or "").strip().replace("\n"," ")
        if not t:
            t = (th.get_attribute("textContent") or "").strip().replace("\n"," ")
        texts.append(t)
    return texts

def choose_calendar_table(drv):
    tables = drv.find_elements(By.TAG_NAME, "table")
    print(f"[Tables] found: {len(tables)}")
    chosen = None; best_score = -1
    for idx, t in enumerate(tables):
        # 解析用に保存
        outer = t.get_attribute("outerHTML") or ""
        save_text(outer, os.path.join(DUMP_DIR, f"table_{idx}.html"))

        texts = header_texts_from_table(t)
        sample = texts[:10]
        print(f"[Table {idx}] header sample:", sample)

        score = sum(1 for x in texts if re.search(r"\d{1,2}/\d{1,2}", x))
        if score > best_score:
            best_score = score; chosen = t
    return chosen

def detect_status_with_selenium():
    # URLが空なら既定へ
    if not CALENDAR_URL or CALENDAR_URL.strip() == "":
        print("[Warn] CALENDAR_URL is empty. Fallback to default.")
        url = "https://reserve.fumotoppara.net/reserved/reserved-calendar-list"
    else:
        url = CALENDAR_URL

    os.makedirs(DUMP_DIR, exist_ok=True); os.makedirs(SHOT_DIR, exist_ok=True)
    drv = setup_driver()
    try:
        print(f"[Selenium] GET {url}")
        drv.get(url)

        # ページ全体の描画待ち + 少し余裕
        WebDriverWait(drv, 30).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        time.sleep(2.0)

        # ページ全体HTML保存
        page_html = drv.page_source or ""
        save_text(page_html, os.path.join(DUMP_DIR, "page_source.html"))

        # テーブル待機（最大5回リトライ）
        for attempt in range(5):
            tables = drv.find_elements(By.TAG_NAME, "table")
            if tables:
                break
            time.sleep(1.0)

        table = choose_calendar_table(drv)
        if not table:
            print("[Error] No calendar-like table chosen.")
            return "UNKNOWN"

        # 遅延描画の余裕
        time.sleep(1.0)

        header_texts = header_texts_from_table(table)
        print("[Header] count:", len(header_texts))
        print("[Header] first 12:", header_texts[:12])

        # 対象日の列インデックス（部分一致）
        date_idx = -1
        for i, txt in enumerate(header_texts):
            if txt and (TARGET_DATE_LABEL in txt):
                date_idx = i; break
        if date_idx < 0:
            print(f"[Error] TARGET_DATE_LABEL '{TARGET_DATE_LABEL}' not found in header.")
            save_text(table.get_attribute("outerHTML") or "", os.path.join(DUMP_DIR, "chosen_table.html"))
            return "UNKNOWN"

        # 左端<th>はカテゴリ見出しなので、tdの添字は (date_idx - 1)
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
        # セルテキストは text / innerText / textContent のフォールバック
        txt = (cell.text or "").strip().replace("\n"," ")
        if not txt:
            txt = (cell.get_attribute("innerText") or "").strip().replace("\n"," ")
        if not txt:
            txt = (cell.get_attribute("textContent") or "").strip().replace("\n"," ")
        print(f"[Cell] ({TARGET_CATEGORY}/{TARGET_DATE_LABEL}) text:", txt)

        # 保存（Artifacts）
        save_text(cell.get_attribute("innerHTML") or "", os.path.join(DUMP_DIR, "camp_target_cell.html"))
        try:
            cell.screenshot(os.path.join(SHOT_DIR, "camp_target_cell.png"))
        except Exception as se:
            print(f"[Shot] Failed: {se}")

        # 判定
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

def main():
    if not CHANNEL_TOKEN:
        print("ERROR: LINE_CHANNEL_TOKEN is not set."); sys.exit(2)

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
            to = TO_GROUP_ID or TO_USER_ID
            if not to:
                print("ERROR: push mode requires LINE_TO_GROUP_ID or LINE_TO_USER_ID."); sys.exit(3)
            notify_push(to, LINE_MESSAGE)

    write_last(status)
    sys.exit(0)

if __name__ == "__main__":
    main()
