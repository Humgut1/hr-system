# HR 통합 시스템 — Claude 작업 규칙

## ⚡ 세션 시작 시 필독 (컨텍스트 리셋 후 재시작할 때)

새 세션이 시작됐다면 아래 순서로 파일을 읽고 시작할 것:

1. **이 파일 (CLAUDE.md)** — 전체 규칙 + 완료 이력 + 로드맵 확인
2. **`C:\Users\lg\hr-system\talentcore_plan.html`** — 6개 모듈 전체 기획안 (Workday 기반)
3. **`C:\Users\lg\hr-system\performance_plan.html`** — 성과관리 모듈 상세 기획안

### 현재 진행 상태 (마지막 업데이트: 2026-06-10)

- **완료된 마지막 버전:** `v0.49.0`
- **다음 작업:** `v0.50.0` — 목표 템플릿 + Merit 연동 + 보상 배수 공개
- **현재 논의 중인 내용:**
  - 성과 패치 5개를 v0.48에 통합 완료
  - 로드맵: v0.49~v0.65 (계속 진행 중)

### 핵심 결정사항 (잊으면 안 되는 것들)

- 성과 패치 5개는 **v0.48 성과관리 전면 개편에 통합** (별도 작업 안 함)
  - ① Start/Stop/Continue 피어리뷰 프레임워크
  - ② 익명성 임계값 (응답자 3명 미만 시 결과 비공개)
  - ③ 캘리브레이션 하향 시 사유 필수 입력
  - ④ 최대 1단계 하향 제한 (평균 점수 기준)
  - ⑤ 등급별 보상 배수 직원에게 사전 공개
- 보상 투명성: 글로벌 트렌드는 사전 공개 방향 (Meta=2x/1.15x, Airbnb RSU 배수 공개)
- 캘리브레이션 현재 UI → 9박스 드래그앤드롭으로 전면 교체 예정

### 참고 리서치 문서 (프로젝트 루트에 있음)

| 파일 | 내용 |
|---|---|
| `talentcore_plan.html` | Workday 기반 6개 모듈 전체 기획 (P0/P1/P2, DB 스키마, UI 설계) |
| `performance_plan.html` | 성과관리 모듈 심층 기획 (9박스 목업, 구현 순서, 의존성) |
| `peer_review_cases.html` | 글로벌·국내 10개 기업 피어리뷰 사례 연구 |
| `calibration_plan.html` | 캘리브레이션 흐름 설계 문서 |

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

5. **작업 완료 후 CLAUDE.md에 기록한다.**
   - 완료한 작업과 다음 작업 목록을 항상 최신 상태로 유지한다.

6. **계획 우선** — 코드 작성 전 구현 계획을 설명하고 사용자 승인을 받는다.

7. **자체 검토** — 코드 완성 후 버그, 보안 문제, 불필요한 코드를 스스로 점검한다.

8. **같은 실수 반복 금지** — 발생한 오류와 해결 방법을 기억하고 동일한 실수를 반복하지 않는다.

9. **질문 우선** — 불명확한 사항은 코드 작성 전에 반드시 먼저 질문한다.

---

## 기술 스택
- Backend: Python (Flask)
- Frontend: HTML / 커스텀 CSS (static/css/style.css) / Vanilla JavaScript
- Database: SQLite (sqlite3 직접 쿼리, ORM 사용 안 함)

## 역할 구조
- HR Admin: 전체 기능
- Recruiter: 채용 관리
- Manager: 담당 팀원 성과 관리
- Employee: 본인 정보 조회 및 신청

---

## 완료된 작업

### 1~10단계 (기본 기능 전체) ✅
- 로그인/로그아웃, 역할별 대시보드 4종
- 공지사항, 조직도, 직원 관리, 부서·직급 관리
- 근태 신청/승인/반려/캘린더
- 급여명세서 (4대보험 자동계산, 비과세 처리)
- 재직·경력증명서 발급
- 성과 목표/평가/주기 관리
- 채용 공고/지원자/파이프라인 칸반
- 모바일 반응형 CSS

### 11단계 ✅ — 부서·직급·직군 체계 개편 + 급여 차트 + 버그 수정
- [x] `migrate_db.py` — 부문/본부/실/팀 계층 + CL1~CL9 직급 + 직군 12개 + 연봉 기준표 + 100명 시드
- [x] `database.py` — job_families, salary_grades 테이블, birth_date/job_family_id 컬럼 추가
- [x] `app.py` — COMPANY_INFO 환경변수, 증명서 라우트, 각종 버그 수정 (아래 상세)
- [x] `templates/payroll/admin.html` — 급여 수정 모달 + Canvas 도넛 차트
- [x] `templates/certificate/employment.html` — 법인 양식 리뉴얼
- [x] `templates/certificate/career.html` — 법인 양식 리뉴얼
- [x] `templates/payroll/salary_table.html` — 직급×직군 연봉 기준표 페이지 신규 생성
- [x] `templates/performance/index.html` — cycle 선택기, user_id 그룹핑 수정

### 버그·보안 수정 (Codex 리뷰 반영) ✅
- [x] `PRAGMA foreign_keys = ON` 추가 (get_db)
- [x] 로그인 시 dept_id 세션 저장
- [x] `/logout` POST 허용 (CSRF 방지) + base.html form 변환
- [x] `employee_edit` 이메일 중복 체크
- [x] `calc_working_days` 헬퍼 — 실제 평일 근무일수 계산
- [x] `leave_my` 연차 총계 하드코딩(15) → `calc_annual_leave()` 실계산
- [x] `leave_new` 근무일수 자동계산 + 잔여연차 초과 검사 + 중복기간 검사
- [x] `attendance_approve/reject` pending 상태 확인 + manager 소속 부서 검증
- [x] payroll 생성 count — 실제 INSERT 건수만 카운트
- [x] `performance` cycle 파라미터 처리, manager 소속 제한, user_id 쿼리 추가
- [x] `debug=True` → `FLASK_DEBUG` 환경변수
- [x] `main.js` / `style.css` test-acct 데드코드 제거

