# HR 통합 시스템 — Claude 작업 규칙

## ⚡ 세션 시작 시 필독 (컨텍스트 리셋 후 재시작할 때)

새 세션이 시작됐다면 아래 순서로 파일을 읽고 시작할 것:

1. **이 파일 (CLAUDE.md)** — 전체 규칙 + 완료 이력 + 로드맵 확인
2. **`C:\Users\lg\hr-system\saas_plan.md`** — ★ 국내 HR SaaS 재포지셔닝 플랜 (2026-07-13 확정) — **v1.0까지 모든 작업의 기준 문서, 반드시 읽을 것**
3. **`C:\Users\lg\hr-system\talentcore_plan.html`** — 6개 모듈 전체 기획안 (Workday 기반, 참고용)
4. **`C:\Users\lg\hr-system\performance_plan.html`** — 성과관리 모듈 상세 기획안 (참고용 — 재개편안은 saas_plan.md §4가 우선)

### 현재 진행 상태 (마지막 업데이트: 2026-07-14)

- **완료된 마지막 버전:** `v1.1.0` (v1.0.0은 Phase B 완결 마일스톤 태그)
- **배포 완료:** v0.99.4(Phase B 전체)까지 Oracle Cloud VM 배포 완료 (SSH 키: `C:\Users\lg\Downloads\ssh-key-2026-07-04 (1).key`, `deploy/update.sh`로 git pull + migrate_db + systemctl restart). v1.1.0은 로컬 완료 — Phase C 묶어서 배포 예정
- **다음 작업: `saas_plan.md` §6 실행 순서를 따를 것 (Phase A부터 순서대로)**
  1. ~~Phase A-1: 웹훅 서명 검증~~ ✅ v0.96.0 완료
  2. ~~Phase A-2: CSRF 토큰 전면 적용~~ ✅ v0.97.0 완료
  3. ~~Phase A-3: 감사 로그~~ ✅ v0.98.0 완료
  4. ~~Phase A-4: 테넌트 DB 자동 백업~~ ✅ v0.99.0 완료 (**VM에서 `bash deploy/setup_backup.sh` 1회 실행 필요 — 다음 배포 때 같이**)
  5. ~~Phase A-5: 비밀번호 정책~~ ✅ v0.99.1 완료 — Phase A(보안 기준선) 전체 완료
  6. ~~Phase A 전체 VM 배포 + setup_backup.sh~~ ✅ 완료 (2026-07-13, 백업 cron 등록 + 즉시 1회 백업 확인)
  7. ~~Phase B-6: CSV 직원 임포트~~ ✅ v0.99.2 완료
  8. ~~Phase B-7: 요금제 3계층 + 메뉴 다이어트~~ ✅ v0.99.3 완료
  9. ~~Phase B-8: 연차촉진 알림 + 급여명세 발송~~ ✅ v0.99.4 완료
  10. ~~Phase B-9: 테넌트 온보딩 셀프서비스 점검~~ ✅ 완료 (2026-07-14, 코드 변경 없음 — 가입→로그인→설정 마법사→CSV 임포트(매니저 체인 포함)→직원 로그인→근태 접속까지 신규 테넌트로 전수 검증, 전부 정상. 유일한 404는 구버전 라우트(/onboarding, 제거된 것)로 정상) — Phase B 전체 완료
  11. ~~Phase B 배포 (v0.99.2~0.99.4)~~ ✅ 완료 (2026-07-14, `deploy/update.sh` 실행 — git pull(이미 최신 상태였음)+migrate+재시작, 운영 DB에 `leave_promotion_logs`·`audit_logs` 테이블 + master.db `tenants.plan` 컬럼(데모 테넌트=enterprise) 확인, 랜딩/로그인 200 OK + 랜딩 요금제 섹션 노출 확인, gunicorn 에러 로그 깨끗)
  12. ~~Phase C-10: 성과관리 재개편~~ ✅ v1.1.0 완료 — **다음: Phase C-11: 입사 예정자 기능 (saas_plan.md §5)**
  3. 이후 Phase B(CSV 임포트·요금제 3계층·연차촉진), C(성과 재개편·입사예정자), D — 상세는 saas_plan.md
  - (보류) 온보딩 투어 확장 여부, 도메인 설정(승헌씨 직접)
