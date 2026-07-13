"""
로컬 개발 서버 실행 스크립트.
실행 전 포트 5000에 떠있는 모든 프로세스를 자동으로 정리합니다.
APScheduler로 Slack 예약 알림 3종을 백그라운드에서 실행합니다.

사용법:
    python run.py
"""
import subprocess
import sys
import time
import os


def kill_port(port: int):
    """포트를 점유 중인 모든 프로세스를 종료."""
    try:
        result = subprocess.run(
            f'netstat -ano | findstr ":{port} " | findstr "LISTENING"',
            shell=True, capture_output=True, text=True
        )
        pids = set()
        for line in result.stdout.splitlines():
            parts = line.strip().split()
            if parts:
                pids.add(parts[-1])

        if pids:
            print(f"[run.py] 포트 {port} 점유 프로세스 발견: PID {', '.join(pids)}")
            for pid in pids:
                subprocess.run(f'taskkill /PID {pid} /F', shell=True,
                               capture_output=True)
            time.sleep(1)

            result2 = subprocess.run(
                f'netstat -ano | findstr ":{port} " | findstr "LISTENING"',
                shell=True, capture_output=True, text=True
            )
            if result2.stdout.strip():
                print(f"[run.py] 경고: 일부 프로세스가 아직 남아있습니다.")
            else:
                print(f"[run.py] 포트 {port} 정리 완료.")
        else:
            print(f"[run.py] 포트 {port} 비어있음. 바로 시작합니다.")

    except Exception as e:
        print(f"[run.py] 프로세스 정리 중 오류: {e}")


def load_dotenv(path='.env'):
    """`.env` 파일을 읽어 환경변수로 등록."""
    if not os.path.exists(path):
        return
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, val = line.partition('=')
            key = key.strip()
            val = val.strip()
            if key and val:
                os.environ.setdefault(key, val)


# ─────────────────────────────────────────────────────────────
#  Slack 예약 알림 작업
# ─────────────────────────────────────────────────────────────

def job_interview_reminder():
    """매일 오전 9시 — 오늘 면접 배정된 인터뷰어에게 DM"""
    try:
        import sqlite3
        from datetime import date
        from integrations.slack import send_dm, IS_DEMO
        if IS_DEMO:
            return

        db_path = os.path.join(os.path.dirname(__file__), 'hr_system.db')
        if not os.path.exists(db_path):
            return

        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        today = date.today().isoformat()

        rows = con.execute(
            """SELECT DISTINCT u.email, u.name,
                      a.name AS applicant_name,
                      jp.title AS job_title,
                      ir.scheduled_at
               FROM interview_interviewers ii
               JOIN interview_rounds ir ON ii.round_id = ir.id
               JOIN applicants a ON ir.applicant_id = a.id
               JOIN job_postings jp ON a.posting_id = jp.id
               JOIN users u ON ii.user_id = u.id
               WHERE date(ir.scheduled_at) = ?
                 AND ir.status = 'scheduled'""",
            (today,)
        ).fetchall()
        con.close()

        for row in rows:
            time_str = row['scheduled_at'][11:16] if row['scheduled_at'] else '시간 미정'
            send_dm(
                row['email'],
                f"[TalentCore] 📅 오늘 면접 일정 알림\n\n"
                f"• 지원자: {row['applicant_name']}\n"
                f"• 포지션: {row['job_title']}\n"
                f"• 시간: {time_str}\n\n"
                f"피드백 작성을 잊지 마세요!"
            )
        print(f"[scheduler] interview_reminder 완료 — {len(rows)}건")
    except Exception as e:
        print(f"[scheduler] interview_reminder 오류: {e}")


