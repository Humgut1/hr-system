"""데이터 점검(B12).

  - 규칙마다 '이런 사람이 몇 명' 목록을 만들고, 줄마다 [고치기](고칠 화면으로 가는 링크 또는 바로 고치기)를 붙인다.
  - 새 테이블은 예외 기록 하나(data_check_skips). 예외로 둔 줄은 사유와 함께 남고 목록에서만 빠진다.
  - 바로 고치기는 두 가지만: 매니저 채우기(조직장 추천, 변경 이력 source 'check') ·
    퇴사자 로그인 명부 정리(master tenant_users). 나머지는 사람이 판단할 일이라 화면 링크로 보낸다.
  - 날짜는 파이썬 오늘(KST)을 넘긴다 — SQLite date('now') 는 UTC 라 자정 무렵 하루가 어긋난다.
"""
import csv
import io
from datetime import date, timedelta

import master_db
import people_history

_DDL = (
    '''CREATE TABLE IF NOT EXISTS data_check_skips (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        rule    TEXT NOT NULL,
        ref_id  INTEGER NOT NULL,
        reason  TEXT NOT NULL,
        by_id   INTEGER,
        by_name TEXT,
        at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(rule, ref_id)
    )''',
)

AREAS = (('basic', '기본 정보'), ('manager', '보고 라인'), ('dates', '날짜·상태'), ('leaver', '퇴사자 정리'))
LEVEL_LABEL = {'late': '위험', 'wait': '주의', 'ref': '참고'}

_done = set()


def _dbkey(c):
    try:
        return c.execute('PRAGMA database_list').fetchone()[2]
    except Exception:
        return None


def ensure_schema(c):
    k = _dbkey(c)
    if k and k in _done:
        return
    for q in _DDL:
        c.execute(q)
    c.commit()
    if k:
        _done.add(k)


def _rows(c, q, args=()):
    cur = c.execute(q, args)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _cols(c, table):
    return {r[1] for r in c.execute('PRAGMA table_info(%s)' % table).fetchall()}


