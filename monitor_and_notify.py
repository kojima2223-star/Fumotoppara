
import os, sys, json, time
import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# ---- 安全な環境変数取得（空文字なら既定にフォールバック）----
def env(name: str, default: str | None = None):
    val = os.environ.get(name)
    if val is None or (isinstance(val, str) and val.strip() == ""):
        return default
    return val

# ---- 監視対象 ----
CALENDAR_URL     = env("FUMO_CALENDAR_URL", "https://reserve.fumotoppara.net/reserved/reserved-calendar-list")
TARGET_DATE_LABEL= env("TARGET_DATE_LABEL", "12/31")   # 画面上の表記（例：12/31、12月31日）
TARGET_DATE_ISO  = env("TARGET_DATE_ISO", None)        # 例：2025-12-31（data-date属性がある場合）

# ---- LINE（Messaging API）----
CHANNEL_TOKEN    = env("LINE_CHANNEL_TOKEN")
SEND_MODE        = env("LINE_SEND_MODE", "push")  # "push" | "broadcast" | "multicast"
TO_USER_ID       = env("LINE_TO_USER_ID", None)
TO_GROUP_ID      = env("LINE_TO_GROUP_ID", None)
USER_IDS_CSV     = env("LINE_USER_IDS", "")
LINE_MESSAGE     = env("LINE_MESSAGE", f"🚨 ふもとっぱら {TARGET_DATE_LABEL} に空き（△）が出ました！\n{CALENDAR_URL}")

HEADERS = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {CHANNEL_TOKEN}" if CHANNEL_TOKEN else "",
}

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

def setup_driver() -> webdriver.Chrome:
    opts = Options()
    # ヘッドレス設定
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1280,2000")
    # 言語設定（日本語ページでの文字化け防止）
    opts.add_argument("--lang=ja-JP")
    driver = webdriver.Chrome(options=opts)
    return driver

def detect_status_with_selenium() -> str:
    driver = setup_driver()
    try:
        print(f"[Selenium] GET {CALENDAR_URL}")
        driver.get(CALENDAR_URL)

        # ページの主要要素が描画されるまで待機（必要に応じて安定化）
        # 例：カレンダーコンテナのCSSクラスやidがわかればそこを待つ
        # ここでは暫定的にbodyの読み込み＋少し待機
        WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        time.sleep(2)  # JS描画の余裕時間（必要なら増減）

        # 1) ISO属性で特定（推奨）
        if TARGET_DATE_ISO:
            sel = f'[data-date="{TARGET_DATE_ISO}"]'
            elems = driver.find_elements(By.CSS_SELECTOR, sel)
            if elems:
                text = " ".join(e.text.strip() for e in elems if e.text.strip())
                print(f"[Detect] ISO {TARGET_DATE_ISO} text: {text}")
                for mark in ("△","○","×"):
                    if mark in text:
                        return mark

        # 2) ラベルテキストで近傍探索
        # シンプルに全テキストからラベルを含むものを拾う
        all_text = driver.find_element(By.TAG_NAME, "body").text
        # ログ用に一部出力
        print("[Detect] Body text sample:", all_text[:500].replace("\n"," | "))

        # ラベルが「12/31」のような形式で掲載されているか探索
        if TARGET_DATE_LABEL in all_text:
            # ラベル周辺の行を抽出して、記号があるか見る
            lines = [ln.strip() for ln in all_text.splitlines() if TARGET_DATE_LABEL in ln]
            print("[Detect] Lines around label:")
            for ln in lines[:10]:
                print("  -", ln)
                for mark in ("△","○","×"):
                    if mark in ln:
                        return mark

        # 上記で拾えなければ、テーブルセル系のパターンも網羅的に見る（負荷低）
        candidates = driver.find_elements(By.XPATH, f"//*[contains(text(), '{TARGET_DATE_LABEL}')]")
        if candidates:
            print(f"[Detect] Found {len(candidates)} nodes containing label.")
            for el in candidates[:10]:
                txt = el.text.strip()
                print("  - node:", txt)
                for mark in ("△","○","×"):
                    if mark in txt:
                        return mark

        return "UNKNOWN"
    finally:
        driver.quit()

def main():
    if not CHANNEL_TOKEN:
        print("ERROR: LINE_CHANNEL_TOKEN is not set."); sys.exit(2)

    status = detect_status_with_selenium()
    print(f"[Result] {TARGET_DATE_LABEL} status: {status}")

    if status == "△":
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

    if status == "UNKNOWN":
        print("WARN: まだ特定できません。対象セルのHTML/クラス名が分かれば、待機対象やセレクタを固定化します。")

    sys.exit(0)

if __name__ == "__main__":
    main()
