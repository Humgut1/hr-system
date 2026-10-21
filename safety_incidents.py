"""안전사고(B9).

  - 누구나 신고한다(/safety/report). 신고한 사람은 자기 신고의 진행 상태만 본다.
  - 담당자(지금은 관리자, B11 권한 묶음 'safety')가 목록·상세·처리를 한다. 다친 사람·부위는 건강 정보라
    담당자만 보고, 상세를 열면 감사 로그('view')를 남긴다.
  - 장소는 회사·건물 설정(workplace)의 근무지·층·방을 고르고, 나머지는 글로 적는다.
  - 산업재해조사표: 사망 또는 3일 이상 휴업이 필요한 부상·질병이면 사고일부터 1개월 안에 제출
    (산업안전보건법 제57조 제3항, 시행규칙 제73조). 사망은 중대재해라 지체 없이 노동부에 보고(법 제54조).
    제출 기한 7·3일 전·당일·지난 뒤에 담당자에게 알림 한 번씩(alert_stage). 서식 자동 작성·전자 제출은 안 한다.
"""
import csv
import io
from datetime import date, datetime, timedelta

import workplace

_DDL = (
    '''CREATE TABLE IF NOT EXISTS safety_incidents (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        reporter_id      INTEGER REFERENCES users(id) ON DELETE SET NULL,
        occurred_at      TEXT NOT NULL,
        kind             TEXT NOT NULL,
        severity         TEXT NOT NULL DEFAULT 'none',
        injured_user_id  INTEGER REFERENCES users(id) ON DELETE SET NULL,
        injured_name     TEXT,
        site_code        TEXT,
        floor            TEXT,
        room_code        TEXT,
        place            TEXT,
        description      TEXT NOT NULL,
        injury_part      TEXT,
        immediate_action TEXT,
        status           TEXT NOT NULL DEFAULT 'received',
        assignee_id      INTEGER REFERENCES users(id) ON DELETE SET NULL,
        cause            TEXT,
        corrective       TEXT,
        lost_days        INTEGER,
        report_required  INTEGER NOT NULL DEFAULT 0,
        report_due       TEXT,
        filed_on         TEXT,
        alert_stage      INTEGER,
        closed_at        TEXT,
        created_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at       TEXT
    )''',
    'CREATE INDEX IF NOT EXISTS idx_safety_status ON safety_incidents(status, occurred_at)',
    '''CREATE TABLE IF NOT EXISTS safety_incident_log (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        incident_id INTEGER NOT NULL REFERENCES safety_incidents(id) ON DELETE CASCADE,
        user_id     INTEGER,
        what        TEXT NOT NULL,
        created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )''',
)

KIND_LABEL = {'injury': '부상', 'illness': '질병', 'near_miss': '아차사고', 'property': '시설·물건 피해'}
SEVERITY_LABEL = {'none': '다친 사람 없음', 'first_aid': '응급처치', 'medical': '병원 치료',
                  'lost_time': '휴업 필요', 'fatal': '사망'}
STATUS_LABEL = {'received': '접수', 'investigating': '조사 중', 'closed': '종결'}
HURT_KINDS = ('injury', 'illness')
ALERT_DAYS = (7, 3, 0)
OVERDUE = -1

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


def _add_month(d):
    import people_tenure
    return people_tenure.add_months(d, 1)


