# 나힐생활 위키 스키마

Karpathy LLM Wiki 패턴에 따라 구성된 자연치유 지식 베이스입니다.

## 3-Layer 아키텍처

- **raw/** — 불변 소스 문서 (Firestore에서 export된 아티클)
- **wiki/** — LLM이 생성/관리하는 인터링크 위키 페이지
- **SCHEMA.md** — 이 파일. 위키 구조, 규칙, 워크플로우 정의

## 페이지 유형

### 시리즈 페이지 (`wiki/*.md`)
- raw/ 시리즈 폴더의 아티클을 종합한 주제 요약
- frontmatter: `type: series`, `slug`, `title`, `updated`, `article_count`
- 다른 시리즈 페이지를 `[[슬러그]]` 형식으로 크로스레퍼런스
- 하단에 `## 관련 페이지` 섹션 필수

### 개념 페이지 (`wiki/concepts/*.md`)
- 여러 시리즈에 걸쳐 등장하는 핵심 개념
- frontmatter: `type: concept`, `slug`, `title`, `updated`, `related`
- 300~500자의 짧은 종합 설명
- 관련 시리즈 페이지를 `[[슬러그]]` 형식으로 링크

## 특수 파일

- `INDEX.md` — 카테고리별 주제 카탈로그 (컴파일 시 자동 생성)
- `log.md` — 시간순 컴파일 기록 (append-only)
- `SCHEMA.md` — 이 파일

## 규칙

1. 모든 페이지는 YAML frontmatter 포함
2. 관련 페이지 참조 시 `[[슬러그]]` 형식 사용
3. 각 페이지 하단에 `## 관련 페이지` 섹션 필수
4. 의학적 주장은 "~라고 알려져 있습니다" 형태로 신중 표현
5. 중복 방지: 동일 정보는 한 곳에만, 나머지는 `[[링크]]`로 참조

## 워크플로우

### 인제스트 (Ingest)
1. 새 아티클이 Firestore에 게시됨
2. `github-pusher.ts`가 raw/ 에 push
3. GitHub Actions가 `compile.py` 실행
4. Pass 1: 시리즈 페이지 생성 (크로스레퍼런스 포함)
5. Pass 2: 개념 페이지 생성/업데이트
6. Pass 3: 역링크 감사
7. INDEX.md, log.md 갱신

### 쿼리 (Query)
1. 사용자가 질문 → `/api/chat`
2. INDEX.md를 읽고 관련 페이지 2개 선택 (Haiku)
3. 선택된 페이지의 핵심 내용(extractEssence) 추출
4. 시스템 프롬프트에 컨텍스트로 제공
5. 간결한 답변 생성 (3~5문장)
