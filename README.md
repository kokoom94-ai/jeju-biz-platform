# 제주 지원사업 통합공고 플랫폼

제주 도내 공공기관·출자출연기관·행정/교육/의회·일자리/복지·협회 등 **35개 기관**의
지원사업·공모·입찰·배분 공고를 매일 자동 수집하고, 규칙 기반 AI 분류로 태깅하여
다중 필터 웹 보드로 제공합니다. **운영비 0원** 구조입니다.

## 구조

```
├── index.html                  # 웹 UI (정적, 빌드 불필요)
├── data/
│   ├── announcements.json      # 공고 DB (크롤러가 매일 갱신·커밋)
│   └── crawl_report.json       # 기관별 수집 결과 (장애 감지용)
├── pipeline/
│   ├── institutions.json       # ★ 기관 레지스트리 (기관 추가 = 여기에 항목 추가)
│   ├── adapters.py             # 범용 게시판 크롤러 (config 기반 + 휴리스틱 폴백)
│   ├── classifier.py           # 규칙 기반 분류 엔진 (LLM 0회 호출)
│   ├── run.py                  # 오케스트레이터
│   └── discover.py             # 셀렉터 자동 탐지 도우미
└── .github/workflows/crawl.yml # 매일 KST 05:30 자동 실행
```

## 배포 (10분)

1. **GitHub 레포 생성** (public 권장 — Actions 무료 무제한) 후 이 폴더 전체 push
2. **Netlify** → Import from Git → 이 레포 선택 → 빌드 명령 없음, publish 디렉토리 `.`
3. **Actions 권한**: 레포 Settings → Actions → General → Workflow permissions →
   `Read and write permissions` 체크 (봇이 데이터를 커밋할 수 있도록)
4. Actions 탭에서 `daily-crawl` 수동 실행(Run workflow) → 첫 수집 확인

이후 매일 새벽 자동으로: 크롤링 → 분류 → JSON 커밋 → Netlify 자동 재배포.

## 기관 온보딩 절차

`institutions.json`의 `verified: false` 기관은 셀렉터 확인이 필요합니다.

```bash
pip install -r requirements.txt
python -m pipeline.discover --all-unverified   # 전체 미검증 기관 일괄 점검
python -m pipeline.discover jta jjedu          # 특정 기관만
```

- **✓ 3건 이상 추출 성공** → 휴리스틱 폴백만으로 동작. `verified: true`로 변경하면 끝
- **△ 추출 실패** → 출력된 구조 힌트를 보고 해당 기관 `config`에 셀렉터 지정:
  ```json
  "config": {"row_selector": "table.board tbody tr", "link_selector": "td.subject a", "date_selector": "td.date"}
  ```
- **JS 렌더링 사이트**(드물게 존재) → 해당 기관만 `active: false` 처리 후 추후 확장

## 분류 체계

| 축 | 태그 |
|---|---|
| 대상 (company_types) | 소상공인 · 청년 · 창업 · 관광기업 · 사회적기업 · 협동조합 · 자활기업 · 여성 · 중장년 · 복지시설/법인 · 비영리 · 농어업 · 일반 |
| 형태 (biz_types) | 지원금 · 인력/고용 · 배분/지정기탁 · 교육/컨설팅 · 융자/보증 · 입찰/용역 · 공모전 · 입주/공간 |
| 추출 필드 | 마감일(D-day) · 상시모집 여부 · 연령 조건 · 성별 조건 · 지역 |

분류 규칙 추가/수정은 `classifier.py`의 `COMPANY_RULES` / `BIZ_RULES` 딕셔너리만 편집.
`needs_review: true` 건은 키워드가 안 잡힌 공고 — 주기적으로 확인해 규칙에 어휘 보강.

## 운영 수칙

- 요청 간격 3초(`REQUEST_DELAY_SEC`)는 기관 서버 보호를 위해 유지
- `crawl_report.json`에서 `found: 0`이 이틀 연속인 기관 = 사이트 개편 신호 → discover 재실행
- 90일 지난 마감 공고는 자동 삭제되어 JSON 크기가 일정 수준으로 유지됨
- 공고 원문·첨부는 저장하지 않고 **링크만 제공** (저작권·저장비용 이슈 없음)

## 향후 확장 (필요 시점에)

- 저신뢰(needs_review) 건 LLM 보조 분류 (Gemini 무료 티어 또는 Claude Haiku)
- 키워드 구독 알림 (GitHub Actions에서 무료 이메일 발송)
- HWP/PDF 첨부 텍스트 추출 (pyhwp + pdfplumber)
