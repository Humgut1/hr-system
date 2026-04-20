# HR 통합 시스템 — Claude 작업 규칙

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

## 다음 작업 목록

1. **21단계** — 연장·야간·휴일 수당 자동 계산 (근로기준법 §56)
2. **22단계** — 인사발령 이력 + 조직도 리포팅 라인
3. **23단계** — People Analytics 대시보드 (이직위험도, 임금공정성)
4. **모바일 반응형 점검** — 근태 홈, 연봉 기준표 모바일 레이아웃 확인
5. **배포 준비** — gunicorn 설정, .env 파일 가이드, Railway Volume 설정

---

## 로컬 실행
```bash
cd C:\Users\lg\hr-system
python app.py
# → http://localhost:5000
```

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
