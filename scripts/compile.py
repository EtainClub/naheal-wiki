#!/usr/bin/env python3
"""
compile.py - Karpathy LLM Wiki 패턴에 따른 3-pass 위키 컴파일

디렉토리 구조:
  raw/{시리즈-slug}/_meta.md       ← 시리즈 메타
  raw/{시리즈-slug}/{아티클-slug}.md ← 아티클

출력:
  wiki/{시리즈-slug}.md            ← 시리즈 페이지 (크로스레퍼런스 포함)
  wiki/concepts/{개념-slug}.md     ← 개념 페이지 (여러 시리즈에 걸친 주제)
  wiki/INDEX.md                   ← 카테고리별 인덱스
  wiki/log.md                     ← 컴파일 로그

3-Pass 컴파일:
  Pass 1: 시리즈 페이지 생성 (다른 시리즈 목록을 프롬프트에 포함 → [[wikilinks]])
  Pass 2: 개념 페이지 생성 (키워드 분석 → 크로스커팅 개념 추출)
  Pass 3: 역링크 감사 (orphan 페이지, 깨진 링크 경고)

실행:
  uv run compile                              # 전체 재컴파일
  uv run compile --changed-files raw/시리즈/파일.md  # 변경된 파일만
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import date
from pathlib import Path

import frontmatter

sys.path.insert(0, str(Path(__file__).parent))
from categories import CATEGORY_SLUGS, get_display, get_slug, korean_slugify

RAW_DIR = Path(__file__).parent.parent / "raw"
WIKI_DIR = Path(__file__).parent.parent / "wiki"
CONCEPTS_DIR = WIKI_DIR / "concepts"

# ──────────────────────── Prompts ────────────────────────

COMPILE_PROMPT = """\
당신은 자연 치유 위키 편집자입니다.
'{series_title}' 시리즈의 아티클을 바탕으로 위키 페이지를 작성하세요.

요구사항:
- 한국어, 독자: 암 환자 및 자연 치유 관심 일반인
- 첫 줄: 핵심 키워드 8~12개 쉼표로 나열
- 구조는 내용 성격에 맞게 자유롭게 (고정 템플릿 없음)
- 분량: 800~1500자
- 의학적 주장은 "~라고 알려져 있습니다" 형식으로 신중하게

**크로스 레퍼런스 (매우 중요)**:
아래 위키 페이지 목록 중 내용적으로 관련된 주제가 있으면
본문에서 자연스럽게 [[슬러그]] 형식으로 링크하세요.
예: "활성산소에 대해서는 [[맨발과-어싱]]에서 자세히 다룹니다"

마지막에 반드시 아래 두 섹션을 추가하세요:

## 출처 아티클
각 아티클의 제목과 게시일을 목록으로 표시

## 관련 페이지
관련된 다른 위키 페이지를 [[슬러그]] — 한줄설명 형식으로 나열
최소 2개 이상의 관련 페이지를 포함하세요

현재 위키의 다른 페이지들:
{other_pages_list}

---

{articles_text}
"""

CONCEPT_EXTRACT_PROMPT = """\
아래는 자연치유 위키의 시리즈 페이지들입니다.
각 페이지의 슬러그, 제목, 키워드가 나열되어 있습니다.

이 중에서 **2개 이상의 시리즈 페이지에 공통으로 등장하는 핵심 개념** 5~8개를 선택하세요.

선택 기준:
- 여러 시리즈에서 주요하게 다뤄지는 개념만 선택
- 독자가 독립적으로 궁금해할 만한 주제
- 너무 광범위한 개념(예: "건강", "치유")은 제외

반드시 아래 JSON 배열 형식으로만 응답하세요:
[
  {{"slug": "활성산소", "title": "활성산소", "related_pages": ["맨발과-어싱", "Cancer-Step-Outside-the-Box"]}},
  ...
]

---

{all_pages_info}
"""

CONCEPT_PAGE_PROMPT = """\
'{concept_title}' 개념 페이지를 작성하세요.

이 개념은 다음 위키 페이지들에서 다뤄집니다:
{related_pages_info}

요구사항:
- 300~500자의 종합 설명
- 각 관련 페이지에서 이 개념이 어떻게 다뤄지는지 한 문장씩 요약
- 모든 관련 페이지를 [[슬러그]] 형식으로 링크
- 의학적 주장은 "~라고 알려져 있습니다" 형식으로 신중하게

구조:
첫 줄: 한 문장 정의
본문: 종합 설명 + 각 시리즈에서의 관점
마지막: ## 관련 페이지 (관련 시리즈 페이지 [[링크]] 목록)
"""

INDEX_HEADER = """\
# 자연치유 위키 인덱스