---

### 12단계 ✅ — 근태 홈 + 성과관리 개선
- [x] `database.py` — checkins 테이블, performance_goals에 progress/self_score/self_comment 컬럼 추가
- [x] `app.py` — /attendance/home, /attendance/checkin, /attendance/checkout 라우트
- [x] `app.py` — /performance/goals/<id>/progress, /performance/goals/<id>/self-review 라우트
- [x] `templates/attendance/home.html` — 출퇴근 체크인/아웃 + 연차 바 + 이달 출근 그리드 + 최근 신청 내역
- [x] `templates/performance/index.html` — 진행률 바 + 자기평가 버튼 + 가중 평균 결과 카드 (S/A/B/C/D)
- [x] `templates/performance/self_review.html` — 자기평가 작성 페이지 (1~5점 + 코멘트)
- [x] `templates/base.html` — 사이드바 근태 메뉴 재구성 (홈/신청/내역/승인)

---

### 13단계 ✅ — 마무리 작업
- [x] 평가 주기 + 테스트 목표 데이터 삽입 (2026 상반기)
- [x] `requirements.txt` 생성 (Flask 3.1.3, Werkzeug 3.1.8)
- [x] 전체 23개 페이지 자동 점검 — 모든 역할(admin/employee/manager) 200 OK

---

### 14단계 ✅ — 다면평가 + 매니저 평가 + 캘리브레이션
- [x] `database.py` — peer_assignments, peer_reviews, calibration_results 테이블 추가
- [x] `app.py` — UPWARD_QUESTIONS 5개 (구글 방식), generate_calibration_summary() 규칙 기반 AI 요약
- [x] `app.py` — /performance/peer, /performance/peer/write/<id>, /performance/peer/assignments
- [x] `app.py` — /performance/calibration, /performance/calibration/<id>
- [x] 5개 새 템플릿: peer_reviews, peer_write, peer_assignments, calibration, calibration_detail
- [x] `templates/base.html` — 다면평가/배정/캘리브레이션 사이드바 링크 추가

---

### 15단계 ✅ — 게스트 계정 + 온보딩 튜토리얼
- [x] `database.py` — users 테이블 role CHECK에 'guest' 추가, onboarded/features_enabled 컬럼 추가
- [x] `database.py` — init_db() 내 컬럼 마이그레이션 (기존 DB ALTER TABLE 자동 처리)
- [x] `database.py` — 게스트 계정 seed + 앱 기동마다 guest 없으면 자동 생성
- [x] `migrate_db.py` — init_db() import + 호출 (Procfile에서 migrate_db 먼저 실행 시 안전)
- [x] `app.py` — login_required 데코레이터에 guest POST 차단 추가
- [x] `app.py` — 로그인 시 session['onboarded'] 저장
- [x] `app.py` — FEATURE_DEFS 9개 기능 목록 정의
- [x] `app.py` — /onboarding 라우트 (GET: 기능 선택 화면 / POST: 저장 후 대시보드)
- [x] `app.py` — context_processor inject_user_features() — 모든 템플릿에 user_features set 주입
- [x] `app.py` — admin 첫 로그인 시 onboarding 미완료면 /onboarding 리다이렉트
- [x] `templates/onboarding.html` — 기능 선택 카드 UI (체크박스, 전체선택/해제)
- [x] `templates/base.html` — 사이드바 기능별 show/hide 필터링 적용
- [x] `templates/base.html` — sidebar footer에 admin용 Features 링크 추가
- [x] `.claude/settings.json` — PreToolUse 훅 프롬프트 개선 (과잉 차단 방지)

---

### 16단계 ✅ — 증명서 허브 + 데이터 내보내기(Excel)
- [x] `export_utils.py` — openpyxl 기반 Excel 내보내기 유틸 (헤더 스타일, 통화 포맷, 컬럼 자동 너비)
- [x] `requirements.txt` — openpyxl==3.1.5 추가
- [x] `app.py` — /certificates 허브 라우트 (admin: 직원 선택기, 일반: 본인만)
- [x] `app.py` — /certificate/resignation 퇴직확인서
- [x] `app.py` — /certificate/income 근로소득 원천징수영수증 (연간 집계 + 월별 명세)
- [x] `app.py` — /export 허브 + 7개 내보내기 라우트 (인사/월급여/연간급여/근태/성과/채용)
- [x] `templates/certificate/hub.html` — 증명서 발급 허브 (TalentCore 발급 5종 + 외부 3종)
- [x] `templates/certificate/resignation.html` — 퇴직확인서 양식
- [x] `templates/certificate/income.html` — 근로소득 원천징수영수증
- [x] `templates/export/hub.html` — 데이터 내보내기 허브 (카테고리별 카드 + 년/월 선택기)
- [x] `templates/base.html` — Reports 섹션 추가 (admin 전용 데이터 내보내기 링크)
- [x] `templates/onboarding.html` — 온보딩 1단계: 기능 선택
- [x] `templates/onboarding_company.html` — 온보딩 2단계: 회사 정보 입력

### 17단계 ✅ — Workday 양식 직원 Export + 사번(emp_no)
- [x] `database.py` — users 테이블 emp_no, manager_id, employment_type, termination_date, termination_reason 컬럼 추가
- [x] `app.py` — export_employees() 19컬럼 Workday 양식 (사번, 고용형태, 직속상관, 기본급, 근속연수, 최근성과등급 등)
- [x] `app.py` — employee_new/edit에 신규 필드 처리 + emp_no 자동생성
- [x] `templates/employees/form.html` — 직군/직속상관/고용형태/퇴사 필드 추가
- [x] `.github/workflows/release.yml` — v* 태그 push 시 GitHub Release 자동 생성

---

