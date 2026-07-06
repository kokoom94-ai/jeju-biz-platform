"""게시판 셀렉터 자동 탐지 도우미.

사용: python -m pipeline.discover jta
     python -m pipeline.discover --all-unverified

동작: 대상 기관 게시판 URL을 실제 요청 → 휴리스틱 파서로 게시글 추출 시도 →
결과 미리보기 출력. 3건 이상 잡히면 폴백 파서만으로 운영 가능하다는 뜻이므로
institutions.json에서 verified=true로 바꾸면 됨. 안 잡히면 출력된 HTML 구조
힌트를 보고 row_selector/link_selector를 config에 지정.
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

from .adapters import StaticBoardAdapter, _get
from bs4 import BeautifulSoup

INSTITUTIONS = Path(__file__).resolve().parent / "institutions.json"


def probe(inst: dict):
    print(f"\n{'='*60}\n{inst['name']} ({inst['id']})")
    adapter = StaticBoardAdapter(inst)
    for board in inst["boards"]:
        url = board.get("url")
        if not url:
            continue
        print(f"\n▶ {board['label']}: {url}")
        try:
            posts = adapter.fetch_list(url)
        except Exception as e:
            print(f"  ✗ 요청 실패: {type(e).__name__}: {e}")
            continue
        if len(posts) >= 3:
            print(f"  ✓ {len(posts)}건 추출 성공 — verified=true 설정 가능")
            for p in posts[:5]:
                print(f"    · [{p.posted_at or '날짜?'}] {p.title[:60]}")
        else:
            print(f"  △ {len(posts)}건만 추출됨 — 셀렉터 수동 지정 필요. 구조 힌트:")
            try:
                soup = BeautifulSoup(_get(url), "lxml")
                for sel in ["table", "ul.board", "div.board", "ul.list", "div.list"]:
                    els = soup.select(sel)
                    if els:
                        cls = els[0].get("class")
                        print(f"    - {sel}: {len(els)}개 발견 (class={cls})")
            except Exception:
                pass


def main():
    reg = json.loads(INSTITUTIONS.read_text(encoding="utf-8"))
    args = sys.argv[1:]
    if "--all-unverified" in args:
        targets = [i for i in reg["institutions"] if i.get("active") and not i.get("verified")]
    else:
        ids = set(args)
        targets = [i for i in reg["institutions"] if i["id"] in ids]
    if not targets:
        print("사용법: python -m pipeline.discover <기관id...> | --all-unverified")
        return
    for inst in targets:
        probe(inst)


if __name__ == "__main__":
    main()
