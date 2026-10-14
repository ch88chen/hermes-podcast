#!/usr/bin/env python3
"""
Hermes Daily News Podcast — cloud builder (GitHub Actions)

抓 RSS 新聞 → Claude 寫中英文播報稿 → macOS say 轉語音（AAC）→ 更新 feed.xml
需在 macOS runner 執行
用法: python podcast/build.py <site_dir>
"""
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from xml.sax.saxutils import escape

import anthropic
import feedparser

BASE_URL = "https://ch88chen.github.io/hermes-podcast"
MODEL = os.environ.get("PODCAST_MODEL", "claude-opus-5-5")
KEEP_DAYS = 14
TZ = timezone(timedelta(hours=8))

TOPICS = {
    "ai": {
        "name_zh": "AI 科技新聞",
        "name_en": "AI Tech News",
        "hms": "08:35",
        "feeds": [
            "https://techcrunch.com/category/artificial-intelligence/feed/",
            "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
            "https://feeds.arstechnica.com/arstechnica/technology-lab",
        ],
    },
    "intl": {
        "name_zh": "國際新聞",
        "name_en": "International News",
        "hms": "08:40",
        "feeds": [
            "https://feeds.bbci.co.uk/news/world/rss.xml",
            "https://feeds.bbci.co.uk/news/world/asia/rss.xml",
            "https://www.aljazeera.com/xml/rss/all.xml",
        ],
    },
}

VOICES = {"zh": "Meijia", "en": "Samantha"}

SCRIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "headlines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title_zh": {"type": "string"},
                    "title_en": {"type": "string"},
                    "link": {"type": "string"},
                },
                "required": ["title_zh", "title_en", "link"],
                "additionalProperties": False,
            },
        },
        "script_en": {"type": "string"},
        "script_zh": {"type": "string"},
    },
    "required": ["headlines", "script_en", "script_zh"],
    "additionalProperties": False,
}


def log(msg):
    print(f"[{datetime.now(TZ):%H:%M:%S}] {msg}", flush=True)


def fetch_news(feeds, since):
    items, seen = [], set()
    for url in feeds:
        parsed = feedparser.parse(url)
        if parsed.bozo and not parsed.entries:
            log(f"⚠️  讀取失敗 {url}")
            continue
        for e in parsed.entries[:15]:
            t = e.get("published_parsed") or e.get("updated_parsed")
            if t and datetime(*t[:6], tzinfo=timezone.utc) < since:
                continue
            title = e.get("title", "").strip()
            if not title or title in seen:
                continue
            seen.add(title)
            summary = e.get("summary", "")[:400]
            items.append(f"- {title}\n  {summary}\n  {e.get('link', '')}")
    return items


def write_scripts(client, topic, items, today):
    prompt = f"""今天是 {today:%Y-%m-%d}。以下是過去一天的「{topic['name_zh']}」RSS 新聞：

{chr(10).join(items)}

請挑出 5 則最重要、彼此不重複的新聞，產生：
1. headlines：這 5 則的中英文標題與原文連結
2. script_en：約 200 字的英文播報稿，開頭報日期與節目名稱「{topic['name_en']}」
3. script_zh：約 400 字的繁體中文（台灣用語）播報稿，開頭報日期與節目名稱「{topic['name_zh']}」

播報稿會直接交給語音合成朗讀：只寫口語句子，不要 Markdown、項目符號、網址或表情符號。只根據上面提供的內容，不要補充沒有的事實。"""
    resp = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={
            "effort": "low",
            "format": {"type": "json_schema", "schema": SCRIPT_SCHEMA},
        },
        messages=[{"role": "user", "content": prompt}],
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError(f"Claude 拒絕產生稿件: {resp.stop_details}")
    text = next(b.text for b in resp.content if b.type == "text")
    return json.loads(text)


def tts(text, voice, path):
    """say → AIFF → AAC (.m4a)，回傳秒數"""
    aiff = path[:-4] + ".aiff"
    subprocess.run(["say", "-v", voice, "-o", aiff, text], check=True)
    subprocess.run(["afconvert", "-f", "m4af", "-d", "aac", "-b", "64000", aiff, path], check=True)
    os.remove(aiff)
    out = subprocess.run(["afinfo", path], capture_output=True, text=True).stdout
    return int(float(re.search(r"estimated duration:\s*([\d.]+)", out).group(1)))


def build_episode(client, key, topic, site, today):
    since = datetime.now(timezone.utc) - timedelta(hours=36)
    items = fetch_news(topic["feeds"], since)
    log(f"{topic['name_zh']}：取得 {len(items)} 則候選新聞")
    if len(items) < 3:
        raise RuntimeError(f"{topic['name_zh']} 新聞太少（{len(items)}）")
    data = write_scripts(client, topic, items[:30], today)

    day_dir = os.path.join(site, "episodes", f"{today:%Y-%m-%d}")
    os.makedirs(day_dir, exist_ok=True)
    episodes = []
    for lang in ("zh", "en"):
        fname = f"{key}_{lang}.m4a"
        path = os.path.join(day_dir, fname)
        dur = tts(data[f"script_{lang}"], VOICES[lang], path)
        log(f"🎙  {fname}：{dur} 秒")
        title = (f"{topic['name_zh']}（繁中）" if lang == "zh" else f"{topic['name_en']} (English)")
        episodes.append({
            "file": fname,
            "title": f"{title} - {today:%Y-%m-%d}",
            "lang": lang,
            "hms": topic["hms"] + (":00" if lang == "zh" else ":30"),
            "duration": dur,
            "length": os.path.getsize(path),
            "headlines": data["headlines"],
            "script": data[f"script_{lang}"],
        })
    return episodes


