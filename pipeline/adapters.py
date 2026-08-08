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
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept-Language": "ko-KR,ko;q=0.9",
}
REQUEST_DELAY_SEC = 3  # 기관 서버 부하 방지 (필수 유지)
TIMEOUT = 25

# 공고성 게시글만 통과시키는 제목 필터 (노이즈 제거)
TITLE_INCLUDE = re.compile(
    r"공고|모집|공모|지원|입찰|용역|선정|신청|배분|기탁|교육생|참가|사업|채용"
)
# 채용공고는 이제 별도 카테고리(hiring)로 수집·노출 — 제외 규칙에서 삭제.
# 채용의 '과정' 알림(합격자·서류전형·면접일정)만 계속 배제.
TITLE_EXCLUDE = re.compile(
    r"결과\s*발표|당첨자|합격자|서류전형|면접\s*일정|휴무|점검\s*안내$"
    # 소식성·비공고 게시물 (보도자료, 강의영상, 통계, 결과 소식)
    r"|#?\d+\s*강[.\s]"          # '35강. 무엇이든 물어보세요' 류 강의 시리즈
    r"|[금은동]메달|입상|우승"     # 수상 소식
    r"|명단\s*발표"               # 참가자 명단 발표 (결과 알림)
    r"|참가율|증가율|상승률"       # 통계 보도자료
    r"|성료|성황리|개최했|다녀왔"   # 행사 후기·결과 소식
    r"|한\s*권으로\s*보는|사업소개"  # 상시 사업안내 페이지 (공고 아님)
    r"|간담회|추진방안\s*연구|연구보고서"  # 연구원 보고서·행사 소식
    r"|(?:지원|발전|신고ㆍ?지원)센터$"  # 기관·센터 소개 링크
    r"|공시송달|부정당업자|접수\s*현황"  # 행정처분·경과 알림 (공고 아님)
)

# 공고 게시글이 아닌 URL 배제 (외부 영상, 사이트 메뉴 페이지)
URL_EXCLUDE = re.compile(
    r"youtube\.com|youtu\.be"      # 크롤링 목록에 섞인 영상 링크
    r"|index\.php\?cid="           # 게시글이 아닌 고정 메뉴 페이지 (ijto 등)
    r"|BBS_0000215|BBS_0000216|BBS_0000217"  # 교육청 학교소식·보도자료 게시판
    r"|nosa\.or\.kr/portal/nosa/majorBiz"  # 노사발전재단 사업소개 페이지 (공고 아님)
    r"|jri\.re\.kr/(?:publication|disclosure)"  # 제주연구원 연구보고서·공시 페이지
    r"|jejuessd\.kr|jejunetzero\.re\.kr"  # 크롤링 목록에 섞인 외부 센터 배너
    r"|#\.?$"                       # 본문 없는 앵커 링크 (nosa 루트 등)
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
        self.diag: str | None = None  # 0건 수집 시 원인 진단용

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
                if n == 1:  # 첫 페이지부터 0건 → 차단/개편 의심, 응답 특성 기록
                    t = soup.title.get_text(strip=True) if soup.title else "no-title"
                    self.diag = f"0건 진단: 응답 {len(html)}자, title='{t[:60]}', 링크 {len(soup.find_all('a'))}개"
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
            _u = urljoin(base_url, a["href"])
            if URL_EXCLUDE.search(_u):
                continue  # 영상·메뉴 링크 등 비공고 URL
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
            url = urljoin(base_url, a["href"])
            if URL_EXCLUDE.search(url):
                continue  # 영상·메뉴 링크 등 비공고 URL
            date = _norm_date(row.get_text(" ", strip=True))
            candidates.append(RawPost(title=title, url=url, posted_at=date))
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
            url = urljoin(base_url, a["href"])
            if URL_EXCLUDE.search(url):
                continue  # 영상·메뉴 링크 등 비공고 URL
            candidates.append(RawPost(
                title=title, url=url,
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
