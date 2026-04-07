#!/usr/bin/env python3
"""
compile.py - raw/ 시리즈 폴더 구조를 읽어 주제별 wiki 페이지로 컴파일

디렉토리 구조:
  raw/{시리즈-slug}/_meta.md       ← 시리즈 메타
  raw/{시리즈-slug}/{아티클-slug}.md ← 아티클

실행:
  uv run compile                              # 전체 재컴파일
  uv run compile --changed-files raw/시리즈/파일.md  # 변경된 파일만
"""

import argparse
import os
import sys
import time
from pathlib import Path

import frontmatter

sys.path.insert(0, str(Path(__file__).parent))
from categories import CATEGORY_SLUGS, get_display, get_slug, korean_slugify

RAW_DIR = Path(__file__).parent.parent / "raw"
WIKI_DIR = Path(__file__).parent.parent / "wiki"

COMPILE_PROMPT = """\
당신은 자연 치유 전문 편집자입니다.
아래는 naheal.org 필진이 작성한 '{series_title}' 시리즈의 아티클들입니다.

이 자료들을 바탕으로 '{series_title}' 시리즈의 종합 위키 페이지를 한국어로 작성하세요.

요구사항:
- 독자: 암 환자 및 자연 치유에 관심 있는 일반인
- 첫 줄: 이 시리즈의 핵심 키워드 8~12개를 쉼표로 나열 (예: 맨발걷기, 어싱, 접지, 자유전자, 활성산소, 자율신경계, 암치유)
- 구조: ## 개요, ## 주요 개념, ## 실천 방법, ## 주의사항, ## 출처 아티클 순서
- 출처 아티클 섹션에는 각 아티클의 제목, 저자, 게시일을 목록으로 표시
- 의학적 주장은 "~라고 알려져 있습니다", "~라는 체험 사례가 있습니다" 형식으로 신중하게 작성
- 분량: 800~1500자

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


SeriesGroup = dict  # {slug, title, articles: list[dict]}


def load_raw_files() -> dict[str, SeriesGroup]:
    """raw/ 서브폴더를 탐색해 시리즈 slug 기준으로 그룹화해 반환.

    반환값: {series_slug: {slug, title, articles: [...]}}
    구버전 호환: raw/*.md flat 파일은 seriesTitle 기준으로 그룹화.
    """
    groups: dict[str, SeriesGroup] = {}
    if not RAW_DIR.exists():
        return groups

    # 새 구조: raw/{series-folder}/*.md
    for series_dir in sorted(RAW_DIR.iterdir()):
        if not series_dir.is_dir():
            continue

        # 시리즈 메타 읽기 (_meta.md)
        meta_file = series_dir / "_meta.md"
        series_title = series_dir.name
        series_slug = korean_slugify(series_dir.name) or series_dir.name

        if meta_file.exists():
            try:
                meta = frontmatter.load(meta_file)
                series_title = meta.metadata.get("title", series_dir.name)
                series_slug = meta.metadata.get("seriesSlug") or korean_slugify(series_title) or series_dir.name
            except Exception as e:
                print(f"[경고] {meta_file} 읽기 실패: {e}")

        articles = []
        for md_file in sorted(series_dir.glob("*.md")):
            if md_file.name == "_meta.md":
                continue
            try:
                post = frontmatter.load(md_file)
                articles.append(
                    {
                        "title": post.metadata.get("title", md_file.stem),
                        "authorName": post.metadata.get("authorName", ""),
                        "publishedAt": post.metadata.get("publishedAt", ""),
                        "content": post.content,
                        "file": f"{series_dir.name}/{md_file.name}",
                    }
                )
            except Exception as e:
                print(f"[경고] {series_dir.name}/{md_file.name} 읽기 실패: {e}")

        if articles:
            groups[series_slug] = {
                "slug": series_slug,
                "title": series_title,
                "articles": articles,
            }

    # 구버전 호환: raw/*.md flat 파일 처리
    flat_articles: dict[str, list] = {}
    for md_file in sorted(RAW_DIR.glob("*.md")):
        try:
            post = frontmatter.load(md_file)
            series_title = post.metadata.get("seriesTitle", "일반")
            s_slug = korean_slugify(series_title) or "general"
            flat_articles.setdefault(s_slug, []).append(
                {
                    "title": post.metadata.get("title", "제목 없음"),
                    "authorName": post.metadata.get("authorName", ""),
                    "publishedAt": post.metadata.get("publishedAt", ""),
                    "content": post.content,
                    "file": md_file.name,
                }
            )
        except Exception as e:
            print(f"[경고] {md_file.name} 읽기 실패: {e}")

    for s_slug, arts in flat_articles.items():
        if s_slug not in groups:
            groups[s_slug] = {"slug": s_slug, "title": s_slug, "articles": arts}

    return groups


def _call_llm(prompt: str, model: dict, retries: int = 3) -> str:
    """provider에 따라 LLM 호출 (rate limit 시 재시도)"""
    provider = model["provider"]
    for attempt in range(retries):
        try:
            if provider == "anthropic":
                response = model["client"].messages.create(
                    model=model["name"],
                    max_tokens=4096,
                    messages=[{"role": "user", "content": prompt}],
                )
                return response.content[0].text
            else:  # gemini
                response = model["client"].generate_content(prompt)
                return response.text
        except Exception as e:
            if "rate_limit" in str(e).lower() or "429" in str(e):
                wait = 60 * (attempt + 1)
                print(f"    Rate limit — {wait}초 대기 후 재시도 ({attempt+1}/{retries})...")
                time.sleep(wait)
            else:
                raise
    raise RuntimeError("LLM 호출 실패: 재시도 횟수 초과")


MAX_ARTICLES_PER_SERIES = 20
CHARS_PER_ARTICLE = 2000


def compile_series(series_slug: str, series_title: str, articles: list[dict], model: dict) -> str:
    """시리즈 wiki 페이지 생성 후 파일에 저장, 요약(키워드 줄) 반환"""
    # order 순서대로, 최대 MAX_ARTICLES_PER_SERIES개
    selected = articles[:MAX_ARTICLES_PER_SERIES]

    articles_text = ""
    for i, a in enumerate(selected, 1):
        articles_text += f"### 아티클 {i}: {a['title']}\n"
        articles_text += f"저자: {a['authorName']} | 게시일: {a['publishedAt']}\n\n"
        articles_text += a["content"][:CHARS_PER_ARTICLE]
        articles_text += "\n\n---\n\n"

    prompt = COMPILE_PROMPT.format(series_title=series_title, articles_text=articles_text)

    print(f"  LLM 호출 ({model['provider']}): {series_slug} ({len(articles)}개 아티클)...")
    wiki_text = _call_llm(prompt, model)

    # 파일 헤더 추가
    header = f"---\nslug: {series_slug}\ntitle: {series_title}\nupdated: {_today()}\narticle_count: {len(articles)}\n---\n\n"
    WIKI_DIR.mkdir(exist_ok=True)
    wiki_file = WIKI_DIR / f"{series_slug}.md"
    wiki_file.write_text(header + wiki_text, encoding="utf-8")
    print(f"  ✓ wiki/{series_slug}.md 저장 ({len(wiki_text)}자)")

    # 요약: 헤딩 제외하고 첫 비어있지 않은 줄 (키워드 줄)
    for line in wiki_text.strip().split("\n"):
        line = line.strip()
        if line and not line.startswith("#") and not line.startswith("---"):
            return line[:80]
    return series_title


def _extract_first_sentence(wiki_file: Path) -> str:
    """wiki 파일에서 frontmatter 이후 첫 의미있는 문장 추출"""
    try:
        lines = wiki_file.read_text(encoding="utf-8").splitlines()
        # frontmatter 건너뛰기 (--- ... ---)
        in_frontmatter = False
        past_frontmatter = False
        for line in lines:
            stripped = line.strip()
            if stripped == "---":
                if not past_frontmatter:
                    in_frontmatter = not in_frontmatter
                    if not in_frontmatter:
                        past_frontmatter = True
                    continue
            if in_frontmatter:
                continue
            # frontmatter 이후 첫 비어있지 않은 내용 줄 (헤딩 제외)
            if stripped.startswith("#"):
                continue
            text = stripped.strip()
            if text and not text.startswith("|") and not text.startswith(">"):
                return text[:80]
    except Exception:
        pass
    return ""


def _get_series_title(slug: str) -> str:
    """wiki/{slug}.md frontmatter에서 title 추출"""
    wiki_file = WIKI_DIR / f"{slug}.md"
    if not wiki_file.exists():
        return slug
    try:
        post = frontmatter.load(wiki_file)
        return post.metadata.get("title", slug)
    except Exception:
        return slug


def update_index(slug_summaries: dict[str, str]):
    """wiki/INDEX.md 전체 갱신 (wiki/ 디렉토리의 모든 .md 파일 포함)"""
    existing: dict[str, str] = {}

    # wiki/ 디렉토리의 모든 slug.md 스캔
    if WIKI_DIR.exists():
        for wiki_file in WIKI_DIR.glob("*.md"):
            if wiki_file.name == "INDEX.md":
                continue
            slug = wiki_file.stem
            existing[slug] = _extract_first_sentence(wiki_file)

    # 새로 컴파일된 항목으로 요약 업데이트
    existing.update(slug_summaries)

    rows = ""
    for slug, summary in sorted(existing.items()):
        # 시리즈 제목: wiki 파일 frontmatter에서 읽기, 없으면 slug 사용
        title = _get_series_title(slug) or slug
        rows += f"| {title} | {slug}.md | {summary} |\n"

    WIKI_DIR.mkdir(exist_ok=True)
    (WIKI_DIR / "INDEX.md").write_text(INDEX_HEADER + rows, encoding="utf-8")
    print(f"  ✓ wiki/INDEX.md 갱신 ({len(existing)}개 시리즈)")


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
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="이미 wiki/ 에 파일이 있는 slug는 건너뜀",
    )
    args = parser.parse_args()

    # LLM 선택: ANTHROPIC_API_KEY 우선, 없으면 GEMINI_API_KEY
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    gemini_key = os.environ.get("GEMINI_API_KEY")

    if anthropic_key:
        import anthropic
        model = {
            "provider": "anthropic",
            "name": "claude-haiku-4-5-20251001",
            "client": anthropic.Anthropic(api_key=anthropic_key),
        }
        print("LLM: Claude Haiku (Anthropic)")
    elif gemini_key:
        import google.generativeai as genai
        genai.configure(api_key=gemini_key)
        model = {
            "provider": "gemini",
            "name": "gemini-2.0-flash",
            "client": genai.GenerativeModel("gemini-2.0-flash"),
        }
        print("LLM: Gemini Flash")
    else:
        print("[오류] ANTHROPIC_API_KEY 또는 GEMINI_API_KEY 환경변수가 필요합니다.")
        sys.exit(1)

    all_groups = load_raw_files()
    if not all_groups:
        print("[경고] raw/ 에 처리할 파일이 없습니다.")
        return

    # 재컴파일 대상 시리즈 slug 결정
    if args.changed_files:
        target_slugs: set[str] = set()
        for f in args.changed_files:
            changed_path = Path(f)
            # raw/{series-folder}/... → 시리즈 폴더명으로 slug 결정
            parts = changed_path.parts
            # "raw/시리즈폴더/파일.md" 형태에서 시리즈 폴더명 추출
            raw_idx = next((i for i, p in enumerate(parts) if p == "raw"), -1)
            if raw_idx >= 0 and len(parts) > raw_idx + 1:
                series_folder_name = parts[raw_idx + 1]
                s_slug = korean_slugify(series_folder_name) or series_folder_name
                if s_slug in all_groups:
                    target_slugs.add(s_slug)
                else:
                    # _meta.md 에서 seriesSlug 읽기
                    meta_file = RAW_DIR / series_folder_name / "_meta.md"
                    if meta_file.exists():
                        try:
                            meta = frontmatter.load(meta_file)
                            title = meta.metadata.get("title", series_folder_name)
                            s_slug = meta.metadata.get("seriesSlug") or korean_slugify(title)
                            target_slugs.add(s_slug)
                        except Exception:
                            pass
        print(f"Incremental 컴파일: {target_slugs}")
    else:
        target_slugs = set(all_groups.keys())
        print(f"전체 컴파일: {len(target_slugs)}개 시리즈")

    slug_summaries: dict[str, str] = {}
    slugs = sorted(target_slugs)
    for i, slug in enumerate(slugs):
        group = all_groups.get(slug)
        if not group:
            print(f"  건너뜀: {slug} (아티클 없음)")
            continue
        if args.skip_existing and (WIKI_DIR / f"{slug}.md").exists():
            print(f"  건너뜀: {slug} (이미 존재)")
            continue
        summary = compile_series(slug, group["title"], group["articles"], model)
        slug_summaries[slug] = summary
        # 시리즈 간 rate limit 방지 (마지막 제외)
        if i < len(slugs) - 1:
            time.sleep(5)

    update_index(slug_summaries)
    print("\n컴파일 완료!")


if __name__ == "__main__":
    main()
