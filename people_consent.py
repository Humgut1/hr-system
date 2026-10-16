"""개인정보 수집·이용 동의(B3).

  - 목적(consent_purposes) 하나 = 동의 한 줄. 필수(법에 근거 — 안내 '확인'만)와 선택(동의·철회).
  - 기록(consent_records)은 지우지 않고 쌓는다. 지금 상태 = 그 사람·그 목적의 마지막 기록.
  - 내용을 고치며 '다시 동의'를 고르면 판(version)이 올라가고, 옛 판 동의는 '다시 동의 필요'가 된다.
  - 선택 목적 photo·emergency·personal_email 은 실제 칸과 묶인다:
      철회 → 그 칸을 지운다(사진 파일·비상 연락처·개인 이메일) + 변경 이력.
      철회한 사람은 그 칸을 다시 넣을 수 없다. 한 번도 답하지 않은 사람은
      회사 설정 '동의 전 입력 막기'(consent_strict)가 켜져 있을 때만 막는다(기본 꺼짐).
  - 사람은 사번으로 묶는다 — 현황표·CSV 의 열쇠가 사번.
"""
import csv
import io
import secrets
import sqlite3

import people_history
import people_profile

_DDL = (
    '''CREATE TABLE IF NOT EXISTS consent_purposes (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        code        TEXT NOT NULL UNIQUE,
        title       TEXT NOT NULL,
        kind        TEXT NOT NULL DEFAULT 'optional' CHECK(kind IN ('required','optional')),
        basis       TEXT,
        items       TEXT,
        body        TEXT,
        retention   TEXT,
        version     INTEGER NOT NULL DEFAULT 1,
        active      INTEGER NOT NULL DEFAULT 1,
        sort        INTEGER NOT NULL DEFAULT 0,
        builtin     INTEGER NOT NULL DEFAULT 0,
        updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_by  TEXT
    )''',
    '''CREATE TABLE IF NOT EXISTS consent_records (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        purpose_id  INTEGER NOT NULL REFERENCES consent_purposes(id) ON DELETE CASCADE,
        code        TEXT NOT NULL,
        version     INTEGER NOT NULL,
        action      TEXT NOT NULL CHECK(action IN ('agree','withdraw','notice')),
        at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        ip          TEXT,
        actor_id    INTEGER
    )''',
    'CREATE INDEX IF NOT EXISTS idx_consent_records_user ON consent_records(user_id, purpose_id, id)',
)

_RET = '퇴사 후 지체 없이 파기합니다. 법으로 보관 기간이 정해진 기록은 그 기간만 보관합니다.'
_NO_HARM = '동의하지 않아도 불이익은 없습니다. 이 칸이 비어 있을 뿐입니다.'

SEED = (
    dict(code='hr_contract', title='인사·근로계약 관리', kind='required', sort=1,
         basis='개인정보 보호법 제15조 제1항 제4호(계약 이행)',
         items='이름 · 사번 · 생년월일 · 연락처 · 주소 · 부서 · 직급 · 입사일 · 근태 · 평가',
         body='근로계약을 맺고 지키는 데 꼭 필요한 정보입니다. 동의가 아니라 안내이며, 확인만 받습니다.',
         retention=_RET),
    dict(code='payroll_tax', title='급여·4대 보험·세금', kind='required', sort=2,
         basis='개인정보 보호법 제15조 제1항 제2호(법령상 의무)',
         items='급여 · 계좌 · 4대 보험 · 원천징수 정보',
         body='급여 지급과 법으로 정해진 신고에 쓰입니다. 동의가 아니라 안내이며, 확인만 받습니다.',
         retention=_RET),
    dict(code='photo', title='사진', kind='optional', sort=11,
         basis='정보주체의 동의(제15조 제1항 제1호)', items='얼굴 사진',
         body='조직도·직원 찾기에서 동료가 알아볼 수 있게 보여 줍니다. ' + _NO_HARM, retention=_RET),
    dict(code='emergency', title='비상 연락처', kind='optional', sort=12,
         basis='정보주체의 동의(제15조 제1항 제1호)', items='비상 연락처 이름 · 관계 · 번호(최대 3명)',
         body='사고·급한 일이 있을 때 연락하는 데만 씁니다. ' + _NO_HARM, retention=_RET),
    dict(code='personal_email', title='개인 이메일', kind='optional', sort=13,
         basis='정보주체의 동의(제15조 제1항 제1호)', items='개인 이메일 주소',
         body='퇴사 후 경력증명서·원천징수영수증 같은 서류를 보내는 데 씁니다. ' + _NO_HARM, retention=_RET),
)
FIELD_CODES = ('photo', 'emergency', 'personal_email')   # 실제 칸과 묶인 목적

