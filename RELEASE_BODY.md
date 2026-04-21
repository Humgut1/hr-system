## Personnel Action History + Reporting Line

### Added
- `personnel_actions` table for HR actions:
  - department move
  - position / promotion change
  - role change
  - employment type change
  - manager reassignment
  - salary change
- Employee detail page improvements:
  - reporting chain for upper managers
  - direct reports list
  - personnel action history table
  - admin-only personnel action modal
- Organization chart improvements:
  - reporting-line hierarchy based on `manager_id`
  - direct-report count badges
  - manager summary cards

### Verified
- `python -m py_compile app.py database.py payroll_utils.py export_utils.py migrate_db.py`
- Flask test client:
  - admin login success
  - `GET /employees/1` -> `200`
  - `GET /org` -> `200`
