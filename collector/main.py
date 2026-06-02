"""
note.com 中小企業・IT系経営者記事収集 → Gmail配信
"""

import json
import os
import smtplib
import time
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import httpx

# ─── 設定 ────────────────────────────────────────────────────────────────────

TAGS = ["経営", "社長", "中小企業"]
ARTICLES_PER_TAG = 8          # タグごとの取得件数（重複除去後に10〜15件に絞る）
MAX_ARTICLES = 15              # 最終的に配信する最大件数
MIN_ARTICLES = 10              # 最低配信件数
BODY_PREVIEW_LEN = 100        # 本文冒頭の文字数

SENT_IDS_PATH = Path(__file__).parent.parent / "sent_ids.json"

NOTE_SEARCH_URL = "https://note.com/api/v2/searches"
GMAIL_ADDRESS = os.environ["GMAIL_ADDRESS"]         # 送信元 = 送信先（同じアドレス）
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]  # Googleアプリパスワード

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
            raw_body = a.get("body", "") or a.get("description", "") or ""
            preview = raw_body[:BODY_PREVIEW_LEN].strip()
            if len(raw_body) > BODY_PREVIEW_LEN:
                preview += "…"

            candidates.append({
                "id": unique_id,
                "title": a.get("name", "（タイトルなし）"),
                "author": user.get("nickname", user.get("urlname", "不明")),
                "url": f"https://note.com/{user.get('urlname', '')}/n/{key}" if key else "",
                "preview": preview,
                "published_at": a.get("publishAt", ""),
            })

    # 新しい順に並べて最大MAX_ARTICLES件
    candidates.sort(key=lambda x: x["published_at"], reverse=True)
    return candidates[:MAX_ARTICLES]


# ─── Gmail 配信 ───────────────────────────────────────────────────────────────

def build_email(articles: list[dict]) -> MIMEMultipart:
    """HTMLメールを構築して返す"""
    now_jst = datetime.now(JST).strftime("%Y年%m月%d日 %H:%M JST")
    subject = f"📰 note 経営・IT系記事まとめ（{datetime.now(JST).strftime('%Y/%m/%d')}）"

    # ── HTML本文 ──
    items_html = ""
    for i, a in enumerate(articles, 1):
        preview_html = a["preview"] if a["preview"] else "（本文プレビューなし）"
        items_html += f"""
        <div style="border:1px solid #e0e0e0;border-radius:8px;padding:16px;margin-bottom:16px;">
          <p style="margin:0 0 4px;font-size:16px;font-weight:bold;">
            {i}. <a href="{a['url']}" style="color:#1a73e8;text-decoration:none;">{a['title']}</a>
          </p>
          <p style="margin:0 0 8px;font-size:12px;color:#888;">著者：{a['author']}</p>
          <p style="margin:0;font-size:14px;color:#555;line-height:1.7;">{preview_html}</p>
        </div>"""

    html_body = f"""<!DOCTYPE html>
<html lang="ja">
<head><meta charset="utf-8"></head>
<body style="font-family:'Helvetica Neue',Arial,sans-serif;max-width:700px;margin:0 auto;padding:24px;color:#333;">
  <h2 style="border-bottom:2px solid #1a73e8;padding-bottom:8px;">
    📰 note 経営・IT系記事まとめ
  </h2>
  <p style="color:#666;font-size:13px;">{now_jst}　計 {len(articles)} 件</p>
  {items_html}
  <hr style="border:none;border-top:1px solid #eee;margin-top:32px;">
  <p style="font-size:11px;color:#aaa;">このメールはGitHub Actionsにより自動送信されました。</p>
</body>
</html>"""

    # ── テキスト本文（フォールバック） ──
    text_lines = [f"note 経営・IT系記事まとめ（{now_jst}）", ""]
    for i, a in enumerate(articles, 1):
        text_lines.append(f"{i}. {a['title']}")
        text_lines.append(f"   著者：{a['author']}")
        text_lines.append(f"   {a['preview']}")
        text_lines.append(f"   URL：{a['url']}")
        text_lines.append("")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = GMAIL_ADDRESS
    msg.attach(MIMEText("\n".join(text_lines), "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    return msg


def send_gmail(msg: MIMEMultipart) -> None:
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, GMAIL_ADDRESS, msg.as_string())
    print(f"[INFO] Gmail送信完了 → {GMAIL_ADDRESS}")


# ─── メイン ──────────────────────────────────────────────────────────────────

def main() -> None:
    print("[INFO] 記事収集開始")
    sent_ids = load_sent_ids()
    print(f"[INFO] 送信済みID数: {len(sent_ids)}")

    candidates = collect_candidates(sent_ids)
    print(f"[INFO] 未送信候補: {len(candidates)} 件")

    if len(candidates) == 0:
        print("[WARN] 配信対象の記事がありません。終了します。")
        return

    msg = build_email(candidates)
    send_gmail(msg)

    # 送信済みIDを更新
    new_ids = sent_ids | {a["id"] for a in candidates}
    # 直近5000件のみ保持（肥大化防止）
    if len(new_ids) > 5000:
        new_ids = set(sorted(new_ids)[-5000:])
    save_sent_ids(new_ids)
    print(f"[INFO] 送信済みID保存完了: {len(new_ids)} 件")


if __name__ == "__main__":
    main()