def prune(site, today):
    ep_root = os.path.join(site, "episodes")
    cutoff = today - timedelta(days=KEEP_DAYS)
    for d in os.listdir(ep_root):
        try:
            if datetime.strptime(d, "%Y-%m-%d").date() < cutoff:
                shutil.rmtree(os.path.join(ep_root, d))
                log(f"🗑  移除 {d}")
        except ValueError:
            pass


def build_feed(site):
    ep_root = os.path.join(site, "episodes")
    entries = []
    for day in os.listdir(ep_root):
        meta_path = os.path.join(ep_root, day, "meta.json")
        if not os.path.isfile(meta_path):
            continue
        for ep in json.load(open(meta_path, encoding="utf-8")):
            pub = datetime.strptime(f"{day} {ep['hms']}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
            key = "title_zh" if ep["lang"] == "zh" else "title_en"
            notes = "\n".join(f"• {h[key]}\n  {h['link']}" for h in ep["headlines"])
            entries.append((pub, f"""    <item>
      <title>{escape(ep['title'])}</title>
      <description>{escape(notes + chr(10) + chr(10) + ep['script'])}</description>
      <pubDate>{format_datetime(pub)}</pubDate>
      <guid isPermaLink="false">hermes-{day}-{ep['file'][:-4]}</guid>
      <enclosure url="{BASE_URL}/episodes/{day}/{ep['file']}" length="{ep['length']}" type="audio/x-m4a"/>
      <itunes:duration>{ep['duration']}</itunes:duration>
      <itunes:explicit>false</itunes:explicit>
    </item>"""))
    entries.sort(key=lambda x: x[0], reverse=True)
    feed = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>Hermes 每日新聞語音摘要</title>
    <link>{BASE_URL}/</link>
    <atom:link href="{BASE_URL}/feed.xml" rel="self" type="application/rss+xml"/>
    <description>每日 AI 科技新聞與國際新聞語音摘要（中英文）</description>
    <language>zh-tw</language>
    <lastBuildDate>{format_datetime(datetime.now(TZ))}</lastBuildDate>
    <itunes:author>Chen Lynch</itunes:author>
    <itunes:image href="{BASE_URL}/cover.png"/>
    <itunes:category text="News"/>
    <itunes:explicit>false</itunes:explicit>
    <itunes:block>Yes</itunes:block>
{chr(10).join(e[1] for e in entries)}
  </channel>
</rss>
"""
    with open(os.path.join(site, "feed.xml"), "w", encoding="utf-8") as f:
        f.write(feed)
    log(f"📝 feed.xml：{len(entries)} 集")


def main():
    site = sys.argv[1]
    today = datetime.now(TZ).date()
    day_dir = os.path.join(site, "episodes", f"{today:%Y-%m-%d}")
    # 備援排程：今天已產生過就跳過（GitHub 排程可能漏跑，所以一天設多個觸發時間）
    if "--skip-if-done" in sys.argv and os.path.isfile(os.path.join(day_dir, "meta.json")):
        log("ℹ️  今天已產生過，略過")
        set_output("built", "false")
        return
    staging = os.path.join(site, ".staging")
    shutil.rmtree(staging, ignore_errors=True)
    open(os.path.join(site, ".nojekyll"), "a").close()

    # 先在 staging 產生，至少有一集成功才取代當日資料，避免失敗時清空 feed
    client = anthropic.Anthropic()
    episodes = []
    for key, topic in TOPICS.items():
        try:
            episodes += build_episode(client, key, topic, staging, today)
        except Exception as e:  # 一個主題失敗不影響另一個
            log(f"❌ {topic['name_zh']} 失敗：{e!r}")
    if not episodes:
        shutil.rmtree(staging, ignore_errors=True)
        log("❌ 沒有任何集數產生，不更新 feed")
        sys.exit(1)

    new_day = os.path.join(staging, "episodes", f"{today:%Y-%m-%d}")
    with open(os.path.join(new_day, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(episodes, f, ensure_ascii=False, indent=1)
    shutil.rmtree(day_dir, ignore_errors=True)  # 同日重跑時覆蓋
    shutil.move(new_day, day_dir)
    shutil.rmtree(staging)

    prune(site, today)
    build_feed(site)
    set_output("built", "true")


def set_output(name, value):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as f:
            f.write(f"{name}={value}\n")


if __name__ == "__main__":
    main()
