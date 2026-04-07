# category↔slug 매핑 (naheal Firestore category 값 기준)
# lib/rag/category-slugs.ts 와 동일하게 유지

CATEGORY_SLUGS: dict[str, str] = {
    "운동": "exercise",
    "영양": "nutrition",
    "명상": "meditation",
    "약초": "herbal",
    "체험기": "testimonials",
    "의학정보": "medical-info",
    "마음건강": "mental-health",
    "일반": "general",
}

SLUG_CATEGORIES: dict[str, str] = {v: k for k, v in CATEGORY_SLUGS.items()}

CATEGORY_DISPLAY: dict[str, str] = {
    "exercise": "맨발걷기/운동 치유",
    "nutrition": "식이 요법/영양 치유",
    "meditation": "명상과 마음 치유",
    "herbal": "약초/자연 요법",
    "testimonials": "체험기",
    "medical-info": "의학 정보",
    "mental-health": "마음 건강",
    "general": "자연 치유 일반",
}


def get_slug(category: str) -> str:
    return CATEGORY_SLUGS.get(category, "general")


def get_category(slug: str) -> str:
    return SLUG_CATEGORIES.get(slug, "일반")


def get_display(slug: str) -> str:
    return CATEGORY_DISPLAY.get(slug, slug)
