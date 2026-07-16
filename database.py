import sqlite3
import os
from werkzeug.security import generate_password_hash

_db_dir  = os.environ.get('DB_DIR', '')
DATABASE = os.path.join(_db_dir, 'hr_system.db') if _db_dir else 'hr_system.db'

_DEV_PW   = os.environ.get('HR_DEV_PASSWORD',   'changeme!')
_GUEST_PW = os.environ.get('HR_GUEST_PASSWORD', 'guest1234!')


def init_db(db_path: str = None):
    """DB 스키마 초기화. db_path 지정 시 해당 경로에 생성 (신규 테넌트용)."""
    conn = sqlite3.connect(db_path or DATABASE)
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

            CREATE TABLE IF NOT EXISTS job_family_groups (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                code       TEXT UNIQUE NOT NULL,
                name       TEXT NOT NULL,
                sort_order INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS job_families (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                code       TEXT UNIQUE NOT NULL,
                name       TEXT NOT NULL,
                group_id   INTEGER REFERENCES job_family_groups(id),
                sort_order INTEGER DEFAULT 0
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
                                    'bereavement','military','compensation',
                                    'menstrual','miscarriage','fertility','parental_reduction'
                                )),
                start_date  DATE NOT NULL,
                end_date    DATE NOT NULL,
                days        REAL NOT NULL DEFAULT 1,
                reason      TEXT,
                status      TEXT NOT NULL DEFAULT 'pending'
                                    CHECK(status IN ('pending','reviewed','approved','rejected','cancelled')),
                    approver_id INTEGER REFERENCES users(id),
                    manager_id  INTEGER REFERENCES users(id),
                    manager_approved_at TIMESTAMP,
                    hr_id       INTEGER REFERENCES users(id),
                    hr_approved_at      TIMESTAMP,
                    reject_reason TEXT,
                    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS certificate_requests (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      INTEGER NOT NULL REFERENCES users(id),
                cert_type    TEXT NOT NULL CHECK(cert_type IN ('employment','career','income','resignation')),
                purpose      TEXT,
                status       TEXT NOT NULL DEFAULT 'pending'
                                 CHECK(status IN ('pending','approved','rejected','cancelled')),
                approver_id  INTEGER REFERENCES users(id),
                approved_at  TIMESTAMP,
                reject_reason TEXT,
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS employee_salary (

                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id             INTEGER UNIQUE NOT NULL REFERENCES users(id),
                base_salary         INTEGER NOT NULL DEFAULT 3000000,
                meal_allowance      INTEGER NOT NULL DEFAULT 200000,
                transport_allowance INTEGER NOT NULL DEFAULT 100000,
                updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS salary_history (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id             INTEGER NOT NULL REFERENCES users(id),
                changed_at          DATETIME DEFAULT CURRENT_TIMESTAMP,
                changed_by          INTEGER REFERENCES users(id),
                old_base_salary     INTEGER NOT NULL DEFAULT 0,
                new_base_salary     INTEGER NOT NULL DEFAULT 0,
                old_meal            INTEGER NOT NULL DEFAULT 0,
                new_meal            INTEGER NOT NULL DEFAULT 0,
                old_transport       INTEGER NOT NULL DEFAULT 0,
                new_transport       INTEGER NOT NULL DEFAULT 0,
                reason              TEXT
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

            CREATE TABLE IF NOT EXISTS public_holidays (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                date    DATE    NOT NULL UNIQUE,
                name    TEXT    NOT NULL,
                year    INTEGER NOT NULL
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
                holiday_min  INTEGER DEFAULT 0,
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
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                cycle_id     INTEGER NOT NULL REFERENCES performance_cycles(id),
                user_id      INTEGER NOT NULL REFERENCES users(id),
                self_avg     REAL,
                peer_avg     REAL,
                mgr_avg      REAL,
                upward_avg   REAL,
                suggested_grade TEXT CHECK(suggested_grade IN ('S','A','B','C','D')),
                final_grade  TEXT NOT NULL CHECK(final_grade IN ('S','A','B','C','D')),
                summary_text TEXT,
                note         TEXT,
                is_shared    INTEGER NOT NULL DEFAULT 0,
                decided_by   INTEGER NOT NULL REFERENCES users(id),
                decided_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
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

            CREATE TABLE IF NOT EXISTS personnel_actions (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id        INTEGER NOT NULL REFERENCES users(id),
                action_type    TEXT NOT NULL CHECK(action_type IN (
                                   'dept_change','position_change','role_change',
                                   'employment_type_change','manager_change','salary_change'
                               )),
                from_value     TEXT,
                to_value       TEXT,
                effective_date DATE NOT NULL,
                reason         TEXT,
                status         TEXT NOT NULL DEFAULT 'approved'
                                   CHECK(status IN ('pending','approved','rejected')),
                rejection_reason TEXT,
                processed_by   INTEGER REFERENCES users(id),
                created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS termination_requests (
                id                           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id                      INTEGER NOT NULL REFERENCES users(id),
                request_type                 TEXT NOT NULL CHECK(request_type IN (
                                               'voluntary','mutual','dismissal','contract','retirement'
                                           )),
                request_source               TEXT NOT NULL DEFAULT 'employee'
                                               CHECK(request_source IN ('employee','manager','hr')),
                status                       TEXT NOT NULL DEFAULT 'submitted'
                                               CHECK(status IN (
                                                   'draft','submitted','under_review','approved',
                                                   'in_progress','completed','rejected','cancelled'
                                               )),
                notice_date                  DATE NOT NULL,
                requested_last_work_date     DATE NOT NULL,
                requested_termination_date   DATE NOT NULL,
                final_last_work_date         DATE,
                final_termination_date       DATE,
                reason_code                  TEXT,
                reason_detail                TEXT,
                handover_note                TEXT,
                manager_approved_by          INTEGER REFERENCES users(id),
                manager_approved_at          TIMESTAMP,
                hr_approved_by               INTEGER REFERENCES users(id),
                hr_approved_at               TIMESTAMP,
                rejection_reason             TEXT,
                completed_by                 INTEGER REFERENCES users(id),
                completed_at                 TIMESTAMP,
                created_by                   INTEGER NOT NULL REFERENCES users(id),
                created_at                   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at                   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS offboarding_tasks (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id     INTEGER NOT NULL REFERENCES termination_requests(id) ON DELETE CASCADE,
                task_type      TEXT NOT NULL CHECK(task_type IN (
                                   'handover','asset_return','account_disable',
                                   'payroll_close','documents'
                               )),
                title          TEXT NOT NULL,
                owner_role     TEXT NOT NULL CHECK(owner_role IN ('employee','manager','hr','admin')),
                status         TEXT NOT NULL DEFAULT 'pending'
                                   CHECK(status IN ('pending','completed')),
                due_date       DATE,
                note           TEXT,
                completed_by   INTEGER REFERENCES users(id),
                completed_at   TIMESTAMP,
                created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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

            CREATE TABLE IF NOT EXISTS benefit_configs (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                key            TEXT NOT NULL UNIQUE,
                enabled        INTEGER NOT NULL DEFAULT 0,
                payment_type   TEXT NOT NULL DEFAULT 'monthly_fixed',
                amount         INTEGER NOT NULL DEFAULT 0,
                annual_limit   INTEGER,
                pct            INTEGER,
                grade_pct_json TEXT,
                platform       TEXT,
                note           TEXT,
                updated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS employee_benefit_overrides (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL REFERENCES users(id),
                benefit_key TEXT NOT NULL,
                amount      INTEGER NOT NULL DEFAULT 0,
                enabled     INTEGER NOT NULL DEFAULT 1,
                note        TEXT,
                updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, benefit_key)
            );

            CREATE TABLE IF NOT EXISTS benefit_claims (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id       INTEGER NOT NULL REFERENCES users(id),
                benefit_key   TEXT NOT NULL,
                amount        INTEGER NOT NULL,
                expense_date  TEXT,
                description   TEXT,
                receipt_url   TEXT,
                status        TEXT NOT NULL DEFAULT 'pending'
                                  CHECK(status IN ('pending','approved','rejected')),
                reviewer_name TEXT,
                reviewer_note TEXT,
                submitted_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                reviewed_at   TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS welfare_point_ledger (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL REFERENCES users(id),
                delta       INTEGER NOT NULL,
                reason      TEXT NOT NULL,
                balance_after INTEGER NOT NULL DEFAULT 0,
                ref_id      INTEGER,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS benefit_enrollment_events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL REFERENCES users(id),
                event_type  TEXT NOT NULL CHECK(event_type IN ('onboarding','annual','life_event')),
                event_label TEXT NOT NULL,
                status      TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','completed','skipped')),
                due_date    TEXT,
                completed_at TIMESTAMP,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS bonus_payments (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         INTEGER NOT NULL REFERENCES users(id),
                bonus_type      TEXT NOT NULL,
                amount          INTEGER NOT NULL,
                pay_date        TEXT,
                note            TEXT,
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS notifications (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      INTEGER NOT NULL REFERENCES users(id),
                type         TEXT NOT NULL CHECK(type IN ('action','info')),
                category     TEXT NOT NULL, -- 'leave', 'action', 'cert', 'term', 'perf'
                title        TEXT NOT NULL,
                content      TEXT,
                link         TEXT,
                is_read      INTEGER DEFAULT 0,
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS holidays (
                id   INTEGER PRIMARY KEY AUTOINCREMENT,
                date DATE UNIQUE NOT NULL,
                name TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS one_on_ones (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                manager_id   INTEGER NOT NULL REFERENCES users(id),
                employee_id  INTEGER NOT NULL REFERENCES users(id),
                scheduled_at TIMESTAMP NOT NULL,
                status       TEXT NOT NULL DEFAULT 'scheduled'
                                 CHECK(status IN ('scheduled','done','cancelled')),
                agenda       TEXT,
                notes        TEXT,
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS one_on_one_actions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                meeting_id  INTEGER NOT NULL REFERENCES one_on_ones(id) ON DELETE CASCADE,
                content     TEXT NOT NULL,
                owner_id    INTEGER REFERENCES users(id),
                due_date    DATE,
                status      TEXT NOT NULL DEFAULT 'open'
                                CHECK(status IN ('open','done')),
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS contract_templates (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                name         TEXT NOT NULL,
                contract_type TEXT NOT NULL DEFAULT 'employment'
                                 CHECK(contract_type IN ('employment','nda','probation','freelance')),
                content_html TEXT NOT NULL,
                created_by   INTEGER NOT NULL REFERENCES users(id),
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS contracts (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                template_id  INTEGER REFERENCES contract_templates(id),
                employee_id  INTEGER NOT NULL REFERENCES users(id),
                issued_by    INTEGER NOT NULL REFERENCES users(id),
                title        TEXT NOT NULL,
                content_html TEXT NOT NULL,
                status       TEXT NOT NULL DEFAULT 'pending'
                                 CHECK(status IN ('pending','signed','rejected','cancelled')),
                signed_at    TIMESTAMP,
                sign_ip      TEXT,
                reject_reason TEXT,
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- v0.49: 후계자 계획
            CREATE TABLE IF NOT EXISTS succession_plans (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                position_title  TEXT NOT NULL,
                incumbent_id    INTEGER REFERENCES users(id),
                candidate_id    INTEGER NOT NULL REFERENCES users(id),
                readiness       TEXT NOT NULL DEFAULT 'ready_1y'
                                    CHECK(readiness IN ('ready_now','ready_1y','ready_2y','long_term')),
                note            TEXT,
                created_by      INTEGER NOT NULL REFERENCES users(id),
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- v0.50: 목표 템플릿
            CREATE TABLE IF NOT EXISTS goal_templates (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                title       TEXT NOT NULL,
                description TEXT,
                category    TEXT,           -- 개인/팀/조직
                weight      INTEGER DEFAULT 20,
                created_by  INTEGER REFERENCES users(id),
                is_active   INTEGER NOT NULL DEFAULT 1,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        ''')

        # 컬럼 마이그레이션: 기존 DB에 없을 수 있는 컬럼 추가
        existing = {r[1] for r in c.execute('PRAGMA table_info(users)').fetchall()}
        if 'onboarded' not in existing:
            c.execute('ALTER TABLE users ADD COLUMN onboarded INTEGER NOT NULL DEFAULT 0')
        if 'features_enabled' not in existing:
            c.execute('ALTER TABLE users ADD COLUMN features_enabled TEXT NOT NULL DEFAULT ""')
        # leave_requests.type CHECK 확장 (새 휴가 유형 추가) 및 다단계 승인 컬럼 추가
        lr_cols = {r[1] for r in c.execute('PRAGMA table_info(leave_requests)').fetchall()}
        lr_sql = c.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='leave_requests'"
        ).fetchone()
        
        # 신규 유형(menstrual 등)이 없거나 manager_id가 없으면 재생성 마이그레이션
        if lr_sql and ('menstrual' not in lr_sql[0] or 'manager_id' not in lr_cols):
            c.executescript('''
                PRAGMA foreign_keys = OFF;
                DROP TABLE IF EXISTS _lr_old;
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
                                    CHECK(status IN ('pending','reviewed','approved','rejected','cancelled')),
                    approver_id INTEGER REFERENCES users(id),
                    manager_id  INTEGER REFERENCES users(id),
                    manager_approved_at TIMESTAMP,
                    hr_id       INTEGER REFERENCES users(id),
                    hr_approved_at      TIMESTAMP,
                    reject_reason TEXT,
                    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                -- 기존 데이터 복구 (컬럼이 일치하는 것만)
                INSERT INTO leave_requests (id, user_id, type, start_date, end_date, days, reason, status, approver_id, reject_reason, created_at)
                SELECT id, user_id, type, start_date, end_date, days, reason, status, approver_id, reject_reason, created_at FROM _lr_old;
                DROP TABLE _lr_old;
                PRAGMA foreign_keys = ON;
            ''')

        # personnel_actions 컬럼 마이그레이션
        pa_cols = {r[1] for r in c.execute('PRAGMA table_info(personnel_actions)').fetchall()}
        if 'status' not in pa_cols:
            c.execute("ALTER TABLE personnel_actions ADD COLUMN status TEXT NOT NULL DEFAULT 'approved'")
        if 'rejection_reason' not in pa_cols:
            c.execute("ALTER TABLE personnel_actions ADD COLUMN rejection_reason TEXT")

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
        if 'address' not in existing:
            c.execute('ALTER TABLE users ADD COLUMN address TEXT')
        if 'emergency_name' not in existing:
            c.execute('ALTER TABLE users ADD COLUMN emergency_name TEXT')
        if 'emergency_phone' not in existing:
            c.execute('ALTER TABLE users ADD COLUMN emergency_phone TEXT')
        if 'emergency_relation' not in existing:
            c.execute('ALTER TABLE users ADD COLUMN emergency_relation TEXT')
        if 'buddy_id' not in existing:
            c.execute('ALTER TABLE users ADD COLUMN buddy_id INTEGER REFERENCES users(id)')
        if 'jira_epic_key' not in existing:
            c.execute('ALTER TABLE users ADD COLUMN jira_epic_key TEXT')
        if 'tour_completed' not in existing:
            c.execute('ALTER TABLE users ADD COLUMN tour_completed INTEGER NOT NULL DEFAULT 0')

        # benefit_configs 컬럼 마이그레이션
        bc_cols = {r[1] for r in c.execute('PRAGMA table_info(benefit_configs)').fetchall()}
        if 'payment_type' not in bc_cols:
            c.execute("ALTER TABLE benefit_configs ADD COLUMN payment_type TEXT NOT NULL DEFAULT 'monthly_fixed'")
        if 'annual_limit' not in bc_cols:
            c.execute('ALTER TABLE benefit_configs ADD COLUMN annual_limit INTEGER')
        if 'platform' not in bc_cols:
            c.execute('ALTER TABLE benefit_configs ADD COLUMN platform TEXT')
        if 'grade_pct_json' not in bc_cols:
            c.execute('ALTER TABLE benefit_configs ADD COLUMN grade_pct_json TEXT')

        # payslips 컬럼 마이그레이션
        payslip_cols = {r[1] for r in c.execute('PRAGMA table_info(payslips)').fetchall()}
        if 'bonus_pay' not in payslip_cols:
            c.execute('ALTER TABLE payslips ADD COLUMN bonus_pay INTEGER NOT NULL DEFAULT 0')
        if 'benefits_json' not in payslip_cols:
            c.execute('ALTER TABLE payslips ADD COLUMN benefits_json TEXT')

        # benefit_claims 컬럼 마이그레이션 (구 스키마 → 신 스키마)
        bc_claim_cols = {r[1] for r in c.execute('PRAGMA table_info(benefit_claims)').fetchall()}
        if 'submitted_at' not in bc_claim_cols:
            c.execute('ALTER TABLE benefit_claims ADD COLUMN submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP')
        if 'reviewed_at' not in bc_claim_cols:
            c.execute('ALTER TABLE benefit_claims ADD COLUMN reviewed_at TIMESTAMP')
        if 'reviewer_name' not in bc_claim_cols:
            c.execute('ALTER TABLE benefit_claims ADD COLUMN reviewer_name TEXT')
        if 'reviewer_note' not in bc_claim_cols:
            c.execute('ALTER TABLE benefit_claims ADD COLUMN reviewer_note TEXT')
        if 'expense_date' not in bc_claim_cols:
            c.execute('ALTER TABLE benefit_claims ADD COLUMN expense_date TEXT')

        # bonus_payments 컬럼 마이그레이션 (구 스키마 → 신 스키마)
        bp_cols = {r[1] for r in c.execute('PRAGMA table_info(bonus_payments)').fetchall()}
        if 'bonus_type' not in bp_cols:
            c.execute('ALTER TABLE bonus_payments ADD COLUMN bonus_type TEXT NOT NULL DEFAULT ""')
        if 'pay_date' not in bp_cols:
            c.execute('ALTER TABLE bonus_payments ADD COLUMN pay_date TEXT')
        if 'created_at' not in bp_cols:
            c.execute('ALTER TABLE bonus_payments ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP')

        # calibration_results 컬럼 마이그레이션 (v0.48)
        cal_cols = {r[1] for r in c.execute('PRAGMA table_info(calibration_results)').fetchall()}
        if 'potential_score' not in cal_cols:
            c.execute('ALTER TABLE calibration_results ADD COLUMN potential_score INTEGER DEFAULT NULL')
        if 'box_position' not in cal_cols:
            c.execute('ALTER TABLE calibration_results ADD COLUMN box_position INTEGER DEFAULT NULL')
        if 'downgrade_reason' not in cal_cols:
            c.execute('ALTER TABLE calibration_results ADD COLUMN downgrade_reason TEXT DEFAULT NULL')
        if 'retention_risk' not in cal_cols:
            c.execute("ALTER TABLE calibration_results ADD COLUMN retention_risk TEXT DEFAULT NULL")
        if 'loss_impact' not in cal_cols:
            c.execute("ALTER TABLE calibration_results ADD COLUMN loss_impact TEXT DEFAULT NULL")
        if 'achievable_level' not in cal_cols:
            c.execute("ALTER TABLE calibration_results ADD COLUMN achievable_level TEXT DEFAULT NULL")

        # 성과관리 재개편 마이그레이션 (v1.1.0 — Phase C-10, saas_plan.md §4)
        pc_cols = {r[1] for r in c.execute('PRAGMA table_info(performance_cycles)').fetchall()}
        if 'include_peer' not in pc_cols:
            c.execute('ALTER TABLE performance_cycles ADD COLUMN include_peer INTEGER NOT NULL DEFAULT 1')
        if 'stage' not in pc_cols:
            c.execute("ALTER TABLE performance_cycles ADD COLUMN stage TEXT NOT NULL DEFAULT 'goal'")
            # 기존 주기: 진행 중이던 것은 평가 단계로, 마감된 것은 종료로
            c.execute("UPDATE performance_cycles SET stage='review' WHERE status='active'")
            c.execute("UPDATE performance_cycles SET stage='closed' WHERE status='closed'")
        if 'appeal_until' not in pc_cols:
            c.execute('ALTER TABLE performance_cycles ADD COLUMN appeal_until DATE')

        # 사이클 운영 마이그레이션 (v1.3.3 — R1-D)
        if 'goal_deadline' not in pc_cols:
            c.execute('ALTER TABLE performance_cycles ADD COLUMN goal_deadline DATE')
        if 'review_deadline' not in pc_cols:
            c.execute('ALTER TABLE performance_cycles ADD COLUMN review_deadline DATE')
        if 'acknowledged_at' not in cal_cols:
            c.execute('ALTER TABLE calibration_results ADD COLUMN acknowledged_at TIMESTAMP')

        pg_cols = {r[1] for r in c.execute('PRAGMA table_info(performance_goals)').fetchall()}
        if 'approval_status' not in pg_cols:
            c.execute("ALTER TABLE performance_goals ADD COLUMN approval_status TEXT NOT NULL DEFAULT 'draft'")
            # 기존 목표는 이미 운영 중이던 데이터이므로 확정 상태로 취급
            c.execute("UPDATE performance_goals SET approval_status='confirmed'")
        if 'return_comment' not in pg_cols:
            c.execute('ALTER TABLE performance_goals ADD COLUMN return_comment TEXT')

        # 급여 2단계 확정 (P0-2 — 자동계산 draft → 담당자 확정 confirmed)
        ps_cols = {r[1] for r in c.execute('PRAGMA table_info(payslips)').fetchall()}
        if 'status' not in ps_cols:
            # 기존 명세는 이미 직원에게 공개된 것이므로 확정 취급
            c.execute("ALTER TABLE payslips ADD COLUMN status TEXT NOT NULL DEFAULT 'confirmed'")

        # 승인 체인 설정 (Phase C-13 — 결재선 화면 편집)
        c.execute('''
            CREATE TABLE IF NOT EXISTS approval_chains (
                workflow   TEXT PRIMARY KEY,
                chain      TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # 입사 예정자 (Phase C-11 — 외부 ATS 합격자 수신 + 직원 전환, saas_plan.md §5)
        c.execute('''
            CREATE TABLE IF NOT EXISTS incoming_hires (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                name              TEXT NOT NULL,
                email             TEXT,
                phone             TEXT,
                start_date        DATE,
                department_name   TEXT,
                position_name     TEXT,
                job_title         TEXT,
                salary            INTEGER,
                source            TEXT NOT NULL DEFAULT 'manual'
                                      CHECK(source IN ('manual','csv','webhook','internal')),
                memo              TEXT,
                status            TEXT NOT NULL DEFAULT 'waiting'
                                      CHECK(status IN ('waiting','converted','cancelled')),
                converted_user_id INTEGER REFERENCES users(id),
                created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                converted_at      TIMESTAMP
            )
        ''')

        # 등급 이의제기 (주기당 1회 — UNIQUE 제약)
        c.execute('''
            CREATE TABLE IF NOT EXISTS grade_appeals (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                cycle_id    INTEGER NOT NULL REFERENCES performance_cycles(id),
                user_id     INTEGER NOT NULL REFERENCES users(id),
                reason      TEXT NOT NULL,
                status      TEXT NOT NULL DEFAULT 'pending'
                                CHECK(status IN ('pending','accepted','rejected')),
                old_grade   TEXT,
                new_grade   TEXT,
                response    TEXT,
                resolved_by INTEGER REFERENCES users(id),
                resolved_at TIMESTAMP,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(cycle_id, user_id)
            )
        ''')

        # company_config 컬럼 마이그레이션 (v0.50 — 등급별 보상 배수)
        cc_cols = {r[1] for r in c.execute('PRAGMA table_info(company_config)').fetchall()}
        for col, default in [
            ('merit_s',  '0.08'),   # S등급 기본 인상률 8%
            ('merit_a',  '0.05'),   # A등급 5%
            ('merit_b',  '0.03'),   # B등급 3%
            ('merit_c',  '0.00'),   # C등급 0%
            ('merit_d', '-0.01'),   # D등급 -1% (동결/하향)
            ('bonus_s',  '2.0'),    # S등급 상여 배수 2배
            ('bonus_a',  '1.5'),
            ('bonus_b',  '1.0'),
            ('bonus_c',  '0.5'),
            ('bonus_d',  '0.0'),
            ('show_merit_to_employee', '1'),  # 직원에게 사전 공개 여부
            ('carry_over_max', '10'),           # 연차 이월 최대 일수
            ('welfare_point_annual', '500000'), # 연간 복지포인트 기본 지급액
        ]:
            if col not in cc_cols:
                c.execute(f'ALTER TABLE company_config ADD COLUMN {col} REAL DEFAULT {default}')

        # salary_grades 컬럼 마이그레이션 (v0.51 — Salary Band)
        sg_cols = {r[1] for r in c.execute('PRAGMA table_info(salary_grades)').fetchall()}
        for col, default in [
            ('min_salary', '0'),
            ('mid_salary', '0'),
            ('max_salary', '0'),
        ]:
            if col not in sg_cols:
                c.execute(f'ALTER TABLE salary_grades ADD COLUMN {col} INTEGER DEFAULT {default}')

        # grade_bonus_config 테이블 (v0.53) — 성과등급별 상여 배수
        c.execute('''
            CREATE TABLE IF NOT EXISTS grade_bonus_config (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                grade        TEXT NOT NULL UNIQUE CHECK(grade IN ('S','A','B','C','D')),
                bonus_months REAL NOT NULL DEFAULT 0,
                updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        if not c.execute('SELECT 1 FROM grade_bonus_config LIMIT 1').fetchone():
            c.executemany(
                'INSERT OR IGNORE INTO grade_bonus_config (grade, bonus_months) VALUES (?,?)',
                [('S', 3.0), ('A', 2.0), ('B', 1.0), ('C', 0.5), ('D', 0.0)]
            )

        # termination_requests 컬럼 마이그레이션 (v0.79 — 퇴직자 분석 필드)
        tr_cols = {r[1] for r in c.execute('PRAGMA table_info(termination_requests)').fetchall()}
        if 'is_regrettable' not in tr_cols:
            c.execute('ALTER TABLE termination_requests ADD COLUMN is_regrettable INTEGER DEFAULT NULL')
        if 'is_rehire_eligible' not in tr_cols:
            c.execute('ALTER TABLE termination_requests ADD COLUMN is_rehire_eligible INTEGER DEFAULT NULL')
        if 'exit_reason_category' not in tr_cols:
            c.execute('ALTER TABLE termination_requests ADD COLUMN exit_reason_category TEXT DEFAULT NULL')

        # merit_matrix 테이블 (v0.51) — 성과등급 × Compa 구간 → 인상률
        c.execute('''
            CREATE TABLE IF NOT EXISTS merit_matrix (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                performance_grade TEXT NOT NULL CHECK(performance_grade IN ('S','A','B','C','D')),
                compa_band      TEXT NOT NULL CHECK(compa_band IN ('below','at','above')),
                increase_pct    REAL NOT NULL DEFAULT 0,
                UNIQUE(performance_grade, compa_band)
            )
        ''')
        # 기본값 시드 (비어있을 때만)
        if not c.execute('SELECT 1 FROM merit_matrix LIMIT 1').fetchone():
            defaults = [
                ('S','below',10.0), ('S','at',8.0),  ('S','above',5.0),
                ('A','below',7.0),  ('A','at',5.0),  ('A','above',3.0),
                ('B','below',4.0),  ('B','at',3.0),  ('B','above',1.5),
                ('C','below',1.0),  ('C','at',0.0),  ('C','above',0.0),
                ('D','below',0.0),  ('D','at',-1.0), ('D','above',-1.0),
            ]
            c.executemany(
                'INSERT OR IGNORE INTO merit_matrix (performance_grade, compa_band, increase_pct) VALUES (?,?,?)',
                defaults
            )

        # ── ACR 테이블 (v0.52) ──────────────────────────────────────────────────
        c.execute('''
            CREATE TABLE IF NOT EXISTS compensation_review_cycles (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                review_year INTEGER NOT NULL,
                status      TEXT NOT NULL DEFAULT 'draft'
                            CHECK(status IN ('draft','open','closed')),
                effective_date TEXT,
                created_by  INTEGER REFERENCES users(id),
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS compensation_reviews (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                cycle_id        INTEGER NOT NULL REFERENCES compensation_review_cycles(id),
                employee_id     INTEGER NOT NULL REFERENCES users(id),
                manager_id      INTEGER REFERENCES users(id),
                current_salary  INTEGER DEFAULT 0,
                proposed_increase_pct  REAL DEFAULT 0,
                proposed_salary INTEGER DEFAULT 0,
                manager_note    TEXT,
                hr_override_pct REAL,
                hr_override_salary INTEGER,
                hr_note         TEXT,
                status          TEXT NOT NULL DEFAULT 'pending'
                                CHECK(status IN ('pending','submitted','approved','rejected')),
                approved_by     INTEGER REFERENCES users(id),
                approved_at     TIMESTAMP,
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(cycle_id, employee_id)
            )
        ''')

        # checkins 컬럼 마이그레이션
        checkin_cols = {r[1] for r in c.execute('PRAGMA table_info(checkins)').fetchall()}
        if 'regular_min' not in checkin_cols:
            c.execute('ALTER TABLE checkins ADD COLUMN regular_min  INTEGER DEFAULT 0')
        if 'overtime_min' not in checkin_cols:
            c.execute('ALTER TABLE checkins ADD COLUMN overtime_min INTEGER DEFAULT 0')
        if 'night_min' not in checkin_cols:
            c.execute('ALTER TABLE checkins ADD COLUMN night_min    INTEGER DEFAULT 0')
        if 'holiday_min' not in checkin_cols:
            c.execute('ALTER TABLE checkins ADD COLUMN holiday_min  INTEGER DEFAULT 0')
        if 'break_min' not in checkin_cols:
            c.execute('ALTER TABLE checkins ADD COLUMN break_min    INTEGER DEFAULT 0')
        if 'is_remote' not in checkin_cols:
            c.execute('ALTER TABLE checkins ADD COLUMN is_remote    INTEGER DEFAULT 0')

        # ── v0.55.1 부서 분류체계 ──────────────────────────────────────
        dept_cols = {r[1] for r in c.execute('PRAGMA table_info(departments)').fetchall()}
        if 'dept_type' not in dept_cols:
            c.execute("ALTER TABLE departments ADD COLUMN dept_type TEXT NOT NULL DEFAULT 'team'")
            # 기존 데이터: parent_id 깊이 기준으로 자동 분류
            # depth 0 → division(부문), 1 → hq(본부), 2 → dept(실), 3+ → team(팀)
            all_depts = {r[0]: r[1] for r in c.execute('SELECT id, parent_id FROM departments').fetchall()}
            def get_depth(did, memo={}):
                if did in memo: return memo[did]
                pid = all_depts.get(did)
                depth = 0 if pid is None else 1 + get_depth(pid, memo)
                memo[did] = depth
                return depth
            depth_to_type = {0: 'division', 1: 'hq', 2: 'dept', 3: 'team'}
            for did in all_depts:
                d = get_depth(did)
                dtype = depth_to_type.get(d, 'team')
                c.execute('UPDATE departments SET dept_type=? WHERE id=?', (dtype, did))

        # ── v0.55.0 신규 테이블 ──────────────────────────────────────
        c.execute('''CREATE TABLE IF NOT EXISTS employee_skills (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL REFERENCES users(id),
            skill_name TEXT NOT NULL,
            level      TEXT NOT NULL DEFAULT 'intermediate',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')

        c.execute('''CREATE TABLE IF NOT EXISTS employee_certs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL REFERENCES users(id),
            cert_name   TEXT NOT NULL,
            issued_by   TEXT,
            issued_date DATE,
            expiry_date DATE,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')

        c.execute('''CREATE TABLE IF NOT EXISTS department_headcount (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            department_id INTEGER NOT NULL REFERENCES departments(id),
            target_count  INTEGER NOT NULL DEFAULT 0,
            fiscal_year   INTEGER NOT NULL DEFAULT 2026,
            UNIQUE(department_id, fiscal_year)
        )''')

        # personnel_actions: 미래발령 적용 추적
        pa_cols2 = {r[1] for r in c.execute('PRAGMA table_info(personnel_actions)').fetchall()}
        if 'applied_at' not in pa_cols2:
            c.execute('ALTER TABLE personnel_actions ADD COLUMN applied_at TIMESTAMP')
            c.execute("UPDATE personnel_actions SET applied_at=CURRENT_TIMESTAMP WHERE status='approved'")

        # ── v0.56.0 Work Schedule 시스템 ────────────────────────────────
        c.execute('''CREATE TABLE IF NOT EXISTS work_schedules (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT NOT NULL,
            schedule_type   TEXT NOT NULL DEFAULT 'fixed',
            work_days       TEXT NOT NULL DEFAULT 'mon,tue,wed,thu,fri',
            work_start      TEXT DEFAULT '09:00',
            work_end        TEXT DEFAULT '18:00',
            core_start      TEXT DEFAULT '10:00',
            core_end        TEXT DEFAULT '16:00',
            daily_hours_min INTEGER DEFAULT 480,
            grace_minutes   INTEGER DEFAULT 10,
            is_default      INTEGER DEFAULT 0,
            note            TEXT,
            created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')

        c.execute('''CREATE TABLE IF NOT EXISTS user_schedule_assignments (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id        INTEGER NOT NULL REFERENCES users(id),
            schedule_id    INTEGER NOT NULL REFERENCES work_schedules(id),
            effective_from DATE NOT NULL,
            effective_to   DATE,
            note           TEXT,
            assigned_by    INTEGER REFERENCES users(id),
            created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')

        # checkins: 출결 상태 + 적용 스케줄 컬럼 추가
        checkin_cols2 = {r[1] for r in c.execute('PRAGMA table_info(checkins)').fetchall()}
        if 'attendance_status' not in checkin_cols2:
            c.execute("ALTER TABLE checkins ADD COLUMN attendance_status TEXT DEFAULT 'present'")
        if 'schedule_id' not in checkin_cols2:
            c.execute('ALTER TABLE checkins ADD COLUMN schedule_id INTEGER REFERENCES work_schedules(id)')

        # leave_requests: 반차 오전/오후 구분
        lr_cols = {r[1] for r in c.execute('PRAGMA table_info(leave_requests)').fetchall()}
        if 'half_day_slot' not in lr_cols:
            c.execute("ALTER TABLE leave_requests ADD COLUMN half_day_slot TEXT DEFAULT NULL")

        # ── v0.58.0 OT 승인 + 연차 이월 ────────────────────────────────────
        c.execute('''CREATE TABLE IF NOT EXISTS overtime_requests (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER NOT NULL REFERENCES users(id),
            date         TEXT NOT NULL,
            ot_start     TEXT NOT NULL,
            ot_end       TEXT NOT NULL,
            ot_minutes   INTEGER NOT NULL DEFAULT 0,
            reason       TEXT,
            request_type TEXT NOT NULL DEFAULT 'pre'
                         CHECK(request_type IN ('pre','post')),
            status       TEXT NOT NULL DEFAULT 'pending'
                         CHECK(status IN ('pending','reviewed','approved','rejected')),
            approver_id  INTEGER REFERENCES users(id),
            approved_at  TIMESTAMP,
            manager_id   INTEGER REFERENCES users(id),
            manager_approved_at TIMESTAMP,
            hr_id        INTEGER REFERENCES users(id),
            hr_approved_at      TIMESTAMP,
            reject_reason TEXT,
            created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')

        # overtime_requests: 결재선 확장 (매니저→HR 2단계) 컬럼 마이그레이션
        ot_cols = {r[1] for r in c.execute('PRAGMA table_info(overtime_requests)').fetchall()}
        ot_sql = c.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='overtime_requests'"
        ).fetchone()
        if ot_sql and ('manager_id' not in ot_cols or "'reviewed'" not in ot_sql[0]):
            c.executescript('''
                PRAGMA foreign_keys = OFF;
                DROP TABLE IF EXISTS _ot_old;
                ALTER TABLE overtime_requests RENAME TO _ot_old;
                CREATE TABLE overtime_requests (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id      INTEGER NOT NULL REFERENCES users(id),
                    date         TEXT NOT NULL,
                    ot_start     TEXT NOT NULL,
                    ot_end       TEXT NOT NULL,
                    ot_minutes   INTEGER NOT NULL DEFAULT 0,
                    reason       TEXT,
                    request_type TEXT NOT NULL DEFAULT 'pre'
                                 CHECK(request_type IN ('pre','post')),
                    status       TEXT NOT NULL DEFAULT 'pending'
                                 CHECK(status IN ('pending','reviewed','approved','rejected')),
                    approver_id  INTEGER REFERENCES users(id),
                    approved_at  TIMESTAMP,
                    manager_id   INTEGER REFERENCES users(id),
                    manager_approved_at TIMESTAMP,
                    hr_id        INTEGER REFERENCES users(id),
                    hr_approved_at      TIMESTAMP,
                    reject_reason TEXT,
                    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                INSERT INTO overtime_requests (id, user_id, date, ot_start, ot_end, ot_minutes, reason, request_type, status, approver_id, approved_at, reject_reason, created_at)
                SELECT id, user_id, date, ot_start, ot_end, ot_minutes, reason, request_type, status, approver_id, approved_at, reject_reason, created_at FROM _ot_old;
                DROP TABLE _ot_old;
                PRAGMA foreign_keys = ON;
            ''')

        c.execute('''CREATE TABLE IF NOT EXISTS leave_balances (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         INTEGER NOT NULL REFERENCES users(id),
            year            INTEGER NOT NULL,
            total_days      REAL NOT NULL DEFAULT 0,
            used_days       REAL NOT NULL DEFAULT 0,
            carry_over_days REAL NOT NULL DEFAULT 0,
            carry_over_max  REAL NOT NULL DEFAULT 10,
            updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, year)
        )''')

        # job_postings 컬럼 마이그레이션 (v0.59 — Requisition 연동)
        jp_cols = {r[1] for r in c.execute('PRAGMA table_info(job_postings)').fetchall()}
        for col, ddl in [
            ('employment_type', "TEXT NOT NULL DEFAULT 'full_time'"),
            ('salary_min',      'INTEGER DEFAULT 0'),
            ('salary_max',      'INTEGER DEFAULT 0'),
        ]:
            if col not in jp_cols:
                c.execute(f'ALTER TABLE job_postings ADD COLUMN {col} {ddl}')

        # ── v0.59.0 Requisition 승인 워크플로우 ─────────────────────────
        c.execute('''CREATE TABLE IF NOT EXISTS job_requisitions (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            title              TEXT NOT NULL,
            department_id      INTEGER REFERENCES departments(id),
            position_id        INTEGER REFERENCES positions(id),
            headcount          INTEGER NOT NULL DEFAULT 1,
            employment_type    TEXT NOT NULL DEFAULT 'full_time',
            reason             TEXT,
            required_skills    TEXT,
            salary_min         INTEGER DEFAULT 0,
            salary_max         INTEGER DEFAULT 0,
            target_start_date  TEXT,
            status             TEXT NOT NULL DEFAULT 'draft'
                               CHECK(status IN ('draft','pending_dept','pending_hr','approved','rejected','posted')),
            requester_id       INTEGER REFERENCES users(id),
            dept_approver_id   INTEGER REFERENCES users(id),
            dept_approved_at   TIMESTAMP,
            dept_reject_reason TEXT,
            hr_approver_id     INTEGER REFERENCES users(id),
            hr_approved_at     TIMESTAMP,
            hr_reject_reason   TEXT,
            posting_id         INTEGER REFERENCES job_postings(id),
            created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')

        # job_requisitions 컬럼 마이그레이션 (v0.60 — 레벨체계 연동)
        req_cols = {r[1] for r in c.execute('PRAGMA table_info(job_requisitions)').fetchall()}
        if 'job_family_id' not in req_cols:
            c.execute('ALTER TABLE job_requisitions ADD COLUMN job_family_id INTEGER REFERENCES job_families(id)')
        if 'track' not in req_cols:
            c.execute("ALTER TABLE job_requisitions ADD COLUMN track TEXT DEFAULT 'IC' CHECK(track IN ('IC','M'))")
        if 'salary_mid' not in req_cols:
            c.execute('ALTER TABLE job_requisitions ADD COLUMN salary_mid INTEGER DEFAULT 0')

        # ── v0.61.0 면접 관리 + 채용 컴플라이언스 로그 ─────────────────────
        c.execute('''CREATE TABLE IF NOT EXISTS interview_rounds (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            applicant_id     INTEGER NOT NULL REFERENCES applicants(id),
            round_no         INTEGER NOT NULL DEFAULT 1,
            round_type       TEXT NOT NULL DEFAULT 'technical'
                             CHECK(round_type IN ('hr','technical','culture','executive','other')),
            status           TEXT NOT NULL DEFAULT 'scheduled'
                             CHECK(status IN ('scheduled','completed','cancelled','no_show')),
            scheduled_at     TIMESTAMP,
            actual_start_at  TIMESTAMP,
            actual_end_at    TIMESTAMP,
            planned_min      INTEGER NOT NULL DEFAULT 60,
            actual_min       INTEGER,
            location_type    TEXT NOT NULL DEFAULT 'video'
                             CHECK(location_type IN ('video','in_person','phone')),
            meet_link        TEXT,
            created_by       INTEGER NOT NULL REFERENCES users(id),
            created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')

        c.execute('''CREATE TABLE IF NOT EXISTS interview_interviewers (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            round_id        INTEGER NOT NULL REFERENCES interview_rounds(id),
            interviewer_id  INTEGER NOT NULL REFERENCES users(id),
            is_required     INTEGER NOT NULL DEFAULT 1,
            assigned_by     INTEGER NOT NULL REFERENCES users(id),
            assigned_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(round_id, interviewer_id)
        )''')

        c.execute('''CREATE TABLE IF NOT EXISTS interview_feedback (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            round_id            INTEGER NOT NULL REFERENCES interview_rounds(id),
            interviewer_id      INTEGER NOT NULL REFERENCES users(id),
            recommendation      TEXT NOT NULL CHECK(recommendation IN ('pass','hold','fail')),
            score_technical     INTEGER CHECK(score_technical BETWEEN 1 AND 5),
            score_communication INTEGER CHECK(score_communication BETWEEN 1 AND 5),
            score_culture_fit   INTEGER CHECK(score_culture_fit BETWEEN 1 AND 5),
            score_growth        INTEGER CHECK(score_growth BETWEEN 1 AND 5),
            score_overall       INTEGER CHECK(score_overall BETWEEN 1 AND 5),
            strengths           TEXT,
            concerns            TEXT,
            interview_notes     TEXT,
            is_edited           INTEGER NOT NULL DEFAULT 0,
            edit_reason         TEXT,
            submitted_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(round_id, interviewer_id)
        )''')

        c.execute('''CREATE TABLE IF NOT EXISTS recruit_activity_logs (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type   TEXT NOT NULL,
            actor_id     INTEGER REFERENCES users(id),
            applicant_id INTEGER REFERENCES applicants(id),
            round_id     INTEGER REFERENCES interview_rounds(id),
            meta         TEXT DEFAULT '{}',
            created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')

        # ── v0.63.0 불합격 처리 + 면접 노트 ───────────────────────────────────
        # applicants: disqualified_from 컬럼 추가 (어느 단계에서 불합격됐는지 기록)
        ap_cols = {r[1] for r in c.execute('PRAGMA table_info(applicants)').fetchall()}
        if 'disqualified_from' not in ap_cols:
            c.execute('ALTER TABLE applicants ADD COLUMN disqualified_from TEXT')
        if 'disqualify_reason' not in ap_cols:
            c.execute('ALTER TABLE applicants ADD COLUMN disqualify_reason TEXT')

        # 라운드별 면접 노트 (면접 중 빠른 메모, 피드백 폼과 별개)
        c.execute('''CREATE TABLE IF NOT EXISTS interview_round_notes (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            round_id     INTEGER NOT NULL REFERENCES interview_rounds(id) ON DELETE CASCADE,
            author_id    INTEGER NOT NULL REFERENCES users(id),
            content      TEXT NOT NULL,
            created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')

        # ── v0.62.1 지원자 서류 첨부 ────────────────────────────────────────
        c.execute('''CREATE TABLE IF NOT EXISTS applicant_documents (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            applicant_id  INTEGER NOT NULL REFERENCES applicants(id) ON DELETE CASCADE,
            doc_type      TEXT NOT NULL DEFAULT 'resume',
            original_name TEXT NOT NULL,
            stored_name   TEXT NOT NULL,
            file_size     INTEGER DEFAULT 0,
            uploaded_by   INTEGER REFERENCES users(id),
            uploaded_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')

        # ── v0.95.0 직원 문서함 ──────────────────────────────────────────
        c.execute('''CREATE TABLE IF NOT EXISTS employee_documents (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            doc_type      TEXT NOT NULL DEFAULT 'other',
            original_name TEXT NOT NULL,
            stored_name   TEXT NOT NULL,
            file_size     INTEGER DEFAULT 0,
            uploaded_by   INTEGER REFERENCES users(id),
            uploaded_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')

        # ── v0.98.0 감사 로그 (Phase A-3 보안 기준선) ─────────────────────
        c.execute('''CREATE TABLE IF NOT EXISTS audit_logs (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            actor_id       INTEGER,
            actor_name     TEXT,
            actor_role     TEXT,
            action         TEXT NOT NULL CHECK(action IN ('view','create','update','delete','download','login','login_failed')),
            category       TEXT NOT NULL CHECK(category IN ('salary','performance','personal_info','document','export','auth')),
            target_user_id INTEGER,
            detail         TEXT,
            ip             TEXT,
            created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_logs(created_at)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_audit_target ON audit_logs(target_user_id)')

        # ── v0.99.4 연차촉진 통보 이력 (Phase B-8, 근로기준법 §61) ────────
        c.execute('''CREATE TABLE IF NOT EXISTS leave_promotion_logs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            year        INTEGER NOT NULL,
            round_no    INTEGER NOT NULL CHECK(round_no IN (1, 2)),
            remain_days REAL NOT NULL,
            sent_by     INTEGER REFERENCES users(id),
            sent_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')

        # ── v0.64.0 오퍼 관리 + 이메일 이력 ─────────────────────────────────
        c.execute('''CREATE TABLE IF NOT EXISTS offers (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            applicant_id    INTEGER NOT NULL REFERENCES applicants(id) ON DELETE CASCADE,
            posting_id      INTEGER NOT NULL REFERENCES job_postings(id),
            status          TEXT NOT NULL DEFAULT 'draft'
                            CHECK(status IN ('draft','sent','accepted','negotiating','rejected','expired')),
            salary          INTEGER,
            start_date      TEXT,
            expiry_date     TEXT,
            body            TEXT,
            sent_at         TIMESTAMP,
            responded_at    TIMESTAMP,
            hired_employee_id INTEGER REFERENCES users(id),
            created_by      INTEGER REFERENCES users(id),
            created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')

        c.execute('''CREATE TABLE IF NOT EXISTS recruit_emails (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            applicant_id INTEGER NOT NULL REFERENCES applicants(id) ON DELETE CASCADE,
            email_type   TEXT NOT NULL
                         CHECK(email_type IN ('interview_invite','pass','fail','offer','custom')),
            recipient    TEXT NOT NULL,
            subject      TEXT NOT NULL,
            body         TEXT NOT NULL,
            sent_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            sent_by      INTEGER REFERENCES users(id)
        )''')

        # offers: TC 컴포넌트 컬럼 추가 (v0.65)
        of_cols = {r[1] for r in c.execute('PRAGMA table_info(offers)').fetchall()}
        for col, ddl in [
            ('bonus_pct',     'INTEGER DEFAULT 20'),
            ('rsu_total',     'INTEGER DEFAULT 0'),
            ('rsu_vest_years','INTEGER DEFAULT 4'),
            ('signing_bonus', 'INTEGER DEFAULT 0'),
            ('job_level',     'TEXT'),
            ('track',         'TEXT'),
            ('location',      'TEXT DEFAULT "서울 강남"'),
            ('wfh_days',      'INTEGER DEFAULT 2'),
            ('benefits_json', 'TEXT'),
            ('company_signer','TEXT'),
            ('company_signer_title', 'TEXT'),
            # v1.2.1 — 국내형 스톡옵션 모드 (Phase C-12, 벤처기업법 §16-3)
            ('equity_type',   "TEXT DEFAULT 'rsu'"),   # rsu | stock_option
            ('option_qty',    'INTEGER DEFAULT 0'),    # 스톡옵션 수량(주)
            ('strike_price',  'INTEGER DEFAULT 0'),    # 행사가(원/주)
        ]:
            if col not in of_cols:
                c.execute(f'ALTER TABLE offers ADD COLUMN {col} {ddl}')

        # ── v0.65.0 채용 요청서↔공고 연동 ────────────────────────────────
        jr_cols = {r[1] for r in c.execute('PRAGMA table_info(job_requisitions)').fetchall()}
        if 'job_level' not in jr_cols:
            c.execute('ALTER TABLE job_requisitions ADD COLUMN job_level TEXT')
        jp_cols_v65 = {r[1] for r in c.execute('PRAGMA table_info(job_postings)').fetchall()}
        if 'requisition_id' not in jp_cols_v65:
            c.execute('ALTER TABLE job_postings ADD COLUMN requisition_id INTEGER REFERENCES job_requisitions(id)')

        # applicants: hired_from_offer_id 컬럼 추가
        ap_cols2 = {r[1] for r in c.execute('PRAGMA table_info(applicants)').fetchall()}
        if 'hired_from_offer_id' not in ap_cols2:
            c.execute('ALTER TABLE applicants ADD COLUMN hired_from_offer_id INTEGER REFERENCES offers(id)')
        if 'hired_employee_id' not in ap_cols2:
            c.execute('ALTER TABLE applicants ADD COLUMN hired_employee_id INTEGER REFERENCES users(id)')

        # ── v0.62.0 파이프라인 고도화 ────────────────────────────────────────
        # job_postings 에 recruiter_id / hiring_manager_id / coordinator_id 추가
        jp_cols2 = {r[1] for r in c.execute('PRAGMA table_info(job_postings)').fetchall()}
        for col, ddl in [
            ('recruiter_id',       'INTEGER REFERENCES users(id)'),
            ('hiring_manager_id',  'INTEGER REFERENCES users(id)'),
            ('coordinator_id',     'INTEGER REFERENCES users(id)'),
        ]:
            if col not in jp_cols2:
                c.execute(f'ALTER TABLE job_postings ADD COLUMN {col} {ddl}')

        # applicants.stage → 새 9단계 체계 마이그레이션
        # SQLite는 CHECK 제약 수정 불가 → 테이블 재생성(rename swap) 방식
        NEW_STAGES = ('review','screening','inter1','kickoff','inter2','debrief','offer','accepted','rejected')
        STAGE_MAP_MIGRATE = {
            'applied':    'review',
            'screening':  'screening',
            'interview1': 'inter1',
            'interview2': 'inter2',
            'final':      'debrief',
            'offered':    'offer',
            'hired':      'accepted',
            'rejected':   'rejected',
        }
        existing_stages = {r[0] for r in c.execute("SELECT DISTINCT stage FROM applicants").fetchall()}
        needs_migration  = bool(existing_stages - set(NEW_STAGES))
        if needs_migration:
            c.execute('''CREATE TABLE IF NOT EXISTS applicants_new (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                posting_id  INTEGER NOT NULL REFERENCES job_postings(id),
                name        TEXT NOT NULL,
                email       TEXT NOT NULL,
                phone       TEXT,
                source      TEXT DEFAULT 'direct',
                resume_note TEXT,
                stage       TEXT NOT NULL DEFAULT 'review',
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )''')
            rows = c.execute('SELECT * FROM applicants').fetchall()
            for row in rows:
                row = dict(row)
                old_stage = row['stage']
                row['stage'] = STAGE_MAP_MIGRATE.get(old_stage, 'review')
                c.execute(
                    'INSERT INTO applicants_new (id,posting_id,name,email,phone,source,resume_note,stage,created_at) '
                    'VALUES (:id,:posting_id,:name,:email,:phone,:source,:resume_note,:stage,:created_at)', row
                )
            conn.commit()
            conn.execute('PRAGMA foreign_keys = OFF')
            conn.execute('DROP TABLE applicants')
            conn.execute('ALTER TABLE applicants_new RENAME TO applicants')
            conn.commit()
            conn.execute('PRAGMA foreign_keys = ON')

        # job_family_groups / job_families 컬럼 마이그레이션
        _jfg_cols = {r[1] for r in c.execute("PRAGMA table_info(job_family_groups)")}
        if not _jfg_cols:
            pass  # 테이블은 CREATE TABLE IF NOT EXISTS로 이미 생성됨
        _jf_cols = {r[1] for r in c.execute("PRAGMA table_info(job_families)")}
        if 'group_id' not in _jf_cols:
            c.execute("ALTER TABLE job_families ADD COLUMN group_id INTEGER REFERENCES job_family_groups(id)")
        if 'sort_order' not in _jf_cols:
            c.execute("ALTER TABLE job_families ADD COLUMN sort_order INTEGER DEFAULT 0")

        # Workday Job Architecture 5개 그룹 시드 (없으면 삽입)
        JFG_SEED = [
            ('TECH',    'Technology',       1),
            ('PRODUCT', 'Product & Design', 2),
            ('GTM',     'Go-to-Market',     3),
            ('CORP',    'Corporate',        4),
            ('PEOPLE',  'People',           5),
        ]
        for gcode, gname, gsort in JFG_SEED:
            exists = c.execute('SELECT id FROM job_family_groups WHERE code=?', (gcode,)).fetchone()
            if not exists:
                c.execute('INSERT INTO job_family_groups (code, name, sort_order) VALUES (?,?,?)', (gcode, gname, gsort))

        # 기존 job_families group_id/sort_order 보정 (코드 기반)
        JF_GROUP_MAP = {
            'SWE': ('TECH', 1), 'FE': ('TECH', 2), 'DE': ('TECH', 3),
            'ML': ('TECH', 4),  'INFRA': ('TECH', 5), 'SEC': ('TECH', 6), 'QA': ('TECH', 7),
            'PM': ('PRODUCT', 1), 'UXR': ('PRODUCT', 2), 'DESIGN': ('PRODUCT', 3), 'TW': ('PRODUCT', 4),
            'SALES': ('GTM', 1), 'BD': ('GTM', 2), 'CS': ('GTM', 3), 'MKT': ('GTM', 4), 'GROWTH': ('GTM', 5),
            'FIN': ('CORP', 1), 'LEGAL': ('CORP', 2), 'STRAT': ('CORP', 3), 'BIZ_OPS': ('CORP', 4),
            'HR': ('PEOPLE', 1), 'TA': ('PEOPLE', 2), 'COMP': ('PEOPLE', 3),
            # 구 코드 → 그룹 매핑 (레거시 호환)
            'DATA': ('TECH', 3), 'OPS': ('GTM', 3),
        }
        for jf_code, (gcode, sort) in JF_GROUP_MAP.items():
            grp = c.execute('SELECT id FROM job_family_groups WHERE code=?', (gcode,)).fetchone()
            if grp:
                c.execute('UPDATE job_families SET group_id=?, sort_order=? WHERE code=?', (grp[0], sort, jf_code))

        # salary_grades 밴드 데이터 전면 업데이트 (v0.60 → v0.80 — Workday Job Architecture 기반)
        # 23개 직군 코드 기반 배수 (IC_BANDS 기준값에 곱해서 계산)
        JF_MULT = {
            'SWE': 1.00, 'FE': 0.97,  'DE': 1.05,  'ML': 1.10,  'INFRA': 1.00,
            'SEC': 1.05, 'QA': 0.92,
            'PM': 1.00,  'UXR': 0.92, 'DESIGN': 0.90, 'TW': 0.82,
            'SALES': 0.85, 'BD': 0.88, 'CS': 0.83, 'MKT': 0.85, 'GROWTH': 0.88,
            'FIN': 0.85, 'LEGAL': 0.90, 'STRAT': 0.95, 'BIZ_OPS': 0.80,
            'HR': 0.80, 'TA': 0.83, 'COMP': 0.88,
            # 레거시 코드 호환
            'DATA': 1.05, 'OPS': 0.75,
        }
        # IC 기준 밴드 (만원): {level: (min, mid, max)}
        IC_BANDS = {
            1: (2400, 2700, 3200),
            2: (3200, 3700, 4400),
            3: (4200, 5000, 6000),
            4: (5500, 7000, 8500),
            5: (7500, 9300, 11500),
            6: (10000, 12500, 16000),
            7: (14000, 18000, 22000),
            8: (18000, 24000, 30000),
            9: (25000, 35000, 50000),
        }
        # 기존 데이터 있는 경우도 업데이트 (UPSERT 효과)
        for jf_code, mult in JF_MULT.items():
            jf_row = c.execute('SELECT id FROM job_families WHERE code=?', (jf_code,)).fetchone()
            if not jf_row:
                continue
            jf_id = jf_row[0]
            for level, (mn, md, mx) in IC_BANDS.items():
                pos = c.execute('SELECT id FROM positions WHERE level=?', (level,)).fetchone()
                if not pos:
                    continue
                pos_id = pos[0]
                m_mn  = int(mn * 10000 * mult)
                m_mid = int(md * 10000 * mult)
                m_max = int(mx * 10000 * mult)
                m_ann = m_mid
                existing = c.execute(
                    'SELECT id FROM salary_grades WHERE job_family_id=? AND position_id=?',
                    (jf_id, pos_id)
                ).fetchone()
                if existing:
                    c.execute(
                        'UPDATE salary_grades SET min_salary=?, mid_salary=?, max_salary=?, annual_salary=? WHERE id=?',
                        (m_mn, m_mid, m_max, m_ann, existing[0])
                    )
                else:
                    c.execute(
                        'INSERT INTO salary_grades (job_family_id, position_id, annual_salary, min_salary, mid_salary, max_salary) VALUES (?,?,?,?,?,?)',
                        (jf_id, pos_id, m_ann, m_mn, m_mid, m_max)
                    )

        # company_config 기본 row (없으면 삽입 — setup_completed=0 유지해서 위자드 표시)
        if c.execute('SELECT COUNT(*) FROM company_config').fetchone()[0] == 0:
            c.execute('INSERT INTO company_config (id) VALUES (1)')

        # 2026년 한국 공휴일 시드 (관공서 공휴일에 관한 규정)
        holidays_2026 = [
            ('2026-01-01', '신정'),
            ('2026-01-28', '설날 연휴'),
            ('2026-01-29', '설날'),
            ('2026-01-30', '설날 연휴'),
            ('2026-03-01', '삼일절'),
            ('2026-05-05', '어린이날'),
            ('2026-05-15', '부처님오신날'),
            ('2026-06-06', '현충일'),
            ('2026-08-15', '광복절'),
            ('2026-09-24', '추석 연휴'),
            ('2026-09-25', '추석'),
            ('2026-09-26', '추석 연휴'),
            ('2026-10-03', '개천절'),
            ('2026-10-09', '한글날'),
            ('2026-12-25', '크리스마스'),
        ]
        existing_hd = {r[0] for r in c.execute('SELECT date FROM public_holidays WHERE year=2026').fetchall()}
        for hdate, hname in holidays_2026:
            if hdate not in existing_hd:
                c.execute('INSERT INTO public_holidays (date, name, year) VALUES (?,?,2026)', (hdate, hname))

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

        # 2026년 한국 공휴일 데이터 보장
        if c.execute('SELECT COUNT(*) FROM holidays').fetchone()[0] == 0:
            holidays_2026 = [
                ('2026-01-01', '신정'),
                ('2026-02-16', '설날'), ('2026-02-17', '설날'), ('2026-02-18', '설날'),
                ('2026-03-01', '삼일절'),
                ('2026-05-05', '어린이날'),
                ('2026-05-24', '부처님오신날'),
                ('2026-06-06', '현충일'),
                ('2026-08-15', '광복절'),
                ('2026-09-24', '추석'), ('2026-09-25', '추석'), ('2026-09-26', '추석'),
                ('2026-10-03', '개천절'),
                ('2026-10-09', '한글날'),
                ('2026-12-25', '크리스마스')
            ]
            c.executemany('INSERT OR IGNORE INTO holidays (date, name) VALUES (?, ?)', holidays_2026)

        # ── v0.71 — 부양가족 + 생애사건 ──────────────────────────────────
        # users 테이블 marital_status 컬럼 추가
        user_cols = {r[1] for r in c.execute('PRAGMA table_info(users)').fetchall()}
        if 'marital_status' not in user_cols:
            c.execute("ALTER TABLE users ADD COLUMN marital_status TEXT NOT NULL DEFAULT 'single' "
                      "CHECK(marital_status IN ('single','married','divorced','widowed'))")
        if 'gender' not in user_cols:
            c.execute("ALTER TABLE users ADD COLUMN gender TEXT CHECK(gender IN ('M','F','other'))")

        # payslips 컬럼 추가 (세금계산 결과 저장용)
        payslip_cols2 = {r[1] for r in c.execute('PRAGMA table_info(payslips)').fetchall()}
        for col, dflt in [
            ('income_deduction',         '0'),
            ('earned_income',            '0'),
            ('total_personal_deduction', '0'),
            ('num_dependents',           '0'),
            ('child_tax_credit_amount',  '0'),
        ]:
            if col not in payslip_cols2:
                c.execute(f'ALTER TABLE payslips ADD COLUMN {col} INTEGER NOT NULL DEFAULT {dflt}')

        # employee_dependents 테이블
        c.execute('''
            CREATE TABLE IF NOT EXISTS employee_dependents (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                name           TEXT NOT NULL,
                relation       TEXT NOT NULL
                               CHECK(relation IN ('spouse','child','parent','grandparent','sibling')),
                birth_date     DATE,
                gender         TEXT CHECK(gender IN ('M','F','other')),
                is_disabled    INTEGER NOT NULL DEFAULT 0,
                annual_income  INTEGER NOT NULL DEFAULT 0,
                is_cohabiting  INTEGER NOT NULL DEFAULT 1,
                is_adopted     INTEGER NOT NULL DEFAULT 0,
                birth_order    INTEGER,
                note           TEXT,
                created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # life_events 테이블
        c.execute('''
            CREATE TABLE IF NOT EXISTS life_events (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                event_type     TEXT NOT NULL
                               CHECK(event_type IN (
                                   'marriage','divorce','birth','adoption',
                                   'death_of_dependent','disability_onset',
                                   'child_school_entry','child_age_out'
                               )),
                event_date     DATE NOT NULL,
                description    TEXT,
                created_by     INTEGER REFERENCES users(id),
                created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # ── v0.81 Dashboard Widget Preferences ─────────────────────────
        c.execute('''CREATE TABLE IF NOT EXISTS dashboard_widgets (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            widget_key TEXT NOT NULL,
            enabled    INTEGER NOT NULL DEFAULT 1,
            UNIQUE(user_id, widget_key)
        )''')

        # ── 외부 서비스 연동 ─────────────────────────────────
        c.execute('''CREATE TABLE IF NOT EXISTS integration_configs (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            service    TEXT NOT NULL UNIQUE,
            enabled    INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT
        )''')
        for svc in ('slack', 'jira', 'confluence'):
            c.execute(
                "INSERT OR IGNORE INTO integration_configs (service, enabled) VALUES (?, 0)",
                (svc,)
            )

        c.execute('''CREATE TABLE IF NOT EXISTS integration_logs (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            event         TEXT NOT NULL,
            service       TEXT NOT NULL,
            status        TEXT NOT NULL,
            detail        TEXT,
            employee_name TEXT,
            raw_response  TEXT,
            created_at    TEXT NOT NULL
        )''')

        # ── 온보딩 체크리스트 ─────────────────────────────────
        c.execute('''CREATE TABLE IF NOT EXISTS onboarding_progress (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            task_key    TEXT NOT NULL,
            task_label  TEXT NOT NULL,
            category    TEXT NOT NULL DEFAULT 'general',
            sort_order  INTEGER NOT NULL DEFAULT 0,
            done        INTEGER NOT NULL DEFAULT 0,
            done_at     TEXT,
            UNIQUE(user_id, task_key)
        )''')

        conn.commit()
    finally:
        conn.close()