- **v0.91~v1.1.0 완료 내역 요약:**
  - v1.1.0 — **성과관리 재개편 (Phase C-10, saas_plan.md §4)**: ①주기 상태머신 — `performance_cycles.stage`(goal→progress→review→calibration→appeal→closed) + `include_peer`(다면평가 주기별 토글) + `appeal_until`, 주기 관리 화면에 스텝퍼·단계 전환 버튼(appeal 진입 시 등급 자동 공개+7일 이의기간+알림, 미처리 이의 있으면 종료 차단). ②목표 승인 워크플로우 — `performance_goals.approval_status`(draft/submitted/confirmed/returned)+`return_comment`, 직원 제출(3~5개·가중치 합 100% 검증) → 팀장/HR 확정·반려(사유 필수, 알림), 확정 전 평가 불가, 제출 후 수정 불가, draft 목표 삭제 가능. ③단계별 게이팅 — 목표 등록은 goal 단계만, 자기평가·팀장평가·다면평가는 review 단계만, 진행률은 calibration부터 잠금, 캘리브레이션 확정은 review/calibration 단계만. ④이의제기 신규 — `grade_appeals` 테이블(주기당 1인 1회 UNIQUE), 직원 신청(10자 이상) → `/performance/appeals`에서 팀장(직속만)/HR 인용(등급 조정+calibration 반영)·기각, 전 과정 알림+감사 로그. ⑤다면평가 게이팅 — include_peer=0이면 사이드바 '다면평가 배정'(context processor `peer_enabled`)·직원 다면평가 탭·관리자 다면평가 관리 탭 숨김+라우트 차단. ⑥기존 데이터 마이그레이션: active 주기→review, 기존 목표→confirmed. 부수 수정: index.html 다면평가 관리 패널의 잘못된 calibration 링크→peer_assignments. 테스트 31케이스 통과 (제출 검증/반려·재제출/단계 차단/하향 사유/이의 1회 제한/종료 차단 등)
  - v0.99.4 — **연차촉진 알림 + 급여명세 발송 (Phase B-8)**: ①연차사용촉진(§61) — `leave_promotion_logs` 테이블(법적 증빙용 발송 이력), `/admin/leave-promotion` 화면(잔여연차 보유자 목록 + 1차/2차 촉진 선택 발송 + 이력 표시), 인앱 알림+이메일 동시 발송, 설정 바로가기에 링크. ②급여명세 이메일(§48 교부 의무) — `send_payslip_email()`(요약 금액+상세 링크), 급여 생성 2개 경로(compensation/admin_payroll) 모두에 best-effort 발송 훅. `_send_simple()` 공통 헬퍼, SMTP 미설정 시 데모 모드
  - v0.99.3 — **요금제 3계층 + 메뉴 다이어트 (Phase B-7)**: master.db `tenants.plan` 컬럼(core/growth/enterprise, 기본 growth, 데모 테넌트=enterprise). `PLAN_PRICES`(2,500/4,500/7,000원), `PLAN_FEATURES` 게이팅 — Growth: performance/recruiting/onboarding/welfare/peer_review, Enterprise: +succession/talent_advanced/comp_advanced/data_wizard. `inject_plan` context processor(`plan_features` set)로 사이드바 메뉴 + 보상관리 급여구조·ACR 탭 + 분석 데이터마법사 탭(버튼+패널 모두) 게이팅. 청구 금액이 요금제 단가 기반으로 변경(`get_plan_price`). SaaS 슈퍼어드민 테넌트 상세에 요금제 변경 폼(`/saas/tenants/<id>/plan`). 랜딩에 3계층 요금제 섹션 신규. 주의: 로그인(`session.clear()`) 후 CSRF 토큰 재발급 — 테스트 시 로그인 후 토큰 재취득 필요
  - v0.99.2 — **CSV 직원 일괄 임포트 (Phase B-6, 실고객 진입로)**: `/employees/import` 2단계 플로우(업로드→검증 미리보기→초기 비밀번호 지정 후 확정). UTF-8(BOM)+CP949 모두 지원, 검증(필수값/이메일 형식·중복(DB+파일 내)/부서·직급·직군 이름 매칭/고용형태/날짜 정규화/급여 콤마 파싱), 오류 행은 제외하고 유효 행만 등록, 매니저이메일 2차 매핑(같은 파일 내 상호참조 가능), 사번 자동생성+employee_salary+master.db 테넌트 유저 등록+peak headcount 갱신, 감사 로그 기록. 템플릿 CSV는 실제 등록된 부서/직급/직군 이름으로 동적 생성. 직원 목록에 'CSV 임포트' 버튼. 임시 검증 데이터는 `static/uploads/imports/`(gitignore)에 토큰 파일로 저장 후 확정 시 삭제
  - v0.99.1 — **비밀번호 정책 (Phase A-5, Phase A 완결)**: `validate_password()` — KISA 기준 8자 이상 + 영문/숫자/특수문자 중 2종 이상. 적용 4지점: 회원가입, 직원 등록, 직원 수정(비번 변경 시), 프로필 비밀번호 변경. 폼 힌트 문구 갱신. 통합 테스트 시 주의: 로그인하면 `session.clear()`로 CSRF 토큰이 재발급되므로 로그인 후 토큰을 다시 받아야 함
  - v0.99.0 — **테넌트 DB 자동 백업 (Phase A-4)**: `backup_db.py` 신규 (sqlite3 온라인 백업 API로 무중단 스냅샷, master.db+hr_system.db+tenant_*.db 전체, 최근 14개 보관 자동 정리, `--list`/`--restore` 지원, 복원 시 기존 파일 `.pre-restore` 보존). 로컬은 run.py APScheduler 매일 03:00, 운영은 `deploy/setup_backup.sh`로 cron 등록(미실행 상태 — 다음 배포 때 1회 실행). `backups/` gitignore. 참고: `tenant_None.db` 파일 발견 — 어딘가 tenant_id=None으로 DB를 만든 버그 흔적, 추후 조사 필요
  - v0.98.0 — **감사 로그 (Phase A-3)**: `audit_logs` 테이블 + `log_audit()` 헬퍼(best-effort, 실패해도 본 요청 안 막음). 계측 지점: 로그인 성공/실패, 타인 프로필 민감정보 열람, 급여 변경 6곳(개별수정 2·일괄인상 2·성과연동·ACR), 직원 문서함 업/다운/삭제, 직원정보 수정, 캘리브레이션 등급 확정, Excel 내보내기 전수(`before_request`로 export_* 엔드포인트 일괄). `/admin/audit-logs` 조회 화면(카테고리/행위/기간/검색 필터 + 요약 카드), 사이드바 관리자 섹션에 링크. 로컬 admin 비번은 `admin1234!`로 재설정됨(로컬 한정)
  - v0.97.0 — **CSRF 방어 전면 적용 (Phase A-2)**: 세션별 토큰(`secrets.token_hex(32)`) + `before_request` 전역 검증(웹훅 3곳만 예외, 실패 시 403). 템플릿 149개 폼을 개별 수정하지 않고 `static/js/csrf.js`가 3중 자동 주입 — ①submit 이벤트 캡처 ②`form.submit()` 프로토타입 패치(프로그래매틱 제출 9곳 커버) ③fetch 래핑(X-CSRF-Token 헤더). base.html 미상속 독립 템플릿 6곳(login/signup/saas 3종/admin setup)에도 meta+스크립트 삽입. 테스트 7종 + 브라우저 실검증 완료
  - v0.96.0 — **웹훅 서명 검증 (Phase A-1)**: Slack `/slack/command`·`/slack/interactive`에 공식 v0 서명 검증(`SLACK_SIGNING_SECRET`, HMAC-SHA256 + 타임스탬프 5분 리플레이 방지, 실패 시 401), 토스 `/billing/webhook`은 서명 헤더가 없어 공식 권장 방식인 결제 재조회 검증(`TOSS_SECRET_KEY`로 GET /v1/payments/{paymentKey}, 재조회 실패 시 400 + 페이로드 무시). 시크릿 미설정 시 경고 로그만 남기고 통과(개발 모드). 테스트 클라이언트로 7개 시나리오 검증 완료
  - v0.91.0 — (버전 번호만 존재, 상세 내역 CLAUDE.md 미기록 상태 — 확인 필요)
  - v0.92.0 — 데모 배너에 "웹사이트 보기" 링크 추가, `landing()`이 데모 세션이면 대시보드 자동 리다이렉트 안 되도록 예외 처리
  - v0.93.0 — **로그인/회원가입 데모세션 버그 수정**: `login()`/`signup()`도 `landing()`처럼 데모 세션이면 clear 후 폼을 정상 표시하도록 수정 (기존엔 데모 세션 있으면 로그인 버튼 눌러도 무조건 대시보드로 튕김). 로그인 페이지 죽은 "비밀번호 찾기" 링크 제거, "관리자에게 문의"→"회원가입" 문구 수정. 전체 190개 라우트 인증 데코레이터 감사 + `employee_detail()` IDOR 감사(문제 없음 확인) 완료
  - v0.94.0 — 랜딩페이지 Product Tour 스크린샷 6종 → 실제 HTML/CSS 목업으로 전면 교체, 스크롤 연동(IntersectionObserver) 버그 수정(급여·보상 탭 선택 안 되던 문제), 채용 지원자 상세 UX 정리
  - v0.95.0 — **v1 잔여 항목 완료**: 89개 GET 라우트 전수 에러 스윕(6건 500 에러 수정: export_calibration/salary_history/skills의 `u.position` 컬럼 오참조, export_contracts의 `c.contract_type`/`issued_at`/`expires_at` 오참조, admin_bonus_pay의 `performance_reviews`→`calibration_results` 오참조 + Row 직렬화 오류), UI 스윕(카드 배경 hex→CSS 변수), **직원 문서함 신규 기능**(`employee_documents` 테이블, 본인·직속매니저·admin만 접근하는 업로드/다운로드/삭제, IDOR 방지 소유권 검증), README를 Oracle Cloud 운영 주소로 갱신