### 18단계 ✅ — 특별휴가 + 스마트 휴가 신청 UI
- [x] `database.py` — leave_requests.type CHECK 확장 (출산/배우자출산/육아/병가/경조사/공가/재량 등 13종)
- [x] `app.py` — LEAVE_META 딕셔너리 (법령근거/소진여부/고정일수/최대일수 포함), leave_new 유효성 검사 강화
- [x] `templates/leave/new.html` — 카드 그리드 타입 선택기, 법령 배지, 고정일수 자동 적용, 잔여 사용량 표시

---

### 19단계 ✅ — 퇴직금 자동 계산 + 최저임금 체크
- [x] `payroll_utils.py` — MIN_WAGE_HOURLY/MONTHLY 상수, check_min_wage(), calc_severance() 추가
- [x] `database.py` — severance_payments 테이블 추가
- [x] `app.py` — employee_resign() → severance 페이지로 리다이렉트
- [x] `app.py` — /employee/<id>/severance GET/POST 라우트 (퇴직금 계산 + 기록 저장)
- [x] `app.py` — admin_payroll() update_salary: 최저임금 미달 시 경고 플래시
- [x] `templates/employees/severance.html` — 퇴직금 계산 결과 페이지

---

### 20단계 ✅ — 직원 프로필 상세 + 퇴직 마법사 UX 개편
- [x] `app.py` — TERMINATE_TYPES 딕셔너리 추가 (5종)
- [x] `app.py` — /employees/<id> GET 라우트 (employee_detail) — 탭별 데이터 조회
- [x] `app.py` — /employees/<id>/offboard GET/POST 라우트 — 3단계 마법사 + 퇴직 처리 통합
- [x] `app.py` — employee_edit 저장 후 리다이렉트: 목록 → 프로필 상세
- [x] `templates/employees/detail.html` — 5탭 프로필 페이지 (기본정보/인사정보/급여/근태/성과)
- [x] `templates/employees/offboard.html` — 3단계 마법사 (퇴직정보/퇴직금/체크리스트)
- [x] `templates/employees/list.html` — 행 클릭 시 상세 이동, 퇴직 버튼 → 마법사 링크
- [x] `templates/employees/form.html` — 수정 시 뒤로가기 → 프로필 상세

### 21단계 ✅ — 연장·야간 수당 자동계산 + 유연근무 블록 스케줄러
- [x] `payroll_utils.py` — `_calc_night_overlap()`, `calc_day_hours()`, `calc_extra_pay()` 추가 (근로기준법 §56)
- [x] `database.py` — checkins에 regular_min/overtime_min/night_min 컬럼, users에 work_type, flex_schedules/flex_blocks 테이블 추가
- [x] `app.py` — 체크아웃 시 수당 자동계산, WORK_TYPES/BLOCK_TYPES 상수, 유연근무 라우트 5개
- [x] `templates/attendance/flex_schedule.html` — 시간×요일 그리드, 클릭 팝업(15분 단위), 요약 바
- [x] `templates/attendance/flex_approvals.html` — 미니 그리드 + 승인/반려
- [x] `templates/base.html` — 유연근무 계획/승인 사이드바 링크
- [x] `templates/employees/form.html` — 근무제 유형 선택 필드

### 22단계 ✅ — 회사 설정 마법사 + Company Settings
- [x] `database.py` — company_config 테이블 (근무제도/휴가/급여/성과 전체 정책 저장)
- [x] `app.py` — get_company_config(), inject_company_config() context_processor
- [x] `app.py` — admin_setup() 5단계 마법사 (GET/POST), admin_settings() 탭 설정 페이지
- [x] `app.py` — 첫 로그인 시 setup_completed 미완료면 /admin/setup 리다이렉트
- [x] `templates/admin/setup.html` — 5단계 JS 마법사 (회사정보/근무제도/휴가/급여/성과)
- [x] `templates/admin/settings.html` — 탭형 회사 설정 재구성 페이지
- [x] `templates/base.html` — sidebar에 Company Settings 링크 추가

### 23단계 ✅ — 인사발령 이력 + 조직도 리포팅 라인
- [x] `database.py` — `personnel_actions` 테이블 추가
- [x] `app.py` — 인사발령 기록(INSERT) 및 프로필 내 이력 조회 로직
- [x] `app.py` — `manager_id` 기반 `reporting_chain` 빌드 및 조직도 반영
- [x] `templates/employees/detail.html` — 발령 이력 탭 및 매니저 변경 기능

### v0.24.0 ✅ — 퇴직 프로세스 고도화 (워크플로우)
- [x] `app.py` — 본인 퇴직 신청(`termination_my`), 매니저/HR 승인 큐(`termination_requests`)
- [x] `app.py` — 오프보딩 태스크 자동 생성 및 상태 추적
- [x] `app.py` — 최종 승인 시 직원 상태 변경 및 퇴직금(severance) 연동
- [x] 4개 새 템플릿: `termination_my`, `termination_new`, `termination_requests`, `termination_detail`

### v0.25.0 ✅ — 전사 다단계 승인 체계 (Workflow Expansion)
- [x] `database.py` — `certificate_requests` 테이블 추가, `leave_requests`/`personnel_actions` 스키마 고도화
- [x] `app.py` — 근태 다단계 승인 (신청→매니저 검토→HR 최종승인)
- [x] `app.py` — 인사발령 승인 프로세스 (기안→HR 승인 시 실제 데이터 반영)
- [x] `app.py` — 증명서 발급 워크플로우 (신청→HR 승인 시 출력 가능)
- [x] 3개 템플릿 업데이트: `attendance/list.html`, `employees/detail.html`, `certificate/hub.html`

---

