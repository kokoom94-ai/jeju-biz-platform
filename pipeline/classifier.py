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
    "loan_guarantee": ["융자", "특례보증", "신용보증", "보증지원", "이차보전", "정책자금 대출"],
    "space":          ["입주기업 모집", "입주 모집", "공간 지원", "입주자 모집"],
    "contest":        ["공모전", "경진대회", "아이디어 공모", "콘테스트"],
}


# ===== 분야(sector) 체계 — UI 필터의 단일 축 =====
SECTORS = ["fund","loan","tech","employ","market","startup","edu",
           "contest","bid","space","dist","etc"]

SECTOR_RULES: dict[str, list[str]] = {
    "fund":    ["지원금", "보조금", "사업비 지원", "지원사업", "육성사업", "바우처", "쿠폰"],
    "loan":    ["융자", "특례보증", "신용보증", "보증지원", "이차보전", "정책자금", "대출"],
    "tech":    ["R&D", "기술개발", "기술지원", "특허", "지식재산", "시제품", "스마트공장", "디지털 전환"],
    "employ":  ["인건비", "고용지원", "일자리", "채용지원", "인력지원", "고용창출", "직업훈련", "재취업"],
    "market":  ["수출", "판로", "해외진출", "마케팅 지원", "전시회", "박람회", "입점", "온라인 판로", "내수"],
    "startup": ["창업", "예비창업", "스타트업", "초기기업", "액셀러레이팅", "보육"],
    "edu":     ["교육생 모집", "아카데미", "컨설팅", "역량강화", "양성과정", "교육 지원", "세미나", "설명회"],
    "contest": ["공모전", "경진대회", "아이디어 공모", "콘테스트", "챌린지"],
    "bid":     ["입찰", "용역", "제안서", "나라장터", "협상에 의한 계약", "구매"],
    "space":   ["입주기업 모집", "입주 모집", "공간 지원", "입주자 모집", "사무공간"],
    "dist":    ["배분", "지정기탁", "성금", "모금회", "사랑의열매"],
}

# 기업마당 공식 분야명 → sector 매핑
BIZINFO_SECTOR = {"금융": "loan", "기술": "tech", "인력": "employ", "수출": "market",
                  "내수": "market", "창업": "startup", "경영": "edu", "기타": "etc"}


# ===== 공고유형(type) — UI 2차 필터의 단일 축 (항목당 정확히 1개) =====
# support(지원사업) / bid(용역·입찰) / recruit(모집공고) / contest(공모전) / hiring(채용공고)
TYPES = ["support", "bid", "recruit", "contest", "hiring"]

_T_HIRING  = re.compile(r"직원\s*채용|채용\s*공고|채용공고|채용\s*계획|채용\s*재공고"
                        r"|기간제\s*(?:직원|근로자)|공무직|인턴\s*(?:채용|모집)"
                        r"|경력직|신입\s*채용|직원\s*(?:공개)?\s*모집|임기제공무원|대체인력")
# 입찰 '강' 마커 — 조달 절차가 명시된 경우만 (지원사업 오폭 방지 위해 축소)
_T_BID     = re.compile(r"입찰|위탁\s*용역|협상에\s*의한\s*계약|수의계약"
                        r"|낙찰|개찰|사전\s*규격|공급업체\s*선정|대행\s*용역")
# 입찰 '약' 마커 — 지원 패턴에 해당하지 않을 때만 적용
_T_BID_WEAK = re.compile(r"용역")
_T_CONTEST = re.compile(r"공모전|경진\s*대회|경진대회|콘테스트|챌린지|아이디어\s*공모"
                        r"|디자인\s*공모|영상\s*공모|사진\s*공모|어워드|시상")
