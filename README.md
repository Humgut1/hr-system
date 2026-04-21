# TalentCore HR Management System

Flask 기반 HR 통합 시스템입니다. 스타트업과 중소 규모 조직을 기준으로 인사, 근태, 급여, 성과, 증명서, 조직도, 채용 기능을 하나의 앱으로 묶었습니다.

## Live Demo

- URL: https://hr-system-production-5c51.up.railway.app
- Guest account:
  - Email: `guest@talentcore.com`
  - Password: `guest1234!`

## Core Features

### People Management
- 직원 등록, 수정, 비활성화
- 부문-그룹-부서-팀 계층 구조
- 직군(Job Family) 및 급여 테이블
- 직원 상세 프로필
- 인사발령 이력 관리
  - 부서 이동
  - 직급 변경
  - 역할 변경
  - 고용형태 변경
  - 직속상관 변경
  - 급여 변경

### Attendance
- 휴가 신청 / 승인 / 반려
- 반차, 병가, 재택, 외근, 법정 특별휴가
- 연차 자동 계산
- 출퇴근 체크인 / 체크아웃
- 유연근무 블록 스케줄러 및 승인 화면

### Payroll
- 월별 급여명세서 생성
- 4대보험 및 세액 자동 계산
- 최저임금 체크
- 퇴직금 계산 및 저장
- 급여 관리 화면 및 급여 차트

### Performance
- 평가 주기 관리
- KPI / OKR 목표 관리
- 자기평가
- Peer / Upward Review
- Calibration 보드

### Recruiting
- 채용공고 등록 / 수정 / 마감
- 지원자 등록
- 단계별 파이프라인
- 지원자 활동 로그

### Org & Documents
- 공지사항
- 부서 조직도
- `manager_id` 기반 Reporting Line 시각화
- 재직 / 경력 / 퇴직 / 소득 증명서
- Excel Export

## Tech Stack

- Backend: Python, Flask
- Database: SQLite (`sqlite3`, direct SQL)
- Frontend: Jinja2, Vanilla JavaScript
- CSS: Custom design system
- Deployment: Railway, Gunicorn

## Local Setup

```bash
git clone https://github.com/your-username/hr-system.git
cd hr-system
pip install -r requirements.txt
python app.py
```

브라우저: `http://localhost:5000`

## Database

앱 실행 시 DB가 자동 초기화됩니다.

DB를 초기화하려면:

```bash
rm hr_system.db
python app.py
```

확장 시드/마이그레이션이 필요하면:

```bash
python migrate_db.py
python app.py
```

## Project Structure

```text
hr-system/
├─ app.py
├─ database.py
├─ migrate_db.py
├─ payroll_utils.py
├─ export_utils.py
├─ templates/
├─ static/
├─ README.md
└─ RELEASE_BODY.md
```

## Environment Variables

- `HR_SECRET_KEY`
- `HR_DEV_PASSWORD`
- `HR_GUEST_PASSWORD`
- `FLASK_DEBUG`
- `COMPANY_NAME`
- `COMPANY_REG_NO`
- `COMPANY_CEO`
- `COMPANY_ADDRESS`
- `COMPANY_TEL`

## Deployment

Railway 기준:

1. GitHub 저장소 연결
2. Public Port `8080` 설정
3. 환경변수 등록
4. 태그/푸시 또는 일반 푸시로 배포

`Procfile`:

```text
web: gunicorn app:app --bind 0.0.0.0:$PORT
```

## Changelog

### v0.23.0 - 2026-04-22
- 인사발령 이력 기능 추가
- 직원 상세 페이지에 리포팅 체인 / 직속 부하 표시 추가
- 직원 상세 페이지에 관리자용 발령 처리 모달 추가
- 조직도 페이지에 `manager_id` 기반 Reporting Line 추가
- 검증:
  - `python -m py_compile app.py database.py payroll_utils.py export_utils.py migrate_db.py`
  - Flask test client로 `GET /employees/1`, `GET /org` 응답 `200` 확인

### v0.22.0 - 2026-04-21
- 회사 설정 마법사 추가
- Guest 체험 모드 추가

### v0.21.0 - 2026-04-20
- 연장 / 야간 수당 자동 계산
- 유연근무 블록 스케줄러 추가

## Notes

- ORM 없이 direct SQL만 사용합니다.
- 새 UI 라이브러리 없이 기존 Jinja2 + Vanilla JS 구조를 유지합니다.
