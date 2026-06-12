"""
seed_requisitions.py
채용 요청서 + 공고 연동 시드 데이터
- job_postings(기존 10건)에 requisition_id 컬럼 추가 및 연결
- job_requisitions 10건 생성 (approved, 실제 직군/레벨/밴드 연동)
- job_postings salary_min/max 업데이트
"""
import sqlite3
import os

DB = os.environ.get('DB_DIR', '')
DB = os.path.join(DB, 'hr_system.db') if DB else 'hr_system.db'

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
c = conn.cursor()

# ── 1. 컬럼 마이그레이션 ─────────────────────────────────────────────
jr_cols = {r[1] for r in c.execute('PRAGMA table_info(job_requisitions)').fetchall()}
if 'job_level' not in jr_cols:
    c.execute('ALTER TABLE job_requisitions ADD COLUMN job_level TEXT')
    print('added job_requisitions.job_level')

jp_cols = {r[1] for r in c.execute('PRAGMA table_info(job_postings)').fetchall()}
if 'requisition_id' not in jp_cols:
    c.execute('ALTER TABLE job_postings ADD COLUMN requisition_id INTEGER REFERENCES job_requisitions(id)')
    print('added job_postings.requisition_id')

# ── 2. 기존 데이터 확인 ──────────────────────────────────────────────
postings = c.execute(
    'SELECT id, title, department_id FROM job_postings ORDER BY id'
).fetchall()

families = {r['name']: r['id'] for r in c.execute('SELECT id, name FROM job_families').fetchall()}
positions = {r['level']: r['id'] for r in c.execute('SELECT id, level FROM positions').fetchall()}
admin_id  = c.execute("SELECT id FROM users WHERE role='admin' LIMIT 1").fetchone()['id']
dept_mgr  = c.execute("SELECT id FROM users WHERE role='manager' LIMIT 1").fetchone()
dept_mgr_id = dept_mgr['id'] if dept_mgr else admin_id

def get_band(family_id, pos_id):
    row = c.execute(
        'SELECT min_salary, mid_salary, max_salary FROM salary_grades '
        'WHERE job_family_id=? AND position_id=?', (family_id, pos_id)
    ).fetchone()
    return row if row else None

# ── 3. 공고별 요청서 설정 ────────────────────────────────────────────
# posting_id → (job_family_name, job_level CL1~CL9, track IC/M, reason)
SPEC = {
    1:  ('소프트웨어 엔지니어링', 'CL4', 'IC',  '백엔드 플랫폼 확장 인력 충원'),
    2:  ('소프트웨어 엔지니어링', 'CL3', 'IC',  '프론트엔드 개발자 신규 충원'),
    3:  ('데이터/ML 엔지니어링', 'CL4', 'IC',  'ML 추천 시스템 고도화 인력'),
    4:  ('인프라/DevOps',        'CL3', 'IC',  '클라우드 인프라 안정화'),
    5:  ('프로덕트 매니지먼트',   'CL5', 'IC',  'B2B SaaS PM 리드급 채용'),
    6:  ('인프라/DevOps',        'CL4', 'IC',  'SRE 포지션 신설'),
    7:  ('마케팅/그로스',         'CL3', 'IC',  '브랜드 마케팅 팀 빌딩'),
    8:  ('영업/CS',              'CL4', 'M',   'Enterprise 영업 매니저'),
    9:  ('디자인/UX',            'CL3', 'IC',  'UX 디자이너 충원'),
    10: ('인사/HR',              'CL3', 'IC',  'IT 채용 담당자 신규'),
}

LEVEL_TO_POS = {f'CL{i}': i for i in range(1, 10)}
import json

for posting in postings:
    pid = posting['id']
    if pid not in SPEC:
        continue

    fam_name, job_level, track, reason = SPEC[pid]
    fam_id  = families.get(fam_name)
    if not fam_id:
        # 이름 부분 매칭
        for fname, fid in families.items():
            if any(k in fname for k in fam_name.split('/')):
                fam_id = fid
                break

    pos_level = LEVEL_TO_POS.get(job_level, 3)
    pos_id    = positions.get(pos_level)
    band      = get_band(fam_id, pos_id) if fam_id and pos_id else None

    sal_min  = band['min_salary'] if band else 40_000_000
    sal_mid  = band['mid_salary'] if band else 55_000_000
    sal_max  = band['max_salary'] if band else 70_000_000

    # 기존 요청서 있으면 skip
    existing = c.execute('SELECT id FROM job_requisitions WHERE posting_id=?', (pid,)).fetchone()
    if existing:
        req_id = existing['id']
        print(f'posting {pid}: requisition already exists (id={req_id}), updating...')
        c.execute('''UPDATE job_requisitions SET
            job_family_id=?, job_level=?, track=?, salary_mid=?,
            salary_min=?, salary_max=?, status=\'approved\'
            WHERE id=?''',
            (fam_id, job_level, track, sal_mid, sal_min, sal_max, req_id))
    else:
        req_id = c.execute('''
            INSERT INTO job_requisitions
            (title, department_id, headcount, employment_type, reason,
             salary_min, salary_max, status,
             requester_id, dept_approver_id, dept_approved_at,
             hr_approver_id, hr_approved_at,
             posting_id, job_family_id, track, salary_mid, job_level)
            VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now','-20 days'),?,datetime('now','-15 days'),?,?,?,?,?)
        ''', (
            posting['title'],
            posting['department_id'],
            1,
            'regular',
            reason,
            sal_min, sal_max,
            'approved',
            admin_id, dept_mgr_id,
            admin_id,
            pid,
            fam_id, track, sal_mid, job_level,
        )).lastrowid
        print(f'posting {pid} "{posting["title"]}": created requisition {req_id} ({job_level} {track}, mid={sal_mid:,})')

    # job_postings salary + requisition_id 업데이트
    c.execute('UPDATE job_postings SET salary_min=?, salary_max=?, requisition_id=? WHERE id=?',
              (sal_min, sal_max, req_id, pid))

conn.commit()
conn.close()
print('\n시드 완료.')
