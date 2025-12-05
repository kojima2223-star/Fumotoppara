
# -*- coding: utf-8 -*-
"""
ふもとっぱら予約カレンダー監視 + LINE Messaging API（Flex）通知
— 単日差分版（A案固定：×→○／×→△のときだけ通知）

● 何をするか
- ふもとっぱら予約カレンダーの <table> をSeleniumで取得
- ヘッダー(<th>)から対象日列インデックスを特定
- 「キャンプ宿泊」行の同列<td>を読み取り → ○/△/×/ー を判定
- 前回ステータスとの比較で「×→○」または「×→△」に変化したときだけ Flex Message で通知
- 調査用にページHTML／選択テーブル／対象セルHTML/スクショをArtifactsへ保存可能（YAMLでupload）

● 必要なSecrets/Variables（主なもの）
- LINE_CHANNEL_TOKEN（必須）
- LINE_TO_USER_ID または LINE_TO_GROUP_ID（pushの宛先のいずれか）
- FUMO_CALENDAR_URL（省略時は既定URL）
- TARGET_CATEGORY_LABEL（省略時「キャンプ宿泊」）
- TARGET_DATE_LABEL（例：12/31。ヘッダーの表記に部分一致）
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
TARGET_CATEGORY    = env("TARGET_CATEGORY_LABEL", "キャンプ宿泊")   # この行だけを見る
TARGET_DATE_LABEL  = env("TARGET_DATE_LABEL", "12/31")             # ヘッダー表記に部分一致（例：12/31）

# --------- LINE設定 ---------
CHANNEL_TOKEN      = env("LINE_CHANNEL_TOKEN")
SEND_MODE          = env("LINE_SEND_MODE", "push")                 # push|broadcast|multicast
TO_USER_ID         = env("LINE_TO_USER_ID", None)
TO_GROUP_ID        = env("LINE_TO_GROUP_ID", None)
USER_IDS_CSV       = env("LINE_USER_IDS", "")
LINE_MESSAGE       = env("LINE_MESSAGE", f"🚨 ふもとっぱら（{TARGET_CATEGORY}）{TARGET_DATE_LABEL} に変化あり！\n{CALENDAR_URL}")

HEADERS = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {CHANNEL_TOKEN}" if CHANNEL_TOKEN else "",
}

# --------- 保存（Artifacts用） ---------
DUMP_DIR   = "html_dump"
SHOT_DIR   = "shots"
CACHE_FILE = "last_status.txt"


# --------- Flex Message（Bubble） ---------
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
                {
                    "type": "box", "layout": "horizontal", "contents": [
                        {"type": "text", "text": "対象日", "size": "sm", "color": "#6b7280"},
                        {"type": "text", "text": date_label, "size": "sm", "align": "end", "weight": "bold"}
                    ]
                },
                {
                    "type": "box", "layout": "horizontal", "contents": [
                        {"type": "text", "text": "今回", "size": "sm", "color": "#6b7280"},
                        {
                            "type": "box", "layout": "horizontal", "contents": [
                                {"type": "text", "text": status, "size": "sm", "weight": "bold", "color": color, "margin": "xs"},
                                {"type": "text", "text": label, "size": "sm", "color": color, "margin": "sm"}
                            ],
                            "justifyContent": "flex-end"
                        }
                    ]
                },
                {
                    "type": "box", "layout": "horizontal", "contents": [
                        {"type": "text", "text": "前回", "size": "sm", "color": "#6b7280"},
                        {
                            "type": "box", "layout": "horizontal", "contents": [
                                {"type": "text", "text": prev_status or "-", "size": "sm", "weight": "bold", "color": "#6b7280", "margin": "xs"},