### v0.26.0 ✅ — Workday 스타일 UX 전면 개편 + People Analytics
- [x] `templates/base.html` — 사이드바 Me/Team/Admin 3분류 재편, 카테고리 접기/펼치기
- [x] `templates/base.html` — 헤더 글로벌 검색바 (/ 단축키, 키보드 네비게이션)
- [x] `static/css/style.css` — nav-category, global-search, inbox-widget, quick-actions-grid 스타일 추가
- [x] `templates/dashboard/admin.html` — Hero Banner + Inbox (휴가/증명서/발령/퇴직 통합) + Quick Actions 8개
- [x] `templates/dashboard/manager.html` — Hero Banner + Inbox (팀 대기 승인) + Quick Actions
- [x] `templates/dashboard/employee.html` — Hero Banner + Quick Actions 8개 (아이콘 그리드)
- [x] `templates/dashboard/recruiter.html` — Hero Banner + Quick Actions + Pipeline Summary
- [x] `app.py` — dashboard admin/manager inbox 쿼리 추가
- [x] `app.py` — /analytics 라우트 (people_analytics) 추가
- [x] `templates/analytics/index.html` — 부서/직급별 Headcount, 월별 퇴직 추이, Leave 사용률, Attrition Risk, Compa-ratio

### v0.27.0 ✅ — 1:1 미팅
- [x] `database.py` — `one_on_ones`, `one_on_one_actions` 테이블 추가
- [x] `app.py` — `/one-on-ones` 목록, `/one-on-ones/new` 예약, `/one-on-ones/<id>` 상세 (노트/액션아이템)
- [x] `templates/one_on_ones/list.html` — 탭 필터(전체/예정/완료/취소) + 오픈 액션 카운트
- [x] `templates/one_on_ones/form.html` — 미팅 예약 폼
- [x] `templates/one_on_ones/detail.html` — 노트 인라인 편집 + 액션아이템 추가/완료 토글
- [x] `templates/base.html` — Me > 1:1 Meetings, Team > 1:1 Schedule 링크 추가

### v0.28.0 ✅ — 전자계약
- [x] `database.py` — `contract_templates`, `contracts` 테이블 추가
- [x] `app.py` — 계약서 목록/템플릿 생성/발행/보기/서명/거절/취소 7개 라우트
- [x] `templates/contracts/list.html` — 계약서 + 템플릿 탭
- [x] `templates/contracts/template_form.html` — 템플릿 생성 (기본 양식 자동 채우기)
- [x] `templates/contracts/issue.html` — 직원 선택 + 템플릿 불러오기 + 발행
- [x] `templates/contracts/view.html` — 계약서 본문 + 전자서명 / 거절 / 취소 액션 + 인쇄
- [x] `templates/base.html` — Me > Contracts 링크 추가
- [x] `.claude/settings.json` — PostToolUse 훅 제거, 작업 완료 시 한 번만 체크

---

### v0.29.0 ✅ — 휴일근로 수당 + 공휴일 관리
- [x] `database.py` — `public_holidays` 테이블 + 2026년 한국 공휴일 15일 시드
- [x] `database.py` — `checkins`에 `holiday_min` 컬럼 추가 (마이그레이션 포함)
- [x] `app.py` — 체크아웃 시 `public_holidays` 조회 → 공휴일이면 `holiday_min` 자동 저장
- [x] `app.py` — 급여 생성/조회 시 `holidays` → `public_holidays` 참조 통일
- [x] `app.py` — `/admin/holidays` 라우트 (연도별 조회, 추가, 삭제)
- [x] `templates/admin/holidays.html` — 공휴일 목록 + 모달 추가 + 법령 기준 안내
- [x] `templates/base.html` — Admin > Public Holidays 링크 추가

---

### v0.28.1 ✅ — 전자계약 기능 수정 + 계약서 문서 양식 개편
- [x] `app.py` — `CONTRACT_DEFAULTS` 전면 재작성 (근로/NDA/수습/프리랜서 4종, 인라인 스타일 적용 실제 계약서 양식)
- [x] `app.py` — `contract_issue` POST: `{{employee_name}}`, `{{salary}}`, `{{department}}` 등 발행 시 실데이터로 자동 치환
- [x] `app.py` — `contract_issue` 직원 쿼리에 `dept` 필드 추가 (기존 `emp.dept` 오류 수정)
- [x] `templates/contracts/view.html` — 계약서 본문에 문서 용지 래퍼(`#contract-paper`) 추가, 인쇄 스타일 개선
- [x] `templates/contracts/template_form.html` — 편집/미리보기 탭 분할 패널 추가 (실시간 렌더링 확인)
- [x] `templates/contracts/issue.html` — 편집/미리보기 탭 추가, `emp.dept` 참조 수정

### v0.31.0 ✅ — 전자계약 버그 수정 + 1:1 미팅 제거 + 모바일 반응형 개선
- [x] `app.py` — contracts_list `created_at` 누락 수정, contract_issue 직원 쿼리 `department_id` 수정
- [x] `app.py` — contract_sign/reject `session.get('user_name')` 수정
- [x] `app.py` — attendance 라우트 `reviewed_count` 누락 수정
- [x] `app.py` — 1:1 미팅 라우트 3개 제거
- [x] `templates/base.html` — 1:1 미팅 사이드바 링크 제거
- [x] `static/css/style.css` — 모바일 반응형 인라인 그리드 오버라이드, 검색바 축소
- [x] `Procfile` — gunicorn `--workers 2 --timeout 120` 추가

### v0.32.0 ✅ — 시드 데이터 고도화 (포트폴리오 데모용)
- [x] `migrate_db.py` — `_seed_transactional()` 함수 추가
- [x] 급여명세서 600건 (100명 × 6개월, 실제 4대보험 계산)
- [x] 공지사항 +7건 (건강검진/사무실이전/OKR킥오프/스톡옵션 등)
- [x] 휴가신청 40건 (승인/대기/반려 혼합)
- [x] 출퇴근 기록 ~920건 (30명 × 45일)
- [x] 성과 주기 2개 + 목표 400개 + 리뷰 200건
- [x] 전자계약 5건 (서명 완료)

---

### v0.32.1 ✅ — 채용 시드 데이터
- [x] `migrate_db.py` — 채용공고 10건 (오픈 8건/마감 2건), 지원자 57명, 파이프라인 로그 207건

---