_done = set()


def _key(c):
    try:
        return c.execute('PRAGMA database_list').fetchone()[2]
    except Exception:
        return None


def ensure_schema(c):
    k = _key(c)
    if k and k in _done:
        return
    for q in _DDL:
        c.execute(q)
    if not c.execute('SELECT 1 FROM consent_purposes LIMIT 1').fetchone():
        for s in SEED:
            try:
                c.execute('INSERT INTO consent_purposes (code,title,kind,basis,items,body,retention,sort,builtin) '
                          'VALUES (?,?,?,?,?,?,?,?,1)',
                          (s['code'], s['title'], s['kind'], s['basis'], s['items'], s['body'],
                           s['retention'], s['sort']))
            except sqlite3.IntegrityError:
                pass   # 다른 워커가 먼저 넣음
    c.commit()
    if k:
        _done.add(k)


def purposes(c, only_active=True):
    ensure_schema(c)
    q = 'SELECT * FROM consent_purposes'
    if only_active:
        q += ' WHERE active=1'
    return [dict(r) for r in _rows(c, q + ' ORDER BY sort, id')]


def _rows(c, q, args=()):
    cur = c.execute(q, args)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _last(c, uid):
    """{purpose_id: 마지막 기록}"""
    out = {}
    for r in _rows(c, 'SELECT * FROM consent_records WHERE user_id=? ORDER BY id', (uid,)):
        out[r['purpose_id']] = r
    return out


def state_of(p, rec):
    """'done'(확인·동의, 지금 판) / 'renew'(옛 판) / 'withdrawn' / 'none'"""
    if not rec:
        return 'none'
    if rec['action'] == 'withdraw':
        return 'withdrawn'
    return 'done' if rec['version'] == p['version'] else 'renew'


STATE_LABEL = {'done': '동의', 'renew': '다시 동의 필요', 'withdrawn': '철회', 'none': '미응답'}
STATE_LABEL_REQ = {'done': '확인', 'renew': '다시 확인 필요', 'withdrawn': '확인', 'none': '미확인'}


def strict(c):
    r = c.execute("SELECT value FROM company_settings WHERE key='consent_strict'").fetchone()
    return bool(r and str(r[0]) == '1')


def blocked(c, uid):
    """입력을 막을 칸 목적 코드 집합(photo·emergency·personal_email 중)."""
    ensure_schema(c)
    ps = {p['code']: p for p in purposes(c) if p['code'] in FIELD_CODES}
    if not ps:
        return set()
    last = _last(c, uid) if uid else {}
    st = strict(c)
    out = set()
    for code, p in ps.items():
        s = state_of(p, last.get(p['id']))
        if s == 'withdrawn' or (st and s != 'done'):
            out.add(code)
    return out


def filter_fields(c, uid, fields, ecs):
    """동의 없는 칸은 저장하지 않는다(지우기는 허용). (fields, ecs)"""
    b = blocked(c, uid)
    if not b:
        return fields, ecs
    if fields and 'personal_email' in b and fields.get('personal_email'):
        fields = {k: v for k, v in fields.items() if k != 'personal_email'}
    if ecs and 'emergency' in b:
        ecs = None
    return fields, ecs


def _clear(c, base, tenant_id, uid, code, actor_id, actor_name):
    """철회한 목적의 칸을 지운다."""
    if code == 'photo':
        if people_profile.photo_remove(c, base, tenant_id, uid):
            people_history.record_change(c, uid, 'photo', '있음', '없음', 'consent', actor_id, actor_name,
                                         reason='사진 동의 철회')
    elif code == 'personal_email':
        r = c.execute('SELECT personal_email FROM users WHERE id=?', (uid,)).fetchone()
        if r and r[0]:
            c.execute('UPDATE users SET personal_email=NULL WHERE id=?', (uid,))
            people_history.record_change(c, uid, 'personal_email', r[0], None, 'consent', actor_id, actor_name,
                                         reason='개인 이메일 동의 철회')
    elif code == 'emergency':
        ch = people_profile.emergency_save(c, uid, [])
        if ch:
            people_history.record_change(c, uid, 'emergency_contacts', ch[0], ch[1], 'consent', actor_id,
                                         actor_name, reason='비상 연락처 동의 철회')


def record(c, uid, p, action, ip=None, actor_id=None):
    c.execute('INSERT INTO consent_records (user_id,purpose_id,code,version,action,ip,actor_id) '
              'VALUES (?,?,?,?,?,?,?)', (uid, p['id'], p['code'], p['version'], action, ip, actor_id))


