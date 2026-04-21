# TalentCore — HR Management System

A full-featured HR platform built with Flask, designed for startups and small-to-mid sized companies. Built as a portfolio project by a non-CS-major using Claude Code.

> 비전공자가 Claude Code로 만든 HR 통합 시스템 제작기

---

## Live Demo

**[hr-system-production-5c51.up.railway.app](https://hr-system-production-5c51.up.railway.app)**

| Role | Email | Password |
|------|-------|----------|
| Guest (read-only) | guest@talentcore.com | guest1234! |

> 전체 기능 체험은 게스트 계정으로 가능합니다. 데이터 수정은 불가합니다.

---

## Features

### People Management
- Employee directory with add / edit / deactivate
- Department & position hierarchy (Division → Group → Department → Team, up to 4 levels)
- Job family system (12 families) with grade-based salary table (CL1–CL9)
- Org chart visualization with expand/collapse per node
- Role-based access control — 4 roles (Admin / Manager / Employee / Recruiter) with route-level enforcement

### Attendance
- Leave types: annual, half-day AM/PM, sick, remote, outing
- Working-day auto-calculation (weekdays only, start–end date)
- Remaining leave balance calculated per Korean Labor Standards Act (근로기준법) — tenure-based accrual, not hardcoded
- Duplicate period & over-limit validation on submission
- Manager approval / rejection workflow with reason field
- Team calendar with department filter and multi-event per cell
- Daily check-in / check-out with monthly attendance grid

### Payroll
- Monthly payslip generation per employee
- **Korean 4대보험 auto-calculation (2026 rates)**
  - National Pension (국민연금) 4.5%
  - Health Insurance (건강보험) 3.545% + Long-term Care (장기요양) 12.95% of health premium
  - Employment Insurance (고용보험) 0.9%
- Income tax (소득세) + Local income tax (지방소득세) auto-calculation
- Non-taxable allowances per 소득세법 시행령 §12 — meal ₩200k, transport ₩100k excluded from tax base
- Print-optimized payslip layout (all deduction line items visible, 근로기준법 §48 compliant)
- Donut chart visualization of payroll distribution on admin screen
- **Employment certificate (재직증명서)** & **Career certificate (경력증명서)** — corporate legal format, print/PDF ready

### Performance Management

#### Goal Setting & Review Cycles
- Admin-managed review cycles (active / closed) — opening a new cycle auto-closes the previous one
- Per-employee goal registration with KPI / OKR category, weight (1–100), and SMART guide
- Progress tracking (0–100%) with visual progress bar
- Weighted average score calculation → final grade (S / A / B / C / D)

#### Self-Review
- Employees submit self-assessment per goal: 1–5 score + written comment
- Self-scores feed into the calibration data packet

#### 360° Peer Review
- HR assigns reviewer pairs per cycle (peer assignments management screen)
- Peers write qualitative reviews: Strengths + Areas for Growth
- **Upward review** (Google-style) — 5 standardized questions scored 1–5:
  1. Creates a clear shared vision for the team
  2. Gives actionable, specific feedback
  3. Does not micromanage
  4. Takes interest in my career growth
  5. Is someone I would want on my next team
- Review completion status tracked per assignee

#### Calibration
- Admin-only calibration board showing all employees in a cycle
- Per-employee data packet: self-score, manager score, peer scores, upward scores
- **Rule-based AI calibration summary** — auto-generated narrative per employee based on score patterns:
  - Detects high performer signals (all scores ≥ 4.5)
  - Flags score divergence between self and manager (gap ≥ 1.5)
  - Identifies upward feedback concerns (upward avg < 3.0)
  - Produces plain-language summary without external API calls
- HR sets final calibrated grade (S / A / B / C / D) with notes
- Calibration results stored separately from raw scores — audit trail preserved

### Recruiting
- Job posting lifecycle: draft → open → closed
- Direct applicant registration per posting (name, email, source, resume note)
- **8-stage pipeline kanban board**: Applied → Screening → Interview 1 → Interview 2 → Final → Offered → Hired / Rejected
- Drag-free stage change via dropdown — stage history auto-logged
- Activity log timeline per applicant (who changed what, when)
- Recruiter + Admin only — Manager / Employee get 403

### Announcements & Org
- Pinned announcements with notification badge (unread within 3 days)
- Hierarchical org chart with expandable nodes per department level

---

## Tech Stack

| Layer | Choice |
|-------|--------|
| Backend | Python 3 / Flask |
| Database | SQLite (direct SQL, no ORM) |
| Frontend | Jinja2 templates / Vanilla JS |
| CSS | Custom design system — Editorial Soft-Minimalism |
| Font | Plus Jakarta Sans |
| Deployment | Railway + Gunicorn |

---

## Local Setup

```bash
git clone https://github.com/your-username/hr-system.git
cd hr-system

pip install -r requirements.txt

python app.py
# → http://localhost:5000
```

DB is auto-initialized on first run with seed accounts and sample data.

To reset the database:
```bash
rm hr_system.db
python app.py
```

---

## Project Structure

```
hr-system/
├── app.py              # Flask routes & business logic (~2400 lines)
├── database.py         # Schema definition & seed data
├── payroll_utils.py    # Payroll calculation helpers
├── migrate_db.py       # DB migration script (extended seed data)
├── static/
│   ├── css/
│   │   ├── design-system.css   # Design tokens & base components
│   │   └── style.css           # App-specific styles
│   └── js/main.js              # Modal, toast, sidebar helpers
└── templates/
    ├── base.html               # App shell & sidebar
    ├── login.html
    ├── dashboard/              # Role-specific dashboards (4)
    ├── employees/
    ├── attendance/
    ├── leave/
    ├── payroll/
    ├── performance/
    ├── recruit/
    ├── announcements/
    ├── certificate/
    └── org/
```

---

## Design System

Custom CSS design system based on the **Editorial Soft-Minimalism** spec:

- **No-Line rule** — no `1px solid` borders; sections separated by background color layers
- **Surface hierarchy** — `surface` → `surface-container-low` → `surface-container-lowest`
- **Large radius** — cards at `2rem (32px)`, modals at `3rem (48px)`
- **Gradient CTA** — primary buttons use `linear-gradient(135deg, #2b5bff, #6b8eff)`
- **Color** — `#151c23` for text (never pure black), `#2b5bff` primary blue used sparingly

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `HR_SECRET_KEY` | `dev-only-change-in-prod` | Flask session secret |
| `HR_DEV_PASSWORD` | `changeme!` | Seed account password |
| `FLASK_DEBUG` | `` | Set to `true` to enable debug mode |
| `COMPANY_NAME` | `주식회사 탤런트코어` | Company name on certificates |
| `COMPANY_REG_NO` | `000-00-00000` | Business registration number |
| `COMPANY_CEO` | `대표이사` | CEO name |
| `COMPANY_ADDRESS` | `서울특별시 강남구 테헤란로 000` | Company address |
| `COMPANY_TEL` | `02-0000-0000` | Company phone |

---

## Deployment (Railway)

1. Connect GitHub repo to Railway
2. Set **Networking → Public Networking → Port** to `8080`
3. Add environment variables above as needed
4. Railway auto-deploys on every `git push`

Procfile is already configured:
```
web: gunicorn app:app --bind 0.0.0.0:$PORT
```

---

## Changelog

### v0.16.0 — 2026-04-20
**Data Export & Certificate Hub**

#### Added
- **Excel Export** (`/export`) — 7 download endpoints for HR data
  - Employee directory (name, dept, position, job family, hire date, status)
  - Monthly payroll detail (all deduction line items + totals row)
  - Annual payroll summary (per-employee annual gross / net / tax totals)
  - Attendance log (leave requests by year or month)
  - Performance goals by review cycle (progress, self-score, comment)
  - Applicant pipeline (all postings × all stages)
- **Certificate Hub** (`/certificates`) — unified document issuance center
  - Admin can issue certificates on behalf of any employee
  - TalentCore-issued: 재직증명서, 경력증명서, 퇴직확인서, 급여명세서, 근로소득 원천징수영수증
  - External-only notice: 건강보험, 국민연금, 고용보험 (links to issuing agencies)
- **퇴직확인서** — corporate legal format with company seal block
- **근로소득 원천징수영수증** — annual income summary + monthly breakdown table

#### Changed
- Sidebar: added **Reports** section (admin-only) with export link
- Sidebar: Documents section consolidated to single Certificate Hub link

---

### v0.18.0 — 2026-04-20
**특별휴가 + 스마트 휴가 신청 UI**

#### Added
- 휴가 유형 6 → 13종 확장
  - 출산전후휴가 (근로기준법 §74, 90일 고정)
  - 배우자출산휴가 (남녀고용평등법 §18의2, 10일 고정)
  - 육아휴직 (남녀고용평등법 §19, 최대 365일)
  - 가족돌봄휴직 (남녀고용평등법 §22의2, 최대 90일)
  - 경조사휴가, 예비군·민방위, 대체휴무
- 휴가 신청 폼 전면 개편: 유형 카드 선택 UI
  - 선택 시 법령 근거 + 설명 즉시 표시
  - 연차 유형: 잔여 연차 실시간 표시
  - 법정 특별휴가: 올해 사용일 / 남은 한도 표시
  - 고정일수 유형: 시작일 선택 시 종료일 자동 계산

---

### v0.21.0 — 2026-04-21
**연장·야간 수당 자동계산 + 유연근무 블록 스케줄러**

#### Added
- 연장·야간 수당 자동계산 (근로기준법 §56)
  - 체크아웃 시 정규/연장/야간 근무 분 자동 분리 저장
  - 시급 = 월 기본급 ÷ 209시간, 가산율 50%
  - 근태 홈에 이번 달 연장·야간 누적 통계 표시
- 유연근무 블록 스케줄러 (`/attendance/flex-schedule`)
  - 시간×요일 그리드 (07:00~22:00 세로, 월~금 가로)
  - 1시간 단위 셀 클릭 → 팝업에서 15분 단위 시작/종료·유형 설정
  - 블록 유형: 오피스 / 재택 / 점심시간
  - 코어타임(10:00~16:00) 구간 하이라이트, 미충족 경고
  - 초안 저장 / 매니저 제출 / 주 단위 네비게이션
- 유연근무 승인 (`/attendance/flex-approvals`)
  - 미니 그리드로 팀원 스케줄 한눈에 확인
  - 승인 / 반려(사유 입력) 처리
- 직원 근무제 유형 설정: 일반근무 / 선택근로제 / 탄력근로제 / 재량근로제

---

### v0.20.0 — 2026-04-20
**직원 프로필 상세 페이지 + 퇴직 3단계 마법사**

#### Added
- 직원 프로필 상세 페이지 (`/employees/<id>`)
  - 5개 탭: 기본정보 / 인사정보 / 급여 / 근태·휴가 / 성과
  - 연차 잔여 현황 바, 최근 급여·휴가·목표 요약 표시
  - 정보 수정 / 퇴직 처리 버튼 프로필 페이지에 집중 (목록에서 제거)
- 퇴직 3단계 마법사 (`/employees/<id>/offboard`)
  - 1단계: 퇴직 유형(5종) + 최종 근무일 + 상세 사유
  - 2단계: 퇴직금 미리보기 (근로기준법 §34, IRP 안내)
  - 3단계: 오프보딩 체크리스트 (시스템 접근 해제, 장비 반납, 인수인계 등) + 처리 메모
  - 최종 제출 시 퇴직 처리 + 퇴직금 기록 한 번에 완료

#### Changed
- 직원 목록: 행 전체 클릭 → 프로필 상세 이동
- 직원 정보 수정 후 → 직원 목록 대신 프로필 상세 페이지로 이동

---

### v0.19.0 — 2026-04-20
**퇴직금 자동 계산 + 최저임금 체크**

#### Added
- 퇴직금 계산 페이지 (`/employee/<id>/severance`)
  - 근로기준법 §34 산식: 평균임금 × 30일 × (근속일수 ÷ 365)
  - 최근 3개월 급여명세서 기준 평균임금 자동 계산 (달력 일수 적용)
  - 1년 미만 근속 시 퇴직금 미발생 안내
  - 퇴직금 지급 기록 저장 (`severance_payments` 테이블)
  - IRP 의무 이전 안내 (300만원 초과 시, 근로자퇴직급여보장법 §9)
- 최저임금 체크 (2026년 기준: 월 2,096,270원)
  - 급여 수정 시 기본급이 최저임금 미달이면 경고 플래시 메시지

#### Changed
- 퇴사 처리(`/employee/<id>/resign`) 완료 후 퇴직금 계산 페이지로 자동 이동

---

### v0.17.0 — 2026-04-20
**직원 데이터 Workday 양식 + 사번(emp_no)**

#### Added
- 사번 (`TC-00001` 형식) 자동 부여 — 직원 추가 시 자동 생성
- 직원 폼 신규 필드: 직군, 직속상관, 고용형태(정규직/계약직/인턴), 퇴사일, 퇴사사유
- 직원 엑셀 export: 10개 → 19개 컬럼 Workday 양식
  - 사번, 고용형태, 직속상관, 기본급(월), 근속연수, 최근성과등급, 최근급여변경일 추가
- GitHub Actions 자동 릴리즈: `v*` 태그 push 시 GitHub Release 자동 생성

---

### v0.15.0 — 2026-04-19
**Guest Account & Onboarding Tutorial**

#### Added
- **Guest account** (`guest@talentcore.com`) — read-only, all GET routes accessible, POST blocked with flash message
- **2-step onboarding** for first admin login
  - Step 1: Feature selection (9 toggleable modules — Attendance, Payroll, Performance, etc.)
  - Step 2: Company info input (name, reg no., CEO, address, phone) — persisted to DB
- `company_settings` table — DB-backed company info overrides env vars on certificates
- Feature gating in sidebar — sections hide/show based on enabled features per tenant
- `features_enabled` column on users — comma-separated feature keys

---

### v0.14.0 — 2026-04-18
**360° Peer Review & Calibration**

#### Added
- Peer review assignments — HR assigns reviewer pairs per cycle
- Peer review writing — Strengths + Areas for Growth (qualitative)
- Upward review — 5 Google-style questions scored 1–5
- **Calibration board** — per-employee score packet (self / manager / peer / upward)
- Rule-based calibration summary — auto-generated narrative, no external API
- HR sets final calibrated grade (S–D) with notes; results stored separately from raw scores

---

### v0.13.0 — 2026-04-17
**Attendance Home & Performance UX**

#### Added
- Attendance home (`/attendance/home`) — daily check-in/out, leave balance bar, monthly grid
- Self-review per goal — 1–5 score + written comment
- Progress tracking per goal (0–100%) with visual bar
- Weighted average → final grade (S / A / B / C / D) result card

---

### v0.12.0 — 2026-04-16
**Payroll & Certificates**

#### Added
- Korean 4대보험 auto-calculation (2026 rates) — NPS, Health, LTC, Employment Insurance
- Income tax + local income tax auto-calculation
- Non-taxable allowances per 소득세법 시행령 §12 (meal ₩200k, transport ₩100k)
- 재직증명서 & 경력증명서 — corporate legal format, print/PDF ready
- Grade-based salary table (CL1–CL9 × 12 job families)
- 100-employee seed data with realistic department/grade distribution

---

## About

This project was built as a portfolio piece documenting what a non-CS-major can build with AI-assisted development (Claude Code). The full build log is on my blog.

**Stack decisions:**
- SQLite over PostgreSQL — simplicity first, SaaS migration planned later
- No ORM — direct SQL for full control and learning
- No frontend framework — Vanilla JS + Jinja2 to keep the stack minimal