def norm_dt(s):
    s = (s or '').strip().replace('T', ' ')
    for f in ('%Y-%m-%d %H:%M', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
        try:
            return datetime.strptime(s, f).strftime('%Y-%m-%d %H:%M')
        except ValueError:
            pass
    return None


def report_rule(kind, severity, lost_days, occurred_at):
    """(제출 필요?, 기한 iso) — 사망 또는 3일 이상 휴업인 부상·질병."""
    need = kind in HURT_KINDS and (severity == 'fatal' or (lost_days or 0) >= 3)
    if not need:
        return 0, None
    d = date.fromisoformat(occurred_at[:10])
    return 1, _add_month(d).isoformat()


def locations(c):
    """신고 화면 장소 고르기 — 근무지·층·방."""
    try:
        ss = workplace.sites(c)
        fs = workplace.floors(c)
        rs = workplace.rooms(c)
    except Exception:
        return dict(sites=[], floors=[], rooms=[])
    return dict(sites=[dict(code=s['code'], name=s['name']) for s in ss],
                floors=[dict(site_code=f['site_code'], floor=str(f['floor']),
                             label=f.get('label') or '%sF' % f['floor']) for f in fs],
                rooms=[dict(code=r['code'], name=r['name'], site_code=r['site_code'], floor=str(r['floor'])) for r in rs])


def _place(c, f):
    """폼 → (site, floor, room, place, err). 방을 고르면 근무지·층은 방 기준."""
    site = (f.get('site_code') or '').strip() or None
    floor = (f.get('floor') or '').strip() or None
    rc = (f.get('room_code') or '').strip() or None
    place = (f.get('place') or '').strip()[:200] or None
    loc = locations(c)
    if rc:
        r = next((x for x in loc['rooms'] if x['code'] == rc), None)
        if not r:
            return None, None, None, None, '없는 방입니다.'
        site, floor = r['site_code'], r['floor']
    if site and not any(s['code'] == site for s in loc['sites']):
        return None, None, None, None, '없는 근무지입니다.'
    if floor and not site:
        return None, None, None, None, '층을 고르려면 근무지를 먼저 고르세요.'
    if floor and not any(x['site_code'] == site and x['floor'] == floor for x in loc['floors']):
        return None, None, None, None, '그 근무지에 없는 층입니다.'
    if not (site or place):
        return None, None, None, None, '장소를 고르거나 적어 주세요.'
    return site, floor, rc, place, None


def _log(c, iid, uid, what):
    c.execute('INSERT INTO safety_incident_log (incident_id, user_id, what) VALUES (?,?,?)', (iid, uid, what))


def create(c, f, uid, today=None):
    """신고 저장 → (id, err)."""
    ensure_schema(c)
    occ = norm_dt(f.get('occurred_at'))
    if not occ:
        return None, '사고 일시를 넣어 주세요.'
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    if occ > now:
        return None, '사고 일시가 지금보다 뒤입니다.'
    kind = f.get('kind')
    if kind not in KIND_LABEL:
        return None, '사고 종류를 고르세요.'
    sev = f.get('severity') or 'none'
    if sev not in SEVERITY_LABEL:
        return None, '다친 정도를 고르세요.'
    if kind in HURT_KINDS and sev == 'none':
        return None, '부상·질병이면 다친 정도를 고르세요.'
    desc = (f.get('description') or '').strip()
    if len(desc) < 5:
        return None, '무슨 일이 있었는지 적어 주세요.'
    who = f.get('who') or 'none'
    inj_uid = inj_name = None
    if kind in HURT_KINDS:
        if who == 'self':
            inj_uid = uid
        elif who == 'emp':
            no = (f.get('emp_no') or '').strip()
            r = c.execute("SELECT id FROM users WHERE emp_no=? AND status!='resigned'", (no,)).fetchone() if no else None
            if not r:
                return None, '사번으로 직원을 찾지 못했습니다.'
            inj_uid = r[0]
        elif who == 'other':
            inj_name = (f.get('injured_name') or '').strip()[:60]
            if not inj_name:
                return None, '다친 사람 이름을 적어 주세요.'
        else:
            return None, '다친 사람을 고르세요.'
    site, floor, rc, place, err = _place(c, f)
    if err:
        return None, err
    req, due = report_rule(kind, sev, None, occ)
    cur = c.execute(
        'INSERT INTO safety_incidents (reporter_id, occurred_at, kind, severity, injured_user_id, injured_name, '
        'site_code, floor, room_code, place, description, injury_part, immediate_action, report_required, report_due) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (uid, occ, kind, sev, inj_uid, inj_name, site, floor, rc, place, desc[:2000],
         (f.get('injury_part') or '').strip()[:100] or None, (f.get('immediate_action') or '').strip()[:1000] or None,
         req, due))
    iid = cur.lastrowid
    _log(c, iid, uid, '신고')
    return iid, None


