"""일일 크롤링 오케스트레이터.

실행: python -m pipeline.run            (활성 기관 전체)
      python -m pipeline.run jto jta    (특정 기관만)

동작:
1. institutions.json의 active 기관 순회
2. 목록 수집 → 기존 URL과 대조, 신규만 상세 수집 (증분 처리 → 트래픽 최소화)
3. 규칙 기반 분류 → data/announcements.json 병합 커밋 대상 갱신
4. 마감 지난 공고 status=closed, 90일 지난 closed 건 아카이브 제거
5. crawl_report.json에 기관별 결과 기록 (found=0 연속이면 셀렉터 점검 신호)
"""
from __future__ import annotations
import json
import sys
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path

from .adapters import ADAPTERS, RawPost
from .classifier import classify

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "announcements.json"
REPORT = ROOT / "data" / "crawl_report.json"
INSTITUTIONS = Path(__file__).resolve().parent / "institutions.json"


def load_db() -> dict:
    if DATA.exists():
        return json.loads(DATA.read_text(encoding="utf-8"))
    return {"updated_at": None, "items": []}


def save_db(db: dict):
    db["updated_at"] = datetime.now().isoformat(timespec="seconds")
    DATA.parent.mkdir(exist_ok=True)
    DATA.write_text(json.dumps(db, ensure_ascii=False, indent=1), encoding="utf-8")


def housekeeping(db: dict):
    """마감 처리 + 기존 항목을 최신 정제 규칙으로 재정화 (규칙 개선분 소급 적용)."""
    from .adapters import _clean_title, TITLE_EXCLUDE
    from .classifier import classify_sectors, _extract_deadline
    cleaned = []
    for it in db["items"]:
        if it.get("sample"):
            continue  # 시드 데이터 제거 (실데이터 확보 완료)
        it["title"] = _clean_title(it["title"])
        if TITLE_EXCLUDE.search(it["title"]):
            continue  # 직원채용·합격자발표 등 소급 제거
        OTHER = ("서울","부산","대구","인천","광주광역","대전","울산","세종","경기","강원",
                 "충청북","충청남","충북","충남","전라북","전라남","전북","전남",
                 "경상북","경상남","경북","경남")
        if any(o in it.get("institution", "") for o in OTHER):
            continue  # 타 지자체 소관 공고 소급 제거
        # 분야 소급 부여 (기업마당 건은 board로 식별해도 공식분야 미보존이므로 규칙 분류)
        if not it.get("sectors"):
            it["sectors"] = classify_sectors(it["title"], it.get("summary", ""))
        # 접수마감 재추출 (기업마당 건은 API 접수기간이 정확하므로 유지)
        if it.get("board") != "기업마당":
            end, always = _extract_deadline(it["title"] + "\n" + it.get("summary", ""))
            if end or always:
                it["apply_end"], it["always_open"] = end, always
            elif it.get("apply_end") and it["apply_end"] > "2027-06-30":
                it["apply_end"] = None  # 1년 초과 마감 = 과업기간 오인 가능성 → 기한확인으로
        cleaned.append(it)
    db["items"] = cleaned

    today = date.today().isoformat()
    cutoff = (date.today() - timedelta(days=90)).isoformat()
    kept = []
    for it in db["items"]:
        end = it.get("apply_end")
        if end and end < today:
            it["status"] = "closed"
        if it["status"] == "closed" and (it.get("apply_end") or today) < cutoff:
            continue  # 90일 지난 마감 건은 목록에서 제거 (JSON 비대화 방지)
        kept.append(it)
    db["items"] = kept


def process_institution(inst: dict, db: dict) -> dict:
    adapter = ADAPTERS[inst["adapter"]](inst)
    known_urls = {it["url"] for it in db["items"]}
    found = new = 0
    errors = []

    for board in inst["boards"]:
        if not board.get("url"):
            continue
        try:
            posts: list[RawPost] = adapter.fetch_list(board["url"])
        except Exception as e:
            errors.append(f"{board['label']}: {type(e).__name__} {e}")
            continue
        found += len(posts)
        for post in posts:
            if post.url in known_urls:
                continue  # ★ 증분 처리: 이미 수집된 공고는 상세 요청도 생략
            post = adapter.fetch_detail(post)
            cls = classify(post.title, post.body_text)
            db["items"].append({
                "id": post.content_hash()[:12],
                "institution": inst["name"],
                "institution_short": inst["short"],
                "group": inst["group"],
                "board": board["label"],
                "title": post.title,
                "url": post.url,
                "posted_at": post.posted_at,
                "summary": post.body_text[:280],
                "attachments": post.attachments,
                "status": "open",
                "crawled_at": date.today().isoformat(),
                **cls,
            })
            known_urls.add(post.url)
            new += 1

    return {"institution": inst["id"], "found": found, "new": new, "errors": errors}


def main():
    only = set(sys.argv[1:])
    reg = json.loads(INSTITUTIONS.read_text(encoding="utf-8"))
    insts = [i for i in reg["institutions"]
             if i.get("active") and (not only or i["id"] in only)]

    db = load_db()
    report = {"run_at": datetime.now().isoformat(timespec="seconds"), "results": []}

    for inst in insts:
        try:
            r = process_institution(inst, db)
        except Exception:
            r = {"institution": inst["id"], "found": 0, "new": 0,
                 "errors": [traceback.format_exc(limit=1)]}
        report["results"].append(r)
        print(f"[{inst['id']:>12}] found={r['found']:>3} new={r['new']:>3} "
              f"{'ERR ' + str(r['errors'][:1]) if r['errors'] else ''}")

    # 기업마당 API (도청·시청 등 해외IP 차단 기관 우회 수집)
    from . import bizinfo
    br = bizinfo.collect(db)
    report["results"].append(br)
    print(f"[{'bizinfo':>12}] found={br['found']:>3} new={br['new']:>3} "
          f"{'ERR ' + str(br['errors'][:1]) if br['errors'] else ''}")

    housekeeping(db)
    # 정렬: 마감 임박순 → 게시일 역순
    db["items"].sort(key=lambda x: (x.get("apply_end") or "9999-12-31",
                                    x.get("posted_at") or ""), reverse=False)
    save_db(db)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    total_new = sum(r["new"] for r in report["results"])
    zero = [r["institution"] for r in report["results"] if r["found"] == 0]
    print(f"\n완료: 신규 {total_new}건 / 활성 공고 {len([i for i in db['items'] if i['status']=='open'])}건")
    if zero:
        print(f"⚠ found=0 기관 (셀렉터 점검 필요할 수 있음): {', '.join(zero)}")


if __name__ == "__main__":
    main()
