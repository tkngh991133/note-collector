"""
note.com 中小企業・IT系経営者記事収集 → Anthropic要約 → Teams配信
"""

import json
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx
import anthropic

# ─── 設定 ────────────────────────────────────────────────────────────────────

TAGS = ["経営", "社長", "中小企業"]
ARTICLES_PER_TAG = 8          # タグごとの取得件数（重複除去後に10〜15件に絞る）
MAX_ARTICLES = 15              # 最終的に配信する最大件数
MIN_ARTICLES = 10              # 最低配信件数

SENT_IDS_PATH = Path(__file__).parent.parent / "sent_ids.json"

NOTE_SEARCH_URL = "https://note.com/api/v2/searches"
TEAMS_WEBHOOK_URL = os.environ["TEAMS_WEBHOOK_URL"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

JST = timezone(timedelta(hours=9))


# ─── 重複排除ストア ────────────────────────────────────────────────────────────

def load_sent_ids() -> set[str]:
    if SENT_IDS_PATH.exists():
        data = json.loads(SENT_IDS_PATH.read_text(encoding="utf-8"))
        return set(data.get("ids", []))
    return set()


def save_sent_ids(ids: set[str]) -> None:
    SENT_IDS_PATH.write_text(
        json.dumps({"ids": sorted(ids)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ─── note.com 記事取得 ────────────────────────────────────────────────────────

def fetch_articles_by_tag(tag: str, size: int = 10) -> list[dict]:
    """note非公式APIでタグ検索して記事リストを返す"""
    params = {
        "context": "note",
        "q": tag,
        "size": size,
        "page": 1,
        "sort": "new",
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; NoteCollector/1.0)",
        "Accept": "application/json",
    }
    try:
        resp = httpx.get(NOTE_SEARCH_URL, params=params, headers=headers, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        notes = data.get("data", {}).get("notes", {}).get("contents", [])
        return notes
    except Exception as e:
        print(f"[WARN] タグ「{tag}」の取得失敗: {e}")
        return []


def collect_candidates(sent_ids: set[str]) -> list[dict]:
    """全タグから記事を集めて重複除去・未送信フィルタリング"""
    seen: set[str] = set()
    candidates: list[dict] = []

    for tag in TAGS:
        articles = fetch_articles_by_tag(tag, size=ARTICLES_PER_TAG)
        time.sleep(1)  # レートリミット配慮

        for a in articles:
            note_id = str(a.get("id", ""))
            key = a.get("key", "")
            unique_id = note_id or key
            if not unique_id:
                continue
            if unique_id in seen or unique_id in sent_ids:
                continue
            seen.add(unique_id)

            user = a.get("user", {})
            candidates.append({
                "id": unique_id,
                "title": a.get("name", "（タイトルなし）"),
                "author": user.get("nickname", user.get("urlname", "不明")),
                "url": f"https://note.com/{user.get('urlname', '')}/n/{key}" if key else "",
                "body": a.get("body", "") or a.get("description", "") or "",
                "published_at": a.get("publishAt", ""),
                "tag": tag,
            })

    # 新しい順に並べて最大MAX_ARTICLES件
    candidates.sort(key=lambda x: x["published_at"], reverse=True)
    return candidates[:MAX_ARTICLES]


# ─── Anthropic 要約 ───────────────────────────────────────────────────────────

def summarize_articles(articles: list[dict]) -> list[dict]:
    """各記事を3行要約＋ネタ切り口コメント付きで返す"""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    for article in articles:
        title = article["title"]
        body_snippet = article["body"][:800] if article["body"] else "（本文なし）"

        prompt = f"""あなたは中小企業・IT系経営者向けの情報キュレーターです。
以下のnote記事について答えてください。

【タイトル】{title}
【本文抜粋】{body_snippet}

以下の形式でJSON出力してください（他の文字は一切出力しない）：
{{
  "summary": "3行の要約（各行を改行区切りで）",
  "angle": "この記事がネタになりそうな切り口を1文で"
}}"""

        try:
            message = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = message.content[0].text.strip()
            # JSONブロックがあれば抽出
            if "```" in raw:
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            parsed = json.loads(raw)
            article["summary"] = parsed.get("summary", "要約取得失敗")
            article["angle"] = parsed.get("angle", "")
        except Exception as e:
            print(f"[WARN] 要約失敗「{title}」: {e}")
            article["summary"] = "要約を取得できませんでした"
            article["angle"] = ""

        time.sleep(0.5)  # API負荷軽減

    return articles


# ─── Teams 配信 ───────────────────────────────────────────────────────────────

def build_teams_payload(articles: list[dict]) -> dict:
    """Adaptive Card形式のTeamsペイロードを構築"""
    now_jst = datetime.now(JST).strftime("%Y年%m月%d日 %H:%M JST")

    facts_sections = []
    for i, a in enumerate(articles, 1):
        summary_lines = a["summary"].replace("\\n", "\n")
        body_text = (
            f"**要約:**\n{summary_lines}\n\n"
            f"**切り口:** {a['angle']}"
        ) if a["angle"] else f"**要約:**\n{summary_lines}"

        facts_sections.append({
            "type": "Container",
            "items": [
                {
                    "type": "TextBlock",
                    "text": f"{i}. [{a['title']}]({a['url']})",
                    "wrap": True,
                    "weight": "Bolder",
                    "size": "Medium",
                },
                {
                    "type": "TextBlock",
                    "text": f"著者: {a['author']}",
                    "wrap": True,
                    "isSubtle": True,
                    "size": "Small",
                },
                {
                    "type": "TextBlock",
                    "text": body_text,
                    "wrap": True,
                    "size": "Small",
                },
            ],
            "separator": i > 1,
        })

    payload = {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": [
                        {
                            "type": "TextBlock",
                            "text": f"📰 note 経営・IT系記事まとめ（{now_jst}）",
                            "weight": "Bolder",
                            "size": "Large",
                            "wrap": True,
                        },
                        {
                            "type": "TextBlock",
                            "text": f"本日 {len(articles)} 件の記事をお届けします",
                            "isSubtle": True,
                            "size": "Small",
                        },
                        *facts_sections,
                    ],
                },
            }
        ],
    }
    return payload


def post_to_teams(payload: dict) -> None:
    resp = httpx.post(
        TEAMS_WEBHOOK_URL,
        json=payload,
        timeout=30,
        headers={"Content-Type": "application/json"},
    )
    resp.raise_for_status()
    print(f"[INFO] Teams配信完了: {resp.status_code}")


# ─── メイン ──────────────────────────────────────────────────────────────────

def main() -> None:
    print("[INFO] 記事収集開始")
    sent_ids = load_sent_ids()
    print(f"[INFO] 送信済みID数: {len(sent_ids)}")

    candidates = collect_candidates(sent_ids)
    print(f"[INFO] 未送信候補: {len(candidates)} 件")

    if len(candidates) < MIN_ARTICLES:
        print(f"[WARN] 候補が{MIN_ARTICLES}件未満({len(candidates)}件)のため配信スキップ")
        # 候補が1件以上あれば配信する
        if len(candidates) == 0:
            return

    articles = summarize_articles(candidates)

    payload = build_teams_payload(articles)
    post_to_teams(payload)

    # 送信済みIDを更新
    new_ids = sent_ids | {a["id"] for a in articles}
    # 直近5000件のみ保持（肥大化防止）
    if len(new_ids) > 5000:
        keep = sorted(new_ids)[-5000:]
        new_ids = set(keep)
    save_sent_ids(new_ids)
    print(f"[INFO] 送信済みID保存完了: {len(new_ids)} 件")


if __name__ == "__main__":
    main()