def _decorate(c, r, today=None):
    today = today or date.today()
    r['kind_label'] = KIND_LABEL.get(r['kind'], r['kind'])
    r['severity_label'] = SEVERITY_LABEL.get(r['severity'], r['severity'])
    r['status_label'] = STATUS_LABEL.get(r['status'], r['status'])
    r['due_days'] = (date.fromisoformat(r['report_due']) - today).days if r.get('report_due') else None
    r['due_state'] = None
    if r['report_required']:
        if r['filed_on']:
            r['due_state'] = 'filed'
        elif r['due_days'] < 0:
            r['due_state'] = 'over'
        elif r['due_days'] <= 7:
            r['due_state'] = 'soon'
        else:
            r['due_state'] = 'wait'
    bits = []
    if r.get('site_name'):
        bits.append(r['site_name'])
    if r.get('floor'):
        bits.append('%sF' % r['floor'] if str(r['floor']).lstrip('-').isdigit() else r['floor'])
    if r.get('room_name'):
        bits.append(r['room_name'])
    if r.get('place'):
        bits.append(r['place'])
    r['where'] = ' · '.join(bits) or '-'
    r['injured'] = r.get('injured_label') or r.get('injured_name') or ('-' if r['kind'] in HURT_KINDS else '')
    return r


_SELECT = ('SELECT i.*, rp.name AS reporter, ij.name AS inj_user, ij.emp_no AS inj_emp_no, d.name AS inj_dept, '
           'asg.name AS assignee, s.name AS site_name, rm.name AS room_name '
           'FROM safety_incidents i LEFT JOIN users rp ON rp.id=i.reporter_id '
           'LEFT JOIN users ij ON ij.id=i.injured_user_id LEFT JOIN departments d ON d.id=ij.department_id '
           'LEFT JOIN users asg ON asg.id=i.assignee_id '
           'LEFT JOIN workplace_sites s ON s.code=i.site_code LEFT JOIN workplace_rooms rm ON rm.code=i.room_code ')


def _sel(c, where='', args=()):
    try:
        rows = _rows(c, _SELECT + where, args)
    except Exception:            # workplace 표가 아직 없는 회사
        rows = _rows(c, 'SELECT i.*, rp.name AS reporter, ij.name AS inj_user, ij.emp_no AS inj_emp_no, '
                        'd.name AS inj_dept, asg.name AS assignee FROM safety_incidents i '
                        'LEFT JOIN users rp ON rp.id=i.reporter_id LEFT JOIN users ij ON ij.id=i.injured_user_id '
                        'LEFT JOIN departments d ON d.id=ij.department_id LEFT JOIN users asg ON asg.id=i.assignee_id '
                        + where, args)
    for r in rows:
        if r.get('inj_user'):
            r['injured_label'] = '%s (%s)' % (r['inj_user'], r['inj_emp_no'] or '사번 없음')
        _decorate(c, r)
    return rows


def incident(c, iid):
    ensure_schema(c)
    r = _sel(c, 'WHERE i.id=?', (iid,))
    return r[0] if r else None


def listing(c, status='open', kind='', q=''):
    ensure_schema(c)
    where, args = [], []
    if status == 'open':
        where.append("i.status != 'closed'")
    elif status == 'report':
        where.append('i.report_required=1')
    elif status in STATUS_LABEL:
        where.append('i.status=?')
        args.append(status)
    if kind in KIND_LABEL:
        where.append('i.kind=?')
        args.append(kind)
    if q:
        where.append('(i.description LIKE ? OR i.place LIKE ? OR ij.name LIKE ? OR ij.emp_no LIKE ? OR i.injured_name LIKE ?)')
        args += ['%' + q + '%'] * 5
    return _sel(c, ('WHERE ' + ' AND '.join(where) + ' ' if where else '') + 'ORDER BY i.occurred_at DESC, i.id DESC',
                tuple(args))


def counts(c):
    ensure_schema(c)
    r = c.execute("SELECT SUM(status!='closed'), SUM(status='closed'), COUNT(*), "
                  "SUM(report_required=1 AND filed_on IS NULL) FROM safety_incidents").fetchone()
    return dict(open=r[0] or 0, closed=r[1] or 0, all=r[2] or 0, unfiled=r[3] or 0)