def missing(c, uid):
    """그 사람이 아직 답하지 않았거나 다시 답해야 하는 목적."""
    last = _last(c, uid)
    return [p for p in purposes(c) if state_of(p, last.get(p['id'])) in ('none', 'renew')]


def matrix(c):
    """재직자 × 목적 현황. (목적 목록, 사람 줄 목록)"""
    ps = purposes(c)
    people = _rows(c, "SELECT u.id, u.emp_no, u.name, d.name AS dept FROM users u "
                      "LEFT JOIN departments d ON d.id=u.department_id "
                      "WHERE u.status='active' ORDER BY u.emp_no, u.id")
    last = {}
    for r in _rows(c, 'SELECT user_id, purpose_id, version, action, at FROM consent_records ORDER BY id'):
        last[(r['user_id'], r['purpose_id'])] = r
    for u in people:
        u['cells'] = []
        u['todo'] = 0
        for p in ps:
            rec = last.get((u['id'], p['id']))
            s = state_of(p, rec)
            u['cells'].append(dict(state=s, at=rec['at'] if rec else None))
            if s in ('none', 'renew'):
                u['todo'] += 1
    return ps, people


def csv_bytes(c):
    ps, people = matrix(c)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['사번', '이름', '부서'] + ['%s(v%d)' % (p['title'], p['version']) for p in ps])
    for u in people:
        row = [u['emp_no'] or '', u['name'], u['dept'] or '']
        for p, cell in zip(ps, u['cells']):
            lab = (STATE_LABEL_REQ if p['kind'] == 'required' else STATE_LABEL)[cell['state']]
            row.append(lab + (' ' + cell['at'][:10] if cell['at'] else ''))
        w.writerow(row)
    return ('﻿' + buf.getvalue()).encode('utf-8')


