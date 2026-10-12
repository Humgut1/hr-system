"""
테넌트 DB 자동 백업 스크립트 (Phase A-4 보안 기준선)

대상: master.db + hr_system.db(테넌트 1) + tenant_*.db 전부
방식: sqlite3 온라인 백업 API (서비스 무중단 — 쓰기 중에도 일관된 스냅샷 보장)
보관: backups/YYYYMMDD_HHMMSS/ 폴더, 최근 KEEP_COUNT개만 유지 (오래된 것 자동 삭제)

사용법:
    python backup_db.py                     # 백업 실행
    python backup_db.py --list              # 백업 목록 확인
    python backup_db.py --restore 20260713_030000 hr_system.db
                                            # 특정 백업에서 특정 DB 복원
                                            # (서버를 멈춘 뒤 실행 — 현재 상태를 .pre-restore로 자동 보존)

운영(Oracle Cloud VM) 등록: bash deploy/setup_backup.sh  → 매일 03:00 cron
로컬 개발: run.py의 APScheduler가 매일 03:00 자동 실행
"""
import os
import sys
import glob
import shutil
import sqlite3
from datetime import datetime

# Windows 콘솔(cp949)에서 유니코드 출력 깨짐 방지
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except AttributeError:
    pass

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
BACKUP_DIR = os.path.join(BASE_DIR, 'backups')
KEEP_COUNT = 14   # 최근 14개(일 단위 실행 시 2주치) 유지


def find_databases():
    """백업 대상 DB 파일 목록."""
    targets = []
    for name in ('master.db', 'hr_system.db'):
        p = os.path.join(BASE_DIR, name)
        if os.path.exists(p):
            targets.append(p)
    targets += sorted(glob.glob(os.path.join(BASE_DIR, 'tenant_*.db')))
    return targets


def backup_one(src_path, dest_dir):
    """sqlite3 온라인 백업 API로 단일 DB 백업 (락 걸지 않고 일관된 스냅샷)."""
    dest_path = os.path.join(dest_dir, os.path.basename(src_path))
    src  = sqlite3.connect(src_path)
    dest = sqlite3.connect(dest_path)
    try:
        src.backup(dest)
    finally:
        dest.close()
        src.close()
    return dest_path


def prune_old():
    """오래된 백업 폴더 삭제 (최근 KEEP_COUNT개만 유지)."""
    dirs = sorted(glob.glob(os.path.join(BACKUP_DIR, '[0-9]' * 8 + '_' + '[0-9]' * 6)))
    removed = []
    while len(dirs) > KEEP_COUNT:
        victim = dirs.pop(0)
        shutil.rmtree(victim, ignore_errors=True)
        removed.append(os.path.basename(victim))
    return removed


def run_backup():
    stamp    = datetime.now().strftime('%Y%m%d_%H%M%S')
    dest_dir = os.path.join(BACKUP_DIR, stamp)
    os.makedirs(dest_dir, exist_ok=True)

    targets = find_databases()
    if not targets:
        print('[backup] 백업할 DB 파일이 없습니다.')
        return

    total = 0
    for src in targets:
        dest = backup_one(src, dest_dir)
        size = os.path.getsize(dest)
        total += size
        print(f'[backup] {os.path.basename(src)} → {stamp}/ ({size / 1024:.0f} KB)')

    removed = prune_old()
    if removed:
        print(f'[backup] 오래된 백업 {len(removed)}개 삭제: {", ".join(removed)}')
    print(f'[backup] 완료 — {len(targets)}개 DB, 총 {total / 1024 / 1024:.1f} MB → {dest_dir}')


def list_backups():
    dirs = sorted(glob.glob(os.path.join(BACKUP_DIR, '[0-9]' * 8 + '_' + '[0-9]' * 6)))
    if not dirs:
        print('백업이 없습니다.')
        return
    for d in dirs:
        files = os.listdir(d)
        size  = sum(os.path.getsize(os.path.join(d, f)) for f in files)
        print(f'{os.path.basename(d)} — {len(files)}개 파일, {size / 1024 / 1024:.1f} MB')


def restore(stamp, db_name):
    """백업 파일로 DB 를 되돌린다. **서버(gunicorn)를 먼저 멈추고** 실행할 것.

    WAL 모드(M3) 주의: 본 파일 옆의 -wal/-shm 에는 아직 본 파일에 합쳐지지 않은 변경이 있다.
      - 현재 상태 보존(.pre-restore)은 파일 복사가 아니라 백업 API 로 떠서 -wal 내용까지 담는다.
      - 복원 전에 옛 -wal/-shm 을 지운다. 남겨 두면 다음 열 때 옛 변경이 복원본 위에 덮여 DB 가 깨진다.
    """
    src = os.path.join(BACKUP_DIR, stamp, db_name)
    if not os.path.exists(src):
        print(f'[restore] 백업 파일이 없습니다: {src}')
        sys.exit(1)
    dest = os.path.join(BASE_DIR, db_name)
    if os.path.exists(dest):
        keep = dest + '.pre-restore'
        if os.path.exists(keep):
            os.remove(keep)
        cur = sqlite3.connect(dest)
        out = sqlite3.connect(keep)
        try:
            cur.backup(out)
        finally:
            out.close()
            cur.close()
        print(f'[restore] 현재 상태 보존: {keep}')
    for side in (dest + '-wal', dest + '-shm'):
        if os.path.exists(side):
            os.remove(side)
    shutil.copy2(src, dest)
    print(f'[restore] 복원 완료: {stamp}/{db_name} → {dest}')
    print('[restore] 이제 서버를 다시 시작하세요.')


if __name__ == '__main__':
    args = sys.argv[1:]
    if not args:
        run_backup()
    elif args[0] == '--list':
        list_backups()
    elif args[0] == '--restore' and len(args) == 3:
        restore(args[1], args[2])
    else:
        print(__doc__)