- **배포 중 과거 버그 수정 이력:** `seed_default_superadmin()`이 gunicorn 멀티 워커 동시 기동 시 UNIQUE constraint 에러로 부팅 실패 → `INSERT OR IGNORE`로 수정 (반영 완료)
- **핵심 설계 결정:**
  - 체험하기(/demo)는 guest가 아닌 admin 권한으로 로그인 (모든 기능 체험 가능, `session['demo_mode']`로 쓰기(POST)만 차단), 재진입마다 세션 초기화
  - SaaS 운영자 관리 페이지(`/saas`, 계정 hunie0709/1234)는 `superadmin_required`로 별도 인증 네임스페이스(`session['superadmin_id']`) 사용
  - 로그인 폼은 2단 레이아웃(왼쪽 브랜드 패널 + 오른쪽 폼)이 정상 디자인이며, 900px 이하에서는 왼쪽 패널이 의도적으로 숨겨지는 반응형 동작 (버그 아님, 그대로 유지)
  - TalentCore MCP 서버 추가 완료 (Claude Desktop 연동), Oracle Cloud 배포 스크립트 완비 (setup/nginx/update)
  - SMTP 설정 시 실제 웰컴 이메일 발송 가능 (.env에 SMTP_HOST/USER/PASSWORD/PORT 추가)

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

10. **작업 완료 후 릴리즈 워크플로우** — 매 작업마다 아래 순서를 반드시 따른다:
    1. `git add` + `git commit -m "vX.X.X — 작업 내용"`
    2. `git push`
    3. `git tag vX.X.X` + `git push --tags`
    4. `gh release create vX.X.X --title "vX.X.X — 제목" --notes "내용"`
    5. **서버 재시작** — 기존 python 프로세스 kill 후 `python run.py` 백그라운드 실행
    6. 사용자에게 **바뀐 점 요약** + **테스트 체크리스트** 제공

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

### v0.50.0 ✅ — 목표 템플릿 + Merit 연동 + 보상 배수 공개
- [x] `database.py` — goal_templates 테이블 추가, company_config에 merit_s/a/b/c/d + bonus 배수 + show_merit_to_employee 컬럼 추가
- [x] `app.py` — /performance/goal-templates CRUD 라우트 (admin/manager)
- [x] `app.py` — /performance/goal-templates/<id>/json AJAX 엔드포인트
- [x] `app.py` — bulk-raise: mode=flat/merit 분기, Merit 모드는 캘리브레이션 등급 기반 인상률 자동 적용
- [x] `templates/performance/goal_templates.html` — 템플릿 목록 + 추가 모달 + 활성/비활성 토글
- [x] `templates/performance/goal_form.html` — 템플릿 선택 드롭다운 (선택 시 제목·설명·가중치 자동 입력)
- [x] `templates/performance/index.html` — 등급별 인상률·상여 배수 사전 공개 배너 (company_config 연동)
- [x] `templates/payroll/bulk_raise.html` — 일괄/Merit 모드 탭 전환 + 성과 등급 컬럼 + Merit 시뮬레이션

### v0.51.0 ✅ — Salary Band + Compa-Ratio + Merit Matrix
- [x] `database.py` — salary_grades에 min_salary/mid_salary/max_salary 컬럼 추가 (마이그레이션)
- [x] `database.py` — merit_matrix 테이블 추가 (performance_grade × compa_band → increase_pct, 15개 기본값)
- [x] `payroll_utils.py` — calc_compa_ratio(), compa_band(), merit_from_matrix() 추가
- [x] `app.py` — /admin/salary-bands 라우트 (급여 밴드 CRUD + Merit Matrix 편집 + Compa-Ratio 현황)
- [x] `app.py` — admin_payroll() emps 쿼리에 salary_grades join + compa_ratio 계산
- [x] `app.py` — employee_detail()에 밴드 데이터 + compa_ratio 조회 로직 추가
- [x] `templates/payroll/salary_bands.html` — 3탭 페이지 (급여 밴드 편집 / Merit Matrix / Compa-Ratio 현황)
- [x] `templates/payroll/admin.html` — Compa-Ratio 컬럼 추가 (색상 배지: 하단↓/정상/상단↑)
- [x] `templates/employees/detail.html` — 급여 탭에 밴드 포지션 슬라이더 시각화 (Min─●─Max)
- [x] `templates/base.html` — 관리자 사이드바에 급여 밴드 링크 추가

### v0.52.0 ✅ — ACR 워크플로우 (Annual Compensation Review)
- [x] `database.py` — compensation_review_cycles, compensation_reviews, grade_bonus_config 테이블 추가
- [x] `app.py` — ACR 주기 생성/오픈/마감, 매니저 제안 입력, HR 검토/승인, salary_history 자동 기록
- [x] `templates/payroll/acr_list.html` — ACR 주기 목록 + 생성 모달
- [x] `templates/payroll/acr.html` — 매니저 인상안 입력 + HR 검토/일괄승인 통합 페이지

### v0.53.0 ✅ — Pay Equity + Total Compensation Statement + 상여 고도화
- [x] `app.py` — get_pay_equity_data() 함수, people_analytics에 pay_equity 탭 데이터 연동
- [x] `app.py` — /payroll/total-compensation/<uid> 라우트
- [x] `templates/payroll/total_comp.html` — 연간 총보상 명세서 (기본급/복리후생/퇴직금/상여 합산, 인쇄용)
- [x] `templates/analytics/index.html` — Pay Equity 탭 추가 (Compa-Ratio 이상치/부서별 평균)

### v0.54.1 ✅ — 인터랙티브 조직도 + 셀프서비스 + 검색 고도화 + 프로필 권한
- [x] `app.py` — `/search` 엔드포인트 (이름/이메일/사번/부서/직급 통합 검색, 최대 8건)
- [x] `templates/base.html` — 글로벌 검색 AJAX 전환, 페이지·직원 섹션 구분 표시
- [x] `static/css/style.css` — `.search-section-label` 스타일 추가
- [x] `app.py` — `employee_detail` 권한 분리: admin=전체, manager=직속팀원, 그 외=본인만 민감탭 노출
- [x] `templates/employees/detail.html` — `can_see_sensitive` 기반 급여·근태·성과·복리후생 탭 조건부 표시
- [x] `templates/org/index.html` — CSS 변수 오류 수정(`--text`→`--tx-1` 등), 브레드크럼 무한루프 방어

### v0.54.0 ✅ — 인터랙티브 조직도 + 셀프서비스
- [x] `database.py` — users에 address, emergency_name, emergency_phone, emergency_relation 컬럼 추가
- [x] `app.py` — /org/person/<uid> JSON API 라우트 (슬라이드인 패널용)
- [x] `templates/org/index.html` — 리포팅 라인 이름 클릭 → 슬라이드인 패널 (이메일/연락처/직급/매니저), 한글화
- [x] `templates/profile.html` — 주소 + 비상연락처(이름/관계/연락처) 셀프서비스 편집 추가, 전면 한글화

