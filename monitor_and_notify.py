
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

        # 本文が描画されるまで待機
        WebDriverWait(driver, 25).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        time.sleep(2)  # 余裕時間（必要なら調整）

        # 1) data-date="YYYY-MM-DD" 属性があれば最優先
        if TARGET_DATE_ISO:
            sel = f'//*[@data-date="{TARGET_DATE_ISO}"]'
            iso_nodes = driver.find_elements(By.XPATH, sel)
            if iso_nodes:
                cell = iso_nodes[0]
                # セル内の記号テキストを直接確認
                text = cell.text.strip()
                print(f"[Detect] ISO cell text: {text}")
                for mark in ("△", "○", "×"):
                    if mark in text:
                        return mark
                # セル内の画像アイコンやtitle/altも確認
                icons = cell.find_elements(By.XPATH, ".//img | .//*[contains(@class,'status') or contains(@class,'icon')]")
                for el in icons:
                    alt = (el.get_attribute("alt") or "").strip()
                    title = (el.get_attribute("title") or "").strip()
                    clazz = (el.get_attribute("class") or "").strip()
                    joined = " ".join([text, alt, title, clazz])
                    print(f"[Detect] ISO cell inspect: alt={alt} title={title} class={clazz}")
                    # 文言やクラス名で判定（必要に応じて調整）
                    if ("空きあり" in joined) or ("available" in joined):
                        return "○"
                    if ("残りわずか" in joined) or ("few" in joined):
                        return "△"
                    if ("満席" in joined) or ("満室" in joined) or ("full" in joined):
                        return "×"
                # ここで未判定なら次の手段（ラベル探索）へフォールバック

        # 2) ラベル（例：12/31）でセルを拾い、親セル内を精査
        # a) td系（テーブルカレンダー想定）
        td_nodes = driver.find_elements(
            By.XPATH,
            f"//td[contains(normalize-space(.), '{TARGET_DATE_LABEL}')]"
        )
        # b) div/span系（カード・グリッド想定）
        other_nodes = driver.find_elements(
            By.XPATH,
            f"//*[self::div or self::span][contains(normalize-space(.), '{TARGET_DATE_LABEL}')]"
        )

        candidates = td_nodes or other_nodes
        print(f"[Detect] Found {len(candidates)} candidate cells for label '{TARGET_DATE_LABEL}'.")

        if candidates:
            # 近いセルを順にチェック
            for cell in candidates[:5]:
                cell_text = cell.text.strip()
                print(f"[Detect] Cell text: {cell_text}")

                # まずはセルの直テキストに記号がないか
                for mark in ("△", "○", "×"):
                    if mark in cell_text:
                        return mark

                # 子要素のアイコン・ステータス表記を確認
                # 画像（alt/title）、ステータス用クラス、別spanに記号があるケースを網羅
                child_elems = cell.find_elements(By.XPATH, ".//img | .//span | .//i | .//*[contains(@class,'status') or contains(@class,'icon') or contains(@class,'reserve') or contains(@class,'availability')]")
                for el in child_elems:
                    t = el.text.strip()
                    alt = (el.get_attribute("alt") or "").strip()
                    title = (el.get_attribute("title") or "").strip()
                    clazz = (el.get_attribute("class") or "").strip()
                    joined = " ".join([t, alt, title, clazz]).lower()
                    # 記号で判定
                    if any(m in t for m in ("△", "○", "×")):
                        for m in ("△", "○", "×"):
                            if m in t:
                                return m
                    # 文言やクラス名で判定（必要に応じて言い換え追加）
                    if ("空きあり" in joined) or ("available" in joined):
                        return "○"
                    if ("残りわずか" in joined) or ("few" in joined):
                        return "△"
                    if ("満席" in joined) or ("満室" in joined) or ("full" in joined):
                        return "×"

        # 3) ここまでで判定できない場合はUNKNOWN
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
