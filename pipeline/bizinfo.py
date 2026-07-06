"""기업마당(bizinfo.go.kr) 공식 오픈API 어댑터.

목적: 해외 IP를 차단하는 도청·시청 등의 기업지원 공고를 기업마당 API로 우회 수집.
- 인증키(BIZINFO_KEY)는 GitHub Secrets → 환경변수로 주입 (레포에 절대 커밋 금지)
- 키가 없으면 조용히 스킵 (파이프라인 전체는 정상 진행)
- 제주 관련 공고만 채택: 소관/수행기관명 또는 해시태그에 '제주' 포함
"""
from __future__ import annotations
import os
import re
import hashlib
from datetime import date

import httpx

API_URL = "https://www.bizinfo.go.kr/uss/rss/bizinfoApi.do"


def _parse_period(raw: str | None) -> tuple[str | None, str | None, bool]:
    """'20260620 ~ 20260731' → (시작, 마감, 상시여부)"""
    if not raw:
        return None, None, False
    if re.search(r"상시|예산\s*소진|수시", raw):
        return None, None, True
    dates = re.findall(r"(\d{4})[.\-/]?(\d{2})[.\-/]?(\d{2})", raw)
    fmt = lambda d: f"{d[0]}-{d[1]}-{d[2]}"
    if len(dates) >= 2:
        return fmt(dates[0]), fmt(dates[1]), False
    if len(dates) == 1:
        return None, fmt(dates[0]), False
    return None, None, False


def _org_group(org: str) -> int:
    """소관기관명 → 플랫폼 그룹 매핑."""
    if "제주" in org:
        if any(k in org for k in ("도청", "특별자치도", "제주시", "서귀포")):
            return 3
        return 2  # 제주 소재 공공기관/출자출연 추정
    return 6      # 중앙부처·전국기관 (기업마당 경유)


def collect(db: dict) -> dict:
    key = os.environ.get("BIZINFO_KEY", "").strip()
    if not key:
        return {"institution": "bizinfo", "found": 0, "new": 0,
                "errors": ["BIZINFO_KEY 미설정 — 스킵 (Secrets에 키 등록 시 자동 활성화)"]}

    known_urls = {it["url"] for it in db["items"]}
    found = new = 0
    errors: list[str] = []

    try:
        r = httpx.get(API_URL, params={
            "crtfcKey": key,
            "dataType": "json",
            "searchCnt": "300",
        }, timeout=30)
        r.raise_for_status()
        payload = r.json()
    except Exception as e:
        return {"institution": "bizinfo", "found": 0, "new": 0,
                "errors": [f"{type(e).__name__}: {e}"]}

    # 응답 구조: {"jsonArray": [...]} 형태 (필드명은 기업마당 표준)
    rows = payload.get("jsonArray") or payload.get("items") or []
    from .classifier import classify

    for row in rows:
        title = (row.get("pblancNm") or "").strip()
        org = (row.get("jrsdInsttNm") or "").strip()      # 소관기관
        exc = (row.get("excInsttNm") or "").strip()        # 수행기관
        tags = (row.get("hashtags") or "")
        url = (row.get("pblancUrl") or "").strip()
        if url.startswith("/"):
            url = "https://www.bizinfo.go.kr" + url
        if not title or not url:
            continue

        # 제주 관련 공고만 채택 (도청·시청·도내기관 공고가 여기로 들어옴)
        if not any("제주" in s for s in (org, exc, tags, title)):
            continue

        found += 1
        if url in known_urls:
            continue

        start, end, always = _parse_period(row.get("reqstBeginEndDe"))
        body = " ".join(filter(None, [
            row.get("bsnsSumryCn", ""), tags,
            f"소관: {org}", f"수행: {exc}",
            f"분야: {row.get('pldirSportRealmLclasCodeNm', '')}",
        ]))
        cls = classify(title, body)
        if end:
            cls["apply_end"] = end
        cls["always_open"] = cls.get("always_open") or always

        db["items"].append({
            "id": hashlib.sha256(url.encode()).hexdigest()[:12],
            "institution": org or "기업마당",
            "institution_short": (org or "기업마당")[:10],
            "group": _org_group(org),
            "board": "기업마당",
            "title": title,
            "url": url,
            "posted_at": (row.get("creatPnttm") or "")[:10] or None,
            "summary": (row.get("bsnsSumryCn") or body)[:280],
            "attachments": [],
            "status": "open",
            "crawled_at": date.today().isoformat(),
            **cls,
        })
        known_urls.add(url)
        new += 1

    return {"institution": "bizinfo", "found": found, "new": new, "errors": errors}