### v0.55.0 ✅ — 스킬 관리 + 헤드카운트 + 인사발령 고도화 + 부서 분류체계 개편
- [x] `database.py` — employee_skills (레벨 4단계), employee_certs (만료일), department_headcount 테이블 추가
- [x] `database.py` — personnel_actions에 applied_at 컬럼 추가 (미래 발령 예약 처리)
- [x] `database.py` — departments에 dept_type 컬럼 추가 (division/hq/dept/team), 기존 데이터 자동 분류
- [x] `app.py` — skill_add/delete, cert_add/delete 라우트 (본인·어드민·매니저 권한 체크)
- [x] `app.py` — apply_scheduled_once() before_request 훅 — 서버 기동 후 첫 요청에 만기 발령 자동 반영
- [x] `app.py` — DEPT_TYPES / DEPT_TYPE_LABEL / DEPT_TYPE_COLOR / DEPT_TYPE_PARENT_ALLOWED 상수 정의
- [x] `app.py` — employees() 다중 필터 (직군/직급/고용형태/성과등급), /search 글로벌 검색 엔드포인트
- [x] `app.py` — employee_detail() can_see_sensitive 플래그 (타 팀 매니저 민감정보 차단)
- [x] `templates/employees/detail.html` — 스킬 & 자격증 탭 신규, 민감 탭 권한 조건부 렌더링
- [x] `templates/employees/list.html` — 5종 필터 바 + optgroup 부서 드롭다운 + Excel 버튼
- [x] `templates/admin/departments.html` — 전면 재작성: 유형별 범례·인덴트·계층 뷰, 추가 폼 동적 필터링
- [x] `templates/org/index.html` — CSS 변수 오류 수정, 브레드크럼 무한루프 방지
- [x] `templates/base.html` — 글로벌 검색 AJAX 전환 (직원 실데이터 연동)

### v0.57.0 ✅ — People Analytics 통합 (분석·보고서 + HR 데이터 마법사 + 인브라우저 분석)
- [x] `templates/analytics/index.html` — HR 데이터 마법사 탭 추가 (5번째 탭)
- [x] `templates/analytics/index.html` — 마법사 CSS (소스카드/필드칩/필터/테이블/피벗/통계/차트 스타일)
- [x] `templates/analytics/index.html` — 마법사 JS (wzToggleSource/wzToggleField/wzRunPreview/wzRunExport/wzRenderChart/wzRenderPivot/wzRenderStats/wzSortTable)
- [x] `templates/analytics/index.html` — Chart.js 4.4.1 CDN 추가 (52h 차트 + 마법사 차트)
- [x] `app.py` — people_analytics()에 report_sources=REPORT_SOURCES 전달 추가
- [x] `app.py` — report_builder() → /analytics?tab=wizard 리다이렉트로 교체
- [x] `app.py` — report_preview() rows를 dict로 반환 (list → dict 수정)
- [x] `templates/base.html` — HR 데이터 마법사 사이드바 링크 제거 (Analytics로 통합)
- 분석 서브탭 4종: 테이블(정렬), 차트(bar/line/pie/doughnut), 피벗(그룹·집계함수), 통계(min/max/avg/sum/중앙값)

### v0.56.0 ✅ — Work Schedule 시스템
- [x] `database.py` — work_schedules, user_schedule_assignments 테이블 추가
- [x] `database.py` — checkins에 attendance_status, schedule_id 컬럼 추가 (마이그레이션)
- [x] `database.py` — leave_requests에 half_day_slot 컬럼 추가 (반차 오전/오후, v0.57 활용)
- [x] `app.py` — SCHEDULE_TYPES / SCHEDULE_TYPE_LABEL / SCHEDULE_TYPE_COLOR 상수
- [x] `app.py` — get_user_schedule() — 개별 배정 우선, 없으면 기본 스케줄
- [x] `app.py` — judge_attendance() / judge_early_leave() — 출결 자동 판정 함수
- [x] `app.py` — do_checkin() 개선: attendance_status·schedule_id 저장, 지각 플래시
- [x] `app.py` — do_checkout() 개선: early_leave 판정, 조퇴 플래시
- [x] `app.py` — /admin/schedules 라우트 (스케줄 CRUD + 일괄/개별 배정 + 해제)
- [x] `app.py` — admin_setup POST: 근무제도 선택 시 기본 Work Schedule 자동 생성
- [x] `templates/admin/schedules.html` — 스케줄 카드 목록 + 추가 폼 + 배정 모달(일괄/개별 탭)
- [x] `templates/base.html` — 관리자 사이드바 근무 스케줄 링크 추가
- [x] 기본 스케줄 4종 시드: 고정근무(★기본) / 선택근로제 / 재량근로제 / 임산부 단축근무

### v0.62.1 ✅ — 지원자 상세 2패널 레이아웃 + 서류 뷰어 + 파이프라인 슬라이드패널 수정
- [x] `templates/recruit/applicant_detail.html` — 좌(프로필+스테이지 타임라인) / 우(서류/면접/로그 탭) 2패널 전면 재작성
- [x] `templates/recruit/pipeline.html` — `.side-panel` 배경 투명 버그 수정, `onclick` 중복 제거
- [x] `database.py` — `applicant_documents` 테이블 추가 (이력서/자소서/포트폴리오/자격증/기타)
- [x] `app.py` — `recruit_doc_upload`, `recruit_doc_file`, `recruit_doc_delete` 라우트 추가
- [x] `seed_docs.py` — 샘플 PDF 5건 생성 스크립트

### v0.62.2 ✅ — 드래그앤드롭 수정 + 샘플 PDF 문서
- [x] `templates/recruit/pipeline.html` — `isDragging` 플래그, `e.dataTransfer.setData/getData`, `dragleave` 개선

### v0.66.0 ✅ — 채용 대시보드 (퍼널 + Time-to-Fill + 소스별 합격률)
- [x] `app.py` — `recruit_dashboard()` 라우트 추가 (퍼널/소스별/TTF/월별추이/Top5 공고 쿼리)
- [x] `templates/recruit/dashboard.html` — 채용 대시보드 페이지 신규 생성
- [x] `templates/base.html` — 사이드바에 채용 대시보드 링크 추가

### v0.73.0 ✅ — 로그인/근태 디자인 수정 + 보상 관리 통합 허브
- [x] `templates/login.html` — Google Fonts 제거, talentcore.css 단독 참조
- [x] `templates/attendance/home.html` — `<style>` 블록 내 hex 색상 전량 CSS 변수 교체
- [x] `app.py` — `/compensation` 통합 라우트 신규 (급여 생성·수정·밴드·Matrix·ACR·일괄인상 POST 핸들러 통합)
- [x] `templates/payroll/compensation.html` — 보상 관리 허브 (4탭: 급여 운영/급여 구조/보상 검토/보상 분석)
- [x] `templates/base.html` — 사이드바 급여 관리·급여 밴드·ACR 3링크 → 보상 관리 1링크로 통합

