# HR 통합 시스템 — Claude 작업 규칙

## ⚡ 세션 시작 시 필독

새 세션이 시작됐다면 이 파일의 **"★ 정비 로드맵"** 부터 확인하고 시작할 것.
- 지난 작업 이력은 **`CHANGELOG.md`** (최신이 위). 이 파일엔 이력을 쌓지 않는다.
- 루트의 기획 문서(`saas_plan.md` 등 `*_plan.*` 11개)는 2026-10-12 삭제됨 — 필요하면 git 이력에서 꺼낼 것. 코드 주석의 `saas_plan.md §N`·`improvement_plan.md` 같은 표기는 그 이력을 가리킨다.

### ★ 정비 로드맵 (2026-10-12 수립, 승헌씨 지시 — 위에서부터 한 번에 하나씩)

> 근거: 2026-10-12 저장소 전수 분석(기능 현황·잘된 점·미흡한 점·시장성). 각 단계는 작업 규칙 6번(계획 → 승인) 그대로 따른다.

- ✅ **M0. 죽은 코드·문서 정리 (2026-10-12 완료)** — 상세는 CHANGELOG.md
- ✅ **M1. 문서 정비 (2026-10-12 완료, v1.7.0)** — CLAUDE.md 1,213줄 → 약 200줄, 이력은 CHANGELOG.md, README 갱신, 태그 재개
  - CLAUDE.md 슬림화 — v0.x~v1.6 완료 이력을 `CHANGELOG.md`로 옮기고, 이 파일엔 규칙·현재 상태·로드맵만 남긴다 (세션마다 컨텍스트 낭비 줄이기)
  - 2026-09 작업(아래 요약) 이력 정리해서 CHANGELOG에 편입
  - README 갱신 — 채용 ATS 소개 → Hire 연동으로, Hire·Grow·Screen·Minutes 생태계·Copilot·회의실/자리판 소개, 결제 없음 명시
  - 버전 태그 재개 — v1.6.3 이후 태그 없음. 다음 릴리즈부터 다시 `vX.Y.Z` + 릴리즈 노트
- ✅ **M2. 보안 보강 (2026-10-12 완료, v1.7.1)** — 로그인 시도 제한(계정 5회·IP 20회/15분, 슈퍼어드민 포함), 쿠키 HttpOnly·SameSite=Lax·Secure(운영), 정지 회사 로그인 차단 + 열린 세션 5분 내 끊기, 클라이언트 IP를 X-Real-IP로(감사 로그·전자서명 IP), 로그인 화면 flash 표시
  - 로그인 시도 제한(계정·IP별 N회 실패 시 잠금/지연) — 현재 무제한
  - 세션 쿠키 설정 명시: `SESSION_COOKIE_SECURE`·`HTTPONLY`·`SAMESITE`
  - 테넌트 `suspended` 상태 실제 차단 — 지금은 슈퍼어드민이 정지해도 표시만 되고 로그인이 막히지 않음(웹훅 API 토큰만 막힘). 결제 삭제로 구독 잠금도 없어졌으니 정지 상태를 로그인에서 걸러야 함
- ✅ **M3. SQLite 운영 안정화 (2026-10-12 완료, v1.7.2)** — 전 DB WAL(기동 시 기존 테넌트까지), 연결 timeout 15초, synchronous=NORMAL, 백업 복원을 WAL 안전하게(-wal/-shm 정리 + 백업 API로 .pre-restore). **복원은 서버를 멈춘 뒤에**
- ✅ **M4. 계산 회귀 테스트 (2026-10-12 완료, v1.7.3)** — `tests/test_payroll.py` 53개(법령 기준 기대값) + `.github/workflows/ci.yml`(pytest + 스윕). 테스트가 잡은 **법령 위반 4건 수리**: 최저임금 2025년 값 사용, 연장·휴일 근로수당 가산분(0.5배)만 지급, 연차 기념일 하루 늦게 증가, 근로소득공제 1억 초과 2% 누락
- [ ] **M5. 의존성·정적 자산 정리**
  - Chart.js(jsdelivr CDN, 분석 화면) → `static/`에 내려받아 로컬 참조 (작업 규칙 2번 "외부 CDN 금지"와 정합)
  - requirements.txt: `itsdangerous` 명시(SSO가 직접 씀), `mcp>=1.0.0` 버전 고정
  - pyflakes 경고 정리 — 안 쓰는 import ~30건, 안 쓰는 상수/함수(ANALYTICS_AREAS·ATTENDANCE_STATUS_COLOR·BLOCK_TYPES·WORK_TYPES·LEAVE_DAYS·_cert_user·_week_monday·perf_exclude_label)
  - ~~로그인 화면이 flash 메시지를 안 그림~~ → M2에서 같이 고침