def job_payroll_reminder():
    """매월 25일 오전 9시 — 전 직원에게 급여일 알림 DM"""
    try:
        import sqlite3
        from integrations.slack import send_dm, IS_DEMO
        if IS_DEMO:
            return

        db_path = os.path.join(os.path.dirname(__file__), 'hr_system.db')
        if not os.path.exists(db_path):
            return

        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT email, name FROM users WHERE status='active' AND role != 'guest'"
        ).fetchall()
        con.close()

        for row in rows:
            send_dm(
                row['email'],
                f"[TalentCore] 💰 이번 달 급여명세서가 곧 생성됩니다.\n"
                f"TalentCore → 급여 → 내 급여명세서에서 확인하세요."
            )
        print(f"[scheduler] payroll_reminder 완료 — {len(rows)}명")
    except Exception as e:
        print(f"[scheduler] payroll_reminder 오류: {e}")


def job_peer_review_reminder():
    """피어리뷰 마감 D-3 — 미응답자에게 리마인드 DM"""
    try:
        import sqlite3
        from datetime import date, timedelta
        from integrations.slack import send_dm, IS_DEMO
        if IS_DEMO:
            return

        db_path = os.path.join(os.path.dirname(__file__), 'hr_system.db')
        if not os.path.exists(db_path):
            return

        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        target_date = (date.today() + timedelta(days=3)).isoformat()

        # 마감일이 3일 후인 사이클에서 아직 미제출한 배정자
        rows = con.execute(
            """SELECT DISTINCT u.email, u.name, pc.name AS cycle_name, pc.end_date
               FROM peer_assignments pa
               JOIN performance_cycles pc ON pa.cycle_id = pc.id
               JOIN users u ON pa.reviewer_id = u.id
               WHERE date(pc.end_date) = ?
                 AND pa.id NOT IN (
                     SELECT assignment_id FROM peer_reviews
                 )""",
            (target_date,)
        ).fetchall()
        con.close()

        for row in rows:
            send_dm(
                row['email'],
                f"[TalentCore] ⏰ 피어리뷰 마감 3일 전\n\n"
                f"• 평가 주기: {row['cycle_name']}\n"
                f"• 마감일: {row['end_date']}\n\n"
                f"TalentCore → 성과 → 피어리뷰에서 작성을 완료해 주세요."
            )
        print(f"[scheduler] peer_review_reminder 완료 — {len(rows)}명")
    except Exception as e:
        print(f"[scheduler] peer_review_reminder 오류: {e}")


def start_scheduler():
    """APScheduler 백그라운드 스케줄러 시작"""
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        scheduler = BackgroundScheduler(timezone='Asia/Seoul')

        # 매일 09:00 — 오늘 면접 리마인드
        scheduler.add_job(job_interview_reminder, 'cron', hour=9, minute=0,
                          id='interview_reminder')

        # 매월 25일 09:00 — 급여일 알림
        scheduler.add_job(job_payroll_reminder, 'cron', day=25, hour=9, minute=0,
                          id='payroll_reminder')

        # 매일 10:00 — 피어리뷰 D-3 체크
        scheduler.add_job(job_peer_review_reminder, 'cron', hour=10, minute=0,
                          id='peer_review_reminder')

        # 매일 03:00 — DB 자동 백업 (Phase A-4)
        def job_db_backup():
            try:
                from backup_db import run_backup
                run_backup()
            except Exception as e:
                print(f"[scheduler] db_backup 오류: {e}")
        scheduler.add_job(job_db_backup, 'cron', hour=3, minute=0, id='db_backup')

        scheduler.start()
        print("[scheduler] APScheduler 시작 완료 (면접리마인드/급여일/피어리뷰 D-3/DB백업)")
        return scheduler
    except ImportError:
        print("[scheduler] APScheduler 미설치 — pip install APScheduler==3.10.4")
        return None
    except Exception as e:
        print(f"[scheduler] 시작 오류: {e}")
        return None


if __name__ == '__main__':
    print("=" * 50)
    print("[run.py] TalentCore 개발 서버 시작")
    print("=" * 50)
    load_dotenv()

    kill_port(5000)

    # Slack 예약 알림 스케줄러 시작
    _scheduler = start_scheduler()

    print("[run.py] Flask 앱 기동 중...\n")
    try:
        proc = subprocess.Popen([sys.executable, 'app.py'])
        proc.wait()
    finally:
        if _scheduler:
            _scheduler.shutdown(wait=False)