### v0.75.0 ✅ — Workday Talent 평가 4종
- [x] `database.py` — calibration_results에 retention_risk / loss_impact / achievable_level 컬럼 추가 (마이그레이션)
- [x] `app.py` — talent_card(): Flight Risk 자동 감지 (Compa<0.85 + 성과C/D + 미승진2년+ 중 2개↑)
- [x] `app.py` — /performance/talent-card/<id>/talent-flags POST 라우트 (3개 필드 저장)
- [x] `app.py` — compensation(): merit_review_rows에 flight_risk / flight_risk_reasons 추가
- [x] `templates/performance/talent_card.html` — 헤더 Retention Risk/Loss Impact 배지, Flight Risk 경고 배너, Talent 평가 입력 폼 (4항목)
- [x] `templates/payroll/compensation.html` — ✈ 이탈위험 배지 (보라색)
- [x] 시드 데이터 — 198건 calibration_results에 retention_risk/loss_impact/achievable_level 현실적 값 세팅

### v0.94.0 ✅ — Product Tour HTML 목업 전환 + 스크롤 버그 수정
- [x] `templates/landing/index.html` — Product Tour 섹션 6개 모듈(근태관리/급여·보상/성과관리/채용/조직도/분석·보고서) 스크린샷 → hand-coded HTML/CSS 목업 전면 교체 (기존 AI/MCP 섹션 패턴 재사용)
- [x] `templates/landing/index.html` — 스크롤 연동 `IntersectionObserver` 버그 수정: `{threshold: 0.6}` → `{threshold: 0, rootMargin: '-45% 0px -45% 0px'}` (스텝 높이≠뷰포트 높이일 때 활성 스텝이 씹히던 문제, 급여·보상 탭이 스크롤해도 선택 안 되던 버그 해결)
- [x] `templates/recruit/applicant_detail.html` — 합격자 처리 후 "직원으로 등록" 버튼 → 자동 등록된 직원 프로필 바로가기로 변경, "합격 처리"→"입사 확정" 문구/확인 메시지 정리
- [x] `templates/performance/talent_card.html` — 부가 설명 문구 정리
- [x] Claude Preview로 검증 (뷰포트 1400x900, 6개 모듈 전부 스크롤 시 순차 활성화 확인) + Oracle Cloud VM 배포 완료

### v0.93.0 ✅ — 로그인/회원가입 데모세션 리다이렉트 버그 + 홈페이지 라우팅·보안 감사
- [x] `app.py` — `login()`/`signup()`: `session.get('demo_mode')`가 true면 세션 clear 후 정상 폼 표시 (기존엔 데모 세션 있으면 로그인 버튼 눌러도 무조건 `/dashboard`로 리다이렉트됨 — `landing()`엔 v0.92.0에서 이미 적용됐던 예외 처리가 누락돼 있었음)
- [x] `templates/login.html` — 미구현 상태였던 "비밀번호 찾기"(`href="#"`) 더미 링크 제거, "계정이 없으신가요? 관리자에게 문의" → "회원가입" (실제 동작은 셀프서비스 가입)
- [x] 감사: 전체 190개 라우트 인증 데코레이터 전수 조사 — 데코레이터 없는 11개는 전부 의도된 공개 엔드포인트(로그인/로그아웃/데모/체험종료/SaaS로그인/랜딩/가입/웹훅) 확인
- [x] 감사: `employee_detail()` IDOR 점검 — `templates/employees/detail.html`이 급여/근태/성과/복리후생 탭을 서버사이드 Jinja `{% if can_see_sensitive %}`로 완전히 게이팅하고 있어 권한 없는 사용자에게 데이터가 HTML에 아예 포함되지 않음을 확인 (문제 없음)
- [ ] **미해결** — `/billing/webhook`(토스), `/slack/command`, `/slack/interactive` 3개 엔드포인트에 페이로드 서명 검증 없음. 정식 운영 전 시크릿 확보 후 서명 검증 추가 필요
- [x] Oracle Cloud VM 배포 완료

### v0.90.0 ✅ — 온보딩 가이드 투어 (개요 + 메뉴별 기능 설명)
- [x] `database.py` — `users.tour_completed` 컬럼 추가 (마이그레이션)
- [x] `app.py` — `login()`: `session['show_tour'] = not tour_completed`, `demo_login()`: 재진입마다 `show_tour=True`
- [x] `app.py` — `/tour/complete` POST 라우트: 데모는 세션 플래그만, 실사용자는 DB `tour_completed=1` 영구 저장
- [x] `templates/base.html` — 사이드바 전체 항목에 `data-tour="키"` 부여, 하단에 "가이드 투어 다시보기" 링크 추가
- [x] `templates/base.html` — 오버레이(스포트라이트+툴팁) UI/CSS + JS, `GLOBAL_TOUR`(최초 진입 시 사이드바 구조 5단계) / `PAGE_TOURS`(메뉴별 기능 설명, localStorage 1회) 이원화 구조
- [x] `templates/performance/index.html` — 탭 버튼에 `data-tour-page` 부여 (직원/매니저·관리자 뷰 각각)
- [x] `templates/payroll/compensation.html` — 탭 버튼에 `data-tour-page` 부여
- [x] 근태(5단계)/성과(직원·매니저 분기)/보상관리(4단계)/채용 파이프라인(1단계) 메뉴별 투어 적용, 나머지 메뉴는 후속 확장 가능한 구조로 설계
- [x] 부수 수정: `compensation()` — Compa-Ratio가 None인 직원 비교 시 500 에러 수정 ([app.py:4987](app.py:4987))
- [x] Claude Preview로 로컬 검증 (개요 투어 5단계, 근태/성과/보상관리/채용 페이지별 투어 전부 순서·완료 처리 확인) + Oracle Cloud VM 배포 완료

### v0.89.0 ✅ — 체험하기 admin 전환 + SaaS 운영자 관리 페이지
- [x] `app.py` — `_demo_write_blocked()` 헬퍼: `session['demo_mode']`가 true면 role과 무관하게 모든 POST 차단
- [x] `app.py` — `login_required`/`admin_required`/`manager_or_admin`/`recruiter_or_admin`에 `_demo_write_blocked()` 적용 (기존 guest 전용 체크 대체)
- [x] `app.py` — `/demo` 라우트 전면 수정: guest 대신 **활성 admin 계정**으로 로그인 + `session['demo_mode']=True`, 재진입 시 기존 세션 clear
- [x] `app.py` — `superadmin_required` 데코레이터 + `saas_login`/`saas_logout`/`saas_dashboard`/`saas_tenant_detail`/`saas_tenant_status` 라우트 신규
- [x] `master_db.py` — `superadmins` 테이블 신규, `seed_default_superadmin()`(hunie0709/1234 시드), `get_superadmin_by_username()`, `list_tenants_with_state()`, `set_tenant_status()`
- [x] `templates/saas/login.html`, `dashboard.html`, `tenant_detail.html` — 신규 (base.html 미상속, design-system.css만 재사용)
- [x] `templates/base.html` — 데모 배너/역할 라벨을 `session.demo_mode` 기준으로 우선 분기 ("HR Admin (Demo)")
- [x] Claude Preview로 실사용 검증: 데모 admin 전체 기능 접근 + POST 차단 확인, SaaS 로그인/대시보드/테넌트 상세/상태변경(trial↔suspended) 확인, 일반 관리자 로그인 로직 영향 없음(코드 리뷰로 확인 — 로컬 admin 비밀번호 불일치는 기존 이슈로 이번 변경과 무관)
- [ ] Oracle Cloud VM 배포 — 보류 (SSH 키 미확보, 승헌씨 직접 진행 필요)