def mine(c, uid):
    """신고한 사람용 — 상태만."""
    ensure_schema(c)
    return [dict(id=r['id'], occurred_at=r['occurred_at'], kind_label=KIND_LABEL.get(r['kind'], r['kind']),
                 status_label=STATUS_LABEL.get(r['status'], r['status']), status=r['status'])
            for r in _rows(c, 'SELECT id, occurred_at, kind, status FROM safety_incidents WHERE reporter_id=? '
                              'ORDER BY id DESC LIMIT 20', (uid,))]


def history(c, iid):
    return _rows(c, 'SELECT l.*, u.name FROM safety_incident_log l LEFT JOIN users u ON u.id=l.user_id '
                    'WHERE l.incident_id=? ORDER BY l.id', (iid,))


def handle(c, iid, f, uid):
    """담당자 처리 저장 → (메시지, err, 새로 제출 대상이 됐는지)."""
    r = incident(c, iid)
    if not r:
        return None, '없는 사고입니다.', False
    if r['status'] == 'closed' and f.get('action') != 'reopen':
        return None, '종결된 사고입니다. [다시 열기]를 먼저 누르세요.', False
    act = f.get('action')
    if act == 'reopen':
        c.execute("UPDATE safety_incidents SET status='investigating', closed_at=NULL, updated_at=CURRENT_TIMESTAMP "
                  'WHERE id=?', (iid,))
        _log(c, iid, uid, '다시 열기')
        return '다시 열었습니다.', None, False
    if act == 'take':
        c.execute("UPDATE safety_incidents SET assignee_id=?, status=CASE WHEN status='received' THEN 'investigating' "
                  'ELSE status END, updated_at=CURRENT_TIMESTAMP WHERE id=?', (uid, iid))
        _log(c, iid, uid, '담당 맡음')
        return '담당을 맡았습니다.', None, False
    if act not in ('save', 'close'):
        return None, '알 수 없는 요청입니다.', False
    sev = f.get('severity') or r['severity']
    if sev not in SEVERITY_LABEL:
        return None, '다친 정도를 고르세요.', False
    ld = (f.get('lost_days') or '').strip()
    if ld:
        if not ld.isdigit() or int(ld) > 3650:
            return None, '휴업 일수는 0 이상의 숫자로 넣으세요.', False
        ld = int(ld)
    else:
        ld = None
    filed = (f.get('filed_on') or '').strip() or None
    if filed:
        try:
            fd = date.fromisoformat(filed)
        except ValueError:
            return None, '제출일 형식이 틀렸습니다.', False
        if fd < date.fromisoformat(r['occurred_at'][:10]) or fd > date.today():
            return None, '제출일은 사고일과 오늘 사이여야 합니다.', False
    cause = (f.get('cause') or '').strip()[:2000] or None
    corr = (f.get('corrective') or '').strip()[:2000] or None
    req, due = report_rule(r['kind'], sev, ld, r['occurred_at'])
    if filed and not req:
        filed = None
    if act == 'close':
        if not cause or not corr:
            return None, '종결하려면 원인과 재발 방지 대책을 적어 주세요.', False
        if req and not filed:
            return None, '산업재해조사표를 낸 날짜를 넣어야 종결할 수 있습니다.', False
    changes = []
    if sev != r['severity']:
        changes.append('다친 정도 %s → %s' % (SEVERITY_LABEL[r['severity']], SEVERITY_LABEL[sev]))
    if ld != r['lost_days']:
        changes.append('휴업 %s일' % ('-' if ld is None else ld))
    if filed != r['filed_on']:
        changes.append('조사표 제출 %s' % (filed or '지움'))
    if cause != r['cause']:
        changes.append('원인')
    if corr != r['corrective']:
        changes.append('대책')
    newly = bool(req and not r['report_required'])
    stage = r['alert_stage'] if (req and due == r['report_due']) else None
    status = 'closed' if act == 'close' else ('investigating' if r['status'] == 'received' else r['status'])
    c.execute('UPDATE safety_incidents SET severity=?, lost_days=?, filed_on=?, cause=?, corrective=?, '
              'report_required=?, report_due=?, alert_stage=?, status=?, '
              'assignee_id=COALESCE(assignee_id, ?), closed_at=?, updated_at=CURRENT_TIMESTAMP WHERE id=?',
              (sev, ld, filed, cause, corr, req, due, stage, status, uid,
               datetime.now().strftime('%Y-%m-%d %H:%M') if act == 'close' else None, iid))
    if newly:
        changes.append('조사표 제출 대상(기한 %s)' % due)
    if act == 'close':
        changes.append('종결')
    _log(c, iid, uid, ' · '.join(changes) or '저장')
    return ('종결했습니다.' if act == 'close' else '저장했습니다.'), None, newly