### v0.33.0 ✅ — SaaS 멀티테넌시 + 랜딩 페이지 + 토스페이먼츠
- [x] `master_db.py` — master.db 관리 (tenants / subscriptions / billing_logs / tenant_users)
- [x] `app.py` — get_db() 테넌트 격리 (session['tenant_id'] → tenant_N.db)
- [x] `app.py` — login() master.db 기반 테넌트 라우팅
- [x] `app.py` — /signup 회사 등록 (14일 트라이얼, 테넌트 DB 자동 생성)
- [x] `app.py` — /billing 구독 현황 대시보드
- [x] `app.py` — /billing/register 토스페이먼츠 카드 등록 (billingKey 발급)
- [x] `app.py` — /billing/charge 월별 High-Water Mark 청구
- [x] `app.py` — /billing/webhook 토스 결제 상태 동기화
- [x] `database.py` — init_db(db_path) 파라미터 추가 (신규 테넌트 DB 초기화)
- [x] `migrate_db.py` — _seed_master_db() 데모 테넌트(1) 자동 등록
- [x] `templates/landing/index.html` — 랜딩 페이지 (히어로/기능/요금제/CTA)
- [x] `templates/landing/signup.html` — 회사 가입 폼
- [x] `templates/billing/dashboard.html` — 구독 현황 + 결제 내역
- [x] `templates/billing/register.html` — 토스페이먼츠 카드 등록 위젯

### 요금제
- ₩1,000/인/월 · High-Water Mark 방식 · 최소 인원 없음 · 14일 무료 체험

---

### v0.38.0 ✅ — SaaS 구독 루프 + 접근 제한 미들웨어
- [x] `master_db.py` — compute_sub_state() 상태 머신 (trial/grace/locked/active), migrate_subscriptions()
- [x] `app.py` — subscription_guard() before_request, inject_sub_state() context_processor
- [x] `templates/base.html` — 구독 배너 + Admin 사이드바 Subscription 링크
- [x] `templates/billing/dashboard.html` — 5종 상태별 히어로 카드

### v0.39.0 ✅ — 주52시간 감시 + 복리후생 설정 + 퇴직 종합 정산
- [x] `payroll_utils.py` — BENEFIT_CATALOG (13종), BENEFIT_CATEGORY_LABELS, calc_prorated_salary(), calc_unused_leave_pay(), calc_separation_settlement(), calc_payslip() extra_benefits 파라미터 추가
- [x] `database.py` — benefit_configs, employee_benefit_overrides 테이블 추가, payslips.bonus_pay/benefits_json 마이그레이션
- [x] `app.py` — /admin/benefits 복리후생 설정, /admin/overtime-monitor 주52시간 감시, 급여생성 benefit 반영, employee_severance 종합 정산으로 전면 교체
- [x] `templates/admin/benefits.html` — 비과세/복리후생/상여별 항목 설정 (활성화 토글, 금액/% 입력)
- [x] `templates/admin/overtime_monitor.html` — 52시간 위반/경고 테이블 + 추이 차트
- [x] `templates/employees/severance.html` — 퇴직금+미사용연차수당+일할급여 종합 정산 페이지
- [x] `templates/base.html` — Benefits, 52h Monitor 사이드바 링크 추가

### 비과세 항목 (소득세법 기준)
- 식대 200,000원/월, 교통비 200,000원/월, 자가운전보조금 200,000원/월
- 육아수당 100,000원/월 (만 8세 이하 자녀), 학자금 100,000원/월, 연구보조비 200,000원/월
- 복지포인트·명절상여·성과급 → 과세 (비과세 아님 — 자주 혼동되는 항목)

---

### v0.43.0 ✅ — 근로기준법 준수 근태 계산 고도화
- [x] `payroll_utils.py` — 법정 상수 추가: DAILY_WORK_MAX(480)/WEEKLY_WORK_STD(2400)/WEEKLY_OT_MAX(720)/WEEKLY_TOTAL_MAX(3120)/WEEKLY_WARNING(2880)/MONTHLY_STD_HOURS(209)
- [x] `payroll_utils.py` — `_calc_break_min()`: §54 법정 휴게시간 (4h→30분, 8h→60분)
- [x] `payroll_utils.py` — `calc_day_hours()`: raw_min/break_min/total_min 분리, §54 자동 공제
- [x] `payroll_utils.py` — `get_week_bounds()`: 월요일 기산 주 경계 계산
- [x] `payroll_utils.py` — `calc_weekly_hours()`: 주 누적 근로시간 + 52시간 위반/경고 판정
- [x] `payroll_utils.py` — `check_min_wage()`: effective_hourly(실효시급) + monthly_hours 파라미터 추가
- [x] `database.py` — checkins.break_min 컬럼 추가 (마이그레이션 포함)
- [x] `app.py` — `do_checkout()`: break_min 저장, 주 52시간 실시간 체크, 위반 시 매니저·HR 알림 자동 발송
- [x] `app.py` — `attendance_home()`: weekly_hours 데이터 템플릿 전달
- [x] `app.py` — `overtime_monitor()`: %Y-%W 방식 → 월요일 기준 date() 계산으로 교체
- [x] `templates/attendance/home.html` — 주간 근무시간 바 + 상태 배지 (정상/경고/위반)

---

### v0.44.0 ✅ — 근태 통합 탭 + 버그 수정
- [x] `app.py` — `do_checkout()`: 체크인=체크아웃 동일 시각 → 0분 처리 (경고 플래시)
- [x] `app.py` — `_fix_checkin_data()`: 기존 오염 데이터(check_in==check_out) 자동 정리
- [x] `app.py` — `attendance_home()` 전면 확장: 홈/휴가/캘린더/승인 4탭 데이터 통합
- [x] `app.py` — `leave_new`, `leave_cancel`, `attendance_approve/reject` 리다이렉트 → `/attendance/home?tab=*`
- [x] `templates/attendance/home.html` — 4탭 통합 페이지 전면 재작성 (JS 탭 전환, 인라인 신청 폼)
- [x] `templates/base.html` — My Leaves 제거, Attendance 단일 링크, Team>근태 승인 링크

