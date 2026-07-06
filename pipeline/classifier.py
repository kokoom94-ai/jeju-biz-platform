"""규칙 기반 분류 엔진 (LLM 0회 호출).

제주 공공기관 공고 어휘가 정형화되어 있어 키워드 매칭으로 실용 정확도 확보.
- company_types: 신청 가능 주체 태그 (다중)
- biz_types: 지원 형태 태그 (다중)
- 대상 조건: 연령/성별/지역 추출
- 마감일: 정규식 추출
confidence < 0.6 건은 needs_review=true 로 표시 → 수동 확인 또는 향후 LLM 보조.
"""
from __future__ import annotations
import re
from datetime import date

COMPANY_RULES: dict[str, list[str]] = {
    "small_biz":    ["소상공인", "자영업", "골목상권", "소기업"],
    "youth":        ["청년", "만39세", "만 39세", "19세 이상 39세", "청년창업"],
    "social_ent":   ["사회적기업", "예비사회적기업", "사회적경제기업", "소셜벤처"],
    "self_support": ["자활기업", "자활근로", "자활센터", "자활사업"],
    "coop":         ["협동조합", "마을기업", "공동체"],
    "welfare_org":  ["복지시설", "복지법인", "사회복지법인", "이용시설", "생활시설", "복지관"],
    "women":        ["여성기업", "여성가장", "경력단절", "경단녀", "여성창업", "여성 대표"],
    "midlife":      ["중장년", "신중년", "40세 이상", "4050", "장년층", "만 40세"],
    "nonprofit":    ["비영리", "민간단체", "도민 단체", "NGO", "NPO", "법인·단체"],
    "startup":      ["창업", "예비창업", "스타트업", "초기기업", "창업기업"],
    "farmer_fisher":["농업인", "어업인", "농가", "어가", "농어업", "임업인"],
    "tourism":      ["관광사업체", "관광기업", "여행업", "관광벤처", "숙박업", "마이스", "MICE"],
    "general":      ["도내 기업", "중소기업", "제주 소재 기업", "사업자"],
}

BIZ_RULES: dict[str, list[str]] = {
    "grant":          ["지원금", "보조금", "사업비 지원", "지원사업", "지원 계획 공고", "육성사업"],
    "bid":            ["입찰", "용역", "제안서", "나라장터", "협상에 의한 계약", "구매", "수의계약 안내"],
    "hr_support":     ["인건비", "고용지원", "일자리사업", "채용지원", "인력지원", "고용창출", "일자리 창출"],
    "distribution":   ["배분", "지정기탁", "성금", "모금회", "사랑의열매", "배분사업"],
    "education":      ["교육생 모집", "아카데미", "컨설팅", "역량강화", "양성과정", "교육 지원", "직업훈련"],
    "loan_guarantee": ["융자", "보증", "특례보증", "이차보전", "대출"],
    "space":          ["입주기업 모집", "입주 모집", "공간 지원", "입주자 모집"],
    "contest":        ["공모전", "경진대회", "아이디어 공모", "콘테스트"],
}

DEADLINE_PATTERNS = [
    re.compile(r"(\d{4})[.\-/년]\s*(\d{1,2})[.\-/월]\s*(\d{1,2})일?\s*[(（]?[가-힣]?[)）]?\s*(?:까지|마감|限)"),
    re.compile(r"접수\s*기간[^\d]{0,20}~\s*(\d{4})[.\-/년]\s*(\d{1,2})[.\-/월]\s*(\d{1,2})"),
    re.compile(r"~\s*(\d{4})[.\-/년]\s*(\d{1,2})[.\-/월]\s*(\d{1,2})"),
    re.compile(r"~\s*(\d{1,2})[.\-/월]\s*(\d{1,2})일?\s*(?:까지|마감)?"),
]

ALWAYS_OPEN = re.compile(r"상시\s*모집|예산\s*소진\s*시|소진시까지|수시\s*접수")


def _extract_deadline(text: str) -> tuple[str | None, bool]:
    """(apply_end, is_always_open)"""
    if ALWAYS_OPEN.search(text):
        return None, True
    for i, pat in enumerate(DEADLINE_PATTERNS):
        m = pat.search(text)
        if not m:
            continue
        g = m.groups()
        if len(g) == 3:
            return f"{g[0]}-{int(g[1]):02d}-{int(g[2]):02d}", False
        if len(g) == 2:  # 연도 생략 → 올해로 추정
            y = date.today().year
            return f"{y}-{int(g[0]):02d}-{int(g[1]):02d}", False
    return None, False


def _extract_age(text: str) -> tuple[int | None, int | None]:
    m = re.search(r"만?\s*(\d{2})\s*세\s*(?:이상)?\s*[~∼-]\s*만?\s*(\d{2})\s*세", text)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"만?\s*(\d{2})\s*세\s*이상", text)
    if m:
        return int(m.group(1)), None
    m = re.search(r"만?\s*(\d{2})\s*세\s*이하", text)
    if m:
        return None, int(m.group(1))
    return None, None


def classify(title: str, body: str) -> dict:
    text = f"{title}\n{body}"
    company_types = [c for c, kws in COMPANY_RULES.items() if any(k in text for k in kws)]
    biz_types = [b for b, kws in BIZ_RULES.items() if any(k in text for k in kws)]

    # 제목 단서 보정
    if "입찰" in title or "용역" in title:
        biz_types = list(dict.fromkeys(["bid"] + biz_types))
    if re.search(r"배분|지정기탁", title):
        biz_types = list(dict.fromkeys(["distribution"] + biz_types))

    apply_end, always_open = _extract_deadline(text)
    age_min, age_max = _extract_age(text)

    gender = "무관"
    if re.search(r"여성(?:만|\s*대상|기업|가장|\s*근로자)", text):
        gender = "여성"

    region = "제주 전역"
    if "서귀포" in text and "제주시" not in text:
        region = "서귀포시"
    elif re.search(r"제주시(?:\s|에|의|,)", text) and "서귀포" not in text:
        region = "제주시"

    # 청년 태그 있는데 연령 미추출 → 기본 추정
    if "youth" in company_types and age_min is None and age_max is None:
        age_min, age_max = 19, 39
    if "midlife" in company_types and age_min is None:
        age_min = 40

    conf = 1.0
    if not biz_types:
        conf -= 0.35
    if not company_types:
        conf -= 0.25
    if apply_end is None and not always_open:
        conf -= 0.15

    return {
        "company_types": company_types or ["general"],
        "biz_types": biz_types or ["grant"],
        "apply_end": apply_end,
        "always_open": always_open,
        "age_min": age_min,
        "age_max": age_max,
        "gender": gender,
        "region": region,
        "confidence": round(max(conf, 0.0), 2),
        "needs_review": conf < 0.6,
    }
