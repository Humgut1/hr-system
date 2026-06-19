"""
웰컴 이메일 발송 모듈
- SMTP_HOST / SMTP_USER / SMTP_PASSWORD 없으면 데모 모드 (로그만 기록)
"""
import os
import smtplib
import json
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime

SMTP_HOST = os.environ.get('SMTP_HOST', '')
SMTP_PORT = int(os.environ.get('SMTP_PORT', '587'))
SMTP_USER = os.environ.get('SMTP_USER', '')
SMTP_PASS = os.environ.get('SMTP_PASSWORD', '')
FROM_NAME = os.environ.get('SMTP_FROM_NAME', 'TalentCore HR팀')
IS_DEMO   = not bool(SMTP_HOST and SMTP_USER and SMTP_PASS)

CONFLUENCE_BASE = os.environ.get('CONFLUENCE_BASE_URL', 'https://your-company.atlassian.net/wiki')
TALENTCORE_URL  = os.environ.get('TALENTCORE_URL', 'http://localhost:5000')
SLACK_WORKSPACE = os.environ.get('SLACK_WORKSPACE_URL', 'https://slack.com')


def _build_welcome_html(employee: dict, buddy: dict = None) -> str:
    name       = employee.get('name', '신입사원')
    dept       = employee.get('dept', '')
    pos        = employee.get('pos', '')
    hire_date  = employee.get('hire_date', '')
    emp_email  = employee.get('email', '')

    buddy_section = ''
    if buddy:
        buddy_section = f"""
        <div style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:12px;padding:20px 24px;margin:24px 0;">
          <div style="font-size:13px;font-weight:700;color:#15803d;letter-spacing:1px;text-transform:uppercase;margin-bottom:12px;">👥 버디(Buddy) 소개</div>
          <div style="display:flex;align-items:center;gap:16px;">
            <div style="width:48px;height:48px;background:#22c55e;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:20px;color:white;font-weight:700;flex-shrink:0;">{buddy.get('name','?')[0]}</div>
            <div>
              <div style="font-size:16px;font-weight:700;color:#14532d;">{buddy.get('name','')}</div>
              <div style="font-size:13px;color:#16a34a;">{buddy.get('pos','')} · {buddy.get('dept','')}</div>
              <div style="font-size:13px;color:#15803d;margin-top:4px;">📧 {buddy.get('email','')} &nbsp;|&nbsp; 💬 Slack에서 DM 주세요!</div>
            </div>
          </div>
          <div style="margin-top:12px;font-size:13px;color:#166534;line-height:1.6;">
            첫 2주 동안 궁금한 것은 뭐든지 물어보세요. 점심도 같이 해요! 🍱
          </div>
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="ko">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"></head>
<body style="margin:0;padding:0;background:#f1f5f9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f1f5f9;padding:40px 0;">
  <tr><td align="center">
    <table width="600" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:20px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,0.08);">

      <!-- Header -->
      <tr>
        <td style="background:linear-gradient(135deg,#1e1b4b 0%,#4f46e5 100%);padding:48px 40px;text-align:center;">
          <div style="font-size:32px;margin-bottom:8px;">🎉</div>
          <div style="color:#a5b4fc;font-size:12px;font-weight:700;letter-spacing:2px;text-transform:uppercase;margin-bottom:12px;">Welcome to TalentCore</div>
          <h1 style="color:#fff;font-size:28px;font-weight:800;margin:0 0 8px;letter-spacing:-0.5px;">{name}님, 오늘부터 동료입니다!</h1>
          <p style="color:#c7d2fe;font-size:15px;margin:0;">{dept} · {pos} · 입사일 {hire_date}</p>
        </td>
      </tr>

      <!-- Body -->
      <tr>
        <td style="padding:40px;">

          <p style="font-size:16px;color:#374151;line-height:1.8;margin:0 0 24px;">
            안녕하세요, {name}님! 👋<br>
            저희 팀에 합류하신 것을 진심으로 환영합니다.<br>
            오늘 하루가 설레고 특별한 시작이 되길 바랍니다.
          </p>

          {buddy_section}

          <!-- Day 1 Schedule -->
          <div style="background:#f8fafc;border-radius:14px;padding:24px;margin:24px 0;border-left:4px solid #4f46e5;">
            <div style="font-size:13px;font-weight:700;color:#4f46e5;letter-spacing:1px;text-transform:uppercase;margin-bottom:16px;">📅 오늘의 일정</div>
            <table width="100%" cellpadding="0" cellspacing="0">
              <tr>
                <td style="padding:8px 0;border-bottom:1px solid #e5e7eb;">
                  <span style="font-size:13px;color:#6b7280;width:70px;display:inline-block;">09:00</span>
                  <span style="font-size:14px;color:#111827;font-weight:600;">출근 &amp; 버디와 합류 (로비)</span>
                </td>
              </tr>
              <tr>
                <td style="padding:8px 0;border-bottom:1px solid #e5e7eb;">
                  <span style="font-size:13px;color:#6b7280;width:70px;display:inline-block;">09:30</span>
                  <span style="font-size:14px;color:#111827;font-weight:600;">HR 오리엔테이션 (복지·규칙·시스템 안내)</span>
                </td>
              </tr>
              <tr>
                <td style="padding:8px 0;border-bottom:1px solid #e5e7eb;">
                  <span style="font-size:13px;color:#6b7280;width:70px;display:inline-block;">11:00</span>
                  <span style="font-size:14px;color:#111827;font-weight:600;">IT 세팅 (노트북·계정·시스템 접근)</span>
                </td>
              </tr>
              <tr>
                <td style="padding:8px 0;border-bottom:1px solid #e5e7eb;">
                  <span style="font-size:13px;color:#6b7280;width:70px;display:inline-block;">12:00</span>
                  <span style="font-size:14px;color:#111827;font-weight:600;">🍱 팀 환영 점심 (버디 포함)</span>
                </td>
              </tr>
              <tr>
                <td style="padding:8px 0;border-bottom:1px solid #e5e7eb;">
                  <span style="font-size:13px;color:#6b7280;width:70px;display:inline-block;">14:00</span>
                  <span style="font-size:14px;color:#111827;font-weight:600;">팀 소개 미팅 (매니저 주관)</span>
                </td>
              </tr>
              <tr>
                <td style="padding:8px 0;">
                  <span style="font-size:13px;color:#6b7280;width:70px;display:inline-block;">16:00</span>
                  <span style="font-size:14px;color:#111827;font-weight:600;">온보딩 대시보드 확인 &amp; 남은 할 일 정리</span>
                </td>
              </tr>
            </table>
          </div>

          <!-- Day 1 Task List -->
          <div style="margin:24px 0;">
            <div style="font-size:13px;font-weight:700;color:#374151;letter-spacing:1px;text-transform:uppercase;margin-bottom:16px;">✅ 오늘 해야 할 일 (순서대로)</div>

            <div style="counter-reset:task-counter;">

              <div style="display:flex;align-items:flex-start;gap:14px;padding:14px;border:1px solid #e5e7eb;border-radius:10px;margin-bottom:8px;">
                <div style="width:28px;height:28px;background:#4f46e5;border-radius:50%;color:white;font-size:13px;font-weight:700;display:flex;align-items:center;justify-content:center;flex-shrink:0;">1</div>
                <div>
                  <div style="font-size:14px;font-weight:700;color:#111827;">Slack 프로필 완성하기</div>
                  <div style="font-size:13px;color:#6b7280;margin-top:3px;">사진 업로드 · 직함 입력 · 상태 이모지 설정 · 팀 채널 알림 설정</div>
                </div>
              </div>

              <div style="display:flex;align-items:flex-start;gap:14px;padding:14px;border:1px solid #e5e7eb;border-radius:10px;margin-bottom:8px;">
                <div style="width:28px;height:28px;background:#4f46e5;border-radius:50%;color:white;font-size:13px;font-weight:700;display:flex;align-items:center;justify-content:center;flex-shrink:0;">2</div>
                <div>
                  <div style="font-size:14px;font-weight:700;color:#111827;">이메일 서명 설정</div>
                  <div style="font-size:13px;color:#6b7280;margin-top:3px;">이름 | {pos} | {dept} | {emp_email} | 회사 전화번호</div>
                </div>
              </div>

              <div style="display:flex;align-items:flex-start;gap:14px;padding:14px;border:1px solid #e5e7eb;border-radius:10px;margin-bottom:8px;">
                <div style="width:28px;height:28px;background:#4f46e5;border-radius:50%;color:white;font-size:13px;font-weight:700;display:flex;align-items:center;justify-content:center;flex-shrink:0;">3</div>
                <div>
                  <div style="font-size:14px;font-weight:700;color:#111827;">TalentCore 로그인 & 프로필 완성</div>
                  <div style="font-size:13px;color:#6b7280;margin-top:3px;">비상연락처 · 주소 입력 / 온보딩 체크리스트 확인</div>
                </div>
              </div>

              <div style="display:flex;align-items:flex-start;gap:14px;padding:14px;border:1px solid #e5e7eb;border-radius:10px;margin-bottom:8px;">
                <div style="width:28px;height:28px;background:#4f46e5;border-radius:50%;color:white;font-size:13px;font-weight:700;display:flex;align-items:center;justify-content:center;flex-shrink:0;">4</div>
                <div>
                  <div style="font-size:14px;font-weight:700;color:#111827;">필수 앱 설치 & 로그인</div>
                  <div style="font-size:13px;color:#6b7280;margin-top:3px;">Slack · Zoom · 1Password · Notion · 개발툴 (VS Code 등)</div>
                </div>
              </div>

              <div style="display:flex;align-items:flex-start;gap:14px;padding:14px;border:1px solid #e5e7eb;border-radius:10px;margin-bottom:8px;">
                <div style="width:28px;height:28px;background:#4f46e5;border-radius:50%;color:white;font-size:13px;font-weight:700;display:flex;align-items:center;justify-content:center;flex-shrink:0;">5</div>
                <div>
                  <div style="font-size:14px;font-weight:700;color:#111827;">보안 설정 완료</div>
                  <div style="font-size:13px;color:#6b7280;margin-top:3px;">이메일 2단계 인증(2FA) · 화면 잠금 5분 · VPN 연결 테스트</div>
                </div>
              </div>

              <div style="display:flex;align-items:flex-start;gap:14px;padding:14px;border:1px solid #e5e7eb;border-radius:10px;margin-bottom:8px;">
                <div style="width:28px;height:28px;background:#4f46e5;border-radius:50%;color:white;font-size:13px;font-weight:700;display:flex;align-items:center;justify-content:center;flex-shrink:0;">6</div>
                <div>
                  <div style="font-size:14px;font-weight:700;color:#111827;">Confluence 온보딩 가이드 읽기</div>
                  <div style="font-size:13px;color:#6b7280;margin-top:3px;">회사 소개 · 팀 소개 · 복리후생 · 취업규칙 필독 페이지</div>
                </div>
              </div>

              <div style="display:flex;align-items:flex-start;gap:14px;padding:14px;border:1px solid #e5e7eb;border-radius:10px;margin-bottom:8px;">
                <div style="width:28px;height:28px;background:#4f46e5;border-radius:50%;color:white;font-size:13px;font-weight:700;display:flex;align-items:center;justify-content:center;flex-shrink:0;">7</div>
                <div>
                  <div style="font-size:14px;font-weight:700;color:#111827;">근로계약서 전자서명</div>
                  <div style="font-size:13px;color:#6b7280;margin-top:3px;">TalentCore → 내 계약서 메뉴에서 서명 완료</div>
                </div>
              </div>

              <div style="display:flex;align-items:flex-start;gap:14px;padding:14px;background:#f0fdf4;border:1px solid #bbf7d0;border-radius:10px;margin-bottom:8px;">
                <div style="width:28px;height:28px;background:#10b981;border-radius:50%;color:white;font-size:13px;font-weight:700;display:flex;align-items:center;justify-content:center;flex-shrink:0;">8</div>
                <div>
                  <div style="font-size:14px;font-weight:700;color:#065f46;">Slack #general에 자기소개 올리기 🎉</div>
                  <div style="font-size:13px;color:#059669;margin-top:3px;">이름 · 담당 업무 · 취미나 관심사 한 가지 · 환영 이모지 달아주세요!</div>
                </div>
              </div>

            </div>
          </div>

          <!-- Links -->
          <div style="background:#f8fafc;border-radius:14px;padding:20px 24px;margin:24px 0;">
            <div style="font-size:13px;font-weight:700;color:#374151;margin-bottom:14px;">🔗 바로가기 링크</div>
            <table width="100%" cellpadding="0" cellspacing="0">
              <tr>
                <td style="padding:6px 0;">
                  <a href="{TALENTCORE_URL}/me/onboarding" style="color:#4f46e5;text-decoration:none;font-size:14px;font-weight:600;">🟢 TalentCore 온보딩 대시보드</a>
                  <span style="font-size:13px;color:#6b7280;"> — 오늘 할 일 체크리스트</span>
                </td>
              </tr>
              <tr>
                <td style="padding:6px 0;">
                  <a href="{CONFLUENCE_BASE}/spaces/HR" style="color:#0065ff;text-decoration:none;font-size:14px;font-weight:600;">📘 Confluence 온보딩 가이드</a>
                  <span style="font-size:13px;color:#6b7280;"> — 필독 문서 모음</span>
                </td>
              </tr>
              <tr>
                <td style="padding:6px 0;">
                  <a href="{SLACK_WORKSPACE}" style="color:#4a154b;text-decoration:none;font-size:14px;font-weight:600;">💬 Slack 워크스페이스</a>
                  <span style="font-size:13px;color:#6b7280;"> — 프로필 설정 후 자기소개!</span>
                </td>
              </tr>
            </table>
          </div>

          <p style="font-size:14px;color:#6b7280;line-height:1.8;margin:24px 0 0;">
            궁금한 점은 언제든지 버디 또는 HR팀(<a href="mailto:{SMTP_USER}" style="color:#4f46e5;">{SMTP_USER}</a>)에게 연락 주세요.<br>
            오늘 하루 잘 부탁드립니다! 🙌
          </p>

        </td>
      </tr>

      <!-- Footer -->
      <tr>
        <td style="background:#f8fafc;border-top:1px solid #e5e7eb;padding:24px 40px;text-align:center;">
          <p style="font-size:12px;color:#9ca3af;margin:0;">
            이 이메일은 TalentCore HR 시스템에서 자동 발송되었습니다.<br>
            © 2026 TalentCore. All rights reserved.
          </p>
        </td>
      </tr>

    </table>
  </td></tr>
</table>
</body>
</html>"""