### v0.45.0 ✅ — 임금 시스템 개선 4종 + 급여 이력 시드
- [x] `database.py` — salary_history 테이블 추가 (변경일/변경자/전후금액/사유)
- [x] `app.py` — POST /payroll/preview 라우트 (INSERT 없이 계산 결과만 JSON 반환)
- [x] `app.py` — generate 완료 시 전 직원 인앱 알림 자동 발송 (add_notification)
- [x] `app.py` — update_salary: 변경 전 값 조회 → salary_history 자동 기록
- [x] `app.py` — GET/POST /payroll/bulk-raise (전체/부서별 % 일괄 인상 + salary_history 기록)
- [x] `app.py` — employee_detail에 salary_history 쿼리 추가
- [x] `templates/payroll/admin.html` — 미리보기 모달 (AJAX fetch → 직원별 예상 금액 테이블 → 확정)
- [x] `templates/payroll/admin.html` — 급여 수정 모달에 변경 사유 필드 추가
- [x] `templates/payroll/bulk_raise.html` — 일괄 인상 페이지 (시뮬레이션 + 요약 카드 + 적용)
- [x] `templates/employees/detail.html` — 급여 탭에 salary_history 타임라인 추가
- [x] `templates/base.html` — Admin 사이드바 Bulk Raise 링크 추가
- [x] `migrate_db.py` — salary_history 시드 로직 추가 (약 60% 직원, 1~2회 이력)
- [x] DB 직접 실행 — salary_history 67건 생성 완료

### v0.46.0 ✅ — 사이드바 전면 재편 + 한글화 + 페이지 통합
- [x] `templates/base.html` — 사이드바 카테고리 4개로 재편 (나/회사/팀/관리자) + 전체 한글화
- [x] `templates/base.html` — 관리자 섹션 10개 항목 → 급여관리·채용관리·분석보고서·설정 4개로 통합
- [x] `templates/base.html` — Recruiter 중복 섹션 제거, 관리자>채용관리로 통합
- [x] `templates/base.html` — Performance+Peer Review → 성과 단일 항목으로 통합
- [x] `templates/payroll/admin.html` — 급여관리 내부 탭 추가 (급여생성/일괄인상/상여지급/환급신청)
- [x] `templates/analytics/index.html` — 분석·보고서 탭 통합 (인력분석 + 데이터내보내기)
- [x] `templates/admin/settings.html` — 바로가기 탭 추가 (부서·공휴일·복리후생·52h 등 8개)
- [x] `app.py` — people_analytics에 cycles/today_year/today_month 변수 추가

---

### v0.47.0 ✅ — 성과관리 전면 개편 + 기획안 문서
- [x] `templates/performance/index.html` — 직원/매니저 뷰 재설계 (To-Do 위젯, 3탭)
- [x] `templates/performance/goal_form.html` — AI 어시스턴트 패널 (SMART 체크)
- [x] `templates/performance/calibration.html` — 캘리브레이션 UI (점수바, 이상감지, 공개)
- [x] `templates/performance/peer_assignments.html` — 부서별 그룹핑 + 드롭다운 검색
- [x] `app.py` — calibration 라우트 분리, AI 목표 어시스턴트, peer_assignments 개선
- [x] `database.py` — calibration_results 컬럼 추가 (self_avg, peer_avg, mgr_avg, is_shared)
- [x] `peer_review_cases.html` — 글로벌·국내 피어리뷰 사례 리서치 문서 (10개 기업)
- [x] `performance_plan.html` — Workday 기반 성과관리 모듈 개편 기획안
- [x] `talentcore_plan.html` — TalentCore 전체 모듈 개편 로드맵 (6개 모듈)

### v0.48.0 ✅ — 성과관리 Big Bang (패치 5개 통합)
**피어리뷰 개선**
- [x] `database.py` — calibration_results에 potential_score, box_position, downgrade_reason 컬럼 추가 (마이그레이션 포함)
- [x] `templates/performance/peer_write.html` — Start/Stop/Continue 3섹션 프레임워크로 전면 교체 (🟢Continue/🔴Stop/🟡Start), 익명성 안내 배너 추가
- [x] `app.py` — peer_review_write POST: SSC 3필드 모두 required 서버 검증
- [x] `app.py` — peer_reviews_page: 익명성 임계값 (응답자 3명 미만 시 결과 비공개), peer_count/peer_threshold_met 변수 추가
- [x] `templates/performance/peer_reviews.html` — 임계값 미달 시 잠금 배너, SSC 라벨(Continue/Stop/Start)로 표시 전환

**캘리브레이션 개편**
- [x] `app.py` — calibration confirm: GRADE_NUM 기반 최대 1단계 하향 제한, downgrade_reason 필수 검증
- [x] `app.py` — calibration confirm: potential_score DB 저장
- [x] `templates/performance/calibration.html` — 하향 시 사유 입력 필드 동적 표시 (JS), Potential Score (Low/Mid/High) 컬럼 추가

### v0.49.0 ✅ — Talent Card + 후계자 계획
- [x] `database.py` — succession_plans 테이블 추가 (포지션/현직자/후보/Readiness/메모)
- [x] `app.py` — talent_card() 라우트: 성과 히스토리 + 9박스 위치 + 목표 진행률 + 후계자 현황
- [x] `app.py` — succession() 라우트: 포지션별 후계자 추가/삭제 (매니저·어드민)
- [x] `templates/performance/talent_card.html` — 9박스 위치 시각화 + 등급 타임라인 + 목표 + 후계자
- [x] `templates/performance/succession.html` — 포지션별 후계자 목록 + 추가 모달
- [x] `templates/base.html` — 팀 사이드바에 후계자 계획 링크 추가

---

## 앞으로의 로드맵

> Workday HCM 기반 TalentCore 전면 개편 (talentcore_plan.html 기준)
> 6개 모듈 57개 기능 전체 포함
> ※ 성과 패치 5개는 Phase 1에 통합 (별도 작업 없음)

