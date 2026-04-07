#!/usr/bin/env python3
"""
export_firestore.py - Firestore 아티클을 raw/ 시리즈별 폴더 구조로 export

디렉토리 구조:
  raw/
    {시리즈-slug}/
      _meta.md          ← 시리즈 메타정보
      {아티클-slug}.md  ← 아티클 본문

실행:
  uv run python scripts/export_firestore.py \\
    --output raw/ \\
    --credentials ../naheal/naheal2-firebase-adminsdk-fbsvc-176972e8c5.json
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from categories import get_slug, korean_slugify

try:
    import firebase_admin
    from firebase_admin import credentials, firestore
except ImportError:
    print("[오류] firebase-admin 패키지가 필요합니다: uv add firebase-admin")
    sys.exit(1)

import yaml


def format_series_meta(series: dict, series_slug: str) -> str:
    """_meta.md 파일 내용 생성"""
    published_at = ""
    if series.get("publishedAt"):
        try:
            published_at = series["publishedAt"].strftime("%Y-%m-%d")
        except Exception:
            published_at = str(series.get("publishedAt", ""))

    category = series.get("category", "일반")
    topics = series.get("topics", [])

    metadata = {
        "seriesId": series.get("seriesId") or series.get("id", ""),
        "seriesSlug": series_slug,
        "title": series.get("title", ""),
        "description": series.get("description", ""),
        "category": category,
        "categorySlug": get_slug(category),
        "topics": topics if isinstance(topics, list) else [],
        "authorName": series.get("authorName", ""),
        "authorRole": series.get("authorRole", ""),
        "status": series.get("status", "published"),
        "publishedAt": published_at,
        "articleCount": series.get("articleCount", 0),
        "totalReadTime": series.get("totalReadTime", ""),
        "source": "firestore",
    }

    frontmatter = yaml.dump(metadata, allow_unicode=True, default_flow_style=False)
    overview = series.get("overview", series.get("description", ""))
    return f"---\n{frontmatter}---\n\n{overview}\n"


def format_article(article: dict, series: dict) -> str:
    """아티클 마크다운 파일 내용 생성"""
    published_at = ""
    if article.get("publishedAt"):
        try:
            published_at = article["publishedAt"].strftime("%Y-%m-%d")
        except Exception:
            published_at = str(article.get("publishedAt", ""))

    category = series.get("category", "일반")

    metadata = {
        "articleId": article.get("articleId") or article.get("id", ""),
        "seriesId": series.get("seriesId") or series.get("id", ""),
        "title": article.get("title", ""),
        "order": article.get("order", 0),
        "categorySlug": get_slug(category),
        "publishedAt": published_at,
        "readTime": article.get("readTime", ""),
        "source": "firestore",
    }

    frontmatter = yaml.dump(metadata, allow_unicode=True, default_flow_style=False)
    content = article.get("content", "")
    return f"---\n{frontmatter}---\n\n{content}\n"


def safe_filename(title: str, existing: set[str], max_len: int = 80) -> str:
    """중복 방지: 같은 slug가 있으면 suffix 추가"""
    base = korean_slugify(title, max_len)
    if not base:
        base = "untitled"
    slug = base
    counter = 1
    while slug in existing:
        slug = f"{base}-{counter}"
        counter += 1
    existing.add(slug)
    return slug


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="raw/", help="출력 디렉토리 (기본: raw/)")
    parser.add_argument(
        "--credentials",
        default="../naheal/naheal2-firebase-adminsdk-fbsvc-176972e8c5.json",
        help="Firebase 서비스 계정 JSON 경로",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="출력 디렉토리를 먼저 비우고 시작",
    )
    args = parser.parse_args()

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.clean:
        import shutil
        for item in output_dir.iterdir():
            if item.is_dir():
                shutil.rmtree(item)
            elif item.is_file() and item.name.endswith(".md"):
                item.unlink()
        print(f"[정리] {output_dir}/ 비움 완료")

    cred_path = Path(args.credentials)
    if not cred_path.exists():
        print(f"[오류] 서비스 계정 파일을 찾을 수 없습니다: {cred_path}")
        sys.exit(1)

    print(f"Firebase 초기화: {cred_path.name}")
    cred = credentials.Certificate(str(cred_path))
    firebase_admin.initialize_app(cred)
    db = firestore.client()

    print("Firestore에서 published 시리즈 목록 가져오는 중...")
    series_docs = db.collection("series").where("status", "==", "published").stream()

    total_series = 0
    total_articles = 0
    series_slugs_used: set[str] = set()

    for series_doc in series_docs:
        series_data = series_doc.to_dict()
        series_data["seriesId"] = series_doc.id
        total_series += 1

        # 시리즈 폴더명
        series_title = series_data.get("title", "제목없음")
        series_slug = safe_filename(series_title, series_slugs_used)
        series_dir = output_dir / series_slug
        series_dir.mkdir(exist_ok=True)

        # _meta.md 생성
        meta_content = format_series_meta(series_data, series_slug)
        (series_dir / "_meta.md").write_text(meta_content, encoding="utf-8")

        print(f"\n📂 {series_slug}/")
        print(f"   _meta.md (시리즈: {series_title})")

        # 아티클 가져오기
        articles = (
            db.collection("series")
            .document(series_doc.id)
            .collection("articles")
            .where("status", "==", "published")
            .order_by("order")
            .stream()
        )

        article_slugs_used: set[str] = set()

        for article_doc in articles:
            article_data = article_doc.to_dict()
            article_data["articleId"] = article_doc.id

            content = article_data.get("content", "")
            if len(content) < 50:
                print(f"   건너뜀 (내용 부족): {article_data.get('title', article_doc.id)}")
                continue

            # 아티클 파일명
            article_title = article_data.get("title", "제목없음")
            article_slug = safe_filename(article_title, article_slugs_used)
            article_file = series_dir / f"{article_slug}.md"

            file_content = format_article(article_data, series_data)
            article_file.write_text(file_content, encoding="utf-8")
            total_articles += 1

            order = article_data.get("order", "?")
            print(f"   {order:>2}. {article_slug}.md  ← {article_title}")

    print(f"\n✓ 완료: {total_series}개 시리즈, {total_articles}개 아티클 → {output_dir}/")
    print(f"   생성된 폴더: {', '.join(sorted(series_slugs_used))}")


if __name__ == "__main__":
    main()