### v0.88.0 ✅ — 랜딩 페이지 리뉴얼 + 체험하기(게스트 데모) 라우트
- [x] `templates/landing/index.html` — TalentCore 디자인 시안("Clean Enterprise") 픽셀 단위 재현, 8개 모듈 기능 그리드 + AI/MCP 차별점 섹션 + 4단 지표 섹션 신규 구성
- [x] `design-system.css` 재사용 (Pretendard 이미 임포트됨 — 별도 CDN 추가 없음), `.tc-landing` 네임스페이스로 스타일 격리
- [x] CTA `url_for()` 라우팅: 체험하기 → `/demo`, 로그인 → `/login`
- [x] `app.py` — `/demo` (`demo_login`) 라우트: 데모 테넌트 guest 계정으로 즉시 로그인 (조회 전용)
- [x] Oracle Cloud VM(`161.33.39.127`)에 SSH로 직접 배포 (`deploy/update.sh` 실행 → git pull + migrate_db + systemctl restart)

### v0.85.1 ✅ — Slack 연동 고도화 + Confluence 위키 시드
- [x] Slack DM 알림 8종 (휴가 승인/반려, 인사발령, 근태 위반 등)
- [x] Slack 슬래시커맨드 + 인터랙티브 버튼, APScheduler 기반 예약 알림 3종
- [x] Slack `users.lookupByEmail` GET 방식으로 수정 (DM 발송 버그 수정)
- [x] Confluence 위키 시드 데이터 추가
- [x] TalentCore MCP 서버 추가 (Claude Desktop 연동 — 휴가잔액/급여명세/승인대기/팀 근태 등 조회 도구)
- [x] Oracle Cloud 배포 스크립트 추가 (`deploy/setup.sh`, `nginx`, `update.sh`)
- [x] `.env` 파일 dotenv 로드 + `.gitignore` 등록

### v0.85.0 ✅ — ATS→HRIS 자동화 (오퍼 수락 시 직원 자동 등록 + 온보딩 파이프라인)
- [x] 오퍼 수락 처리 시 `/employees/new` 자동 프리필 + 온보딩 태스크 자동 생성 연동
- [x] 버그 수정: 입사 확정 500 에러 (`add_notification` type 제약 오류), 온보딩 태스크 미생성 버그
- [x] 버디 배정 — 같은 부서 필터링 + 이름·직급·부서 표시 개선
- [x] README 전면 재작성 (v0.85 기준 전체 기능 반영), Admin 계정 정보 제거

### v0.84.0 ✅ — 온보딩 자동화 (Jira 태스크 + 웰컴 이메일 + 버디 시스템 + 대시보드)
- [x] `integrations/jira.py` — 온보딩 에픽 + 10개 팀별 태스크 (IT/GA/HR/MGR) 상세 체크리스트 자동 생성
- [x] `integrations/email_sender.py` — 신규: HTML 웰컴 이메일 (Day 1 스케줄 + 8단계 할일 + 버디 소개), SMTP 미설정 시 Demo 모드
- [x] `integrations/dispatcher.py` — 전면 재작성: `on_employee_created`에 웰컴이메일·Jira에픽·버디DM 통합, `on_buddy_assigned` 신규
- [x] `database.py` — `users`에 `buddy_id`, `jira_epic_key` 컬럼 추가, `onboarding_progress` 테이블 신규
- [x] `app.py` — `employee_detail` 쿼리에 버디 JOIN 추가, `/employees/<id>/assign-buddy` 라우트, `/me/onboarding`, `/me/onboarding/<key>/done` 라우트
- [x] `templates/me/onboarding.html` — 온보딩 대시보드 (진행률 바, Day 1 스케줄, 버디 카드, 카테고리별 체크리스트 12개)
- [x] `templates/employees/detail.html` — 버디 배정 UI + 모달 추가
- [x] `templates/base.html` — 사이드바 "온보딩" 메뉴 추가

### v0.75.1 ✅ — Talent Card 진입점 추가
- [x] `templates/employees/detail.html` — 성과 탭 상단 "Talent Card 보기" 버튼 (매니저/어드민)
- [x] `templates/performance/index.html` — 팀원 목록 각 직원 이름 옆 "Talent Card" 링크

### v0.74.1 ✅ — 핵심인재·캘리브레이션 조정 배지
- [x] `app.py` — merit_review_rows: succession_plans 기반 is_key_talent, downgrade_reason 조회
- [x] `templates/payroll/compensation.html` — ⭐ 핵심 (주황) / ⚠ 조정 (빨강) 배지

### v0.74.0 ✅ — 성과 연동 급여 검토 탭
- [x] `app.py` — `merit_apply` POST 핸들러 추가 (선택 직원 급여 반영 + salary_history 기록)
- [x] `app.py` — `compensation()` GET: 캘리브레이션 등급 + 권고 인상률 + Compa-Ratio 계산 (merit_review_rows)
- [x] `templates/payroll/compensation.html` — ACR 탭 → 성과 연동 급여 검토 탭으로 교체
- [x] 3단계 스텝 표시 (성과 확정 → 검토 → 반영), KPI 바, 부서/등급 필터, 일괄 선택 반영

### v0.73.0 ✅ — 보상 관리 통합 허브
- [x] `app.py` — `/compensation` 라우트 신규 (GET/POST): 급여 관리 + 급여 밴드 + ACR 통합
- [x] `templates/payroll/compensation.html` — 4탭 허브 페이지 (급여 운영/급여 구조/보상 검토/보상 분석)
- [x] `templates/base.html` — 사이드바 급여 관련 3개 항목 → 보상 관리 단일 링크로 통합
- [x] 버그 수정: `payroll_employee` 존재하지 않는 라우트 참조 → `employee_detail`로 수정
- [x] 이전 세션 디자인 개편 완료: 89개 템플릿 hex 색상 → CSS 변수, 로그인 페이지/근태 home 스타일 수정

### v0.72.0~v0.72.5 ✅ — 디자인 개편 (UI/UX 전면 리뉴얼)
- [x] `static/css/design-system.css` — talentcore.css (447줄 모던 미니멀 인디고 디자인 시스템)로 전면 교체
- [x] `templates/base.html` — Google Fonts(Plus Jakarta Sans) 제거, style.css 링크 제거 (talentcore.css 단일 파일로 통합)
- [x] `app.py` — 랜딩 라우트 `landing.html` → `landing/index.html` 수정 + `price_per_seat=1000` 변수 추가
- [x] 89개 템플릿 전체 — 인라인 `style=""` 하드코딩 hex 색상 (~700건) 전량 CSS 변수(`var(--primary)`, `var(--green)` 등)로 교체
  - Pretendard 폰트 CDN (design-system.css @import)
  - 레거시 CSS 변수 aliases (`--border: var(--line)`, `--card: var(--surface)` 등) 유지
  - 의도적 예외 3건: `#217346`(Excel 브랜드), `#4ade80`(그린 그라데이션 끝색), `#2b5bff`(Jinja2 조건 내부)