---

### Phase 1 — 성과관리 완성 (v0.48 ~ v0.50)

> 패치 5개(SSC 피어리뷰/익명성 임계값/하향 사유/1단계 제한/보상 배수 공개)를
> 기획안 성과 기능과 함께 한 번에 구현

#### v0.48.0 — 성과관리 전면 개편 (Big Bang)
**피어리뷰 개선 (패치 통합)**
- [ ] `peer_write.html` — Start / Stop / Continue 3섹션으로 교체
- [ ] `app.py` — 익명성 임계값: 응답자 3명 미만 시 결과 비공개

**캘리브레이션 개편 (패치 통합)**
- [ ] `database.py` — calibration_results에 potential_score, box_position, downgrade_reason 컬럼 추가
- [ ] `calibration.html` — 하향 시 사유 필수 입력 (JS 감지 + 서버 검증)
- [ ] `app.py` — 최대 1단계 하향 제한 로직 (평균 점수 기준)

**9박스 신규**
- [ ] `templates/performance/calibration_9box.html` — 드래그앤드롭 3×3 그리드
- [ ] `app.py` — /performance/calibration/9box GET/POST
- [ ] `calibration.html` — Potential Score 입력 UI (Low/Mid/High, 3가지 기준)
- [ ] `calibration.html` — Forced Distribution 패널 (현재 배분 vs 목표 배분 바 차트)

**등급 히스토리**
- [ ] `templates/employees/detail.html` — 성과 탭 등급 타임라인 (사이클별)
- [ ] `app.py` — is_shared 토글 라우트 + 직원 성과 페이지 등급 공개

#### v0.49.0 — Talent Card + 후계자 계획
- [ ] `database.py` — succession_plans 테이블 (포지션/후보/Readiness)
- [ ] `templates/performance/talent_card.html` — 직원 종합 카드 (성과 히스토리 + 9박스 위치 + 스킬 + 후계자)
- [ ] `app.py` — /performance/talent-card/<user_id> 라우트
- [ ] `templates/performance/succession.html` — 포지션별 후계자 목록 (매니저 이상)

#### v0.50.0 — 목표 템플릿 + Merit 연동 + 보상 배수 공개
- [ ] `database.py` — goal_templates 테이블
- [ ] `app.py` — /performance/goal-templates 관리 라우트
- [ ] `goal_form.html` — 템플릿에서 가져오기 버튼
- [ ] `app.py` — bulk-raise에서 캘리브레이션 등급 기반 인상률 자동 제안
- [ ] `templates/performance/index.html` — 직원에게 등급별 보상 배수 사전 공개 (company_config 연동)

---

### Phase 2 — 보상·급여 강화 (v0.51 ~ v0.53)

#### v0.51.0 — Salary Band + Compa-Ratio + Merit Matrix
- [ ] `database.py` — salary_grades에 min_salary/mid_salary/max_salary 컬럼 추가
- [ ] `database.py` — merit_matrix 테이블 (performance_grade × compa_band → increase_pct)
- [ ] `payroll_utils.py` — calc_compa_ratio() 함수 추가
- [ ] `templates/payroll/admin.html` — Compa-Ratio 컬럼 + 색상 배지 (Red-circle/정상/Under)
- [ ] `templates/employees/detail.html` — 급여 탭에 밴드 내 위치 슬라이더 시각화
- [ ] `templates/admin/settings.html` — Merit Matrix 5×3 인라인 편집 그리드

#### v0.52.0 — ACR 워크플로우 (Annual Compensation Review)
- [ ] `database.py` — compensation_review_cycles, compensation_reviews 테이블
- [ ] `app.py` — ACR 주기 생성/오픈/마감 라우트
- [ ] `templates/payroll/acr.html` — 매니저: 팀원 인상안 입력 (Merit Matrix 가이드 표시)
- [ ] `templates/payroll/acr_review.html` — HR: 전체 인상안 검토/승인
- [ ] `app.py` — ACR 승인 시 salary_history 자동 기록 + 급여 업데이트

#### v0.53.0 — Pay Equity + Total Compensation Statement + 상여 고도화
- [ ] `templates/analytics/index.html` — 보상 분석 탭: Compa-Ratio 점 산포도, 이상치 목록
- [ ] `app.py` — /payroll/total-compensation/<user_id> 라우트
- [ ] `templates/payroll/total_comp.html` — Base + 상여 + 복리후생 + 퇴직금 적립 연간 합산 (인쇄용)
- [ ] `database.py` — grade_bonus_config 테이블
- [ ] `app.py` — 급여 생성 시 성과등급 PI 자동 계산 연동

---

### Phase 3 — HCM Core (v0.54 ~ v0.55)

#### v0.54.0 — 인터랙티브 조직도 + 셀프서비스
- [ ] `templates/org/chart.html` — SVG 재귀 트리 (Vanilla JS, 외부 라이브러리 없음)
- [ ] `app.py` — /org-chart 라우트 (노드 클릭 → 슬라이드인 패널)
- [ ] `templates/profile.html` — 직원 셀프서비스 편집 (전화번호/주소/비상연락처)
- [ ] `templates/employees/form.html` — 급여 수정 모달에 Min─●─Max 밴드 슬라이더

#### v0.55.0 — 스킬 관리 + 헤드카운트 + 인사발령 고도화
- [ ] `database.py` — employee_skills, employee_certs, department_headcount 테이블
- [ ] `app.py` — 스킬/자격증 CRUD, 만료 30일 전 알림
- [ ] `templates/employees/detail.html` — 스킬 & 자격증 탭
- [ ] `app.py` — Future-dated 인사발령 (effective_date 미래 → pending, 기동 시 자동 적용)
- [ ] `templates/employees/list.html` — 다중 필터 (직군/직급/고용형태/성과등급) + Excel 즉시 다운로드

---

### Phase 4 — 근태·스케줄 (v0.56 ~ v0.58)