# ── 화면 ────────────────────────────────────────────────────
def init_app(app, h):
    from flask import (Response, abort, flash, redirect, render_template, request, session, url_for)

    get_db = h['get_db']
    login_required = h['login_required']
    admin_required = h['admin_required']

    # 화면에서 막힌 칸 표시: {% set cb = consent_blocked(uid) %}
    app.jinja_env.globals['consent_blocked'] = lambda uid: blocked(get_db(), uid) if uid else set()

    def ip():
        return request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip()

    @app.route('/me/consents', methods=['GET', 'POST'])
    @login_required
    def my_consents():
        db = get_db()
        ensure_schema(db)
        uid = session['user_id']
        ps = purposes(db)
        if request.method == 'POST':
            code = request.form.get('code') or ''
            act = request.form.get('action') or ''
            if act == 'all':
                # 아직 답하지 않은 필수 안내를 한꺼번에 확인
                last = _last(db, uid)
                n = 0
                for x in ps:
                    if x['kind'] == 'required' and state_of(x, last.get(x['id'])) in ('none', 'renew'):
                        record(db, uid, x, 'notice', ip(), uid)
                        n += 1
                db.commit()
                if n:
                    flash('필수 안내 %d건을 확인했습니다.' % n, 'success')
                return redirect(url_for('my_consents'))
            p = next((x for x in ps if x['code'] == code), None)
            if not p:
                abort(404)
            if p['kind'] == 'required':
                if act != 'notice':
                    abort(400)
                record(db, uid, p, 'notice', ip(), uid)
                db.commit()
                flash("'%s' 안내를 확인했습니다." % p['title'], 'success')
            elif act == 'agree':
                record(db, uid, p, 'agree', ip(), uid)
                db.commit()
                flash("'%s'에 동의했습니다." % p['title'], 'success')
            elif act == 'withdraw':
                record(db, uid, p, 'withdraw', ip(), uid)
                _clear(db, h['base_dir'], h['tenant_id'](), uid, p['code'], uid, session.get('user_name'))
                db.commit()
                msg = "'%s' 동의를 철회했습니다." % p['title']
                if p['code'] in FIELD_CODES:
                    msg += ' 저장돼 있던 정보는 지웠습니다.'
                flash(msg, 'success')
            else:
                abort(400)
            return redirect(url_for('my_consents'))
        last = _last(db, uid)
        rows = []
        for p in ps:
            rec = last.get(p['id'])
            s = state_of(p, rec)
            rows.append(dict(p=p, state=s, at=rec['at'] if rec else None,
                             label=(STATE_LABEL_REQ if p['kind'] == 'required' else STATE_LABEL)[s]))
        req_todo = sum(1 for r in rows if r['p']['kind'] == 'required' and r['state'] in ('none', 'renew'))
        return render_template('consent/me.html', rows=rows, req_todo=req_todo, active_page='profile')

    @app.route('/settings/consents', methods=['GET', 'POST'])
    @admin_required
    def consent_settings():
        db = get_db()
        ensure_schema(db)
        me = session.get('user_name')
        if request.method == 'POST':
            act = request.form.get('action') or ''
            if act == 'strict':
                v = '1' if request.form.get('strict') else '0'
                db.execute("INSERT INTO company_settings (key,value) VALUES ('consent_strict',?) "
                           'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (v,))
                db.commit()
                flash('저장했습니다.', 'success')
            elif act == 'save':
                pid = request.form.get('id', type=int)
                title = (request.form.get('title') or '').strip()[:60]
                kind = request.form.get('kind') if request.form.get('kind') in ('required', 'optional') else 'optional'
                vals = dict(title=title,
                            basis=(request.form.get('basis') or '').strip()[:200] or None,
                            items=(request.form.get('items') or '').strip()[:400] or None,
                            body=(request.form.get('body') or '').strip()[:1000] or None,
                            retention=(request.form.get('retention') or '').strip()[:300] or None)
                if not title:
                    flash('이름을 적어 주세요.', 'error')
                    return redirect(url_for('consent_settings', edit=pid or 'new'))
                if pid:
                    p = db.execute('SELECT * FROM consent_purposes WHERE id=?', (pid,)).fetchone()
                    if not p:
                        abort(404)
                    if p['builtin']:
                        kind = p['kind']   # 기본 목적은 종류를 못 바꾼다(칸 잠금과 묶임)
                    bump = 1 if request.form.get('bump') else 0
                    db.execute('UPDATE consent_purposes SET title=?, kind=?, basis=?, items=?, body=?, retention=?, '
                               'version=version+?, active=?, updated_at=CURRENT_TIMESTAMP, updated_by=? WHERE id=?',
                               (vals['title'], kind, vals['basis'], vals['items'], vals['body'], vals['retention'],
                                bump, 1 if request.form.get('active') else 0, me, pid))
                    flash('저장했습니다.' + (' 판이 올라가 전원이 다시 동의해야 합니다.' if bump else ''), 'success')
                else:
                    n = db.execute('SELECT COUNT(*) FROM consent_purposes').fetchone()[0]
                    db.execute('INSERT INTO consent_purposes (code,title,kind,basis,items,body,retention,sort,updated_by) '
                               'VALUES (?,?,?,?,?,?,?,?,?)',
                               ('c%d_%s' % (n + 1, secrets.token_hex(3)), vals['title'], kind, vals['basis'],
                                vals['items'], vals['body'], vals['retention'], 100 + n, me))
                    flash("'%s' 목적을 더했습니다." % title, 'success')
                db.commit()
            elif act == 'remind':
                ps, people = matrix(db)
                n = 0
                for u in people:
                    if u['todo']:
                        h['add_notification'](u['id'], 'action', 'consent', '개인정보 동의 확인',
                                              '확인하거나 답해야 할 항목이 %d개 있습니다.' % u['todo'],
                                              url_for('my_consents'))
                        n += 1
                flash('%d명에게 동의 요청을 보냈습니다.' % n if n else '보낼 사람이 없습니다.', 'success')
            return redirect(url_for('consent_settings'))
        if request.args.get('csv'):
            h['log_audit']('download', 'personal_info', None, '개인정보 동의 현황 CSV')
            return Response(csv_bytes(db), mimetype='text/csv; charset=utf-8',
                            headers={'Content-Disposition': 'attachment; filename="consents.csv"'})
        allp = purposes(db, only_active=False)
        ps, people = matrix(db)
        f = request.args.get('f', 'todo')
        q = (request.args.get('q') or '').strip()
        shown = [u for u in people if (f != 'todo' or u['todo'])
                 and (not q or q in (u['name'] or '') or q in (u['emp_no'] or '') or q in (u['dept'] or ''))]
        counts = []
        for i, p in enumerate(ps):
            c = dict(done=0, renew=0, withdrawn=0, none=0)
            for u in people:
                c[u['cells'][i]['state']] += 1
            counts.append(c)
        edit = request.args.get('edit')
        ep = None
        if edit and edit != 'new':
            ep = next((x for x in allp if str(x['id']) == edit), None)
        return render_template('consent/settings.html', allp=allp, ps=ps, people=shown[:300], total=len(people),
                               todo=sum(1 for u in people if u['todo']), shown_n=len(shown), counts=counts,
                               f=f, q=q, edit=edit, ep=ep, strict=strict(db),
                               SL=STATE_LABEL, SLR=STATE_LABEL_REQ, active_page='consents')
