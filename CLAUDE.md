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

## 다음 작업 목록

1. **모바일 반응형 점검** — 근태 홈, 연봉 기준표 모바일 레이아웃 확인
2. **배포 준비** — gunicorn 설정, .env 파일 가이드
3. **추가 기능** — 사용자 요청 시

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
