# TalentCore HR System Progress

## Accounts

| Role | Email |
|------|-------|
| HR Admin | admin@company.com |
| Manager | manager@company.com |
| Employee | employee@company.com |
| Recruiter | recruiter@company.com |

## Completed Through v0.24.0

### Step 20
- Employee detail page
- 3-step offboarding wizard

### Step 21
- Overtime / night work calculation
- Flex schedule block planner

### Step 22
- Company setup wizard
- Company settings page
- Guest onboarding / demo flow

### Step 23
- Personnel action history
- Employee detail reporting chain / direct reports
- Manager-based Reporting Line section on org chart

### v0.24.0
- Employee self-service termination request page
- Manager / HR termination queue
- Manager review and HR approval flow
- Offboarding task generation and completion tracking
- Final termination completion with severance linkage

### v0.25.0
- Multi-stage approval workflow expansion (Workday style)
- Attendance: Employee -> Manager (Review) -> HR (Final Approval)
- Personnel Actions: Draft -> HR Approval -> Auto-update master data
- Certificate Requests: Purpose-based request -> HR Review -> Digital Issuance

## Verification

- `python -m py_compile app.py database.py payroll_utils.py export_utils.py migrate_db.py`
- Flask test client:
  - admin login success
  - `GET /employees/1` -> `200`
  - `GET /org` -> `200`
  - `GET /termination/my` -> `200`
  - `GET /termination/requests` -> `200`
  - `GET /termination/requests/new/1` -> `200`
  - employee request submission -> manager review -> HR approval
  - `offboarding_tasks` generated: `5`
  - request status moved to `in_progress`

## Next

1. People Analytics dashboard
2. Holiday overtime pay with holiday DB integration
3. Mobile responsiveness review
4. Deployment readiness

## Local Run

```bash
cd C:\Users\lg\hr-system
python app.py
```

## DB Reset

```bash
rm -f hr_system.db
python app.py
```