def _has(c, table):
    return bool(c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


# 재직자 기본 줄 — 손님 계정(guest)은 사람이 아니라 빼고 본다.
_ACTIVE = ("SELECT u.id uid, u.emp_no, u.name, d.name dept FROM users u "
           "LEFT JOIN departments d ON d.id=u.department_id "
           "WHERE u.status='active' AND u.role!='guest' ")


def _people(c, where, args=()):
    return _rows(c, _ACTIVE + where + ' ORDER BY d.name, u.name', args)


def _emp_link(uid, edit=True):
    return ('/employees/%d/edit' % uid) if edit else ('/employees/%d' % uid)


def suggest_manager(c, uid):
    """그 사람 부서의 조직장 → 상위 조직장 순. 본인·재직 아닌 사람은 건너뛴다. (id, 이름) 또는 None."""
    r = c.execute('SELECT department_id FROM users WHERE id=?', (uid,)).fetchone()
    did, seen = (r[0] if r else None), set()
    while did and did not in seen:
        seen.add(did)
        d = c.execute('SELECT d.parent_id, d.leader_id, u.name, u.status FROM departments d '
                      'LEFT JOIN users u ON u.id=d.leader_id WHERE d.id=?', (did,)).fetchone()
        if not d:
            return None
        if d[1] and d[1] != uid and d[3] == 'active':
            return d[1], d[2]
        did = d[0]
    return None


def _rules(c, today, tid):
    """(key, area, level, 제목, 고치는 법, 줄 목록) 를 차례로 낸다. 줄 = dict(ref_id, uid, emp_no, name, dept, detail, fix_url, fix_label)."""
    t = today.isoformat()
    ucols = _cols(c, 'users')
    dcols = _cols(c, 'departments')

    def ppl(rows, detail=None, edit=True, label='직원 수정'):
        for r in rows:
            r['ref_id'] = r['uid']
            r['detail'] = detail(r) if callable(detail) else (detail or '')
            r['fix_url'], r['fix_label'] = _emp_link(r['uid'], edit), label
        return rows

    # ── 기본 정보 ──
    yield ('no_emp_no', 'basic', 'late', '사번 없음',
           '사번은 엑셀·API·다른 제품이 사람을 찾는 열쇠입니다. 직원 수정에서 넣으세요.',
           ppl(_people(c, "AND TRIM(COALESCE(u.emp_no,''))=''")))

    dup = _rows(c, "SELECT u.id uid, u.emp_no, u.name, d.name dept, u.status FROM users u "
                   "LEFT JOIN departments d ON d.id=u.department_id "
                   "WHERE u.role!='guest' AND TRIM(COALESCE(u.emp_no,''))!='' AND u.emp_no IN ("
                   "  SELECT emp_no FROM users WHERE role!='guest' AND TRIM(COALESCE(emp_no,''))!='' "
                   "  GROUP BY emp_no HAVING COUNT(*)>1) ORDER BY u.emp_no, u.id")
    yield ('dup_emp_no', 'basic', 'late', '같은 사번을 두 명 이상이 씀',
           '한 사람의 기록이 다른 사람에게 붙습니다. 같은 사람이면 재입사·수습 화면의 [합치기], 다른 사람이면 사번을 고치세요.',
           ppl(dup, lambda r: '퇴사자' if r['status'] != 'active' else ''))

    yield ('no_dept', 'basic', 'late', '부서 없음',
           '헤드카운트·인사 분석·결재선에서 빠집니다.',
           ppl(_people(c, 'AND u.department_id IS NULL')))

    if 'status' in dcols:
        yield ('dept_closed', 'basic', 'late', '닫힌 조직에 남은 재직자',
               '조직을 닫은 뒤 옮기지 못한 사람입니다. 새 부서로 옮기세요.',
               ppl(_rows(c, "SELECT u.id uid, u.emp_no, u.name, d.name dept FROM users u "
                            "JOIN departments d ON d.id=u.department_id "
                            "WHERE u.status='active' AND u.role!='guest' AND COALESCE(d.status,'active')!='active' "
                            "ORDER BY d.name, u.name"), '조직 상태: 닫힘'))

    yield ('no_position', 'basic', 'wait', '직급 없음',
           '급여 밴드·직급별 분석·평가 대상 판단에 씁니다.',
           ppl(_people(c, 'AND u.position_id IS NULL')))

    yield ('no_hire_date', 'basic', 'late', '입사일 없음',
           '연차·근속·수습·퇴직금 계산이 모두 입사일에서 시작합니다.',
           ppl(_people(c, "AND TRIM(COALESCE(u.hire_date,''))=''")))

    if 'gender' in ucols:
        yield ('no_gender', 'basic', 'ref', '성별 없음',
               '직급별 여성 비율 같은 다양성 지표에서 빠집니다. 본인이 밝히지 않았다면 예외로 두세요.',
               ppl(_people(c, "AND TRIM(COALESCE(u.gender,''))=''")))

    if _has(c, 'employee_salary'):
        yield ('no_salary', 'basic', 'wait', '급여 정보 없음',
               '급여 계산·보상 분석에서 빠집니다. 직원 상세의 급여에서 넣으세요.',
               ppl(_people(c, 'AND NOT EXISTS (SELECT 1 FROM employee_salary s WHERE s.user_id=u.id)'),
                   edit=False, label='직원 상세'))

    # ── 보고 라인 ──
    roots = {r[0] for r in c.execute('SELECT leader_id FROM departments WHERE parent_id IS NULL AND leader_id IS NOT NULL')}
    nom = [r for r in _people(c, 'AND u.manager_id IS NULL') if r['uid'] not in roots]
    for r in nom:
        s = suggest_manager(c, r['uid'])
        r['ref_id'] = r['uid']
        r['suggest_id'], r['suggest_name'] = (s if s else (None, None))
        r['detail'] = ('추천: %s (조직장)' % s[1]) if s else '추천할 조직장 없음'
        r['fix_url'], r['fix_label'] = _emp_link(r['uid']), '직원 수정'
    yield ('no_manager', 'manager', 'wait', '매니저 없음',
           '휴가·평가·결재가 누구에게 갈지 정해지지 않습니다. 맨 위 조직의 조직장은 빼고 셉니다.', nom)

    bad = _rows(c, "SELECT u.id uid, u.emp_no, u.name, d.name dept, m.name mname, m.status mstatus FROM users u "
                   "LEFT JOIN departments d ON d.id=u.department_id "
                   "LEFT JOIN users m ON m.id=u.manager_id "
                   "WHERE u.status='active' AND u.role!='guest' AND u.manager_id IS NOT NULL "
                   "AND (u.manager_id=u.id OR m.id IS NULL OR m.status!='active') ORDER BY d.name, u.name")
    for r in bad:
        s = suggest_manager(c, r['uid'])
        r['ref_id'] = r['uid']
        r['suggest_id'], r['suggest_name'] = (s if s else (None, None))
        why = '본인이 매니저' if r['mname'] and r['mstatus'] == 'active' else \
              ('매니저 %s 퇴사' % r['mname'] if r['mname'] else '없는 사람')
        r['detail'] = why + ((' · 추천: %s' % s[1]) if s else '')
        r['fix_url'], r['fix_label'] = _emp_link(r['uid']), '직원 수정'
    yield ('bad_manager', 'manager', 'late', '매니저가 퇴사자이거나 본인',
           '그 사람에게 가는 휴가·결재가 아무도 못 보는 곳으로 갑니다.', bad)

    # ── 날짜·상태 ──
    yield ('term_passed_active', 'dates', 'late', '퇴사일이 지났는데 재직',
           '퇴사 처리를 마치지 않았습니다. 로그인·급여·헤드카운트에 그대로 남아 있습니다.',
           ppl(_people(c, "AND TRIM(COALESCE(u.termination_date,''))!='' AND u.termination_date < ?", (t,)),
               edit=False, label='직원 상세'))

    yield ('resigned_no_date', 'dates', 'wait', '퇴사자인데 퇴사일 없음',
           '12개월 이직률·퇴직 분석에서 빠집니다.',
           ppl(_rows(c, "SELECT u.id uid, u.emp_no, u.name, d.name dept FROM users u "
                        "LEFT JOIN departments d ON d.id=u.department_id "
                        "WHERE u.status='resigned' AND u.role!='guest' AND TRIM(COALESCE(u.termination_date,''))='' "
                        "ORDER BY u.name")))

    if _has(c, 'termination_requests'):
        tr = _rows(c, "SELECT r.id ref_id, u.id uid, u.emp_no, u.name, d.name dept, r.status, "
                      "COALESCE(r.final_termination_date, r.requested_termination_date) fd "
                      "FROM termination_requests r JOIN users u ON u.id=r.user_id "
                      "LEFT JOIN departments d ON d.id=u.department_id "
                      "WHERE r.status IN ('approved','in_progress') AND u.status='active' "
                      "AND COALESCE(r.final_termination_date, r.requested_termination_date) < ? ORDER BY fd", (t,))
        for r in tr:
            r['detail'] = '퇴사일 %s · %s' % (r['fd'], '승인됨' if r['status'] == 'approved' else '처리 중')
            r['fix_url'], r['fix_label'] = '/termination/requests/%d' % r['ref_id'], '퇴사 요청'
        yield ('term_req_overdue', 'dates', 'late', '퇴사 요청 날짜가 지났는데 처리 안 됨',
               '승인된 퇴사 요청의 퇴사일이 지났습니다. 요청을 열어 퇴사 처리를 끝내세요.', tr)

    if _has(c, 'incoming_hires'):
        ih = _rows(c, "SELECT id ref_id, NULL uid, '' emp_no, name, department_name dept, start_date "
                      "FROM incoming_hires WHERE status='waiting' AND start_date IS NOT NULL AND start_date < ? "
                      "ORDER BY start_date", (t,))
        for r in ih:
            r['detail'] = '입사 예정일 %s' % r['start_date']
            r['fix_url'], r['fix_label'] = '/hires', '입사 예정자'
        yield ('hire_not_converted', 'dates', 'wait', '입사일이 지났는데 직원 전환 안 됨',
               '입사 예정자 화면에서 [직원으로 전환]하거나, 안 오기로 했으면 취소하세요.', ih)

    if _has(c, 'probation_periods'):
        pr = _rows(c, "SELECT p.id ref_id, u.id uid, u.emp_no, u.name, d.name dept, p.end_date "
                      "FROM probation_periods p JOIN users u ON u.id=p.user_id "
                      "LEFT JOIN departments d ON d.id=u.department_id "
                      "WHERE p.status='active' AND u.status='active' AND p.end_date < ? ORDER BY p.end_date", (t,))
        for r in pr:
            r['detail'] = '수습 끝 %s' % r['end_date']
            r['fix_url'], r['fix_label'] = '/employees/%d#tenure' % r['uid'], '근속·수습'
        yield ('probation_overdue', 'dates', 'wait', '수습이 끝났는데 판정 안 함',
               '합격·불합격·연장 중 하나를 정하세요.', pr)

    two = (today.replace(year=today.year - 2) if not (today.month == 2 and today.day == 29)
           else today.replace(year=today.year - 2, day=28)).isoformat()
    yield ('contract_2y', 'dates', 'ref', '기간제 2년 넘음',
           '기간제법상 2년을 넘겨 일하면 기간 정함이 없는 근로자로 봅니다(예외 사유 있음). 고용형태를 확인하세요.',
           ppl(_people(c, "AND u.employment_type='contract' AND u.hire_date IS NOT NULL AND u.hire_date <= ?", (two,))))

    # ── 퇴사자 정리 ──
    ll = _rows(c, "SELECT d.id ref_id, u.id uid, u.emp_no, u.name, d.name dept, u.status FROM departments d "
                  "JOIN users u ON u.id=d.leader_id WHERE u.status!='active' %s ORDER BY d.name"
               % ("AND COALESCE(d.status,'active')='active'" if 'status' in dcols else ''))
    for r in ll:
        r['detail'] = '%s 조직장' % r['dept']
        r['fix_url'], r['fix_label'] = '/departments/%d' % r['ref_id'], '조직 관리'
    yield ('leaver_leader', 'leaver', 'late', '조직장이 퇴사자',
           '그 조직의 결재·평가 기본값이 비어 있습니다. 조직 관리에서 새 조직장을 정하세요.', ll)

    if 'buddy_id' in ucols:
        yield ('leaver_buddy', 'leaver', 'ref', '버디가 퇴사자',
               '온보딩 화면에 떠난 사람이 버디로 보입니다.',
               ppl(_rows(c, "SELECT u.id uid, u.emp_no, u.name, d.name dept, b.name bname FROM users u "
                            "JOIN users b ON b.id=u.buddy_id LEFT JOIN departments d ON d.id=u.department_id "
                            "WHERE u.status='active' AND b.status!='active' ORDER BY u.name"),
                   lambda r: '버디 %s' % r['bname'], edit=False, label='직원 상세'))

    lg = []
    try:
        gone = _rows(c, "SELECT u.id uid, u.emp_no, u.name, d.name dept, u.email, u.termination_date FROM users u "
                        "LEFT JOIN departments d ON d.id=u.department_id "
                        "WHERE u.status='resigned' AND u.role!='guest' AND TRIM(COALESCE(u.email,''))!=''")
        if gone:
            m = master_db.get_master_db()
            try:
                have = {r[0] for r in m.execute('SELECT email FROM tenant_users WHERE tenant_id=?', (tid,))}
            finally:
                m.close()
            for r in gone:
                if r['email'] in have:
                    r['ref_id'] = r['uid']
                    r['detail'] = '퇴사 %s' % (r['termination_date'] or '날짜 없음')
                    r['fix_url'], r['fix_label'] = _emp_link(r['uid'], False), '직원 상세'
                    lg.append(r)
    except Exception:
        lg = []
    yield ('leaver_login', 'leaver', 'wait', '퇴사자가 로그인 명부에 남음',
           '로그인 화면이 이 회사로 안내합니다(비밀번호가 맞아도 퇴사자라 들어오지는 못함). [명부에서 빼기]로 정리하세요. 재입사하면 다시 올라갑니다.', lg)


def check(c, today=None, tid=1):
    """규칙 목록. 각 dict: key, area, level, label, title, how, rows(예외 뺀 것), skipped(예외 줄)."""
    ensure_schema(c)
    today = today or date.today()
    skips = {}
    for r in _rows(c, 'SELECT * FROM data_check_skips ORDER BY at DESC'):
        skips.setdefault(r['rule'], {})[r['ref_id']] = r
    out = []
    for key, area, level, title, how, rows in _rules(c, today, tid):
        if key == 'term_passed_active':
            tds = dict(c.execute("SELECT id, termination_date FROM users WHERE status='active'").fetchall())
            for r in rows:
                r['detail'] = '퇴사일 %s' % tds.get(r['uid'])
        sk = skips.get(key, {})
        keep, skipped = [], []
        for r in rows:
            if r.get('ref_id') in sk:
                r['skip'] = sk[r['ref_id']]
                skipped.append(r)
            else:
                keep.append(r)
        out.append(dict(key=key, area=area, level=level, label=LEVEL_LABEL[level], title=title, how=how,
                        rows=keep, skipped=skipped))
    return out


def summary(c, today=None, tid=1):
    """인사 분석 화면용: (남은 줄 수, 위험 줄 수)."""
    try:
        rs = check(c, today, tid)
    except Exception:
        return 0, 0
    return sum(len(r['rows']) for r in rs), sum(len(r['rows']) for r in rs if r['level'] == 'late')


def gaps(c):
    """분석 숫자에서 빠지는 재직자 수 — 부서·성별·생년월일·입사일 없음."""
    ucols = _cols(c, 'users')
    q = "SELECT COUNT(*) FROM users WHERE status='active' AND role!='guest' AND "
    g = dict(dept=c.execute(q + 'department_id IS NULL').fetchone()[0],
             hire=c.execute(q + "TRIM(COALESCE(hire_date,''))=''").fetchone()[0],
             birth=c.execute(q + "TRIM(COALESCE(birth_date,''))=''").fetchone()[0],
             gender=c.execute(q + "TRIM(COALESCE(gender,''))=''").fetchone()[0] if 'gender' in ucols else 0)
    return g


def init_app(app, h):
    from flask import Response, abort, flash, redirect, render_template, request, session, url_for

    get_db = h['get_db']
    login_required = h['login_required']
    log_audit = h['log_audit']
    tenant_id = h.get('tenant_id') or (lambda: int(session.get('tenant_id', 1) or 1))
    can = h.get('can') or (lambda code: session.get('user_role') == 'admin')

    app.jinja_env.globals['data_check_can'] = lambda: can('data_check')

    def set_manager(db, uid, mid, reason):
        before = people_history.snapshot(db, uid)
        db.execute('UPDATE users SET manager_id=? WHERE id=?', (mid, uid))
        people_history.record_diff(db, uid, before, 'check', session.get('user_id'), session.get('user_name'),
                                   reason=reason)

    def drop_login(db, uid):
        r = db.execute("SELECT email FROM users WHERE id=? AND status='resigned'", (uid,)).fetchone()
        if not r or not r[0]:
            return False
        master_db.remove_tenant_user(r[0], tenant_id())
        return True

    @app.route('/people/data-check', methods=['GET', 'POST'])
    @login_required
    def data_check():
        if not can('data_check'):
            abort(403)
        db = get_db()
        ensure_schema(db)
        tid = tenant_id()

        if request.method == 'POST':
            if session.get('demo_mode'):
                flash('데모에서는 저장되지 않습니다.', 'warning')
                return redirect(url_for('data_check'))
            act = request.form.get('action')
            rule = (request.form.get('rule') or '').strip()
            back = url_for('data_check', rule=rule or None) + ('#r-' + rule if rule else '')
            rules = {r['key']: r for r in check(db, None, tid)}

            if act == 'skip':
                ref = request.form.get('ref_id', type=int)
                reason = (request.form.get('reason') or '').strip()[:200]
                if rule not in rules or not ref:
                    abort(400)
                if len(reason) < 2:
                    flash('예외로 두려면 사유를 적어 주세요.', 'danger')
                    return redirect(back)
                db.execute('INSERT OR REPLACE INTO data_check_skips (rule, ref_id, reason, by_id, by_name) '
                           'VALUES (?,?,?,?,?)', (rule, ref, reason, session.get('user_id'), session.get('user_name')))
                db.commit()
                flash('예외로 두었습니다.', 'success')
            elif act == 'unskip':
                db.execute('DELETE FROM data_check_skips WHERE rule=? AND ref_id=?',
                           (rule, request.form.get('ref_id', type=int)))
                db.commit()
                flash('예외를 풀었습니다.', 'success')
            elif act in ('manager', 'manager_all'):
                if rule not in ('no_manager', 'bad_manager'):
                    abort(400)
                rows = rules[rule]['rows']
                if act == 'manager':
                    ref = request.form.get('ref_id', type=int)
                    rows = [r for r in rows if r['ref_id'] == ref]
                n = 0
                for r in rows:
                    # 추천은 저장 직전에 다시 계산한다 — 화면을 연 뒤 조직장이 바뀌었을 수 있다.
                    s = suggest_manager(db, r['uid'])
                    if s:
                        set_manager(db, r['uid'], s[0], '데이터 점검: 조직장으로 채움')
                        n += 1
                db.commit()
                log_audit('update', 'personal_info', None, '데이터 점검 매니저 채우기 %d명' % n)
                flash('매니저 %d명을 채웠습니다.' % n if n else '채울 수 있는 사람이 없습니다.', 'success' if n else 'warning')
            elif act in ('login', 'login_all'):
                rows = rules['leaver_login']['rows']
                if act == 'login':
                    ref = request.form.get('ref_id', type=int)
                    rows = [r for r in rows if r['ref_id'] == ref]
                n = sum(1 for r in rows if drop_login(db, r['uid']))
                log_audit('delete', 'personal_info', None, '데이터 점검 퇴사자 로그인 명부 정리 %d명' % n)
                flash('로그인 명부에서 %d명을 뺐습니다.' % n, 'success')
            else:
                abort(400)
            return redirect(back)

        rules = check(db, None, tid)
        if request.args.get('csv'):
            buf = io.StringIO()
            buf.write('﻿')
            w = csv.writer(buf)
            w.writerow(['규칙', '수준', '사번', '이름', '부서', '내용', '예외 사유'])
            for r in rules:
                for x in r['rows'] + r['skipped']:
                    w.writerow([r['title'], r['label'], x.get('emp_no') or '', x.get('name') or '',
                                x.get('dept') or '', x.get('detail') or '',
                                (x.get('skip') or {}).get('reason', '')])
            log_audit('download', 'personal_info', None, '데이터 점검 CSV')
            return Response(buf.getvalue(), mimetype='text/csv; charset=utf-8',
                            headers={'Content-Disposition': 'attachment; filename=data-check.csv'})

        area = request.args.get('area') or ''
        shown = [r for r in rules if not area or r['area'] == area]
        total = sum(len(r['rows']) for r in rules)
        by_level = {k: sum(len(r['rows']) for r in rules if r['level'] == k) for k in LEVEL_LABEL}
        by_area = {k: sum(len(r['rows']) for r in rules if r['area'] == k) for k, _ in AREAS}
        return render_template('data_quality/index.html', rules=shown, total=total, by_level=by_level,
                               by_area=by_area, areas=AREAS, area=area, open_rule=request.args.get('rule') or '',
                               levels=LEVEL_LABEL, active_page='data-check')
