"""
Jira REST API 래퍼
- 환경변수 JIRA_API_TOKEN 없으면 데모 모드
"""
import os
import json
import base64
import urllib.request
from datetime import date, timedelta

BASE_URL    = os.environ.get('JIRA_BASE_URL', '').rstrip('/')
EMAIL       = os.environ.get('JIRA_EMAIL', '')
API_TOKEN   = os.environ.get('JIRA_API_TOKEN', '')
PROJECT_KEY = os.environ.get('JIRA_PROJECT_KEY', 'KAN')
IS_DEMO     = not bool(API_TOKEN)


def _make_onboarding_tasks(name: str, dept: str, pos: str, emp_email: str, hd: date) -> list:
    """입사자 정보를 받아 상세 설명이 포함된 온보딩 태스크 목록 반환"""
    return [
        # ── IT 팀 ──────────────────────────────────────────────
        {
            'summary':  f'[IT] 노트북 · 장비 준비 — {name}',
            'team':     'IT',
            'days':     -3,
            'priority': 'High',
            'description': f"""=== 신규 입사자 장비 준비 체크리스트 ===

대상자: {name} ({pos} / {dept})
입사일: {hd.isoformat()}
회사 이메일: {emp_email}

[ ] MacBook Pro 14" (또는 정책 기준 사양) 발주 확인
[ ] 전원 어댑터 · USB-C 허브 동봉
[ ] 모니터 (27" 4K) 세팅 및 케이블 연결
[ ] 키보드 · 마우스 (무선) 준비
[ ] 헤드셋 준비 (화상회의용)

--- OS 초기 세팅 ---
[ ] macOS 최신 버전 업데이트
[ ] FileVault 암호화 활성화 (보안 정책 필수)
[ ] 회사 MDM(Mobile Device Management) 프로파일 설치
[ ] 화면 보호기 잠금 5분 설정

--- 필수 소프트웨어 사전 설치 ---
[ ] Slack (워크스페이스: TalentCore)
[ ] Zoom (라이선스 계정 배정)
[ ] 1Password (팀 볼트 초대)
[ ] Google Chrome + 북마크 세팅
[ ] Notion (초대 링크 발송)
[ ] VS Code / Cursor (개발팀 해당)
[ ] Figma (디자인팀 해당)

완료 후 입사 당일 오전 9시 전 사무실 지정 자리에 세팅 완료할 것."""
        },
        {
            'summary':  f'[IT] 회사 이메일 계정 생성 — {name}',
            'team':     'IT',
            'days':     -2,
            'priority': 'High',
            'description': f"""=== 이메일 계정 생성 체크리스트 ===

대상자: {name} ({dept})
입사일: {hd.isoformat()}

--- Google Workspace 계정 생성 ---
[ ] 계정 ID 규칙: 이름.성@company.com (예: gildong.hong@company.com)
[ ] 임시 비밀번호 설정 후 입사일에 전달
[ ] 2단계 인증(2FA) 필수 활성화 안내 포함

--- 이메일 초기 설정 ---
[ ] 서명 템플릿 적용
    형식: 이름 | 직급 | 부서
          📧 이메일 | 📱 전화번호
          🌐 www.company.com
[ ] 부재중 자동응답 OFF 확인
[ ] 회사 캘린더 공유 설정

--- 연동 계정 초대 ---
[ ] Google Drive 팀 공유 드라이브 권한 부여
[ ] Google Meet 라이선스 확인
[ ] Slack에 회사 이메일로 초대 발송

계정 정보는 입사 당일 오전에 본인 개인 이메일({emp_email})로 전달."""
        },
        {
            'summary':  f'[IT] 사내 시스템 접근 권한 부여 — {name}',
            'team':     'IT',
            'days':     0,
            'priority': 'High',
            'description': f"""=== 시스템 권한 부여 체크리스트 ===

대상자: {name} | 부서: {dept} | 직급: {pos}
입사일: {hd.isoformat()}

--- 전사 공통 시스템 ---
[ ] TalentCore HR 시스템 (role: employee)
[ ] Notion 워크스페이스 초대
[ ] 1Password 팀 볼트 접근
[ ] 회사 GitHub Organization 초대
[ ] Figma 팀 초대

--- 부서별 추가 권한 ({dept}) ---
[ ] 팀 전용 Slack 채널 (#팀명) 추가
[ ] 팀 Google Drive 폴더 접근 권한
[ ] 팀 Notion 데이터베이스 편집 권한
[ ] 팀 Jira 프로젝트 Member 추가

--- 보안 설정 확인 ---
[ ] VPN 클라이언트 설치 및 계정 발급
[ ] 사내 Wi-Fi 접속 정보 전달
[ ] 보안 인식 교육 수료 확인 (온보딩 주간 내)

※ 권한 부여 후 TalentCore 온보딩 체크리스트 '시스템 접근 완료' 항목 체크"""
        },

        # ── GA 팀 ──────────────────────────────────────────────
        {
            'summary':  f'[GA] 사원증 · 명함 · 사무용품 준비 — {name}',
            'team':     'GA',
            'days':     -2,
            'priority': 'Medium',
            'description': f"""=== GA 입사 준비 체크리스트 ===

대상자: {name} ({pos} / {dept})
입사일: {hd.isoformat()}

--- 사원증 ---
[ ] 증명사진 수령 (HR에서 전달) 또는 사진 촬영 일정 조율
[ ] 사원증 카드 발급 신청 (발급처: XX카드사)
[ ] 건물 출입 권한 등록 (카드키 시스템)
[ ] 주차 등록 필요 여부 확인

--- 명함 ---
[ ] 명함 정보 확인:
    이름: {name}
    직함: {pos}
    부서: {dept}
    이메일: {emp_email}
[ ] 명함 50매 발주 (인쇄소: 기존 거래처)
[ ] 입사 1주일 내 수령 예정 안내

--- 사무용품 기본 키트 ---
[ ] 노트 (A5) × 2
[ ] 볼펜 (검정/파랑) × 3
[ ] 포스트잇 × 1세트
[ ] 클리어파일 × 3
[ ] 개인 보관함 열쇠 발급

--- 자리 배정 ---
[ ] 지정 자리 확인 및 청소
[ ] 좌석 번호 TalentCore 등록
[ ] 팀장에게 자리 위치 안내 요청"""
        },

        # ── HR 팀 ──────────────────────────────────────────────
        {
            'summary':  f'[HR] 입사 웰컴 이메일 발송 — {name}',
            'team':     'HR',
            'days':     -1,
            'priority': 'High',
            'description': f"""=== 웰컴 이메일 발송 체크리스트 ===

대상자: {name}
발송 대상 이메일: {emp_email}
발송 시점: 입사 전날 오후 또는 당일 오전 자동 발송

--- 이메일 포함 내용 ---
[ ] 입사 축하 메시지
[ ] 출근 장소 및 시간 안내
[ ] 첫날 일정 (오리엔테이션 시간표)
[ ] Day 1 해야 할 일 순서 안내
[ ] 온보딩 가이드 Confluence 링크
[ ] 담당 버디 소개 (이름/연락처)
[ ] TalentCore 로그인 정보
[ ] 주차/교통 안내

※ TalentCore + 연동 시스템에서 자동 발송됨 (확인만 할 것)"""
        },
        {
            'summary':  f'[HR] 보안 서약서 · 개인정보 동의서 서명 — {name}',
            'team':     'HR',
            'days':     1,
            'priority': 'Medium',
            'description': f"""=== 서류 서명 체크리스트 ===

대상자: {name}
서명 기한: 입사 후 1일 이내

--- TalentCore 전자서명 ---
[ ] 근로계약서 (TalentCore에서 발행 → 서명 요청)
[ ] 보안 서약서 (기밀유지 / NDA 포함)
[ ] 개인정보 수집·이용 동의서
[ ] 사내 IT 정책 동의서 (이메일·장비 모니터링 등)

--- 종이 서류 (해당 시) ---
[ ] 통장 사본 수령 (급여이체용)
[ ] 주민등록등본 수령 (4대보험 등록용)
[ ] 가족관계증명서 (부양가족 세금공제 신청 시)

HR 담당자: 서명 완료 후 TalentCore 온보딩 진행 상황 업데이트"""
        },
        {
            'summary':  f'[HR] 4대보험 신고 · 급여 등록 — {name}',
            'team':     'HR',
            'days':     1,
            'priority': 'High',
            'description': f"""=== 인사 행정 처리 체크리스트 ===

대상자: {name} | 입사일: {hd.isoformat()}

--- 4대보험 ---
[ ] 국민건강보험 직장가입자 취득 신고 (입사 후 14일 이내)
[ ] 국민연금 사업장가입자 취득 신고
[ ] 고용보험 피보험자 취득 신고
[ ] 산재보험 취득 신고

--- 급여 시스템 등록 ---
[ ] TalentCore 급여 정보 입력 (기본급, 수당, 복리후생)
[ ] 급여 이체 계좌 등록
[ ] 세금 공제 정보 확인 (부양가족 등)
[ ] 첫 달 급여 일할 계산 여부 확인

--- 복리후생 ---
[ ] 복지포인트 연간 지급 처리 (TalentCore 자동)
[ ] 단체보험 피보험자 추가
[ ] 건강검진 대상자 등록 (해당 연도)"""
        },

        # ── 매니저 ──────────────────────────────────────────────
        {
            'summary':  f'[매니저] 버디(Buddy) 배정 및 안내 — {name}',
            'team':     'MGR',
            'days':     -1,
            'priority': 'Medium',
            'description': f"""=== 버디 배정 체크리스트 ===

신규 입사자: {name} ({pos} / {dept})
입사일: {hd.isoformat()}

--- 버디 선정 기준 ---
[ ] 동일 또는 인접 팀 시니어 직원 (1년 이상 재직)
[ ] 최근 6개월 내 버디 역할 미수행자 우선
[ ] 성격/업무스타일 고려 매칭

--- 버디 사전 브리핑 ---
[ ] 버디에게 역할 안내:
    - 첫 2주간 질문 창구 역할
    - 점심 동행 (첫 3일)
    - 팀 문화·비공식 규칙 전달
    - 주 1회 15분 캐치업 (4주간)
[ ] 버디 TalentCore 프로필에 배정 기록
[ ] Slack DM으로 신입 소개 및 버디 역할 안내

--- 입사 당일 ---
[ ] 버디가 신입 맞이 (9시 로비 or 입구)
[ ] 팀원 전체 소개 주선
[ ] 점심 예약 (첫날은 팀 전체 환영 런치 권장)"""
        },
        {
            'summary':  f'[매니저] 팀 소개 · 업무 오리엔테이션 — {name}',
            'team':     'MGR',
            'days':     0,
            'priority': 'Medium',
            'description': f"""=== 팀 오리엔테이션 체크리스트 ===

신규 입사자: {name}
진행 일자: {hd.isoformat()} (입사 당일)

--- 오전 (09:00~12:00) ---
[ ] 09:00 로비 or 팀 공간에서 맞이
[ ] 자리 안내 및 팀원 순차 소개
[ ] 팀 미션 · 현재 진행 프로젝트 브리핑 (30분)
[ ] 팀 회의 일정 · 커뮤니케이션 방식 안내
[ ] Slack 팀 채널 구조 설명

--- 오후 (14:00~17:00) ---
[ ] 현재 팀 OKR / 목표 공유
[ ] 신입의 초기 R&R (역할과 책임) 초안 논의
[ ] 주요 이해관계자 (타 팀) 소개
[ ] 첫 주 일정 안내 (정례 회의, 1:1 등)

--- 입사 첫 주 내 ---
[ ] 1:1 미팅 정례화 (주 1회 30분)
[ ] 30일 목표 초안 협의 시작
[ ] Jira / Notion 팀 스페이스 사용법 안내"""
        },
        {
            'summary':  f'[매니저] 30일 · 90일 수습 목표 설정 — {name}',
            'team':     'MGR',
            'days':     7,
            'priority': 'Low',
            'description': f"""=== 수습 목표 설정 가이드 ===

신규 입사자: {name}
설정 기한: 입사 후 7일 이내

--- 30일 목표 (탐색 & 적응) ---
목표 예시:
[ ] 팀 전체 업무 프로세스 파악 및 문서화 (Confluence)
[ ] 기존 프로젝트 코드베이스/현황 파악
[ ] 팀원 전원과 1:1 인트로 미팅 완료
[ ] 첫 번째 실무 태스크 완료

--- 60일 목표 (기여 시작) ---
목표 예시:
[ ] 독립적으로 중간 규모 태스크 수행 가능
[ ] 팀 회의에서 의견 제시 및 참여
[ ] 담당 영역 Confluence 문서 초안 작성

--- 90일 목표 (전력 기여) ---
목표 예시:
[ ] 독립적인 프로젝트 리드 또는 핵심 기여
[ ] 신규 입사자 온보딩 가이드 개선 제안
[ ] 수습 평가 미팅 (HR + 매니저)

※ 목표 설정 후 TalentCore 성과관리 → 목표 등록 필수
※ 30/60/90일 체크포인트 미팅 캘린더 등록"""
        },
    ]