#### v0.56.0 — Work Schedule 시스템
- [ ] `database.py` — work_schedules, user_schedule_assignments 테이블
- [ ] `app.py` — 스케줄 정의 (고정/유연/재량) + 직원/부서별 배정
- [ ] `app.py` — do_checkin() 에 스케줄 대비 출결 자동 판정 (present/late/early_leave/absent)
- [ ] `templates/admin/schedules.html` — 스케줄 관리 페이지

#### v0.57.0 — 팀 출결 그리드 + 근태 허브 고도화
- [ ] `templates/attendance/team_grid.html` — 팀원×날짜 2D 색상 그리드 (매니저 뷰)
- [ ] `app.py` — attendance_home() 역할별 렌더링 분기 (Employee/Manager/HR Admin)
- [ ] `database.py` — leave_requests에 half_day_slot 컬럼 (am/pm/null)
- [ ] `templates/attendance/home.html` — 반차 오전/오후 구분 UI

#### v0.58.0 — OT 승인 + 연차 이월 + 개인 리포트
- [ ] `database.py` — overtime_requests, leave_balances 테이블
- [ ] `app.py` — OT 사전/사후 승인 워크플로우
- [ ] `app.py` — 연도 교체 시 연차 이월 계산 (carry_over_max 정책)
- [ ] `templates/attendance/home.html` — 개인 월간 리포트 카드 (총 근무/OT/야간/출근일수 + 전월 대비)
- [ ] `app.py` — 최소 11시간 휴식 미준수 감지 플래그

---

### Phase 5 — 채용 ATS 고도화 (v0.59 ~ v0.62)

#### v0.59.0 — Requisition 승인 워크플로우
- [ ] `database.py` — job_requisitions 테이블 (부서장 승인 → HR 승인 → 공고 자동 생성)
- [ ] `app.py` — Requisition CRUD + 3단계 승인 라우트
- [ ] `templates/recruit/requisition.html` — 채용 요청서 폼 + 승인 현황

#### v0.60.0 — 면접 관리
- [ ] `database.py` — interview_rounds, interviews, interview_feedback 테이블
- [ ] `app.py` — 면접 라운드 생성 + 인터뷰어 배정 + 피드백 수집
- [ ] `templates/recruit/interview.html` — 라운드별 피드백 통합 뷰

#### v0.61.0 — 오퍼 관리 + 지원자→직원 전환
- [ ] `database.py` — offers 테이블 (draft→sent→accepted/rejected/expired)
- [ ] `app.py` — 오퍼 생성/발송/수락 라우트
- [ ] `templates/recruit/offer.html` — 오퍼 레터 (인쇄용)
- [ ] `app.py` — 오퍼 수락 시 /employees/new 데이터 프리필 자동 연결

#### v0.62.0 — 채용 인텔리전스
- [ ] `app.py` — 스킬 매칭 스코어 (공고 required_skills ∩ 지원자 skills)
- [ ] `app.py` — Evergreen 상시채용 (is_evergreen + 자식 공고 파생)
- [ ] `templates/recruit/dashboard.html` — 채용 통계 (퍼널/Time-to-Fill/소스별 합격률)
- [ ] `app.py` — 인터뷰 안내/합격/불합격 이메일 알림 템플릿 3종

---

### Phase 6 — 복리후생 (v0.63 ~ v0.65)

#### v0.63.0 — 직원 셀프서비스 + Enrollment Event
- [ ] `database.py` — benefit_enrollment_events 테이블
- [ ] `app.py` — /me/benefits 라우트 (현재 혜택 목록 + 연간 한도 vs 사용액)
- [ ] `templates/me/benefits.html` — 복지포인트 잔액 + 프로그레스 바
- [ ] `app.py` — 입사 시 Enrollment Event 자동 생성 + 인박스 CTA

#### v0.64.0 — Election Status 이력 + 복지포인트 자동 지급
- [ ] `database.py` — benefit_elections 테이블 (Future/Current/Previous/Historical 4상태)
- [ ] `app.py` — 기존 overrides 데이터 elections로 마이그레이션
- [ ] `database.py` — welfare_point_grants 테이블
- [ ] `app.py` — 연간 복지포인트 자동 입금 + 잔액 소멸 로직

#### v0.65.0 — Life Event + Benefit Group + Passive Event
- [ ] `app.py` — 생애사건(결혼/출산/입사) 기록 → Enrollment Event 자동 트리거
- [ ] `templates/employees/detail.html` — 생애사건 탭
- [ ] `database.py` — benefit_configs에 group_key/group_label 추가
- [ ] `app.py` — Passive Event 룰 엔진 (근속/나이 조건 배치 평가)
- [ ] `templates/analytics/index.html` — 복리후생 분석 탭 (항목별 지출/부서별 1인당 비용)

---

### 전체 규모 요약

| Phase | 버전 | 핵심 내용 | 예상 세션 |
|---|---|---|---|
| 1 | v0.48~v0.50 | 성과관리 완성 (패치 통합) | 3~4세션 |
| 2 | v0.51~v0.53 | 보상·급여 강화 | 3~4세션 |
| 3 | v0.54~v0.55 | HCM Core | 2~3세션 |
| 4 | v0.56~v0.58 | 근태·스케줄 | 3세션 |
| 5 | v0.59~v0.62 | 채용 ATS | 4세션 |
| 6 | v0.63~v0.65 | 복리후생 | 3세션 |
| **합계** | **v0.48~v0.65** | **57개 기능** | **18~21세션** |

> 참고 문서: `talentcore_plan.html` (전체 기획), `performance_plan.html` (성과 상세)

---

## 로컬 실행
```bash
cd C:\Users\lg\hr-system
python run.py        # ← 항상 이걸 쓸 것 (포트 5000 자동 정리 후 기동)
# → http://localhost:5000
```

> `python app.py` 직접 실행 금지 — 구버전 서버가 남아 충돌 발생

## DB 마이그레이션 실행 (11단계 작업 후)
```bash
# 서버를 내리고 실행
python migrate_db.py
python app.py
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