_T_SUPPORT = re.compile(r"지원\s*사업|지원사업|지원\s*계획|보조금|지원금|융자|특례보증|보증지원"
                        r"|바우처|육성\s*사업|육성사업|이차보전|사업비\s*지원|R&D|기술개발"
                        r"|사업화\s*지원|수출\s*지원|판로\s*지원|시행\s*공고|장학금"
                        # '○○ 지원' 계열 — 행사·개최·비용·인력 등 무엇을 지원하든 지원사업
                        r"|(?:개최|행사|참가비|응시료|비용|경비|인건비|임차료|수당|이용료"
                        r"|컨설팅|연수과정|양성과정|교육과정)\s*지원"
                        r"|인센티브|집중지원|맞춤형\s*지원"
                        r"|지원\s*(?:안내|공고|알림|변경)"
                        r"|지원$")   # 제목이 '…지원'으로 끝나면 지원사업
_T_RECRUIT = re.compile(r"모집|참가\s*신청|참여\s*기업|참여\s*단체|교육생|아카데미"
                        r"|수강|입주|회원|위원\s*(?:공개)?모집|양성\s*과정")

# 게시판명 단서 (제목보다 우선 — 게시판 자체가 유형을 확정)
_BOARD_TYPE = {"입찰공고": "bid", "입찰정보": "bid", "계약정보": "bid",
               "나라장터": "bid",
               "채용공고": "hiring", "채용정보": "hiring"}


def classify_type(title: str, board: str = "", body: str = "") -> str:
    """공고유형 단일 판정.

    우선순위: 게시판 확정 > 채용 > 입찰(강) > 공모전 > 지원 > 입찰(약: 용역) > 모집.
    - 입찰(강)은 조달 절차 명시(입찰·협상계약·수의계약·위탁용역 등)에 한정.
      '○○ 지원' 류가 입찰로 오폭되지 않도록 지원 판정을 단독 '용역'보다 앞에 둠.
    - '지원사업 운영 용역 <협상에 의한 계약>'처럼 조달 절차가 명시되면 입찰 우선.
    """
    bt = _BOARD_TYPE.get(board or "")
    if bt:
        return bt
    if _T_HIRING.search(title):
        return "hiring"
    if _T_BID.search(title):
        return "bid"
    if _T_CONTEST.search(title):
        return "contest"
    if _T_SUPPORT.search(title):
        return "support"
    if _T_BID_WEAK.search(title):
        return "bid"
    # 기업마당은 중기부 지원사업 통합공고 채널 — '참가기업 모집'류도 실체는 지원사업.
    if board == "기업마당":
        return "support"
    if _T_RECRUIT.search(title):
        return "recruit"
    # 제목 무단서 → 본문 보조 판정 (메뉴 텍스트 오탐 방지 위해 2회 이상)
    if body:
        if sum(1 for _ in _T_BID.finditer(body)) >= 2:
            return "bid"
        if sum(1 for _ in _T_SUPPORT.finditer(body)) >= 2:
            return "support"
    # 최종 폴백: '사업'이 들어가면 지원사업, 아니면 모집공고
    return "support" if "사업" in title else "recruit"


def classify_sectors(title: str, body: str, bizinfo_field: str | None = None) -> list[str]:
    """공고의 분야 태그(다중). 기업마당 공식 분야가 있으면 최우선 반영."""
    found: list[str] = []
    if bizinfo_field:
        for k, v in BIZINFO_SECTOR.items():
            if k in bizinfo_field and v not in found:
                found.append(v)
    text = f"{title}\n{body}"
    # 제목 매칭 우선, 본문은 2회 이상 등장 시 인정 (메뉴 텍스트 오탐 억제)
    for sec, kws in SECTOR_RULES.items():
        if sec in found:
            continue
        if any(k in title for k in kws) or sum(body.count(k) for k in kws) >= 2:
            found.append(sec)
    return found or ["etc"]