def _make_offboarding_tasks(name: str, last_day: date) -> list:
    return [
        {
            'summary':  f'[IT] 노트북 · 장비 반납 — {name}',
            'days':     0,
            'priority': 'High',
            'description': f"""최종 근무일: {last_day.isoformat()}

[ ] 노트북 반납 및 상태 점검 (스크래치, 파손 여부)
[ ] 모니터, 키보드, 마우스, 어댑터 등 주변기기 반납
[ ] 개인 데이터 백업 여부 확인 (회사 데이터 삭제)
[ ] 기기 초기화 (macOS 공장 초기화)
[ ] 사원증(카드키) 반납
[ ] 보안토큰/OTP 기기 반납"""
        },
        {
            'summary':  f'[IT] 사내 시스템 접근 권한 회수 — {name}',
            'days':     0,
            'priority': 'High',
            'description': f"""최종 근무일: {last_day.isoformat()}

[ ] Google Workspace 계정 정지 (퇴직 당일 18:00 이후)
[ ] Slack 계정 비활성화
[ ] GitHub Organization 멤버 제거
[ ] Notion 워크스페이스 접근 차단
[ ] 1Password 팀 볼트 접근 차단
[ ] VPN 계정 비활성화
[ ] 사내 모든 SaaS 서비스 접근 차단 목록 확인"""
        },
        {
            'summary':  f'[HR] 퇴직 면담 (Exit Interview) — {name}',
            'days':     -3,
            'priority': 'Medium',
            'description': f"""Exit Interview 일정: 최종 근무일 3일 전

[ ] 퇴직 사유 청취 (자발/권고/계약만료 등)
[ ] 근무 환경 · 조직문화 피드백 수집
[ ] 개선 제안 사항 기록
[ ] 퇴직자 비밀유지 의무 재안내
[ ] 결과를 HR 보고서에 익명 처리 후 반영"""
        },
        {
            'summary':  f'[HR] 퇴직금 · 미사용 연차 정산 — {name}',
            'days':     0,
            'priority': 'High',
            'description': f"""최종 근무일: {last_day.isoformat()}

[ ] TalentCore 퇴직금 계산 (자동) 확인
[ ] 미사용 연차 수당 계산 확인
[ ] 일할 급여 계산 (마지막 달)
[ ] 4대보험 상실 신고 (퇴직 후 14일 이내)
[ ] 퇴직소득세 원천징수 처리
[ ] 정산금 이체 완료"""
        },
        {
            'summary':  f'[매니저] 업무 인수인계 — {name}',
            'days':     -5,
            'priority': 'High',
            'description': f"""인수인계 기한: 최종 근무일 5일 전부터 시작

[ ] 담당 업무 목록 정리 (Confluence 문서화)
[ ] 진행 중인 프로젝트 현황 인수자에게 브리핑
[ ] 외부 이해관계자 연락처 이관
[ ] 비밀번호 / 계정 정보 1Password 팀 볼트 이관
[ ] Jira 담당 이슈 재배정
[ ] Notion 문서 소유권 이전"""
        },
        {
            'summary':  f'[GA] 사원증 · 개인 물품 반납 — {name}',
            'days':     0,
            'priority': 'Medium',
            'description': f"""최종 근무일: {last_day.isoformat()}

[ ] 사원증 반납
[ ] 개인 보관함 열쇠 반납
[ ] 주차 등록 해제
[ ] 개인 물품 반출 확인
[ ] 명함 미사용분 회수"""
        },
    ]


