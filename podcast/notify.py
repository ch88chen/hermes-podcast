#!/usr/bin/env python3
"""
把當日新聞摘要與語音推送到 Telegram、LINE（取代 Hermes 本機推送）
用法: python podcast/notify.py <site_dir>
未設定某平台的 Secret 時跳過該平台。
"""
import html
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

BASE_URL = "https://ch88chen.github.io/hermes-podcast"
TZ = timezone(timedelta(hours=8))
TOPIC_NAMES = {"ai": "🤖 AI 科技新聞", "intl": "🌍 國際新聞"}


def log(msg):
    print(f"[{datetime.now(TZ):%H:%M:%S}] {msg}", flush=True)


def load_today(site):
    day = f"{datetime.now(TZ):%Y-%m-%d}"
    with open(os.path.join(site, "episodes", day, "meta.json"), encoding="utf-8") as f:
        episodes = json.load(f)
    topics = {}
    for ep in episodes:
        key = ep["file"].split("_")[0]
        topics.setdefault(key, {})[ep["lang"]] = ep
    return day, topics


def wait_until_live(urls, timeout=600):
    """LINE 以網址播放音檔，要等 GitHub Pages 部署完成"""
    deadline = time.time() + timeout
    pending = set(urls)
    while pending and time.time() < deadline:
        pending = {u for u in pending if requests.head(u, timeout=15).status_code != 200}
        if pending:
            time.sleep(15)
    if pending:
        raise RuntimeError(f"GitHub Pages 尚未提供：{pending}")


def send_telegram(day, topics, site):
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        log("⚠️  未設定 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID，跳過 Telegram")
        return True
    api = f"https://api.telegram.org/bot{token}"
    ok = True
    for key, eps in topics.items():
        zh = eps.get("zh") or next(iter(eps.values()))
        lines = [f"<b>{TOPIC_NAMES.get(key, key)}｜{day}</b>", ""]
        for h in zh["headlines"]:
            lines.append(f'• <a href="{html.escape(h["link"])}">{html.escape(h["title_zh"])}</a>')
        if "zh" in eps:
            lines += ["", html.escape(eps["zh"]["script"])]
        r = requests.post(f"{api}/sendMessage", timeout=30, data={
            "chat_id": chat, "text": "\n".join(lines)[:4096],
            "parse_mode": "HTML", "disable_web_page_preview": "true",
        })
        ok &= r.ok
        log(f"Telegram 文字 {key}: {r.status_code}")
        for lang in ("zh", "en"):
            if lang not in eps:
                continue
            ep = eps[lang]
            with open(os.path.join(site, "episodes", day, ep["file"]), "rb") as f:
                r = requests.post(f"{api}/sendAudio", timeout=120,
                                  data={"chat_id": chat, "title": ep["title"],
                                        "performer": "Hermes", "duration": ep["duration"]},
                                  files={"audio": (ep["file"], f, "audio/mp4")})
            ok &= r.ok
            log(f"Telegram 語音 {ep['file']}: {r.status_code}{'' if r.ok else ' ' + r.text[:200]}")
    return ok


def send_line(day, topics):
    token, to = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN"), os.environ.get("LINE_TO")
    if not token or not to:
        log("⚠️  未設定 LINE_CHANNEL_ACCESS_TOKEN / LINE_TO，跳過 LINE")
        return True
    urls = [f"{BASE_URL}/episodes/{day}/{ep['file']}" for eps in topics.values() for ep in eps.values()]
    wait_until_live(urls)
    ok = True
    for key, eps in topics.items():  # 每主題一次 push：文字 + 中英語音（共 3 則，上限 5）
        zh = eps.get("zh") or next(iter(eps.values()))
        text = [f"{TOPIC_NAMES.get(key, key)}｜{day}", ""]
        for h in zh["headlines"]:
            text += [f"• {h['title_zh']}", h["link"]]
        if "zh" in eps:
            text += ["", eps["zh"]["script"]]
        messages = [{"type": "text", "text": "\n".join(text)[:5000]}]
        for lang in ("zh", "en"):
            if lang in eps:
                ep = eps[lang]
                messages.append({"type": "audio",
                                 "originalContentUrl": f"{BASE_URL}/episodes/{day}/{ep['file']}",
                                 "duration": ep["duration"] * 1000})
        r = requests.post("https://api.line.me/v2/bot/message/push", timeout=30,
                          headers={"Authorization": f"Bearer {token}"},
                          json={"to": to, "messages": messages})
        ok &= r.ok
        log(f"LINE {key}: {r.status_code}{'' if r.ok else ' ' + r.text[:200]}")
    return ok


def main():
    site = sys.argv[1]
    day, topics = load_today(site)
    results = []
    for name, fn in (("Telegram", lambda: send_telegram(day, topics, site)),
                     ("LINE", lambda: send_line(day, topics))):
        try:
            results.append(fn())
        except Exception as e:  # 一個平台失敗不影響另一個
            log(f"❌ {name} 失敗：{e!r}")
            results.append(False)
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