def to_csv(rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['번호', '사고 일시', '종류', '다친 정도', '다친 사람 사번', '다친 사람', '부서', '장소', '상태',
                '휴업 일수', '조사표 기한', '조사표 제출일', '담당', '원인', '대책'])
    for r in rows:
        w.writerow([r['id'], r['occurred_at'], r['kind_label'], r['severity_label'], r.get('inj_emp_no') or '',
                    r.get('inj_user') or r.get('injured_name') or '', r.get('inj_dept') or '', r['where'],
                    r['status_label'], '' if r['lost_days'] is None else r['lost_days'], r['report_due'] or '',
                    r['filed_on'] or '', r.get('assignee') or '', r['cause'] or '', r['corrective'] or ''])
    return ('﻿' + buf.getvalue()).encode('utf-8')


# ── 담당자 ────────────────────────────────────────────

def handlers(c):
    """알림 받을 담당자 id — 관리자 + 권한 묶음 'safety'."""
    ids = {r[0] for r in c.execute("SELECT id FROM users WHERE role='admin' AND status='active'")}
    try:
        import perm_groups
        ids |= set(perm_groups.holders(c, 'safety'))
    except Exception:
        pass
    return sorted(ids)


def sweep(c, notify, today=None):
    """하루 한 번: 조사표 제출 기한 7·3·0일 전·지남 알림(단계마다 한 번)."""
    ensure_schema(c)
    today = today or date.today()
    hs = handlers(c)
    sent = 0
    for r in _rows(c, 'SELECT id, report_due, alert_stage FROM safety_incidents '
                      'WHERE report_required=1 AND filed_on IS NULL AND report_due IS NOT NULL'):
        days = (date.fromisoformat(r['report_due']) - today).days
        stage = OVERDUE if days < 0 else next((d for d in sorted(ALERT_DAYS) if days <= d), None)
        if stage is None or (r['alert_stage'] is not None and r['alert_stage'] <= stage):
            continue
        when = ('기한 %d일 지남' % -days) if days < 0 else ('오늘 기한' if days == 0 else '기한 %d일 남음' % days)
        for uid in hs:
            notify(uid, '산업재해조사표 %s' % when, '안전사고 #%d · 제출 기한 %s' % (r['id'], r['report_due']),
                   '/safety?id=%d' % r['id'])
        c.execute('UPDATE safety_incidents SET alert_stage=? WHERE id=?', (stage, r['id']))
        sent += 1
    c.commit()
    return sent


# ── 화면 ──────────────────────────────────────────────