def _auth_header():
    token = base64.b64encode(f'{EMAIL}:{API_TOKEN}'.encode()).decode()
    return {'Authorization': f'Basic {token}', 'Content-Type': 'application/json'}


def _post(path, payload):
    url  = f'{BASE_URL}/rest/api/2/{path}'
    data = json.dumps(payload).encode('utf-8')
    req  = urllib.request.Request(url, data=data, headers=_auth_header())
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def _fmt_date(d: date) -> str:
    return d.isoformat()


def create_onboarding_epic(employee: dict) -> dict:
    name       = employee.get('name', '')
    hire_date  = employee.get('hire_date') or date.today().isoformat()
    dept       = employee.get('dept', '미배정')
    pos        = employee.get('pos', '신입')
    emp_email  = employee.get('email', '')
    hd         = date.fromisoformat(hire_date)
    tasks      = _make_onboarding_tasks(name, dept, pos, emp_email, hd)

    if IS_DEMO:
        return {
            'ok': True, 'demo': True,
            'action': 'create_onboarding_epic',
            'epic_key': f'{PROJECT_KEY}-DEMO',
            'summary': f'[온보딩] {name} ({hire_date})',
            'task_count': len(tasks),
        }
    try:
        epic_desc = f"""{name}님 온보딩 마스터 체크리스트
입사일: {hire_date} | 부서: {dept} | 직급: {pos}
회사 이메일: {emp_email}

== 담당팀별 Task 목록 ==
- IT팀: 장비 준비 / 이메일 계정 / 시스템 권한 (D-3 ~ D-Day)
- GA팀: 사원증 / 명함 / 사무용품 (D-2)
- HR팀: 웰컴 이메일 / 서류 서명 / 4대보험 (D-1 ~ D+1)
- 매니저: 버디 배정 / 팀 오리엔테이션 / 목표 설정 (D-1 ~ D+7)

온보딩 진행 현황은 TalentCore HR 시스템에서도 확인 가능합니다."""

        epic = _post('issue', {
            'fields': {
                'project':     {'key': PROJECT_KEY},
                'summary':     f'[온보딩] {name} ({hire_date})',
                'issuetype':   {'name': 'Epic'},
                'description': epic_desc,
            }
        })
        epic_key = epic.get('key', '')
        subtasks = []
        for task in tasks:
            due = _fmt_date(hd + timedelta(days=task['days']))
            r = _post('issue', {
                'fields': {
                    'project':     {'key': PROJECT_KEY},
                    'summary':     task['summary'],
                    'issuetype':   {'name': 'Task'},
                    'priority':    {'name': task['priority']},
                    'duedate':     due,
                    'description': task['description'],
                    'parent':      {'key': epic_key},
                }
            })
            subtasks.append(r.get('key'))
        return {'ok': True, 'epic_key': epic_key, 'subtasks': subtasks}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def create_offboarding_epic(employee: dict) -> dict:
    name     = employee.get('name', '')
    last_day = employee.get('last_work_date') or date.today().isoformat()
    ld       = date.fromisoformat(last_day)
    tasks    = _make_offboarding_tasks(name, ld)

    if IS_DEMO:
        return {
            'ok': True, 'demo': True,
            'action': 'create_offboarding_epic',
            'epic_key': f'{PROJECT_KEY}-DEMO',
            'summary': f'[오프보딩] {name} (최종 {last_day})',
            'task_count': len(tasks),
        }
    try:
        epic = _post('issue', {
            'fields': {
                'project':     {'key': PROJECT_KEY},
                'summary':     f'[오프보딩] {name} (최종 근무일: {last_day})',
                'issuetype':   {'name': 'Epic'},
                'description': f'{name}님 오프보딩 체크리스트 | 최종 근무일: {last_day}',
            }
        })
        epic_key = epic.get('key', '')
        for task in tasks:
            due = _fmt_date(ld + timedelta(days=task['days']))
            _post('issue', {
                'fields': {
                    'project':     {'key': PROJECT_KEY},
                    'summary':     task['summary'],
                    'issuetype':   {'name': 'Task'},
                    'priority':    {'name': task['priority']},
                    'duedate':     due,
                    'description': task['description'],
                    'parent':      {'key': epic_key},
                }
            })
        return {'ok': True, 'epic_key': epic_key}
    except Exception as e:
        return {'ok': False, 'error': str(e)}
