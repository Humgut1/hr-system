# TalentCore — HR Management System

A full-featured HR platform built with Flask, designed for startups and small-to-mid sized companies. Built as a portfolio project by a non-CS-major using Claude Code.

> 비전공자가 Claude Code로 만든 HR 통합 시스템 제작기 → [블로그 보러가기](https://blog.naver.com/humgut1)

---

## Live Demo

**[hr-system-production-5c51.up.railway.app](https://hr-system-production-5c51.up.railway.app)**

| Role | Email | Password |
|------|-------|----------|
| HR Admin | admin@company.com | changeme! |
| Manager | manager@company.com | changeme! |
| Employee | employee@company.com | changeme! |
| Recruiter | recruiter@company.com | changeme! |

---

## Features

### People Management
- Employee directory — add, edit, deactivate
- Department & position hierarchy
- Org chart visualization
- Role-based access control (Admin / Manager / Employee / Recruiter)

### Attendance
- Leave requests (annual, half-day, sick, remote, outing)
- Manager approval / rejection workflow
- Team calendar with department filter
- Daily check-in / check-out
- Automatic working-day calculation & remaining leave balance

### Payroll
- Monthly payslip generation
- Korean 4대보험 auto-calculation (2026 rates)
  - National Pension 4.5%, Health Insurance 3.545%, Employment Insurance 0.9%
- Income tax & local income tax
- Non-taxable allowances (meal ₩200k, transport ₩100k)
- Print-ready payslip layout
- Employment & career certificate issuance (PDF/print)

### Performance
- Review cycles (active / closed)
- Goal setting with KPI / OKR categories and SMART guide
- 5-point rating scale with weighted average
- Self-review and manager review
- 360° peer review (upward review, Google-style questions)
- Calibration board with rule-based AI summary

### Recruiting
- Job posting management (draft / open / closed)
- 8-stage applicant pipeline kanban board
- Activity log per applicant
- Recruiter-only access control

### Announcements & Org
- Pinned announcements
- Hierarchical org chart (Division → Group → Department → Team)

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
git clone https://github.com/Humgut1/hr-system.git
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

## About

This project was built as a portfolio piece documenting what a non-CS-major can build with AI-assisted development (Claude Code). The full build log is on my blog.

**Stack decisions:**
- SQLite over PostgreSQL — simplicity first, SaaS migration planned later
- No ORM — direct SQL for full control and learning
- No frontend framework — Vanilla JS + Jinja2 to keep the stack minimal
