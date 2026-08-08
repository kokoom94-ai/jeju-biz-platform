"""나라장터(조달청) 입찰공고 OpenAPI 어댑터 — 제주 한정 공고 수집.

g2b.go.kr 웹은 동적 렌더링·세션 기반이라 크롤링이 취약하므로,
공공데이터포털의 '조달청_나라장터 입찰공고정보서비스' 공식 API를 사용.

- 인증키: G2B_KEY 환경변수. 미설정 시 BIZINFO_KEY로 폴백
  (동일한 data.go.kr 발급키 — 단, 해당 API에 대한 '활용신청'이 승인돼 있어야 함)
- 키가 없으면 조용히 스킵 (파이프라인 전체는 정상 진행)
- 용역/물품/공사 3개 유형을 최근 공고게시일 기준으로 조회
- 제주 채택 기준(하나라도 충족):
  ① 수요기관명·공고기관명에 '제주' 포함
  ② 참가제한지역명에 '제주' 포함 (응답에 해당 필드가 있는 경우)
- 수집 건은 type=bid 고정 → UI '용역·입찰' 카테고리에 표시

주의: 조달청 API는 서비스 경로가 개편된 이력이 있어(BidPublicInfoService04 →
/ad/BidPublicInfoService) 실패 시 crawl_report에 상태코드·응답 요약을 남긴다.
경로가 또 바뀌면 그 진단을 보고 API_BASE만 갱신하면 됨.
"""
from __future__ import annotations
import os
import hashlib
from datetime import date, datetime, timedelta

import httpx

# 2024년 개편 이후 경로. 실패 시 구경로로 1회 폴백.
API_BASE = "https://apis.data.go.kr/1230000/ad/BidPublicInfoService"
API_BASE_LEGACY = "https://apis.data.go.kr/1230000/BidPublicInfoService04"

# (오퍼레이션, 라벨) — 용역이 소상공인·기관 실무에 가장 유효, 물품·공사 순
OPERATIONS = [
    ("getBidPblancListInfoServcPPSSrch", "용역"),
    ("getBidPblancListInfoThngPPSSrch", "물품"),
    ("getBidPblancListInfoCnstwkPPSSrch", "공사"),
]

LOOKBACK_DAYS = 3   # 최근 N일 공고 (일일 실행이므로 여유분 포함)
PAGE_ROWS = 500
MAX_PAGES = 4       # 유형당 최대 2,000건 훑기 (전국 일일 공고량 커버)


def _jeju_hit(row: dict) -> bool:
    """제주 관련 공고 판정."""
    hay = " ".join(str(row.get(k) or "") for k in (
        "dminsttNm",        # 수요기관명
        "ntceInsttNm",      # 공고기관명
        "prtcptLmtRgnNm",   # 참가제한지역명 (필드 존재 시)
        "prtcptPsblRgnNm",  # 참가가능지역명 (필드 존재 시)
    ))
    return "제주" in hay


def _org_group(org: str) -> int:
    if "제주" in org:
        if any(k in org for k in ("특별자치도", "제주시", "서귀포", "교육청", "도청")):
            return 3
        return 2
    return 6


def _norm_dt(raw: str | None) -> str | None:
    """'2026-08-07 14:00' / '202608071400' → '2026-08-07'"""
    if not raw:
        return None
    s = str(raw)
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) >= 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return None


def _fetch(base: str, op: str, key: str, page: int, bgn: str, end: str) -> dict:
    r = httpx.get(f"{base}/{op}", params={
        "serviceKey": key,
        "pageNo": page,
        "numOfRows": PAGE_ROWS,
        "inqryDiv": "1",          # 공고게시일시 기준 조회
        "inqryBgnDt": bgn,
        "inqryEndDt": end,
        "type": "json",
    }, timeout=30, follow_redirects=True)
    r.raise_for_status()
    return r.json()


