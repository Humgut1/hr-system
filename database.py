import sqlite3
import os
from werkzeug.security import generate_password_hash

DATABASE = 'hr_system.db'

_DEV_PW   = os.environ.get('HR_DEV_PASSWORD',   'changeme!')
_GUEST_PW = os.environ.get('HR_GUEST_PASSWORD', 'guest1234!')


def init_db():
    conn = sqlite3.connect(DATABASE)
    try:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()

        c.executescript('''
            CREATE TABLE IF NOT EXISTS departments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                parent_id INTEGER REFERENCES departments(id),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                level INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS job_families (
                id   INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS salary_grades (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                job_family_id INTEGER NOT NULL REFERENCES job_families(id),
                position_id   INTEGER NOT NULL REFERENCES positions(id),
                annual_salary INTEGER NOT NULL,
                UNIQUE(job_family_id, position_id)
            );

            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                name TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('admin', 'manager', 'employee', 'recruiter', 'guest')),
                department_id INTEGER REFERENCES departments(id),
                position_id   INTEGER REFERENCES positions(id),
                job_family_id INTEGER REFERENCES job_families(id),
                phone TEXT,
                hire_date  DATE,
                birth_date DATE,
                status TEXT DEFAULT 'active' CHECK(status IN ('active', 'inactive', 'resigned')),
                emp_no TEXT,
                manager_id INTEGER REFERENCES users(id),
                employment_type TEXT NOT NULL DEFAULT 'full_time'
                    CHECK(employment_type IN ('full_time','part_time','contract','intern')),
                termination_date DATE,
                termination_reason TEXT,
                onboarded INTEGER NOT NULL DEFAULT 0,
                features_enabled TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS announcements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                pinned INTEGER DEFAULT 0,
                author_id INTEGER NOT NULL REFERENCES users(id),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS leave_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL REFERENCES users(id),
                type        TEXT NOT NULL
                                CHECK(type IN (
                                    'annual','half_am','half_pm','sick','remote','outing',
                                    'maternity','paternity','parental','family_care',
                                    'bereavement','military','compensation'
                                )),
                start_date  DATE NOT NULL,
                end_date    DATE NOT NULL,
                days        REAL NOT NULL DEFAULT 1,
                reason      TEXT,
                status      TEXT NOT NULL DEFAULT 'pending'
                                CHECK(status IN ('pending','approved','rejected','cancelled')),
                approver_id INTEGER REFERENCES users(id),
                reject_reason TEXT,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS employee_salary (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id             INTEGER UNIQUE NOT NULL REFERENCES users(id),
                base_salary         INTEGER NOT NULL DEFAULT 3000000,
                meal_allowance      INTEGER NOT NULL DEFAULT 200000,
                transport_allowance INTEGER NOT NULL DEFAULT 100000,
                updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS payslips (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id              INTEGER NOT NULL REFERENCES users(id),
                year                 INTEGER NOT NULL,
                month                INTEGER NOT NULL,
                base_salary          INTEGER NOT NULL,
                meal_allowance       INTEGER NOT NULL DEFAULT 0,
                transport_allowance  INTEGER NOT NULL DEFAULT 0,
                overtime_pay         INTEGER NOT NULL DEFAULT 0,
                national_pension     INTEGER NOT NULL DEFAULT 0,
                health_insurance     INTEGER NOT NULL DEFAULT 0,
                long_term_care       INTEGER NOT NULL DEFAULT 0,
                employment_insurance INTEGER NOT NULL DEFAULT 0,
                income_tax           INTEGER NOT NULL DEFAULT 0,
                local_income_tax     INTEGER NOT NULL DEFAULT 0,
                gross_pay            INTEGER NOT NULL,
                total_deduction      INTEGER NOT NULL,
                net_pay              INTEGER NOT NULL,
                created_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, year, month)
            );

            CREATE TABLE IF NOT EXISTS performance_cycles (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT NOT NULL,
                start_date DATE NOT NULL,
                end_date   DATE NOT NULL,
                status     TEXT NOT NULL DEFAULT 'active'
                               CHECK(status IN ('active','closed')),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS performance_goals (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                cycle_id     INTEGER NOT NULL REFERENCES performance_cycles(id),
                user_id      INTEGER NOT NULL REFERENCES users(id),
                category     TEXT NOT NULL DEFAULT 'KPI'
                                 CHECK(category IN ('OKR','KPI')),
                title        TEXT NOT NULL,
                description  TEXT,
                weight       INTEGER NOT NULL DEFAULT 100
                                 CHECK(weight BETWEEN 1 AND 100),
                progress     INTEGER NOT NULL DEFAULT 0
                                 CHECK(progress BETWEEN 0 AND 100),
                self_score   INTEGER CHECK(self_score BETWEEN 1 AND 5),
                self_comment TEXT,
                status       TEXT NOT NULL DEFAULT 'active'
                                 CHECK(status IN ('active','completed')),
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS performance_reviews (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                goal_id     INTEGER NOT NULL REFERENCES performance_goals(id),
                reviewer_id INTEGER NOT NULL REFERENCES users(id),
                score       INTEGER NOT NULL CHECK(score BETWEEN 1 AND 5),
                comment     TEXT,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(goal_id, reviewer_id)
            );

            CREATE TABLE IF NOT EXISTS job_postings (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                title         TEXT NOT NULL,
                department_id INTEGER REFERENCES departments(id),
                position_id   INTEGER REFERENCES positions(id),
                description   TEXT,
                requirements  TEXT,
                status        TEXT NOT NULL DEFAULT 'open'
                                  CHECK(status IN ('draft','open','closed')),
                deadline      DATE,
                created_by    INTEGER NOT NULL REFERENCES users(id),
                created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS applicants (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                posting_id  INTEGER NOT NULL REFERENCES job_postings(id),
                name        TEXT NOT NULL,
                email       TEXT NOT NULL,
                phone       TEXT,
                source      TEXT DEFAULT 'direct',
                resume_note TEXT,
                stage       TEXT NOT NULL DEFAULT 'applied'
                                CHECK(stage IN ('applied','screening','interview1',
                                                'interview2','final','offered',
                                                'hired','rejected')),
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS applicant_logs (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                applicant_id INTEGER NOT NULL REFERENCES applicants(id),
                stage        TEXT NOT NULL,
                note         TEXT,
                changed_by   INTEGER NOT NULL REFERENCES users(id),
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS checkins (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      INTEGER NOT NULL REFERENCES users(id),
                date         DATE    NOT NULL,
                check_in     TIME,
                check_out    TIME,
                note         TEXT,
                regular_min  INTEGER DEFAULT 0,
                overtime_min INTEGER DEFAULT 0,
                night_min    INTEGER DEFAULT 0,
                UNIQUE(user_id, date)
            );

            CREATE TABLE IF NOT EXISTS flex_schedules (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      INTEGER NOT NULL REFERENCES users(id),
                week_start   DATE    NOT NULL,
                status       TEXT    NOT NULL DEFAULT 'draft',
                note         TEXT,
                submitted_at TIMESTAMP,
                approved_by  INTEGER REFERENCES users(id),
                approved_at  TIMESTAMP,
                reject_reason TEXT,
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, week_start)
            );

            CREATE TABLE IF NOT EXISTS flex_blocks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                schedule_id INTEGER NOT NULL REFERENCES flex_schedules(id) ON DELETE CASCADE,
                work_date   DATE    NOT NULL,
                start_time  TEXT    NOT NULL,
                end_time    TEXT    NOT NULL,
                block_type  TEXT    NOT NULL DEFAULT 'office'
            );

            CREATE TABLE IF NOT EXISTS peer_assignments (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                cycle_id    INTEGER NOT NULL REFERENCES performance_cycles(id),
                reviewee_id INTEGER NOT NULL REFERENCES users(id),
                reviewer_id INTEGER NOT NULL REFERENCES users(id),
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(cycle_id, reviewee_id, reviewer_id)
            );

            CREATE TABLE IF NOT EXISTS peer_reviews (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                cycle_id    INTEGER NOT NULL REFERENCES performance_cycles(id),
                reviewee_id INTEGER NOT NULL REFERENCES users(id),
                reviewer_id INTEGER NOT NULL REFERENCES users(id),
                review_type TEXT NOT NULL CHECK(review_type IN ('peer','upward')),
                score       INTEGER CHECK(score BETWEEN 1 AND 5),
                strength    TEXT,
                improvement TEXT,
                q1_score    INTEGER CHECK(q1_score BETWEEN 1 AND 5),
                q2_score    INTEGER CHECK(q2_score BETWEEN 1 AND 5),
                q3_score    INTEGER CHECK(q3_score BETWEEN 1 AND 5),
                q4_score    INTEGER CHECK(q4_score BETWEEN 1 AND 5),
                q5_score    INTEGER CHECK(q5_score BETWEEN 1 AND 5),
                comment     TEXT,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(cycle_id, reviewee_id, reviewer_id, review_type)
            );

            CREATE TABLE IF NOT EXISTS calibration_results (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                cycle_id    INTEGER NOT NULL REFERENCES performance_cycles(id),
                user_id     INTEGER NOT NULL REFERENCES users(id),
                final_grade TEXT NOT NULL CHECK(final_grade IN ('S','A','B','C','D')),
                note        TEXT,
                decided_by  INTEGER NOT NULL REFERENCES users(id),
                decided_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(cycle_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS company_settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS company_config (
                id                         INTEGER PRIMARY KEY DEFAULT 1,
                -- 근무 제도
                work_system                TEXT NOT NULL DEFAULT 'standard',
                work_start                 TEXT NOT NULL DEFAULT '09:00',
                work_end                   TEXT NOT NULL DEFAULT '18:00',
                lunch_start                TEXT NOT NULL DEFAULT '12:00',
                lunch_end                  TEXT NOT NULL DEFAULT '13:00',
                core_start                 TEXT NOT NULL DEFAULT '10:00',
                core_end                   TEXT NOT NULL DEFAULT '16:00',
                flex_settle_months         INTEGER NOT NULL DEFAULT 1,
                elastic_unit               TEXT NOT NULL DEFAULT '2weeks',
                -- 재택 정책
                remote_allowed             INTEGER NOT NULL DEFAULT 1,
                remote_max_days_week       INTEGER NOT NULL DEFAULT 3,
                -- 휴가 정책
                leave_policy               TEXT NOT NULL DEFAULT 'legal',
                leave_extra_days           INTEGER NOT NULL DEFAULT 0,
                allow_half_day             INTEGER NOT NULL DEFAULT 1,
                allow_quarter_day          INTEGER NOT NULL DEFAULT 0,
                sick_policy                TEXT NOT NULL DEFAULT 'annual',
                sick_days_year             INTEGER NOT NULL DEFAULT 0,
                -- 급여 기본 설정
                pay_day                    INTEGER NOT NULL DEFAULT 25,
                default_meal_allowance     INTEGER NOT NULL DEFAULT 200000,
                default_transport_allowance INTEGER NOT NULL DEFAULT 100000,
                -- 성과관리
                perf_cycle                 TEXT NOT NULL DEFAULT 'semiannual',
                use_peer_review            INTEGER NOT NULL DEFAULT 1,
                use_self_review            INTEGER NOT NULL DEFAULT 1,
                grade_system               TEXT NOT NULL DEFAULT 'SABCD',
                -- 셋업 상태
                setup_completed            INTEGER NOT NULL DEFAULT 0,  -- 0=미완료, 1=완료
                setup_step                 INTEGER NOT NULL DEFAULT 0,
                updated_at                 TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS severance_payments (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id          INTEGER NOT NULL REFERENCES users(id),
                hire_date        DATE    NOT NULL,
                termination_date DATE    NOT NULL,
                tenure_days      INTEGER NOT NULL,
                basis_total_pay  INTEGER NOT NULL DEFAULT 0,
                basis_days       INTEGER NOT NULL DEFAULT 92,
                avg_daily_wage   INTEGER NOT NULL DEFAULT 0,
                severance_amount INTEGER NOT NULL,
                note             TEXT,
                processed_by     INTEGER NOT NULL REFERENCES users(id),
                processed_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        ''')

        # 컬럼 마이그레이션: 기존 DB에 없을 수 있는 컬럼 추가
        existing = {r[1] for r in c.execute('PRAGMA table_info(users)').fetchall()}
        if 'onboarded' not in existing:
            c.execute('ALTER TABLE users ADD COLUMN onboarded INTEGER NOT NULL DEFAULT 0')
        if 'features_enabled' not in existing:
            c.execute('ALTER TABLE users ADD COLUMN features_enabled TEXT NOT NULL DEFAULT ""')
        # leave_requests.type CHECK 확장 (새 휴가 유형 추가)
        lr_sql = c.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='leave_requests'"
        ).fetchone()
        if lr_sql and 'maternity' not in lr_sql[0]:
            c.executescript('''
                PRAGMA foreign_keys = OFF;
                ALTER TABLE leave_requests RENAME TO _lr_old;
                CREATE TABLE leave_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id     INTEGER NOT NULL REFERENCES users(id),
                    type        TEXT NOT NULL
                                    CHECK(type IN (
                                        'annual','half_am','half_pm','sick','remote','outing',
                                        'maternity','paternity','parental','family_care',
                                        'bereavement','military','compensation'
                                    )),
                    start_date  DATE NOT NULL,
                    end_date    DATE NOT NULL,
                    days        REAL NOT NULL DEFAULT 1,
                    reason      TEXT,
                    status      TEXT NOT NULL DEFAULT 'pending'
                                    CHECK(status IN ('pending','approved','rejected','cancelled')),
                    approver_id INTEGER REFERENCES users(id),
                    reject_reason TEXT,
                    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                INSERT INTO leave_requests SELECT * FROM _lr_old;
                DROP TABLE _lr_old;
                PRAGMA foreign_keys = ON;
            ''')

        if 'emp_no' not in existing:
            c.execute('ALTER TABLE users ADD COLUMN emp_no TEXT')
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_emp_no ON users(emp_no) WHERE emp_no IS NOT NULL")
            c.execute("UPDATE users SET emp_no = 'TC-' || printf('%05d', id) WHERE emp_no IS NULL")
        if 'manager_id' not in existing:
            c.execute('ALTER TABLE users ADD COLUMN manager_id INTEGER REFERENCES users(id)')
        if 'employment_type' not in existing:
            c.execute('ALTER TABLE users ADD COLUMN employment_type TEXT NOT NULL DEFAULT "full_time"')
        if 'termination_date' not in existing:
            c.execute('ALTER TABLE users ADD COLUMN termination_date DATE')
        if 'termination_reason' not in existing:
            c.execute('ALTER TABLE users ADD COLUMN termination_reason TEXT')
        if 'work_type' not in existing:
            c.execute('ALTER TABLE users ADD COLUMN work_type TEXT NOT NULL DEFAULT "standard"')

        # checkins 컬럼 마이그레이션
        checkin_cols = {r[1] for r in c.execute('PRAGMA table_info(checkins)').fetchall()}
        if 'regular_min' not in checkin_cols:
            c.execute('ALTER TABLE checkins ADD COLUMN regular_min  INTEGER DEFAULT 0')
        if 'overtime_min' not in checkin_cols:
            c.execute('ALTER TABLE checkins ADD COLUMN overtime_min INTEGER DEFAULT 0')
        if 'night_min' not in checkin_cols:
            c.execute('ALTER TABLE checkins ADD COLUMN night_min    INTEGER DEFAULT 0')

        # company_config 기본 row (없으면 삽입 — setup_completed=0 유지해서 위자드 표시)
        if c.execute('SELECT COUNT(*) FROM company_config').fetchone()[0] == 0:
            c.execute('INSERT INTO company_config (id) VALUES (1)')

        if c.execute('SELECT COUNT(*) FROM departments').fetchone()[0] == 0:
            departments = [
                ('경영지원', None), ('개발', None), ('마케팅', None),
                ('영업', None), ('인사', None),
            ]
            c.executemany('INSERT INTO departments (name, parent_id) VALUES (?, ?)', departments)

            positions = [
                ('인턴', 1), ('사원', 2), ('주임', 3), ('대리', 4),
                ('과장', 5), ('차장', 6), ('부장', 7), ('이사', 8),
            ]
            c.executemany('INSERT INTO positions (name, level) VALUES (?, ?)', positions)

            pw      = generate_password_hash(_DEV_PW)
            gpw     = generate_password_hash(_GUEST_PW)
            ALL_F   = ('attendance,payroll,performance,peer_review,calibration,'
                       'recruiting,announcements,org_chart,certificates')
            users = [
                ('admin@company.com',     pw,  'HR 관리자', 'admin',     5, 7, '2022-01-03', 0,  ''),
                ('manager@company.com',   pw,  '김팀장',    'manager',   2, 6, '2021-05-10', 1,  ALL_F),
                ('employee@company.com',  pw,  '이직원',    'employee',  2, 4, '2023-03-06', 1,  ALL_F),
                ('recruiter@company.com', pw,  '박채용',    'recruiter', 5, 4, '2023-07-01', 1,  ALL_F),
                ('kim@company.com',       pw,  '김철수',    'employee',  2, 3, '2024-01-15', 1,  ALL_F),
                ('lee@company.com',       pw,  '이영희',    'employee',  3, 4, '2023-11-01', 1,  ALL_F),
                ('park@company.com',      pw,  '박민준',    'employee',  4, 5, '2022-08-20', 1,  ALL_F),
                ('choi@company.com',      pw,  '최지수',    'employee',  1, 2, '2024-03-01', 1,  ALL_F),
                ('guest@talentcore.com',  gpw, 'Guest',    'guest',     1, 3, '2024-01-01', 1,  ALL_F),
            ]
            c.executemany(
                'INSERT INTO users '
                '(email, password_hash, name, role, department_id, position_id, '
                ' hire_date, onboarded, features_enabled) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                users
            )

        # guest 계정 보장 (migrate_db 실행 후 삭제되어도 앱 기동 시 자동 복구)
        if c.execute("SELECT COUNT(*) FROM users WHERE role='guest'").fetchone()[0] == 0:
            ALL_F = ('attendance,payroll,performance,peer_review,calibration,'
                     'recruiting,announcements,org_chart,certificates')
            gpw = generate_password_hash(_GUEST_PW)
            c.execute(
                "INSERT INTO users (email, password_hash, name, role, department_id, "
                "position_id, hire_date, onboarded, features_enabled) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                ('guest@talentcore.com', gpw, 'Guest', 'guest', 1, 3,
                 '2024-01-01', 1, ALL_F)
            )

        if c.execute('SELECT COUNT(*) FROM announcements').fetchone()[0] == 0:
            admin_id = c.execute("SELECT id FROM users WHERE role='admin' LIMIT 1").fetchone()[0]
            announcements = [
                ('2026년 상반기 성과평가 일정 안내', '2026년 상반기 성과평가가 5월 1일부터 시작됩니다.\n\n평가 기간: 2026.05.01 ~ 05.31\n평가 방법: TalentCore 시스템 내 성과 평가 메뉴에서 진행\n\n모든 팀장은 기간 내 팀원 평가를 완료해 주세요.', 1, admin_id),
                ('재택근무 정책 업데이트', '2026년 4월부터 재택근무 정책이 아래와 같이 변경됩니다.\n\n- 주 2회 재택 가능 (화, 목 권장)\n- 재택 신청은 전주 금요일까지 시스템에서 신청\n- 팀장 승인 후 확정\n\n자세한 사항은 HR에 문의 바랍니다.', 0, admin_id),
                ('사내 복지 포인트 지급 안내', '2026년 1분기 복지 포인트가 지급되었습니다.\n\n- 지급 금액: 인당 200,000원\n- 사용 기한: 2026.06.30\n- 사용처: 제휴 가맹점 및 온라인몰\n\n복지 포인트 관련 문의는 경영지원팀으로 연락 주세요.', 0, admin_id),
            ]
            c.executemany(
                'INSERT INTO announcements (title, content, pinned, author_id) VALUES (?, ?, ?, ?)',
                announcements
            )

        conn.commit()
    finally:
        conn.close()