DEADLINE_PATTERNS = [
    re.compile(r"(\d{4})[.\-/년]\s*(\d{1,2})[.\-/월]\s*(\d{1,2})일?\s*[(（]?[가-힣]?[)）]?\s*(?:까지|마감|限)"),
    re.compile(r"접수\s*기간[^\d]{0,20}~\s*(\d{4})[.\-/년]\s*(\d{1,2})[.\-/월]\s*(\d{1,2})"),
    re.compile(r"~\s*(\d{4})[.\-/년]\s*(\d{1,2})[.\-/월]\s*(\d{1,2})"),
    re.compile(r"~\s*(\d{1,2})[.\-/월]\s*(\d{1,2})일?\s*(?:까지|마감)?"),
]

ALWAYS_OPEN = re.compile(r"상시\s*모집|예산\s*소진\s*시|소진시까지|수시\s*접수")


def _extract_deadline(text: str) -> tuple[str | None, bool]:
    """접수 마감일 추출. 제목 괄호기간 > 접수/신청 문맥 > 까지/마감 순.
    용역·과업기간 날짜는 배제."""
    if ALWAYS_OPEN.search(text):
        return None, True
    # 최우선: 제목의 괄호 기간 표기 "(6/22~7/21)" — 공고 관행상 접수기간
    first_line = text.split("\n", 1)[0]
    m = re.search(r"[(（]\s*\d{1,2}\s*[./]\s*\d{1,2}\s*[~∼\-]\s*(\d{1,2})\s*[./]\s*(\d{1,2})\s*\.?\s*[)）]", first_line)
    if m:
        return f"{date.today().year}-{int(m.group(1)):02d}-{int(m.group(2)):02d}", False
    m = re.search(r"[(（]\s*[~∼\-]\s*(\d{1,2})\s*[./]\s*(\d{1,2})\s*\.?\s*[)）]", first_line)  # "(~7/21)" 형태
    if m:
        return f"{date.today().year}-{int(m.group(1)):02d}-{int(m.group(2)):02d}", False
    DATE = r"(\d{4})[.\-/년]\s*(\d{1,2})[.\-/월]\s*(\d{1,2})"
    EXCLUDE_CTX = re.compile(r"용역\s*기간|과업|계약\s*기간|사업\s*기간|협약|수행\s*기간|납품"
                             r"|채용\s*기간|추진\s*기간|운영\s*기간|근무\s*기간"
                             r"|교육\s*기간|연수\s*기간|행사\s*기간|위촉\s*기간|임용\s*(?:예정)?일")
    APPLY_CTX = re.compile(r"접수|신청|제출|응모|입찰\s*(?:서|참가|마감)|마감\s*일시|모집\s*기간|공고\s*기간")

    apply_dates, other_dates = [], []
    for line in text.split("\n"):
        dates = re.findall(DATE, line)
        if not dates:
            continue
        if EXCLUDE_CTX.search(line):
            continue  # 과업·용역기간 줄의 날짜는 마감일이 아님
        last = dates[-1]  # 기간 표기 시 마지막 날짜가 종료일
        iso = f"{last[0]}-{int(last[1]):02d}-{int(last[2]):02d}"
        if APPLY_CTX.search(line):
            apply_dates.append(iso)
        elif re.search(r"까지|마감", line):
            other_dates.append(iso)
    if apply_dates:
        return apply_dates[0], False
    if other_dates:
        return other_dates[0], False
    # 연도 생략형 ("~7/20 마감")
    m = re.search(r"~\s*(\d{1,2})[./월]\s*(\d{1,2})일?\s*(?:까지|마감)", text)
    if m:
        return f"{date.today().year}-{int(m.group(1)):02d}-{int(m.group(2)):02d}", False
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
    # biz_types: 본문에는 사이트 메뉴 텍스트가 섞일 수 있어 제목 매칭을 우선하고,
    # 본문 매칭은 키워드가 2회 이상 등장할 때만 인정 (오탐 억제)
    biz_types = []
    for b, kws in BIZ_RULES.items():
        if any(k in title for k in kws):
            biz_types.append(b)
        elif sum(body.count(k) for k in kws) >= 2:
            biz_types.append(b)

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
        "sectors": classify_sectors(title, body),
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