def init_app(app, h):
    from flask import Response, abort, flash, redirect, render_template, request, session, url_for

    get_db = h['get_db']
    login_required = h['login_required']
    log_audit = h['log_audit']
    can = h.get('can') or (lambda code: session.get('user_role') == 'admin')

    def notify(uid, title, content, link):
        get_db().execute('INSERT INTO notifications (user_id, type, category, title, content, link) '
                         'VALUES (?,?,?,?,?,?)', (uid, 'action', 'safety', title, content, link))

    swept = {}

    @app.before_request
    def safety_daily_sweep():
        if not session.get('user_id') or session.get('demo_mode') or request.endpoint in (None, 'static'):
            return
        try:
            db = get_db()
            k = _dbkey(db)
            today = date.today().isoformat()
            if swept.get(k) == today:
                return
            ensure_schema(db)
            r = db.execute("SELECT value FROM company_settings WHERE key='safety_sweep_date'").fetchone()
            if not r or r['value'] != today:
                db.execute("INSERT OR REPLACE INTO company_settings (key, value) VALUES ('safety_sweep_date', ?)",
                           (today,))
                db.commit()
                sweep(db, notify)
            swept[k] = today
        except Exception as e:
            app.logger.error('safety sweep failed: %s', e)

    app.jinja_env.globals['safety_can'] = lambda: can('safety')

    @app.route('/safety/report', methods=['GET', 'POST'])
    @login_required
    def safety_report():
        db = get_db()
        ensure_schema(db)
        uid = session['user_id']
        if request.method == 'POST':
            if session.get('demo_mode'):
                flash('데모에서는 저장되지 않습니다.', 'warning')
                return redirect(url_for('safety_report'))
            iid, err = create(db, request.form, uid)
            if err:
                db.rollback()
                flash(err, 'error')
                return render_template('safety/report.html', f=request.form, loc=locations(db), mine=mine(db, uid),
                                       kinds=KIND_LABEL, sevs=SEVERITY_LABEL, active_page='safety_report')
            r = incident(db, iid)
            hs = [x for x in handlers(db) if x != uid]
            for x in hs:
                notify(x, '안전사고 신고 · %s' % r['kind_label'], '%s · %s' % (r['occurred_at'], r['where']),
                       '/safety?id=%d' % iid)
            if r['report_required']:
                for x in hs:
                    notify(x, '산업재해조사표 제출 대상', '안전사고 #%d · 기한 %s' % (iid, r['report_due']),
                           '/safety?id=%d' % iid)
            db.commit()
            flash('신고했습니다. 담당자가 확인합니다.', 'success')
            return redirect(url_for('safety_report'))
        return render_template('safety/report.html', f={}, loc=locations(db), mine=mine(db, uid),
                               kinds=KIND_LABEL, sevs=SEVERITY_LABEL, active_page='safety_report',
                               now=datetime.now().strftime('%Y-%m-%dT%H:%M'))

    @app.route('/safety', methods=['GET', 'POST'])
    @login_required
    def safety_admin():
        if not can('safety'):
            abort(403)
        db = get_db()
        ensure_schema(db)
        sel = request.values.get('id', type=int)
        if request.method == 'POST':
            if session.get('demo_mode'):
                flash('데모에서는 저장되지 않습니다.', 'warning')
                return redirect(url_for('safety_admin', id=sel))
            msg, err, newly = handle(db, sel, request.form, session['user_id'])
            if err:
                db.rollback()
                flash(err, 'error')
            else:
                if newly:
                    r = incident(db, sel)
                    for x in handlers(db):
                        if x != session['user_id']:
                            notify(x, '산업재해조사표 제출 대상', '안전사고 #%d · 기한 %s' % (sel, r['report_due']),
                                   '/safety?id=%d' % sel)
                db.commit()
                log_audit('update', 'personal_info', None, '안전사고 #%d 처리' % sel)
                flash(msg, 'success')
            return redirect(url_for('safety_admin', id=sel))
        status = request.args.get('status') or 'open'
        kind = request.args.get('kind') or ''
        q = request.args.get('q', '').strip()
        if request.args.get('csv'):
            log_audit('download', 'personal_info', None, '안전사고 목록 CSV')
            return Response(to_csv(listing(db, status, kind, q)), mimetype='text/csv; charset=utf-8',
                            headers={'Content-Disposition': 'attachment; filename=safety_incidents.csv'})
        ctx = dict(active_page='safety', status=status, kind=kind, q=q, counts=counts(db), kinds=KIND_LABEL,
                   sevs=SEVERITY_LABEL, statuses=STATUS_LABEL, items=listing(db, status, kind, q))
        if sel:
            r = incident(db, sel)
            if not r:
                abort(404)
            log_audit('view', 'personal_info', r['injured_user_id'], '안전사고 #%d 상세' % sel)
            ctx.update(r=r, hist=history(db, sel))
        return render_template('safety/admin.html', **ctx)