def send_welcome_email(employee: dict, buddy: dict = None) -> dict:
    """신규 입사자에게 웰컴 이메일 발송"""
    to_email = employee.get('email', '')
    name     = employee.get('name', '')

    if not to_email:
        return {'ok': False, 'error': '이메일 주소 없음'}

    html_body = _build_welcome_html(employee, buddy)
    subject   = f'🎉 {name}님, TalentCore에 오신 것을 환영합니다!'

    if IS_DEMO:
        return {
            'ok': True, 'demo': True,
            'action': 'send_welcome_email',
            'to': to_email,
            'subject': subject,
            'note': 'SMTP 미설정 — 데모 모드 (실제 발송 안 됨)',
        }

    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From']    = f'{FROM_NAME} <{SMTP_USER}>'
        msg['To']      = to_email

        text_body = f"""{name}님, TalentCore에 오신 것을 환영합니다!
입사일: {employee.get('hire_date','')} | 부서: {employee.get('dept','')}

오늘 할 일:
1. Slack 프로필 완성
2. 이메일 서명 설정
3. TalentCore 로그인 & 프로필 완성
4. 필수 앱 설치
5. 보안 설정 (2FA)
6. Confluence 온보딩 가이드 읽기
7. 근로계약서 전자서명
8. Slack #general 자기소개

온보딩 대시보드: {TALENTCORE_URL}/me/onboarding
"""
        msg.attach(MIMEText(text_body, 'plain', 'utf-8'))
        msg.attach(MIMEText(html_body, 'html', 'utf-8'))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, to_email, msg.as_string())

        return {'ok': True, 'to': to_email, 'subject': subject}
    except Exception as e:
        return {'ok': False, 'error': str(e)}
