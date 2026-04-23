## Termination Workflow

### Added
- Employee self-service termination request page
- Manager / HR termination queue
- Manager review and HR approval flow
- Offboarding task generation with completion tracking
- Admin finalization flow that updates employee status and writes severance records
- Navigation links for `My Termination` and `Termination Queue`

### Updated
- Employee detail and employee list now start the termination process instead of jumping straight to the old direct offboarding entry
- Legacy admin offboarding route now redirects to an open termination request when one exists

### Verified
- `python -m py_compile app.py database.py payroll_utils.py export_utils.py migrate_db.py`
- Flask test client:
  - `GET /termination/my` -> `200`
  - `POST /termination/my` -> `302`
  - manager review -> `302`
  - HR approval -> `302`
  - `GET /termination/requests/<id>` -> `200`
  - generated offboarding tasks: `5`
  - request status moved to `in_progress`