- [ ] **M6. app.py 분리** — 2.1만 줄 단일 파일을 Flask Blueprint로 모듈별 분리(인사/근태/급여·보상/성과/채용 요청서·오퍼 결재/설정/SaaS/API·SSO). 동작 변경 없이 이동만, 단계별로 스윕 통과 확인
- [ ] **M7. 정리 후보 결정·처리** — 아래 "정리 후보" 목록을 승헌씨가 남김/삭제 결정 → 삭제분 처리. 남은 ATS DB 표 처리도 여기서 같이 결정
- [ ] **M8. 기능 추가 (우선순위 순, 각각 별도 기획 후 진행)**
  1. MCP 쓰기 도구(사람 확인 후 실행형) — 휴가 신청, 결재 승인/반려
  2. 4대보험 취득·상실 신고 서식 + 원천세 신고용 엑셀 내보내기
  3. 법정 문서 전자 교부·열람 기록 (연차촉진·근로계약·급여명세 수신 확인)
  4. '연결된 앱' 허브 — Hire·Grow·Screen·Minutes 한 화면 + 설정 메뉴 "Hire 연동" 명칭을 "연결된 앱"으로

### 🧹 정리 후보 (2026-10-12 리스트업 — 남길지 지울지 승헌씨 결정 대기, M7)

| 후보 | 지금 상태 | 범위(코드 위치) | 비고 |
|---|---|---|---|
| **승계 계획** | 기본 숨김(`BASE_FEATURES`에 없음) + 성과 부가 기능 스위치 기본 꺼짐 | `/performance/succession`, `/export/succession`, `succession_plans` 표, 템플릿 performance/succession.html | 보상 검토의 '핵심 인재' 배지가 이 표를 읽음 — 지우면 같이 정리 |
| **Talent Card** (+ Flight Risk·Talent 평가 4종) | 기본 숨김(`talent_advanced`) | `/performance/talent-card/<id>`, `.../talent-flags`, 템플릿 talent_card.html, calibration_results의 retention_risk·loss_impact·achievable_level | 직원 상세·성과 화면의 진입 링크도 같이 |
| **9박스 잠재력(Potential)** | 기본 숨김(`talent_advanced`) — 캘리브레이션의 잠재력 칼럼·배너 | calibration.html의 `'talent_advanced' in features` 블록, calibration_results.potential_score·box_position | 드래그앤드롭 9박스 화면은 원래 구현 안 됨 |
| **급여 구조(밴드·Merit Matrix·ACR·Compa-Ratio)** | 기본 숨김(`comp_advanced`) | `/admin/salary-bands`(+`-legacy`), `/payroll/acr/*` 8개, 보상 허브의 급여 구조 탭·Compa 칼럼, merit_matrix·compensation_review_* 표 | 오퍼 밴드 초과 결재(Hire)·연봉 밴드 API(`/api/salary-band`)는 밴드 표를 씀 → 표는 남겨야 할 수 있음 |
| **HR 데이터 마법사** | 기본 숨김(`data_wizard`) | `/report/builder`·`/report/preview`·`/report/export`, analytics의 마법사 탭(JS 대량), REPORT_SOURCES | 분석 화면 크기의 상당 부분 |
| **1:1 면담 / 피드백 / 목표 정렬** | 성과 부가 기능 스위치 기본 꺼짐(설정에서 켤 수 있음) | PERF_ADDONS, C2 블록(`/one-on-ones`, `/feedback*`, `/goal-alignment` 등) | 2026-09-14 만든 것 — 켜서 쓸 계획이면 유지 |
| **Copilot** | 회사 설정 스위치 기본 꺼짐, `ANTHROPIC_API_KEY` 없으면 규칙 기반 | copilot.py, `/payroll/acr/<id>/copilot/<emp>` 등 | 차별점(AI 네이티브) 축이라 유지 쪽이 자연스러움 |
| **Confluence 연동** | `.env`에 CONFLUENCE_BASE_URL 없으면 꺼짐 | integrations/confluence.py(118줄), dispatcher 온보딩 훅, 연동 설정 화면 | Slack·Jira와 달리 실사용 시나리오 약함 |
| **남은 ATS DB 표** | 코드는 삭제, 표·데이터는 남음 | applicants·applicant_logs·applicant_documents·job_postings·offers·interview_*·recruit_activity_logs·recruit_emails (database.py 스키마) + `static/uploads/applicant_docs/` | job_postings는 채용 요청서 상세(연결된 공고 카드)·training_company가 아직 참조. offers는 앱에서 더 안 씀(Hire 오퍼 결재는 `offer_approvals` 표) |