naheal.org 필진의 자연 치유 지식을 LLM이 주제별로 정리한 위키입니다.

> **면책 고지**: 정보 제공 목적이며 의학적 조언을 대체하지 않습니다.
> 건강 관련 결정 전 반드시 전문 의료인과 상담하세요.

"""

# ──────────────────────── Types ────────────────────────

SeriesGroup = dict  # {slug, title, category, description, articles: list[dict]}

# ──────────────────────── Helpers ────────────────────────


def _today() -> str:
    return date.today().isoformat()


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


# ──────────────────────── Load ────────────────────────


def load_raw_files() -> dict[str, SeriesGroup]:
    """raw/ 서브폴더를 탐색해 시리즈 slug 기준으로 그룹화해 반환."""
    groups: dict[str, SeriesGroup] = {}
    if not RAW_DIR.exists():
        return groups

    for series_dir in sorted(RAW_DIR.iterdir()):
        if not series_dir.is_dir():
            continue

        meta_file = series_dir / "_meta.md"
        series_title = series_dir.name
        series_slug = korean_slugify(series_dir.name) or series_dir.name
        category = "일반"
        description = ""

        if meta_file.exists():
            try:
                meta = frontmatter.load(meta_file)
                series_title = meta.metadata.get("title", series_dir.name)
                series_slug = (
                    meta.metadata.get("seriesSlug")
                    or korean_slugify(series_title)
                    or series_dir.name
                )
                category = meta.metadata.get("category", "일반")
                description = meta.metadata.get("description", "")
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
                "category": category,
                "description": description[:100],
                "articles": articles,
            }

    return groups


# ──────────────────── Pass 1: Series Pages ────────────────────

MAX_ARTICLES_PER_SERIES = 20
CHARS_PER_ARTICLE = 2000


def compile_series(
    series_slug: str,
    series_title: str,
    articles: list[dict],
    other_pages: list[dict],
    model: dict,
) -> str:
    """시리즈 wiki 페이지 생성 후 파일에 저장, 키워드 줄 반환"""
    selected = articles[:MAX_ARTICLES_PER_SERIES]

    articles_text = ""
    for i, a in enumerate(selected, 1):
        articles_text += f"### 아티클 {i}: {a['title']}\n"
        articles_text += f"저자: {a['authorName']} | 게시일: {a['publishedAt']}\n\n"
        articles_text += a["content"][:CHARS_PER_ARTICLE]
        articles_text += "\n\n---\n\n"

    # 다른 페이지 목록 생성 (현재 시리즈 제외)
    other_pages_list = "\n".join(
        f"- [[{p['slug']}]]: {p['title']} — {p['description']}"
        for p in other_pages
        if p["slug"] != series_slug
    )
    if not other_pages_list:
        other_pages_list = "(아직 다른 페이지가 없습니다)"

    prompt = COMPILE_PROMPT.format(
        series_title=series_title,
        articles_text=articles_text,
        other_pages_list=other_pages_list,
    )

    print(f"  LLM 호출 ({model['provider']}): {series_slug} ({len(articles)}개 아티클)...")
    wiki_text = _call_llm(prompt, model)

    # frontmatter + 본문 저장
    header = (
        f"---\nslug: {series_slug}\ntitle: {series_title}\n"
        f"type: series\nupdated: {_today()}\n"
        f"article_count: {len(articles)}\n---\n\n"
    )
    WIKI_DIR.mkdir(exist_ok=True)
    wiki_file = WIKI_DIR / f"{series_slug}.md"
    wiki_file.write_text(header + wiki_text, encoding="utf-8")
    print(f"  ✓ wiki/{series_slug}.md 저장 ({len(wiki_text)}자)")

    # 키워드 줄 추출 (첫 비어있지 않은 비-헤딩 줄)
    for line in wiki_text.strip().split("\n"):
        line = line.strip()
        if line and not line.startswith("#") and not line.startswith("---"):
            return line[:120]
    return series_title


# ──────────────── Pass 2: Concept Pages ────────────────


def _extract_keywords(wiki_file: Path) -> str:
    """wiki 파일에서 키워드 줄(첫 비-헤딩 줄) 추출"""
    try:
        post = frontmatter.load(wiki_file)
        for line in post.content.strip().split("\n"):
            line = line.strip()
            if line and not line.startswith("#") and not line.startswith("---"):
                return line[:150]
    except Exception:
        pass
    return ""


def generate_concept_pages(
    all_groups: dict[str, SeriesGroup],
    model: dict,
) -> list[dict]:
    """여러 시리즈에 걸친 핵심 개념 추출 → 개념 페이지 생성"""

    # 모든 시리즈 페이지의 키워드 수집
    all_pages_info = ""
    for slug, group in sorted(all_groups.items()):
        wiki_file = WIKI_DIR / f"{slug}.md"
        keywords = _extract_keywords(wiki_file) if wiki_file.exists() else ""
        all_pages_info += f"- [[{slug}]]: {group['title']}\n  키워드: {keywords}\n"

    # LLM에게 개념 추출 요청
    prompt = CONCEPT_EXTRACT_PROMPT.format(all_pages_info=all_pages_info)
    print("\n[Pass 2] 개념 추출 중...")
    result_text = _call_llm(prompt, model)

    # JSON 파싱
    json_match = re.search(r"\[[\s\S]*\]", result_text)
    if not json_match:
        print("  [경고] 개념 추출 JSON 파싱 실패")
        return []

    try:
        concepts = json.loads(json_match.group())
    except json.JSONDecodeError:
        print("  [경고] 개념 추출 JSON 디코드 실패")
        return []

    CONCEPTS_DIR.mkdir(parents=True, exist_ok=True)
    generated = []

    for i, concept in enumerate(concepts):
        c_slug = korean_slugify(concept.get("slug", "")) or f"concept-{i}"
        c_title = concept.get("title", c_slug)
        related = concept.get("related_pages", [])

        # 관련 시리즈 페이지 정보 수집
        related_info = ""
        for r_slug in related:
            group = all_groups.get(r_slug)
            if group:
                wiki_file = WIKI_DIR / f"{r_slug}.md"
                kw = _extract_keywords(wiki_file) if wiki_file.exists() else ""
                related_info += f"- [[{r_slug}]]: {group['title']} — {kw}\n"

        if not related_info:
            continue

        prompt = CONCEPT_PAGE_PROMPT.format(
            concept_title=c_title,
            related_pages_info=related_info,
        )

        print(f"  LLM 호출: 개념 페이지 '{c_title}'...")
        concept_text = _call_llm(prompt, model)

        # 저장
        header = (
            f"---\nslug: concepts/{c_slug}\ntitle: {c_title}\n"
            f"type: concept\nupdated: {_today()}\n"
            f"related: {json.dumps(related, ensure_ascii=False)}\n---\n\n"
        )
        concept_file = CONCEPTS_DIR / f"{c_slug}.md"
        concept_file.write_text(header + concept_text, encoding="utf-8")
        print(f"  ✓ wiki/concepts/{c_slug}.md 저장")

        generated.append(
            {"slug": f"concepts/{c_slug}", "title": c_title, "related": related}
        )

        # rate limit 방지
        if i < len(concepts) - 1:
            time.sleep(3)

    print(f"  ✓ {len(generated)}개 개념 페이지 생성 완료")
    return generated


# ──────────────── Pass 3: Backlink Audit ────────────────


def audit_backlinks():
    """모든 위키 페이지의 [[wikilinks]]를 검사하여 경고 출력"""
    print("\n[Pass 3] 역링크 감사...")

    # 존재하는 모든 slug 수집
    valid_slugs: set[str] = set()
    if WIKI_DIR.exists():
        for f in WIKI_DIR.glob("*.md"):
            if f.name in ("INDEX.md", "SCHEMA.md", "log.md"):
                continue
            valid_slugs.add(f.stem)
    if CONCEPTS_DIR.exists():
        for f in CONCEPTS_DIR.glob("*.md"):
            valid_slugs.add(f"concepts/{f.stem}")

    # 각 페이지의 [[links]] 추출 및 검증
    broken_links: list[str] = []
    link_counts: dict[str, int] = {s: 0 for s in valid_slugs}

    all_files = list(WIKI_DIR.glob("*.md")) + list(CONCEPTS_DIR.glob("*.md")) if CONCEPTS_DIR.exists() else list(WIKI_DIR.glob("*.md"))

    for wiki_file in all_files:
        if wiki_file.name in ("INDEX.md", "SCHEMA.md", "log.md"):
            continue
        try:
            content = wiki_file.read_text(encoding="utf-8")
            links = re.findall(r"\[\[([^\]]+)\]\]", content)
            for link in links:
                if link in valid_slugs:
                    link_counts[link] = link_counts.get(link, 0) + 1
                else:
                    broken_links.append(f"  ⚠ {wiki_file.name}: [[{link}]] → 존재하지 않는 페이지")
        except Exception:
            pass

    # 경고 출력
    if broken_links:
        print(f"  깨진 링크 {len(broken_links)}개:")
        for msg in broken_links[:10]:
            print(msg)

    orphans = [s for s, c in link_counts.items() if c == 0 and not s.startswith("concepts/")]
    if orphans:
        print(f"  고아 페이지 (아무도 링크하지 않음): {', '.join(orphans)}")

    if not broken_links and not orphans:
        print("  ✓ 모든 링크 정상")


# ──────────────── INDEX.md ────────────────


def _get_page_meta(file_path: Path) -> dict:
    """wiki 파일에서 frontmatter 메타데이터 추출"""
    try:
        post = frontmatter.load(file_path)
        return dict(post.metadata)
    except Exception:
        return {}


def update_index(
    all_groups: dict[str, SeriesGroup],
    concept_pages: list[dict],
):
    """wiki/INDEX.md를 카테고리별로 재생성"""
    WIKI_DIR.mkdir(exist_ok=True)

    # 시리즈 페이지를 카테고리별로 그룹화
    categorized: dict[str, list[dict]] = {}
    for slug, group in sorted(all_groups.items()):
        wiki_file = WIKI_DIR / f"{slug}.md"
        if not wiki_file.exists():
            continue
        cat = group.get("category", "일반")
        cat_display = get_display(get_slug(cat))
        keywords = _extract_keywords(wiki_file)
        categorized.setdefault(cat_display, []).append(
            {"slug": slug, "title": group["title"], "keywords": keywords}
        )

    # INDEX.md 작성
    content = INDEX_HEADER

    content += "## 시리즈 페이지\n\n"
    for cat_name, pages in sorted(categorized.items()):
        content += f"### {cat_name}\n\n"
        content += "| 주제 | 파일 | 키워드 |\n"
        content += "|------|------|--------|\n"
        for p in pages:
            content += f"| {p['title']} | [[{p['slug']}]] | {p['keywords'][:60]} |\n"
        content += "\n"

    if concept_pages:
        content += "## 개념 페이지\n\n"
        content += "여러 시리즈에 걸쳐 등장하는 핵심 개념입니다.\n\n"
        content += "| 개념 | 파일 | 관련 시리즈 |\n"
        content += "|------|------|------------|\n"
        for cp in concept_pages:
            related_str = ", ".join(cp["related"][:4])
            content += f"| {cp['title']} | [[{cp['slug']}]] | {related_str} |\n"
        content += "\n"

    (WIKI_DIR / "INDEX.md").write_text(content, encoding="utf-8")
    total = sum(len(v) for v in categorized.values()) + len(concept_pages)
    print(f"  ✓ wiki/INDEX.md 갱신 ({total}개 페이지)")


# ──────────────── Log ────────────────


def append_log(message: str):
    """wiki/log.md에 컴파일 기록 추가"""
    log_file = WIKI_DIR / "log.md"
    if not log_file.exists():
        log_file.write_text("# 위키 컴파일 로그\n\n", encoding="utf-8")

    entry = f"## [{_today()}] {message}\n\n"
    existing = log_file.read_text(encoding="utf-8")
    log_file.write_text(existing + entry, encoding="utf-8")


# ──────────────── Main ────────────────


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

    # LLM 선택
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

    # 재컴파일 대상 결정
    if args.changed_files:
        target_slugs: set[str] = set()
        for f in args.changed_files:
            changed_path = Path(f)
            parts = changed_path.parts
            raw_idx = next((i for i, p in enumerate(parts) if p == "raw"), -1)
            if raw_idx >= 0 and len(parts) > raw_idx + 1:
                series_folder_name = parts[raw_idx + 1]
                s_slug = korean_slugify(series_folder_name) or series_folder_name
                if s_slug in all_groups:
                    target_slugs.add(s_slug)
                else:
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

    # ─── Pass 1: 시리즈 페이지 생성 ───
    print("\n[Pass 1] 시리즈 페이지 컴파일...")

    # 다른 페이지 정보 목록 생성 (크로스레퍼런스용)
    other_pages = [
        {"slug": g["slug"], "title": g["title"], "description": g.get("description", "")}
        for g in all_groups.values()
    ]

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
        summary = compile_series(
            slug, group["title"], group["articles"], other_pages, model
        )
        slug_summaries[slug] = summary
        if i < len(slugs) - 1:
            time.sleep(5)

    # ─── Pass 2: 개념 페이지 생성 ───
    concept_pages = generate_concept_pages(all_groups, model)

    # ─── Pass 3: 역링크 감사 ───
    audit_backlinks()

    # ─── INDEX.md 갱신 ───
    print()
    update_index(all_groups, concept_pages)

    # ─── 로그 기록 ───
    log_msg = (
        f"compile | {len(slug_summaries)}개 시리즈 컴파일, "
        f"{len(concept_pages)}개 개념 페이지 생성"
    )
    append_log(log_msg)

    print(f"\n✅ 컴파일 완료! ({len(slug_summaries)} 시리즈 + {len(concept_pages)} 개념)")


if __name__ == "__main__":
    main()