def collect(db: dict) -> dict:
    key = (os.environ.get("G2B_KEY") or os.environ.get("BIZINFO_KEY") or "").strip()
    if not key:
        return {"institution": "g2b", "found": 0, "new": 0,
                "errors": ["G2B_KEY/BIZINFO_KEY 미설정 — 스킵"]}

    known_urls = {it["url"] for it in db["items"]}
    found = new = 0
    errors: list[str] = []

    now = datetime.now()
    bgn = (now - timedelta(days=LOOKBACK_DAYS)).strftime("%Y%m%d") + "0000"
    end = now.strftime("%Y%m%d") + "2359"

    base = API_BASE
    for op, label in OPERATIONS:
        for page in range(1, MAX_PAGES + 1):
            try:
                data = _fetch(base, op, key, page, bgn, end)
            except Exception as e:
                # 신경로 첫 실패 시 구경로 폴백 1회
                if base == API_BASE:
                    try:
                        base = API_BASE_LEGACY
                        data = _fetch(base, op, key, page, bgn, end)
                    except Exception as e2:
                        errors.append(f"{label}: {type(e2).__name__} {str(e2)[:80]}")
                        break
                else:
                    errors.append(f"{label}: {type(e).__name__} {str(e)[:80]}")
                    break

            body = (data.get("response") or {}).get("body") or {}
            rows = body.get("items") or []
            if isinstance(rows, dict):  # 단건 응답 관행 대응
                rows = rows.get("item") or []
            if isinstance(rows, dict):
                rows = [rows]
            if not rows:
                if page == 1:
                    hdr = (data.get("response") or {}).get("header") or {}
                    msg = hdr.get("resultMsg") or str(data)[:100]
                    if hdr.get("resultCode") not in (None, "00"):
                        errors.append(f"{label}: API 응답 [{hdr.get('resultCode')}] {msg}")
                break

            found += len(rows)
            for row in rows:
                if not _jeju_hit(row):
                    continue
                title = (row.get("bidNtceNm") or "").strip()
                if not title:
                    continue
                no = row.get("bidNtceNo") or ""
                ord_ = row.get("bidNtceOrd") or ""
                url = (row.get("bidNtceDtlUrl") or row.get("bidNtceUrl") or
                       f"https://www.g2b.go.kr:8101/ep/invitation/publish/bidInfoDtl.do"
                       f"?bidno={no}&bidseq={ord_}")
                if url in known_urls:
                    continue
                org = (row.get("dminsttNm") or row.get("ntceInsttNm") or "나라장터").strip()
                db["items"].append({
                    "id": hashlib.sha256(url.encode()).hexdigest()[:12],
                    "institution": org,
                    "institution_short": org[:10],
                    "group": _org_group(org),
                    "board": "나라장터",
                    "title": f"[{label}] {title}",
                    "url": url,
                    "posted_at": _norm_dt(row.get("bidNtceDt")),
                    "summary": " / ".join(filter(None, [
                        f"공고기관: {row.get('ntceInsttNm') or ''}",
                        f"수요기관: {row.get('dminsttNm') or ''}",
                        f"예산: {row.get('asignBdgtAmt') or row.get('presmptPrce') or ''}",
                    ]))[:280],
                    "attachments": [],
                    "status": "open",
                    "crawled_at": date.today().isoformat(),
                    "sectors": ["bid"],
                    "company_types": ["general"],
                    "biz_types": ["bid"],
                    "type": "bid",
                    "apply_end": _norm_dt(row.get("bidClseDt")),
                    "always_open": False,
                    "age_min": None, "age_max": None,
                    "gender": "무관", "region": "제주 전역",
                    "confidence": 0.9, "needs_review": False,
                })
                known_urls.add(url)
                new += 1

            total = body.get("totalCount") or 0
            if page * PAGE_ROWS >= int(total):
                break

    return {"institution": "g2b", "found": found, "new": new, "errors": errors}
