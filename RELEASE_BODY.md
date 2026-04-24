## v0.26.0 — Workday-style UX Overhaul + People Analytics

### UX Redesign (v0.26.1 ~ v0.26.2)
- Sidebar restructured into **Me / Team / Admin** collapsible categories
- Each category remembers expand/collapse state via localStorage
- **Global search bar** in header — press `/` to focus, arrow keys to navigate, searches all menu items
- All 4 dashboards rebuilt with **Hero Banner** showing today's date and pending action count

### Inbox & Quick Actions (v0.26.2)
- **Unified Inbox** on Admin and Manager dashboards — surfaces pending leave, certificate requests, personnel actions, and termination requests in one place with direct action buttons
- **Quick Actions grid** — icon-style buttons for the most common tasks per role

### People Analytics Dashboard (v0.26.3)
- New `/analytics` route (Admin only)
- **Headcount by Department and Grade** — horizontal bar charts
- **Monthly Turnover Trend** — bar chart for last 12 months
- **Leave Utilization by Department** — color-coded by usage rate
- **Attrition Risk Table** — simplified Deloitte model (tenure, compa-ratio, performance grade, leave pattern) → risk score 0–100
- **Compa-ratio Summary** — actual salary vs grade benchmark
- HR Maturity indicator (Stage 1: Descriptive Analytics)

### Verified
- `python -m py_compile app.py database.py payroll_utils.py` ✅
- `GET /dashboard` (admin/manager/employee) → 200 ✅
- `GET /analytics` → 200 ✅
