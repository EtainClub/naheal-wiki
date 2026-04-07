#!/usr/bin/env python3
"""
compile.py - raw/ 마크다운 파일을 주제별 wiki 페이지로 컴파일

실행:
  uv run compile                                      # 전체 재컴파일
  uv run compile --changed-files raw/abc.md raw/xyz.md  # 변경된 파일만
"""

import argparse
import os
import sys
from pathlib import Path

import frontmatter
import google.generativeai as genai

sys.path.insert(0, str(Path(__file__).parent))
from categories import CATEGORY_SLUGS, get_display, get_slug

RAW_DIR = Path(__file__).parent.parent / "raw"
WIKI_DIR = Path(__file__).parent.parent / "wiki"

COMPILE_PROMPT = """\
당신은 자연 치유 전문 편집자입니다.
아래는 naheal.org 필진이 작성한 '{display_name}' 관련 아티클들입니다.

이 자료들을 바탕으로 '{display_name}' 주제의 종합 위키 페이지를 한국어로 작성하세요.

요구사항:
- 독자: 암 환자 및 자연 치유에 관심 있는 일반인
- 구조: ## 개요, ## 주요 개념, ## 실천 방법, ## 주의사항, ## 출처 아티클 순서
- 출처 아티클 섹션에는 각 아티클의 제목, 시리즈명, 저자, 게시일을 목록으로 표시
- 의학적 주장은 "~라고 알려져 있습니다", "~라는 체험 사례가 있습니다" 형식으로 신중하게 작성
- 분량: 1000~2000자

---

{articles_text}
"""

INDEX_HEADER = """\
# 자연치유 위키 인덱스

naheal.org 필진이 작성한 자연 치유 지식을 LLM이 주제별로 정리한 위키입니다.

> **면책 고지**: 이 위키의 내용은 정보 제공 목적이며 의학적 조언을 대체하지 않습니다.
> 건강 관련 결정 전 반드시 전문 의료인과 상담하세요.

## 주제 목록

| 주제 | 파일 | 요약 |
|------|------|------|
"""


def load_raw_files() -> dict[str, list[dict]]:
    """raw/ 파일들을 category slug 기준으로 그룹화해 반환"""
    groups: dict[str, list[dict]] = {}
    if not RAW_DIR.exists():
        return groups
    for md_file in sorted(RAW_DIR.glob("*.md")):
        try:
            post = frontmatter.load(md_file)
            slug = post.metadata.get("categorySlug") or get_slug(
                post.metadata.get("category", "일반")
            )
            groups.setdefault(slug, []).append(
                {
                    "slug": slug,
                    "title": post.metadata.get("title", "제목 없음"),
                    "seriesTitle": post.metadata.get("seriesTitle", ""),
                    "authorName": post.metadata.get("authorName", ""),
                    "publishedAt": post.metadata.get("publishedAt", ""),
                    "content": post.content,
                    "file": md_file.name,
                }
            )
        except Exception as e:
            print(f"[경고] {md_file.name} 읽기 실패: {e}")
    return groups


def compile_category(slug: str, articles: list[dict], model) -> str:
    """slug에 해당하는 wiki 페이지 생성 후 파일에 저장, 요약 반환"""
    display = get_display(slug)
    articles_text = ""
    for i, a in enumerate(articles, 1):
        articles_text += f"### 아티클 {i}: {a['title']}\n"
        articles_text += f"시리즈: {a['seriesTitle']} | 저자: {a['authorName']} | 게시일: {a['publishedAt']}\n\n"
        articles_text += a["content"][:4000]  # 토큰 절약을 위해 앞 4000자만
        articles_text += "\n\n---\n\n"

    prompt = COMPILE_PROMPT.format(display_name=display, articles_text=articles_text)

    print(f"  Gemini 호출: {slug} ({len(articles)}개 아티클)...")
    response = model.generate_content(prompt)
    wiki_text = response.text

    # 파일 헤더 추가
    header = f"---\nslug: {slug}\ncategory: {display}\nupdated: {_today()}\narticle_count: {len(articles)}\n---\n\n"
    WIKI_DIR.mkdir(exist_ok=True)
    wiki_file = WIKI_DIR / f"{slug}.md"
    wiki_file.write_text(header + wiki_text, encoding="utf-8")
    print(f"  ✓ wiki/{slug}.md 저장 ({len(wiki_text)}자)")

    # 요약 (첫 문장 추출)
    first_line = wiki_text.strip().split("\n")
    for line in first_line:
        line = line.strip().lstrip("#").strip()
        if line and not line.startswith("---"):
            return line[:60]
    return display


def update_index(slug_summaries: dict[str, str]):
    """wiki/INDEX.md 전체 갱신"""
    # 기존 INDEX.md에서 slug_summaries에 없는 항목도 유지
    existing: dict[str, str] = {}
    index_file = WIKI_DIR / "INDEX.md"
    if index_file.exists():
        for line in index_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("|") and ".md" in line:
                parts = [p.strip() for p in line.split("|")]
                if len(parts) >= 4 and parts[2].endswith(".md"):
                    slug = parts[2].replace(".md", "")
                    summary = parts[3] if len(parts) > 3 else ""
                    existing[slug] = summary

    # 업데이트
    existing.update(slug_summaries)

    rows = ""
    for slug, summary in sorted(existing.items()):
        display = get_display(slug)
        rows += f"| {display} | {slug}.md | {summary} |\n"

    WIKI_DIR.mkdir(exist_ok=True)
    index_file.write_text(INDEX_HEADER + rows, encoding="utf-8")
    print(f"  ✓ wiki/INDEX.md 갱신 ({len(existing)}개 주제)")


def _today() -> str:
    from datetime import date
    return date.today().isoformat()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--changed-files",
        nargs="*",
        help="변경된 raw/ 파일 경로 목록 (없으면 전체 재컴파일)",
    )
    args = parser.parse_args()

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("[오류] GEMINI_API_KEY 환경변수가 설정되지 않았습니다.")
        sys.exit(1)

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-2.0-flash")

    all_groups = load_raw_files()
    if not all_groups:
        print("[경고] raw/ 에 처리할 파일이 없습니다.")
        return

    # 재컴파일 대상 slug 결정
    if args.changed_files:
        target_slugs: set[str] = set()
        for f in args.changed_files:
            fname = Path(f).name
            raw_file = RAW_DIR / fname
            if raw_file.exists():
                try:
                    post = frontmatter.load(raw_file)
                    slug = post.metadata.get("categorySlug") or get_slug(
                        post.metadata.get("category", "일반")
                    )
                    target_slugs.add(slug)
                except Exception:
                    pass
        print(f"Incremental 컴파일: {target_slugs}")
    else:
        target_slugs = set(all_groups.keys())
        print(f"전체 컴파일: {len(target_slugs)}개 카테고리")

    slug_summaries: dict[str, str] = {}
    for slug in sorted(target_slugs):
        articles = all_groups.get(slug, [])
        if not articles:
            print(f"  건너뜀: {slug} (아티클 없음)")
            continue
        summary = compile_category(slug, articles, model)
        slug_summaries[slug] = summary

    update_index(slug_summaries)
    print("\n컴파일 완료!")


if __name__ == "__main__":
    main()