### v0.68.0 ✅ — 복리후생 Enrollment Event + 복지포인트 자동 지급
- [x] `database.py` — `benefit_enrollment_events` 테이블 추가, `company_config`에 `welfare_point_annual` 컬럼 추가
- [x] `app.py` — `me_benefits()`: 복지포인트 잔액·이력·연간한도·enrollment event 조회 추가
- [x] `app.py` — `enrollment_complete()` 라우트: 직원 enrollment 완료 처리
- [x] `app.py` — `admin_welfare_points()` 라우트: 전체 일괄/개별 지급, 기준액 설정
- [x] `app.py` — `employee_new` POST: 입사 시 enrollment event 자동 생성 + 인박스 알림
- [x] `templates/admin/welfare_points.html` — 복지포인트 관리 페이지 신규 (현황 테이블 + 3종 버튼)
- [x] `templates/me/benefits.html` — 복지포인트 잔액 카드 + 이력 + enrollment 배너 추가
- [x] `templates/base.html` — 관리자 사이드바에 복지포인트 링크 추가

### v0.67.0 ✅ — 복리후생 셀프서비스 + 채용 사이드바 그룹화
- [x] `app.py` — `me_benefits()` 라우트 신규: 직원별 복리후생 항목 조회 (비과세/과세 분리, 월 합계)
- [x] `templates/me/benefits.html` — 복리후생 현황 페이지 (요약 카드 3종, 항목 그리드, 비과세 안내)
- [x] `templates/base.html` — "나" 섹션에 복리후생 링크 추가
- [x] `templates/base.html` — 채용 관련 링크 3개를 접이식 서브그룹으로 통합 (대시보드/공고·파이프라인/채용 요청서)
- [x] `static/css/style.css` — nav-subgroup 접이식 스타일 추가 (max-height 트랜지션)

### v0.66.0 ✅ — 채용 대시보드 (퍼널 + Time-to-Fill + 소스별 합격률)
- [x] `app.py` — `recruit_dashboard()` 라우트: 퍼널/소스별/TTF/월별 추이/Top5 데이터 집계
- [x] `templates/recruit/dashboard.html` — 신규: 채용 퍼널 바 차트, 소스별 합격률 테이블, 월별 추이, TTF 테이블, Top5 공고 바
- [x] `templates/base.html` — 채용 서브그룹 내 대시보드 링크 추가

### v0.65.1 ✅ — 채용 요청서 시드 + 오퍼 초안 저장 버그 수정
- [x] `app.py` — `recruit_offers()`: `jp.job_family_id` → `jr.job_family_id` 수정 (초안 저장 500 에러 해결)
- [x] `app.py` — `recruit_applicant_detail()`: `job_requisitions` LEFT JOIN 추가 (기본급 프리필 수정)
- [x] `seed_requisitions.py` — 채용 공고 10건에 요청서 시드 데이터 연동

### v0.65.0 ✅ — 오퍼 레터 TC 구성 개편 (인라인 편집 + 밴드 연동)
- [x] `database.py` — `offers`에 `bonus_pct`, `rsu_total`, `rsu_vest_years`, `signing_bonus`, `job_level`, `track`, `location`, `wfh_days`, `company_signer`, `company_signer_title` 컬럼 추가 (마이그레이션)
- [x] `app.py` — `recruit_offers()`: TC 컴포넌트 필드 저장, 요청서 레벨·밴드 프리필
- [x] `app.py` — `recruit_offer_update()` 신규: AJAX 인라인 편집 저장 (`/recruit/offers/<id>/update`)
- [x] `app.py` — `recruit_offer_letter()`: 연봉 밴드 Compa-Ratio 슬라이더용 데이터 조회
- [x] `templates/recruit/offer_letter.html` — 전면 재작성: 기본급/성과급/RSU/사이닝 TC 테이블, RSU 베스팅 바 차트, Compa-Ratio 밴드 슬라이더, contenteditable 인라인 편집 + 자동저장, 하단 수정 패널, Print CSS
- [x] `templates/recruit/applicant_detail.html` — 오퍼 생성 폼 TC 컴포넌트 분리 (성과급%/RSU/사이닝/재택일수/서명자), 실시간 TC 합계 미리보기

### v0.64.0 ✅ — 오퍼 관리 + 이메일 템플릿 + 지원자→직원 전환
- [x] `database.py` — `offers`, `recruit_emails` 테이블 추가; `applicants`에 `hired_from_offer_id`, `hired_employee_id` 컬럼 추가
- [x] `app.py` — `OFFER_STATUS_LABEL`, `EMAIL_TEMPLATES` 상수, `_save_recruit_email`, `_render_email_template` 헬퍼
- [x] `app.py` — `recruit_offers`: 오퍼 생성/발송/draft 저장, 오퍼 이메일 자동 기록
- [x] `app.py` — `recruit_offer_letter`: 인쇄용 오퍼 레터 페이지
- [x] `app.py` — `recruit_offer_send`: 기존 draft → sent 상태 전환 + 이메일 이력 기록
- [x] `app.py` — `recruit_email_send`: 커스텀 이메일 작성 저장 엔드포인트
- [x] `app.py` — `recruit_email_preview`: 템플릿 JSON 미리보기 (AJAX용)
- [x] `app.py` — `recruit_disqualify`: 불합격 이메일 발송 옵션 추가 (send_email 체크 시 이력 저장)
- [x] `app.py` — `recruit_hire`: 합격 처리 후 `/employees/new?from_applicant=...` 쿼리 파라미터 프리필 리다이렉트
- [x] `app.py` — `employee_new` GET/POST: `from_applicant` 처리, 직원 등록 완료 시 `hired_employee_id` 역링크
- [x] `templates/recruit/offer_letter.html` — 인쇄용 오퍼 레터 (조건 테이블 + 서명란 + Print CSS)
- [x] `templates/recruit/applicant_detail.html` — 5탭 재편 (서류/면접/오퍼/이메일/활동 로그), 오퍼 생성 폼, 이메일 작성 모달, 불합격 모달 이메일 옵션

### v0.63.0 ✅ — 채용 프로세스 전면 개선 (불합격/오퍼거절 분리 + 면접 노트 + 불합격 UX)
- [x] `database.py` — `interview_round_notes` 테이블, `applicants`에 `disqualified_from`/`disqualify_reason` 컬럼 추가
- [x] `app.py` — `TERMINAL_STAGES`, `ACTIVE_STAGES` 분리; `disqualified`(불합격) vs `rejected`(오퍼거절) 명확 구분
- [x] `app.py` — `recruit_disqualify`, `recruit_offer_reject`, `recruit_hire`, `recruit_round_note_add` 라우트 추가
- [x] `app.py` — `recruit_stage_update`: 터미널 스테이지 이동 차단, 전용 라우트로만 처리
- [x] `app.py` — `recruit_pipeline`: `rejection_reason_codes` 템플릿 변수 누락 버그 수정
- [x] `templates/recruit/applicant_detail.html` — 불합격 모달(사유코드+메모), 오퍼거절 모달, 면접 라운드 노트 UI 추가
- [x] `templates/recruit/pipeline.html` — 불합격 칸반 열 제거→하단 섹션 분리, 불합격 드래그 시 모달 표시