### 현재 상태 (마지막 업데이트: 2026-10-12)

- **최신 버전:** CHANGELOG.md 맨 위 참고 (태그는 v1.6.3 이후 v1.7.0부터 재개)
- **라이브:** https://talentcore.duckdns.org (Oracle Cloud VM 161.33.39.127, Nginx+Gunicorn 워커 2, Let's Encrypt — 인증서 만료 2026-10-19, certbot.timer 자동 갱신)
- **배포:** v1.6.2까지 VM 반영 확인(2026-07-21). 2026-09 작업 이후는 VM 반영 여부 미확인 → 다음 배포 때 `deploy/update.sh`. **v1.7.0 첫 기동 시 master.db의 `subscriptions`·`billing_logs` 표 DROP + trial→active 전환** — 배포 전 `python backup_db.py`로 백업
- **운영 메모:** 백업 cron(`deploy/setup_backup.sh`, 매일 03:00, 14개 보관) · 일일 에러 요약 cron(`deploy/setup_monitoring.sh`) · 슈퍼어드민 비번 변경 완료(`deploy/change_superadmin_pw.py`)
- **운영 전제:** 무료·오픈소스(MIT), 데모는 가짜 시드만(실데이터 없음). 방침·약관은 운영 인스턴스 기준이며 self-host 시 각자 작성
- **제품 경계:** 채용 공고·지원자·면접·오퍼 작성은 외부 앱 **Hire**. TalentCore는 사람·조직·결재의 주인 — 채용 요청서·헤드카운트·입사 예정자·오퍼 밴드 초과 결재·회의실/입사일 API, SSO(Hire·Grow·Screen·Minutes) 표 발급

### 핵심 설계 결정 (잊으면 안 되는 것들)

- 체험하기(/demo)는 guest가 아닌 admin 권한으로 로그인 (모든 기능 체험 가능, `session['demo_mode']`로 쓰기(POST)만 차단), 재진입마다 세션 초기화
- SaaS 운영자 관리 페이지(`/saas`, 계정 hunie0709 — 시드 기본 비번 1234, 운영 VM은 2026-07-21 무작위 16자로 변경됨)는 `superadmin_required`로 별도 인증 네임스페이스(`session['superadmin_id']`) 사용
- 로그인 폼은 2단 레이아웃(왼쪽 브랜드 패널 + 오른쪽 폼)이 정상 디자인이며, 900px 이하에서는 왼쪽 패널이 의도적으로 숨겨지는 반응형 동작 (버그 아님, 그대로 유지)
- TalentCore MCP 서버 추가 완료 (Claude Desktop 연동), Oracle Cloud 배포 스크립트 완비 (setup/nginx/update)
- SMTP 설정 시 실제 웰컴 이메일 발송 가능 (.env에 SMTP_HOST/USER/PASSWORD/PORT 추가)
- 부가 기능 노출은 `BASE_FEATURES`(app.py) 고정 목록 — 요금제 없음. 연습 회사(training_mode)만 숨김 기능까지 연다
- 성과 부가 기능(1:1·피드백·목표 정렬·승계)·Copilot은 회사 설정 스위치, 기본 꺼짐
- 성과 패치 5개는 **v0.48 성과관리 전면 개편에 통합** (별도 작업 안 함)
  - ① Start/Stop/Continue 피어리뷰 프레임워크
  - ② 익명성 임계값 (응답자 3명 미만 시 결과 비공개)
  - ③ 캘리브레이션 하향 시 사유 필수 입력
  - ④ 최대 1단계 하향 제한 (평균 점수 기준)
  - ⑤ 등급별 보상 배수 직원에게 사전 공개
