#!/usr/bin/env python3
"""
export_firestore.py - 기존 Firestore 아티클을 raw/ 마크다운으로 일괄 export

실행:
  python scripts/export_firestore.py --output raw/ --credentials ../naheal/naheal2-firebase-adminsdk-fbsvc-176972e8c5.json
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from categories import get_slug

try:
    import firebase_admin
    from firebase_admin import credentials, firestore
except ImportError:
    print("[오류] firebase-admin 패키지가 필요합니다: pip install firebase-admin")
    sys.exit(1)

import yaml


def slugify(text: str) -> str:
    """파일명으로 사용 불가한 문자 제거"""
    return text.strip().replace("/", "-").replace(" ", "-")[:80]


def format_raw_file(article: dict, series: dict) -> str:
    """YAML frontmatter + 마크다운 본문 생성"""
    published_at = ""
    if article.get("publishedAt"):
        try:
            published_at = article["publishedAt"].strftime("%Y-%m-%d")
        except Exception:
            published_at = str(article.get("publishedAt", ""))

    category = series.get("category", "일반")
    topics = series.get("topics", [])

    metadata = {
        "articleId": article.get("articleId") or article.get("id", ""),
        "seriesId": series.get("seriesId") or series.get("id", ""),
        "title": article.get("title", ""),
        "seriesTitle": series.get("title", ""),
        "category": category,
        "categorySlug": get_slug(category),
        "topics": topics if isinstance(topics, list) else [],
        "authorName": series.get("authorName", ""),
        "publishedAt": published_at,
        "source": "firestore",
    }

    frontmatter = yaml.dump(metadata, allow_unicode=True, default_flow_style=False)
    content = article.get("content", "")
    return f"---\n{frontmatter}---\n\n{content}\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="raw/", help="출력 디렉토리")
    parser.add_argument(
        "--credentials",
        default="../naheal/naheal2-firebase-adminsdk-fbsvc-176972e8c5.json",
        help="Firebase 서비스 계정 JSON 경로",
    )
    args = parser.parse_args()

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    cred_path = Path(args.credentials)
    if not cred_path.exists():
        print(f"[오류] 서비스 계정 파일을 찾을 수 없습니다: {cred_path}")
        sys.exit(1)

    print(f"Firebase 초기화: {cred_path}")
    cred = credentials.Certificate(str(cred_path))
    firebase_admin.initialize_app(cred)
    db = firestore.client()

    print("Firestore에서 시리즈 목록 가져오는 중...")
    series_docs = db.collection("series").where("status", "==", "published").stream()

    total = 0
    series_count = 0

    for series_doc in series_docs:
        series_data = series_doc.to_dict()
        series_data["seriesId"] = series_doc.id
        series_count += 1

        articles = (
            db.collection("series")
            .document(series_doc.id)
            .collection("articles")
            .where("status", "==", "published")
            .stream()
        )

        for article_doc in articles:
            article_data = article_doc.to_dict()
            article_data["articleId"] = article_doc.id

            content = article_data.get("content", "")
            if len(content) < 50:
                print(f"  건너뜀 (내용 부족): {article_data.get('title', article_doc.id)}")
                continue

            raw_content = format_raw_file(article_data, series_data)
            output_file = output_dir / f"{article_doc.id}.md"
            output_file.write_text(raw_content, encoding="utf-8")
            total += 1

            print(f"  ✓ {article_doc.id}.md — {article_data.get('title', '')}")

    print(f"\n완료: {series_count}개 시리즈, {total}개 아티클 → {output_dir}/")


if __name__ == "__main__":
    main()
