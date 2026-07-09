# TalentCore — HR Management System

> 200명 규모 스타트업을 위한 Workday형 HR 통합 플랫폼  
> Flask + SQLite · 비전공자가 Claude Code로 단독 개발

---

## Live Demo

| | |
|---|---|
| **URL (Oracle Cloud, 운영 배포)** | http://161.33.39.127 |
| **체험 방법** | 랜딩 페이지에서 "체험하기" 클릭 → 관리자 권한으로 즉시 로그인 (모든 기능 열람 가능, 데이터 저장/수정은 차단) |

> 별도 계정 없이 "체험하기" 버튼만으로 HR Admin 권한의 모든 화면을 둘러볼 수 있습니다. 재접속 시마다 세션이 초기화됩니다.

---

## 한 줄 요약

엑셀과 수기 문서로 돌아가는 HR 업무를 하나의 웹 앱으로 통합합니다.  
연차 신청부터 급여명세서, 성과 평가, 채용 파이프라인, 전자계약까지 —  
HR 담당자 1명이 200명 조직을 운영할 수 있도록 설계했습니다.

---

## 주요 기능

### 인사 관리 (HCM)
- 직원 프로필 — 기본정보 / 인사정보 / 급여 / 근태 / 성과 / 스킬 탭
- 사번 자동 생성 (`TC-01001` 형식)
- 부서 계층 구조 (부문 → 본부 → 실 → 팀)
- 직급 체계 CL1~CL9 + IC / M 트랙 분리
- 인사발령 이력 + 미래 발령 예약 (발령일에 자동 반영)
- 조직도 — 매니저 기반 리포팅 라인, 슬라이드 패널
- 스킬 & 자격증 관리 (레벨 4단계, 만료일 알림)
- 직원 문서함 (신분증 / 통장 사본 등 개인서류 업로드, 본인·직속매니저·HR만 접근)
- 퇴직 마법사 3단계 + 퇴직금 자동 계산 (근로기준법 §34)

### 근태 관리
- 체크인 / 체크아웃 (출결 자동 판정 — 정상 / 지각 / 조퇴 / 결근)
- 근무 스케줄 시스템 (고정 / 선택근로 / 재량근로 / 단축근무)
- 휴가 13종 (연차 / 반차 / 병가 / 출산 / 육아 / 경조사 등) + 이월 (최대 10일)
- 연장 · 야간 · 휴일 수당 자동 계산 (근로기준법 §56)
- 주 52시간 실시간 감시 → 위반 시 매니저 자동 알림
- 법정 휴게시간 자동 공제 (§54)
- 유연근무 스케줄러 (시간×요일 블록 그리드)
- OT 사전/사후 신청 + 승인 워크플로우

### 급여 관리
- 급여명세서 자동 생성 (4대보험 + 소득세 자동 계산)
- 비과세 항목 자동 처리 (식대 / 교통비 / 육아수당 등, 소득세법 기준)
- 급여 밴드 (Min/Mid/Max) + Compa-Ratio 시각화 슬라이더
- Merit Matrix — 성과 등급 × Compa 구간 → 인상률 자동 산출
- 일괄 인상 / Merit 모드 (캘리브레이션 등급 기반 자동 적용)
- ACR (Annual Compensation Review) 워크플로우
- 연봉 변경 이력 타임라인
- 복리후생 설정 (월 지급 자동 반영)
- 복지포인트 지급 + 잔액 관리
- 최저임금 자동 체크
- Total Compensation Statement (기본급 + 상여 + 복리후생 + 퇴직금 합산)

### 성과 관리
- OKR 목표 설정 + AI SMART 체크 어시스턴트
- 목표 템플릿 라이브러리 (팀별 공유)
- 피어리뷰 — Start / Stop / Continue 프레임워크
- 익명성 임계값 (응답자 3명 미만 시 결과 비공개)
- 매니저 평가 + 자기평가
- 캘리브레이션 — 최대 1단계 하향 제한, 하향 시 사유 필수
- 9박스 그리드 (성과 × 잠재력)
- Talent Card — 성과 히스토리 + 9박스 위치 + Flight Risk 자동 감지
- 후계자 계획 (포지션별 후보 Readiness 관리)
- 등급별 보상 배수 직원 사전 공개

### 채용 (ATS)
- 채용 요청서 3단계 승인 → 공고 자동 생성
- IC/M 트랙 레벨 체계 연동 + 급여 밴드 실시간 조회
- 칸반 파이프라인 (드래그앤드롭)
- 지원자 상세 — 서류뷰어 / 면접 / 오퍼 / 이메일 / 활동로그 탭
- 면접 라운드 관리 + 인터뷰어 배정 + 피드백 수집
- 불합격 사유 코드 관리 (컴플라이언스)
- 오퍼 레터 (기본급 / 성과급 / RSU / 사이닝 구성), 밴드 슬라이더
- 오퍼 수락 시 직원 레코드 자동 생성 + 온보딩 파이프라인 즉시 가동
- 채용 대시보드 (퍼널 / Time-to-Fill / 소스별 합격률)