- 보상 투명성: 글로벌 트렌드는 사전 공개 방향 (Meta=2x/1.15x, Airbnb RSU 배수 공개)
- 9박스 드래그앤드롭 캘리브레이션은 계획만 있고 미구현 — 잠재력 칼럼 존폐는 정리 후보(M7)에서 결정

### 도메인 메모

- **비과세 항목 (소득세법)**: 식대 200,000원/월, 교통비 200,000원/월, 자가운전보조금 200,000원/월, 육아수당 100,000원/월(만 8세 이하 자녀), 학자금 100,000원/월, 연구보조비 200,000원/월. 복지포인트·명절상여·성과급은 **과세** (자주 혼동)
- **레벨체계 (안 B — IC/M 분리 트랙)**: IC L1(Jr.)~L9(Fellow/CTO) 전 레벨 진입 가능 / M1(Team Lead)~M5(VP)는 L5 이상만 / M 트랙 보상 동레벨 IC 대비 +10% / 직군별 배수 SWE·PM·INFRA=1.0, DATA=1.05, DESIGN·LEGAL=0.9, STRAT=0.95, MKT·SALES·FIN=0.85, HR=0.8, OPS=0.75

### 아이디어 백로그 (착수 미정)

**HRBP 인텔리전스 (최후순위, 리서치 후 진행)**

> 실제 기업(예: 무신사 등)의 재무제표·사업보고서를 분석해서 HR 의사결정에 연계하는 모듈.
> "몇 명을 뽑아야 하는가" 같은 비즈니스 기여형 HR 기능.

- 재무제표 업로드 → 매출/영업이익 추이 파싱
- Headcount Planning: 매출 대비 적정 인원 시뮬레이션
- Workforce Cost 분석: 인건비/매출 비율, 부서별 ROI
- 채용 계획 자동 제안: 성장률 기반 적정 채용 인원 산출
- 외부 사례 리서치 필요 (Workday Adaptive Planning, Anaplan HRBP 모듈 등)

---

## 프로젝트 개요
HR 통합 시스템 개발 프로젝트. (200명 규모 스타트업용 Workday형 웹 애플리케이션)

---

## 작업 규칙 ← Claude가 매 작업 전 반드시 읽을 것

1. **한 번에 하나의 작업만 한다.**
   - 사용자가 "확인했어" 또는 "다음" 이라고 하기 전까지 다음 작업으로 넘어가지 않는다.

2. **외부 UI 라이브러리 추가 금지.**
   - 현재 프로젝트: 커스텀 CSS (style.css) 사용. 외부 라이브러리 CDN 추가 금지.
   - ※ 사용자가 Tailwind 전환을 지시할 경우 별도 논의 후 진행.

3. **ORM 사용 금지 — 직접 쿼리만 사용.**
   - 현재 프로젝트: SQLite + 직접 SQL 쿼리 사용 (sqlite3 모듈).
   - ※ 사용자가 Supabase 전환을 지시할 경우 별도 논의 후 진행.

4. **파일 생성 시 전체 코드를 보여준다.**
   - 일부만 보여주거나 `# ... 기존 코드 ...` 형태로 생략하지 않는다.

