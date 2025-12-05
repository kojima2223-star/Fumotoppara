
import os, sys, json, requests
from bs4 import BeautifulSoup

# ---- 監視対象の設定（Secrets or Variablesから注入）----
CALENDAR_URL = os.environ.get("FUMO_CALENDAR_URL", "https://reserve.fumotoppara.net/reserved/reserved-calendar-list")
TARGET_DATE_LABEL = os.environ.get("TARGET_DATE_LABEL", "12/31")  # 画面上の表記
TARGET_DATE_ISO = os.environ.get("TARGET_DATE_ISO")               # 例: 2025-12-31（data-date 属性がある場合に推奨）

# ---- LINE（Messaging API）----
CHANNEL_TOKEN = os.environ.get("LINE_CHANNEL_TOKEN")
SEND_MODE = os.environ.get("LINE_SEND_MODE", "push")  # "push" | "broadcast" | "multicast"
TO_USER_ID = os.environ.get("LINE_TO_USER_ID")        # push宛先（個人）
TO_GROUP_ID = os.environ.get("LINE_TO_GROUP_ID")      # push宛先（グループ）
USER_IDS_CSV = os.environ.get("LINE_USER_IDS", "")    # multicast用: "Uxxxx,Uyyyy"
LINE_MESSAGE = os.environ.get("LINE_MESSAGE", f"🚨 ふもとっぱら {TARGET_DATE_LABEL} に空き（△）が出ました！\n{CALENDAR_URL}")

HEADERS = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {CHANNEL_TOKEN}" if CHANNEL_TOKEN else "",
}

def notify_push(target_id: str, text: str):
    url = "https://api.line.me/v2/bot/message/push"
    payload = {"to": target_id, "messages": [{"type": "text", "text": text}]}
    r = requests.post(url, headers=HEADERS, data=json.dumps(payload), timeout=20)
    r.raise_for_status()
    print(f"[LINE] Push sent to {target_id}: {r.status_code}")

def notify_broadcast(text: str):
    url = "https://api.line.me/v2/bot/message/broadcast"
    payload = {"messages": [{"type": "text", "text": text}]}
    r = requests.post(url, headers=HEADERS, data=json.dumps(payload), timeout=20)
    r.raise_for_status()
    print(f"[LINE] Broadcast sent: {r.status_code}")

def notify_multicast(user_ids, text: str):
    url = "https://api.line.me/v2/bot/message/multicast"
    payload = {"to": user_ids, "messages": [{"type": "text", "text": text}]}
    r = requests.post(url, headers=HEADERS, data=json.dumps(payload), timeout=20)
    r.raise_for_status()
    print(f"[LINE] Multicast sent({len(user_ids)} users): {r.status_code}")

def fetch_calendar_html() -> str:
    r = requests.get(CALENDAR_URL, timeout=20)
    print(f"[Fetch] Calendar status: {r.status_code}")
    return r.text

def detect_status(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")

    # 1) ISO属性での特定（最も安定）
    if TARGET_DATE_ISO:
        el = soup.select_one(f'[data-date="{TARGET_DATE_ISO}"]')
        if el:
            text = el.get_text(separator=" ", strip=True)
            print(f"[Detect] ISO match text: {text}")
            for mark in ("△", "○", "×"):
                if mark in text:
                    return mark

    # 2) ラベル近傍探索（テキスト照合）
    candidates = []
    for tag in soup.find_all(["td", "div", "span", "li"]):
        text = tag.get_text(separator=" ", strip=True)
        if text and TARGET_DATE_LABEL in text:
            candidates.append(text)

    if candidates:
        print("[Detect] Candidates around label:")
        for c in candidates[:10]:
            print("  -", c)
        for text in candidates:
            for mark in ("△", "○", "×"):
                if mark in text:
                    return mark

    return "UNKNOWN"

def main():
    if not CHANNEL_TOKEN:
        print("ERROR: LINE_CHANNEL_TOKEN is not set."); sys.exit(2)

    html = fetch_calendar_html()
    status = detect_status(html)
    print(f"[Result] {TARGET_DATE_LABEL} status: {status}")

    if status == "△":
        # 宛先判定＋送信
        if SEND_MODE == "broadcast":
            notify_broadcast(LINE_MESSAGE)  # 友だち全員へ
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
        print("WARN: ステータス特定に失敗。対象セルのHTML断片を共有いただければセレクタを調整します。")

    sys.exit(0)

if __name__ == "__main__":
    main()