### v0.61.0 ✅ — 면접 관리 (라운드/배정/피드백 + 채용 컴플라이언스 로그)
- [x] `database.py` — `interview_rounds`, `interview_interviewers`, `interview_feedback`, `recruit_activity_logs` 테이블 추가
- [x] `app.py` — `ROUND_TYPE_LABEL`, `ROUND_STATUS_LABEL`, `RECOMMENDATION_LABEL`, `REJECTION_REASON_CODES` 상수
- [x] `app.py` — `log_recruit()` 헬퍼 (채용 활동 자동 로깅)
- [x] `app.py` — `recruit_round_new`, `recruit_round_assign_interviewer`, `recruit_round_remove_interviewer`, `recruit_round_complete`, `recruit_round_feedback` 라우트
- [x] `app.py` — `recruit_applicant_detail` 전면 확장 (라운드/인터뷰어/피드백/로그 통합 조회, `dict()` 변환)
- [x] `templates/recruit/applicant_detail.html` — 3탭 재작성 (기본정보 / 면접 / 활동로그)
- [x] `templates/recruit/feedback_form.html` — 별점 UI + 추천 버튼 + 수정 이력 컴플라이언스 폼 (신규)
- 컴플라이언스: 불합격 사유 코드 9종, 피드백 수정 이력 영구 보존, Analytics 추출용 구조화 점수 5필드

### v0.60.0 ✅ — 레벨체계 연동 (IC/M 투트랙 + 연봉 밴드 자동완성)
- [x] `database.py` — `job_requisitions`에 `job_family_id`, `track`, `salary_mid` 컬럼 마이그레이션
- [x] `database.py` — `salary_grades` 전면 업데이트: levels.fyi + 블라인드 리서치 기반 시장 연봉 (L1~L9 × 12 직군, 직군별 배수 적용)
- [x] `app.py` — `REQUISITION_TRACK_LABEL`, `M_TRACK_TITLE`, `IC_TRACK_TITLE`, `MANAGER_TRACK_MIN_LEVEL` 상수
- [x] `app.py` — `/api/salary-band` 엔드포인트 (job_family_id + level + track → min/mid/max JSON)
- [x] `app.py` — `requisition_new` POST: job_family_id, track, salary_mid 저장
- [x] `app.py` — `requisition_detail`: job_family, track, IC/M 타이틀 표시
- [x] `templates/recruit/requisition_form.html` — 직군 카드 그리드 + 레벨 카드(L1~L9) + IC/M 트랙 토글 + 연봉 밴드 실시간 미리보기 (AJAX)

### 레벨체계 설계 (안 B — IC/M 분리 트랙)
- IC 트랙: L1(Jr.) → L9(Fellow/CTO), 전 레벨 진입 가능
- M 트랙: M1(Team Lead) ~ M5(VP), L5 이상에서만 선택 가능
- M 트랙 보상: 동레벨 IC 대비 +10% (Amazon SDM vs SDE 기준)
- 직군별 배수: SWE/PM/INFRA=1.0, DATA=1.05, DESIGN/LEGAL=0.9, STRAT=0.95, MKT/SALES/FIN=0.85, HR=0.8, OPS=0.75

### v0.59.0 ✅ — Requisition 승인 워크플로우 (채용 요청서)
- [x] `database.py` — job_requisitions 테이블 추가 (draft→부서장→HR→공고 3단계 승인)
- [x] `database.py` — job_postings에 employment_type/salary_min/salary_max 컬럼 마이그레이션
- [x] `app.py` — REQUISITION_STATUS_LABEL / REQUISITION_EMP_TYPE_LABEL 상수
- [x] `app.py` — requisition_list: 목록 조회 (role별 필터 — 본인/부서/전체)
- [x] `app.py` — requisition_new: 작성 + 임시저장/즉시제출 분기
- [x] `app.py` — requisition_detail: 상세 + 승인 흐름 스텝 표시
- [x] `app.py` — requisition_submit: draft → pending_dept + 부서 매니저 알림
- [x] `app.py` — requisition_dept_approve: 부서장 승인/반려 + HR 알림
- [x] `app.py` — requisition_hr_approve: HR 최종 승인 → job_postings 자동 생성 / 반려
- [x] `templates/recruit/requisition_list.html` — 상태 필터 탭 + 행 클릭 → 상세
- [x] `templates/recruit/requisition_form.html` — 작성 폼 (급여 만원→원 변환 JS)
- [x] `templates/recruit/requisition_detail.html` — 승인 흐름 스텝바 + 반려 모달 + 공고 링크
- [x] `templates/base.html` — 관리자 사이드바 채용 요청서 링크 추가

### v0.58.0 ✅ — OT 승인 + 연차 이월 + 개인 리포트
- [x] `database.py` — overtime_requests 테이블 추가 (사전/사후 신청, 승인/반려 워크플로우)
- [x] `database.py` — leave_balances 테이블 추가 (연도별 연차 잔액 + 이월 관리)
- [x] `database.py` — company_config에 carry_over_max 컬럼 추가 (이월 최대 일수, 기본 10일)
- [x] `app.py` — attendance_home(): OT 신청 목록, 팀 OT 승인 대기, 개인 월간 리포트 계산, 11h 휴식 위반 감지
- [x] `app.py` — /attendance/overtime/new POST — OT 사전/사후 신청
- [x] `app.py` — /attendance/overtime/<id>/approve POST — OT 승인 + 인앱 알림
- [x] `app.py` — /attendance/overtime/<id>/reject POST — OT 반려 + 인앱 알림
- [x] `app.py` — /attendance/leave-carryover POST — 전년도 잔여 연차 이월 (Admin 전용)
- [x] `templates/attendance/home.html` — OT 탭 추가 (신청 폼 + 내역 + 팀 승인 대기 + 연차 이월)
- [x] `templates/attendance/home.html` — 개인 월간 리포트 카드 (출근일/총근무/연장/야간 + 전월 대비)
- [x] `templates/attendance/home.html` — 11시간 휴식 미준수 경고 배너 (최근 14일 기준)

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

### Phase 7 — HRBP 인텔리전스 (최후순위, 리서치 후 진행)

> 실제 기업(예: 무신사 등)의 재무제표·사업보고서를 분석해서 HR 의사결정에 연계하는 모듈.
> "몇 명을 뽑아야 하는가" 같은 비즈니스 기여형 HR 기능.

- 재무제표 업로드 → 매출/영업이익 추이 파싱
- Headcount Planning: 매출 대비 적정 인원 시뮬레이션
- Workforce Cost 분석: 인건비/매출 비율, 부서별 ROI
- 채용 계획 자동 제안: 성장률 기반 적정 채용 인원 산출
- 외부 사례 리서치 필요 (Workday Adaptive Planning, Anaplan HRBP 모듈 등)

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