5. **작업 완료 후 기록한다.**
   - 상세 이력은 `CHANGELOG.md`, CLAUDE.md에는 현재 상태·로드맵 체크만 갱신한다 (CLAUDE.md에 이력을 쌓지 않는다).

6. **계획 우선** — 코드 작성 전 구현 계획을 설명하고 사용자 승인을 받는다.

7. **자체 검토** — 코드 완성 후 버그, 보안 문제, 불필요한 코드를 스스로 점검한다.

8. **같은 실수 반복 금지** — 발생한 오류와 해결 방법을 기억하고 동일한 실수를 반복하지 않는다.

9. **질문 우선** — 불명확한 사항은 코드 작성 전에 반드시 먼저 질문한다.

10. **작업 완료 후 릴리즈 워크플로우** — 매 작업마다 아래 순서를 반드시 따른다:
    1. **테스트 통과 확인** — `python -m pytest tests/ -q`(계산 회귀) + `python tests/sweep.py` (132 GET 라우트 × 3역할, 500 에러 0건이어야 함. 실제로 잡아 온 버그 유형: 없는 컬럼/엔드포인트/Row 메서드 참조)
    2. `CHANGELOG.md` 맨 위에 이번 버전 항목 추가 + `git add` + `git commit -m "vX.X.X — 작업 내용"`
    3. `git push`
    4. `git tag vX.X.X` + `git push --tags`
    5. GitHub Release 작성(가능할 때) — `gh release create vX.X.X --title "vX.X.X — 제목" --notes "내용"`. ※ `.github/workflows/release.yml`은 현재 없음(태그 push만으로 릴리즈가 자동 생성되지 않음). gh/권한이 없으면 CHANGELOG가 릴리즈 노트 역할
    6. **서버 재시작** — 기존 python 프로세스 kill 후 `python run.py` 백그라운드 실행
    7. 사용자에게 **바뀐 점 요약** + **테스트 체크리스트** 제공

---

## 기술 스택
- Backend: Python (Flask)
- Frontend: HTML / 커스텀 CSS (`static/css/design-system.css` 중심, 랜딩은 `landing.css`) / Vanilla JavaScript
- Database: SQLite (sqlite3 직접 쿼리, ORM 사용 안 함)

## 역할 구조
- HR Admin: 전체 기능
- Recruiter: 채용 요청서·입사 예정자 (공고·지원자·면접은 Hire)
- Manager: 담당 팀원 성과 관리
- Employee: 본인 정보 조회 및 신청

---

## 로컬 실행
```bash
cd C:\Users\lg\hr-system
python run.py        # ← 항상 이걸 쓸 것 (포트 5000 자동 정리 후 기동)
# → http://localhost:5000
```

> `python app.py` 직접 실행 금지 — 구버전 서버가 남아 충돌 발생

## DB 초기화·마이그레이션
```bash
# 서버를 내리고 실행 — 스키마 보강 + 데모 시드(직원 147명). 운영 VM은 deploy/update.sh가 대신 돌린다
python migrate_db.py
python run.py
python training_company.py --reset   # (선택) Grow 교육용 연습 회사 처음 상태로
```

---

## 매일 개발 일지 작성 규칙

"오늘 일지 정리해줘" 라고 하면:

1. `git log --since="1 day ago"` 로 오늘 변경사항 확인
2. 오늘 대화에서 승헌씨 반응 파악:
   - 어디서 막혔는지 / 어떤 오류에 당황했는지
   - 뭔가 됐을 때 반응 / 몰라서 질문했던 것들
3. 아래 형식으로 정리해서 출력:

---
완료한 것:
막혔던 것:
해결 방법:
오늘의 인상적인 순간:
AI가 본 승헌씨 오늘의 모습: (가감없이, 솔직하게)
내일 할 것:
---

## 블로그 맥락

이 내용은 claude.ai에서 블로그 글로 변환됨.

목적: 취업용 + 개인 기록 반반
독자: 지인 (현재 네이버 서이웃 공개)
컨셉: 비전공자가 Claude Code로 ATS 만드는 과정
톤: 구어체, 솔직함, 포장 없음, AI 시각 포함
제목: 그날그날 자유롭게
