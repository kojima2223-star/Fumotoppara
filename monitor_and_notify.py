
# -*- coding: utf-8 -*-
import os, sys, time, requests, re
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

def env(name, default=None):
    v = os.environ.get(name)
    if v is None or (isinstance(v, str) and v.strip() == ""):
        return default
    return v

CALENDAR_URL       = env("FUMO_CALENDAR_URL", "https://reserve.fumotoppara.net/reserved/reserved-calendar-list")
TARGET_CATEGORY    = env("TARGET_CATEGORY_LABEL", "キャンプ宿泊")
TARGET_DATE_LABEL  = env("TARGET_DATE_LABEL", "12/31")   # 例: 12/31（曜日は含めなくてOK）
NOTIFY_DIFF_ONLY   = env("NOTIFY_DIFF_ONLY", "0") == "1"

CHANNEL_TOKEN      = env("LINE_CHANNEL_TOKEN")
SEND_MODE          = env("LINE_SEND_MODE", "push")
TO_USER_ID         = env("LINE_TO_USER_ID", None)
TO_GROUP_ID        = env("LINE_TO_GROUP_ID", None)
USER_IDS_CSV       = env("LINE_USER_IDS", "")
LINE_MESSAGE       = env("LINE_MESSAGE", f"🚨 ふもとっぱら（{TARGET_CATEGORY}）{TARGET_DATE_LABEL} に空き（△）が出ました！\n{CALENDAR_URL}")

HEADERS = {"Content-Type":"application/json","Authorization":f"Bearer {CHANNEL_TOKEN}" if CHANNEL_TOKEN else ""}

DUMP_DIR  = "html_dump"
SHOT_DIR  = "shots"
CACHE_FILE = "last_status.txt"

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

def setup_driver():
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1440,2400")
    opts.add_argument("--lang=ja-JP")
    return webdriver.Chrome(options=opts)

def save(text, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f: f.write(text)

def read_last():
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f: return f.read().strip()
    except FileNotFoundError: return ""

def write_last(s):
    with open(CACHE_FILE, "w", encoding="utf-8") as f: f.write(s)

def find_calendar_table(drv):
    """
    カレンダーに該当しそうな<table>を選ぶ。
    - thead内に日付が並ぶ<th>がある
    - または1行目<th>群に日付が並ぶ
    複数テーブルがある場合は、ヘッダーに「12/」や「1/」などの月日が含まれるものを優先。
    """
    tables = drv.find_elements(By.TAG_NAME, "table")
    print(f"[Tables] found: {len(tables)}")
    chosen = None
    best_score = -1

    for idx, t in enumerate(tables):
        # できるだけ広いヘッダー候補を集める
        ths = t.find_elements(By.XPATH, ".//thead/tr/th | .//tr[1]/th")
        texts = [th.text.strip().replace("\n"," ") for th in ths]
        sample = texts[:10]
        print(f"[Table {idx}] header sample:", sample)
        # スコア：日付らしさで評価（例: "12/31", "1/1" などが含まれる数）
        score = sum(1 for x in texts if re.search(r"\d{1,2}/\d{1,2}", x))
        if score > best_score:
            best_score = score; chosen = t
        # 保存（解析用）
        html = t.get_attribute("outerHTML") or ""
        save(html, os.path.join(DUMP_DIR, f"table_{idx}.html"))

    return chosen

def get_header_texts(table):
    """
    ヘッダー<th>群を最大限拾う（thead優先→1行目→trに複数行ある場合は最初に日付が並ぶ行）
    """
    # まず thead
    ths = table.find_elements(By.XPATH, ".//thead/tr/th")
    if not ths:
        # 次に 1行目 tr の th
        ths = table.find_elements(By.XPATH, "./tr[1]/th")
    if not ths:
        # それでも無ければ、th行のうち最もth数が多い行を選ぶ
        rows = table.find_elements(By.XPATH, ".//tr")
        best = []
        max_ths = -1
        for r in rows:
            r_ths = r.find_elements(By.XPATH, "./th")
            if len(r_ths) > max_ths:
                max_ths = len(r_ths); best = r_ths
        ths = best
    texts = [th.text.strip().replace("\n"," ") for th in ths]
    return texts

def detect_status_with_selenium():
    os.makedirs(DUMP_DIR, exist_ok=True); os.makedirs(SHOT_DIR, exist_ok=True)
    drv = setup_driver()
    try:
        print(f"[Selenium] GET {CALENDAR_URL}")
        drv.get(CALENDAR_URL)
        WebDriverWait(drv, 30).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        # テーブルの描画を待つ（最大3回リトライ）
        for attempt in range(3):
            tables = drv.find_elements(By.TAG_NAME, "table")
            if tables: break
            time.sleep(1.0)

        table = find_calendar_table(drv)
        if not table:
            print("[Error] No table chosen.")
            return "UNKNOWN"

        header_texts = get_header_texts(table)
        print("[Header] count:", len(header_texts))
        print("[Header] first 12:", header_texts[:12])
        # 対象日（例: "12/31"）の列インデックス（部分一致）
        date_idx = -1
        for i, txt in enumerate(header_texts):
            if TARGET_DATE_LABEL in txt:
                date_idx = i; break
        if date_idx < 0:
            print(f"[Error] TARGET_DATE_LABEL '{TARGET_DATE_LABEL}' not found in header.")
            # 解析用にテーブル全体を保存
            save(table.get_attribute("outerHTML") or "", os.path.join(DUMP_DIR, "chosen_table.html"))
            return "UNKNOWN"

        # データ列の <td> は通常「左端<th>がカテゴリ」なので、tdの添字= (date_idx - 1)
        td_idx = date_idx - 1
        if td_idx < 0:
            print("[Error] td_idx negative. Header layout may differ.")
            save(table.get_attribute("outerHTML") or "", os.path.join(DUMP_DIR, "chosen_table.html"))
            return "UNKNOWN"

        # 「キャンプ宿泊」行を取得（左端<th>にカテゴリ名）
        camp_row = table.find_element(
            By.XPATH,
            ".//tr[th[contains(normalize-space(.), 'キャンプ宿泊')] or normalize-space(th[1])='キャンプ宿泊']"
        )
        # その行の <td> 群
        tds = camp_row.find_elements(By.XPATH, "./td")
        print("[Row] td count:", len(tds))
        if td_idx >= len(tds):
            print(f"[Error] td_idx({td_idx}) >= len(tds)({len(tds)})")
            save(camp_row.get_attribute("outerHTML") or "", os.path.join(DUMP_DIR, "camp_row.html"))
            return "UNKNOWN"

        cell = tds[td_idx]
        txt = cell.text.strip().replace("\n", " ")
        print(f"[Cell] ({TARGET_CATEGORY}/{TARGET_DATE_LABEL}) text:", txt)
        # 保存（Artifacts）
        save(cell.get_attribute("innerHTML") or "", os.path.join(DUMP_DIR, "camp_target_cell.html"))
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
