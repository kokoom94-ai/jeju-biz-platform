"""범용 게시판 크롤링 어댑터.

설계 원칙:
- 기관별 코드가 아니라 institutions.json의 config(셀렉터)로 동작
- config가 비어 있으면 휴리스틱 자동 탐지(폴백)로 게시글 목록 추출 시도
- 사이트 구조가 바뀌면 discover.py로 셀렉터만 재탐지 → 코드 수정 없음
"""
from __future__ import annotations
import hashlib
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; JejuBizBoard/1.0; +https://github.com/OWNER/jeju-biz-platform)"
}
REQUEST_DELAY_SEC = 3  # 기관 서버 부하 방지 (필수 유지)
TIMEOUT = 25

# 공고성 게시글만 통과시키는 제목 필터 (노이즈 제거)
TITLE_INCLUDE = re.compile(
    r"공고|모집|공모|지원|입찰|용역|선정|신청|배분|기탁|교육생|참가|사업"
)
TITLE_EXCLUDE = re.compile(
    r"결과\s*발표|당첨자|합격자|서류전형|면접\s*일정|휴무|점검\s*안내$"
    r"|직원\s*채용|채용\s*공고|채용공고|기간제\s*(직원|근로자)|공무직"
)


@dataclass
class RawPost:
    title: str
    url: str
    posted_at: str | None = None
    body_text: str = ""
    attachments: list[dict] = field(default_factory=list)  # {name, url}

    def content_hash(self) -> str:
        base = "".join((self.title + self.body_text).split())
        return hashlib.sha256(base.encode()).hexdigest()


def _get(url: str) -> str:
    time.sleep(REQUEST_DELAY_SEC)
    r = httpx.get(url, headers=HEADERS, timeout=TIMEOUT, follow_redirects=True)
    r.raise_for_status()
    # 한국 공공기관은 EUC-KR 잔존 사이트가 있음
    if r.encoding and r.encoding.lower() in ("iso-8859-1", "ascii"):
        r.encoding = "utf-8"
    return r.text


def _clean_title(t: str) -> str:
    """게시판 링크 텍스트에 섞여 들어오는 메타 노이즈 제거."""
    t = re.sub(r"작성자\s*[:：].*$", "", t)
    t = re.sub(r"(첨부파일|새\s*글|new)\s*.*$", "", t, flags=re.I)
    t = re.sub(r"조회\s*[:：]?\s*\d+.*$", "", t)
    t = re.sub(r"\s*\d{4}[-./]\d{1,2}[-./]\d{1,2}\.?\s*$", "", t)  # 제목 끝 게시일 제거
    t = re.sub(r"^\s*(공지|필독|\d{1,4})\s+", "", t)
    return re.sub(r"\s{2,}", " ", t).strip()


def _norm_date(text: str | None) -> str | None:
    if not text:
        return None
    m = re.search(r"(\d{4})[.\-/년]\s*(\d{1,2})[.\-/월]\s*(\d{1,2})", text)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.search(r"(\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})", text)
    if m:
        return f"20{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return None