### 온보딩 자동화
오퍼 수락 즉시 아래가 자동 실행됩니다:

1. 온보딩 체크리스트 12개 생성 (TalentCore 내 진행 관리)
2. 웰컴 이메일 발송 (Day 1 일정 + 버디 소개 포함)
3. Jira 온보딩 에픽 + 부서별 태스크 자동 생성 (IT / GA / HR / 매니저)
4. Confluence 팀 페이지 멤버 추가 + 개인 프로필 페이지 생성
5. Slack 채널 자동 초대 + #general 입사 소개 메시지

버디 시스템 — 같은 부서 배정, 버디 확정 시 양방향 Slack DM 발송

### Slack 실시간 알림

| 이벤트 | 수신자 |
|---|---|
| 급여명세서 생성 | 직원 개인 DM (실수령액 포함) |
| 휴가 승인 / 반려 | 신청자 DM |
| 면접관 배정 | 면접관 DM (지원자명 · 일정 포함) |
| 오퍼 발송 / 거절 | 담당 리크루터 / HR 전원 DM |
| 계약서 서명 요청 / 완료 | 직원 / 발급자 DM |
| 인사발령 확정 | 본인 DM (발령일 포함) |
| 퇴직 신청 접수 | HR 전원 + 직속 매니저 DM |
| 입사 확정 | 버디 온보딩 안내 DM |

### 전자계약
- 계약서 템플릿 4종 기본 제공 (근로 / NDA / 수습 / 프리랜서)
- 변수 자동 치환 (`{{employee_name}}`, `{{salary}}` 등)
- 전자서명 + IP 기록

### 증명서 & 보고서
- 재직 / 경력 / 퇴직 / 근로소득 원천징수영수증 발급
- Excel 내보내기 7종
- People Analytics (Headcount / 이직률 / Pay Equity / Compa-Ratio)
- HR 데이터 마법사 (소스 선택 → 테이블 / 차트 / 피벗 / 통계)

---

## 기술 스택

| 항목 | 내용 |
|---|---|
| Backend | Python 3.12 · Flask 3.1 |
| Database | SQLite (sqlite3 직접 쿼리, ORM 없음) |
| Frontend | HTML · Vanilla JS · 커스텀 CSS (Pretendard) |
| 외부 연동 | Slack Bot API · Jira REST API · Confluence REST API · SMTP |
| 배포 | Oracle Cloud VM (Nginx + Gunicorn, SSH 배포) |
| 인증 | Session 기반 · 역할별 접근제어 |

---

## 역할별 권한

| 역할 | 접근 범위 |
|---|---|
| **HR Admin** | 전체 기능 |
| **Manager** | 담당 팀원 근태 승인, 성과 평가, 채용 면접 |
| **Employee** | 본인 정보 조회, 근태 신청, 급여명세서, 증명서 |
| **Recruiter** | 채용 공고, 지원자, 면접, 오퍼 |
| **Guest** | 읽기 전용 데모 |

---

## 로컬 실행

```bash
git clone https://github.com/Humgut1/hr-system.git
cd hr-system
pip install -r requirements.txt
python migrate_db.py   # DB 초기화 + 시드 데이터 (직원 100명)
python run.py          # → http://localhost:5000
```

### 환경변수 (선택)

`.env` 파일 생성 시 외부 연동 활성화. 없으면 Demo 모드로 동작합니다.

```env
# Slack
SLACK_BOT_TOKEN=xoxb-...
SLACK_WORKSPACE_ID=T...

# Jira / Confluence
JIRA_EMAIL=your@email.com
JIRA_API_TOKEN=...
JIRA_BASE_URL=https://yoursite.atlassian.net
JIRA_PROJECT_KEY=KAN
CONFLUENCE_BASE_URL=https://yoursite.atlassian.net/wiki
CONFLUENCE_SPACE_KEY=HR

# SMTP (웰컴 이메일)
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=your@email.com
SMTP_PASSWORD=...
```

---

## 시드 데이터

`python migrate_db.py` 실행 시 자동 생성:

- 직원 100명 (부서 12개, 직급 9단계, 직군 12개)
- 급여명세서 600건 (6개월치, 4대보험 실계산)
- 근태 기록 920건
- 성과 목표 400개 + 리뷰 200건
- 휴가 신청 40건
- 채용 공고 10건 + 지원자 57명
- 전자계약 5건

---

## 개발 배경

비전공자 HR 주니어가 Claude Code를 활용해 단독으로 개발했습니다.  
Workday, SAP SuccessFactors 등 실제 HRIS 제품을 레퍼런스로 삼아  
실무에서 쓸 수 있는 수준의 기능을 목표로 설계했습니다.

---

## License

MIT
