"""
로컬 개발 서버 실행 스크립트.
실행 전 포트 5000에 떠있는 모든 프로세스를 자동으로 정리합니다.

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
            time.sleep(1)  # 소켓 반환 대기

            # 정리 확인
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


if __name__ == '__main__':
    print("=" * 50)
    print("[run.py] TalentCore 개발 서버 시작")
    print("=" * 50)
    load_dotenv()

    kill_port(5000)

    print("[run.py] Flask 앱 기동 중...\n")
    proc = subprocess.Popen([sys.executable, 'app.py'])
    proc.wait()