class StaticBoardAdapter:
    """정적 HTML 게시판 범용 어댑터 (대상 기관의 90%+ 커버)."""

    def __init__(self, inst: dict):
        self.inst = inst
        self.config = inst.get("config") or {}

    # ---------- 목록 ----------
    def fetch_list(self, board_url: str) -> list[RawPost]:
        pages = int(self.config.get("pages", 1))
        param = self.config.get("page_param", "p")
        all_posts: list[RawPost] = []
        seen: set[str] = set()
        for n in range(1, pages + 1):
            url = board_url if n == 1 else (
                f"{board_url}{'&' if '?' in board_url else '?'}{param}={n}")
            try:
                html = _get(url)
            except Exception:
                break
            soup = BeautifulSoup(html, "lxml")
            row_sel = self.config.get("row_selector")
            posts = self._parse_with_config(soup, url) if row_sel else []
            if not posts:
                posts = self._parse_heuristic(soup, url)
            fresh = [p for p in posts if p.url not in seen]
            if not fresh:
                break  # 다음 페이지가 없거나 동일 내용 반복 → 중단
            for p in fresh:
                seen.add(p.url)
            all_posts.extend(fresh)
        return all_posts

    def _parse_with_config(self, soup, base_url) -> list[RawPost]:
        posts = []
        for row in soup.select(self.config["row_selector"]):
            a = row.select_one(self.config.get("link_selector", "a"))
            if not a or not a.get("href"):
                continue
            title = _clean_title(a.get_text(" ", strip=True))
            if not self._title_ok(title):
                continue
            date_sel = self.config.get("date_selector")
            date_el = row.select_one(date_sel) if date_sel else None
            posts.append(RawPost(
                title=title,
                url=urljoin(base_url, a["href"]),
                posted_at=_norm_date(date_el.get_text(strip=True) if date_el else None),
            ))
        return posts

    def _parse_heuristic(self, soup, base_url) -> list[RawPost]:
        """셀렉터 없이 게시판 행을 추정: 링크 밀도가 높은 반복 구조를 찾음."""
        candidates: list[RawPost] = []
        # 1순위: table 행
        for row in soup.select("table tr"):
            a = row.select_one("a[href]")
            if not a:
                continue
            title = _clean_title(a.get_text(" ", strip=True))
            if len(title) < 8 or not self._title_ok(title):
                continue
            date = _norm_date(row.get_text(" ", strip=True))
            candidates.append(RawPost(title=title, url=urljoin(base_url, a["href"]), posted_at=date))
        if len(candidates) >= 3:
            return candidates[:40]
        # 2순위: ul/li 리스트형 게시판
        candidates = []
        for li in soup.select("ul li"):
            a = li.select_one("a[href]")
            if not a:
                continue
            title = _clean_title(a.get_text(" ", strip=True))
            if len(title) < 10 or not self._title_ok(title):
                continue
            candidates.append(RawPost(
                title=title, url=urljoin(base_url, a["href"]),
                posted_at=_norm_date(li.get_text(" ", strip=True)),
            ))
        return candidates[:40]

    @staticmethod
    def _title_ok(title: str) -> bool:
        return bool(TITLE_INCLUDE.search(title)) and not TITLE_EXCLUDE.search(title)

    # ---------- 상세 ----------
    def fetch_detail(self, post: RawPost) -> RawPost:
        try:
            html = _get(post.url)
        except Exception:
            return post
        soup = BeautifulSoup(html, "lxml")
        for tag in soup(["script", "style", "nav", "header", "footer"]):
            tag.decompose()
        body_sel = self.config.get("body_selector")
        body = soup.select_one(body_sel) if body_sel else None
        if body is None:
            # 본문 추정: 링크 밀도가 낮으면서 텍스트가 긴 블록 (네비게이션·메뉴 배제)
            best, best_score = None, 0.0
            for b in soup.select("div, td, article, section"):
                text = b.get_text(" ", strip=True)
                if len(text) < 80:
                    continue
                link_len = sum(len(x.get_text(strip=True)) for x in b.find_all("a"))
                ratio = min(link_len / max(len(text), 1), 1.0)
                score = len(text) * (1 - ratio) ** 2   # 링크 비중 높을수록 강한 감점
                if score > best_score:
                    best, best_score = b, score
            body = best
        post.body_text = (body.get_text("\n", strip=True) if body else "")[:20000]
        # 첨부파일 링크 (다운로드는 하지 않고 링크만 보존 — 무예산 원칙)
        for a in soup.select("a[href*='download'], a[href*='file'], a[href$='.hwp'], a[href$='.pdf'], a[href$='.hwpx']"):
            name = a.get_text(" ", strip=True)
            if name:
                post.attachments.append({"name": name[:120], "url": urljoin(post.url, a["href"])})
        post.attachments = post.attachments[:8]
        return post


ADAPTERS = {"static": StaticBoardAdapter}
