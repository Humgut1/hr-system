"""리포트 2판: 만들어 이름 붙여 저장하고, 열 때 기준일·부서·기간을 고르고, 묶어 합계·평균을 보고,
나누고(공유), 정해진 날 받는다(자동 보내기).

  - 정의는 `saved_reports.definition` JSON 한 칸. 표는 정의 · 공유 · 자동 보내기 · 보낸 결과 넷.
  - 데이터 원천: 직원(기준일 반영 — people_effective) · 휴가 · 인사 발령 · 퇴사자.
    직원 원천에는 옛 리포트 마법사(REPORT_SOURCES)의 출퇴근·급여·성과 등 칸도 붙는다(기간 칸을 따른다, 기준일은 안 따름).
  - 계산은 전부 파이썬에서 한다: 줄 읽기 → 계산 칸 → 거르기 → 정렬 → 묶기. SQL 은 상수로만 만든다.
  - 계산 칸 식은 직접 읽는다(eval 안 씀): 숫자 · + - * / ( ) · [칸 이름] · 일수([날짜칸]) · 년수([날짜칸]) · 반올림(x, 자리).
  - 사람은 언제나 사번으로 보인다(첫 칸 고정).
  - 권한: 관리자 또는 권한 묶음 'reports'. 급여 칸은 관리자 또는 'reports_pay' 만 — 없으면 빼고 그렇다고 알린다.
    볼 수 있는 리포트 = 내가 만든 것 · 나에게 나눈 것 · 전원에게 나눈 것(관리자는 전부). 고치기는 만든 사람과 관리자만.
  - 자동 보내기: 매주(요일) / 매달(날짜). 그날 첫 요청 때 엑셀을 만들어 표에 넣고 받는 사람에게 앱 알림 + 내려받기 링크.
    메일은 안 보낸다(메일 키 없음). 급여 칸이 있는 리포트는 급여 권한 없는 사람에게 안 보낸다.
"""
import io
import json
import re
from collections import OrderedDict
from datetime import date, timedelta

import people_effective as pe

# ── 원천과 칸 ─────────────────────────────────────────
# 칸 = (key, label, type, pay). type: t 글 / n 숫자 / d 날짜
_T, _N, _D = 't', 'n', 'd'

_PEOPLE = [
    ('emp_no', '사번', _T, False), ('name', '이름', _T, False),
    ('dept', '부서', _T, False), ('dept_code', '조직코드', _T, False), ('upper', '상위 조직', _T, False),
    ('position', '직급', _T, False), ('manager', '매니저', _T, False), ('employment_type', '고용형태', _T, False),
    ('salary', '기본급', _N, True), ('hire_date', '입사일', _D, False), ('tenure', '근속(년)', _N, False),
    ('age', '나이', _N, False), ('gender', '성별', _T, False), ('job_family', '직군', _T, False),
    ('job_title', '직함', _T, False), ('work_type', '근무 형태', _T, False), ('status', '재직 상태', _T, False),
    ('termination_date', '퇴사일', _D, False),
]
_LEAVE = [
    ('emp_no', '사번', _T, False), ('name', '이름', _T, False), ('dept', '부서', _T, False),
    ('leave_type', '휴가 종류', _T, False), ('start_date', '시작일', _D, False), ('end_date', '끝일', _D, False),
    ('days', '일수', _N, False), ('leave_status', '상태', _T, False), ('created', '신청일', _D, False),
]
_ACTIONS = [
    ('emp_no', '사번', _T, False), ('name', '이름', _T, False), ('dept', '부서', _T, False),
    ('action', '발령 종류', _T, False), ('from_value', '이전', _T, False), ('to_value', '이후', _T, False),
    ('effective_date', '효력일', _D, False), ('pa_status', '상태', _T, False), ('reason', '사유', _T, False),
]
_LEAVERS = [
    ('emp_no', '사번', _T, False), ('name', '이름', _T, False), ('dept', '부서', _T, False),
    ('position', '직급', _T, False), ('employment_type', '고용형태', _T, False),
    ('hire_date', '입사일', _D, False), ('termination_date', '퇴사일', _D, False),
    ('tenure', '근속(년)', _N, False), ('exit_cat', '퇴사 사유 분류', _T, False), ('exit_text', '퇴사 사유', _T, False),
    ('regret', '아쉬운 퇴사', _T, False),
]

SOURCES = OrderedDict([
    ('people', dict(label='직원', fields=_PEOPLE, date=None,
                    note='기준일 그날 재직한 사람. 부서·직급·매니저·고용형태·기본급은 그날 효력인 값.')),
    ('leave', dict(label='휴가', fields=_LEAVE, date='start_date', note='휴가 신청 한 건이 한 줄. 기간은 시작일 기준.')),
    ('actions', dict(label='인사 발령', fields=_ACTIONS, date='effective_date',
                     note='인사 발령 한 건이 한 줄. 기간은 효력일 기준.')),
    ('leavers', dict(label='퇴사자', fields=_LEAVERS, date='termination_date',
                     note='퇴사한 사람 한 명이 한 줄. 기간은 퇴사일 기준.')),
])

OPS = OrderedDict([('eq', '같음'), ('ne', '다름'), ('has', '포함'), ('gt', '보다 큼'), ('lt', '보다 작음'),
                   ('ge', '이상'), ('le', '이하'), ('empty', '비어 있음'), ('filled', '값 있음')])
AGGS = OrderedDict([('count', '수'), ('sum', '합계'), ('avg', '평균'), ('min', '최소'), ('max', '최대')])
FREQ = OrderedDict([('weekly', '매주'), ('monthly', '매달')])
DOW = ['월', '화', '수', '목', '금', '토', '일']
MAX_ROWS_VIEW = 500
MAX_CALCS = 8
MAX_FILTERS = 12
_EXTRA_PAY_SOURCES = ('payroll', 'salary_hist')

_DDL = (
    '''CREATE TABLE IF NOT EXISTS saved_reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        description TEXT,
        source TEXT NOT NULL DEFAULT 'people',
        definition TEXT NOT NULL DEFAULT '{}',
        owner_id INTEGER REFERENCES users(id),
        shared_all INTEGER NOT NULL DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
        deleted_at TEXT)''',
    '''CREATE TABLE IF NOT EXISTS report_shares (
        report_id INTEGER NOT NULL REFERENCES saved_reports(id),
        user_id INTEGER NOT NULL REFERENCES users(id),
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (report_id, user_id))''',
    '''CREATE TABLE IF NOT EXISTS report_schedules (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        report_id INTEGER NOT NULL UNIQUE REFERENCES saved_reports(id),
        freq TEXT NOT NULL DEFAULT 'weekly',
        dow INTEGER NOT NULL DEFAULT 0,
        dom INTEGER NOT NULL DEFAULT 1,
        recipients TEXT NOT NULL DEFAULT '',
        active INTEGER NOT NULL DEFAULT 1,
        last_sent_on TEXT,
        created_by INTEGER,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP)''',
    '''CREATE TABLE IF NOT EXISTS report_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        report_id INTEGER NOT NULL REFERENCES saved_reports(id),
        ran_on TEXT NOT NULL,
        rows INTEGER NOT NULL DEFAULT 0,
        recipients TEXT NOT NULL DEFAULT '',
        skipped TEXT NOT NULL DEFAULT '',
        file BLOB,
        kind TEXT NOT NULL DEFAULT 'schedule',
        created_by INTEGER,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP)''',
    'CREATE INDEX IF NOT EXISTS idx_report_runs_report ON report_runs(report_id)',
)

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
    if k:
        _done.add(k)


def forget(c=None):
    if c is None:
        _done.clear()
    else:
        _done.discard(_dbkey(c))


# ── 칸 목록 ─────────────────────────────────────────
def fields_of(source, extra=None):
    """원천의 칸 OrderedDict {key: dict(label,type,pay,group)}. extra = 옛 마법사 칸(직원 원천만)."""
    src = SOURCES.get(source) or SOURCES['people']
    out = OrderedDict()
    for k, lab, t, pay in src['fields']:
        out[k] = dict(key=k, label=lab, type=t, pay=pay, group=src['label'])
    if source == 'people' and extra:
        for s in extra:
            if s['key'] == 'employee':
                continue
            for f in s['fields']:
                out['x_' + f['key']] = dict(key='x_' + f['key'], label=f['label'], type=_N if f['agg'] else _T,
                                            pay=s['key'] in _EXTRA_PAY_SOURCES, group=s['label'] + ' (기간)',
                                            raw=f['key'])
    return out


def _all_fields(d, extra):
    """원천 칸 + 계산 칸."""
    fs = fields_of(d['source'], extra)
    for cdef in d.get('calcs') or []:
        fs[cdef['key']] = dict(key=cdef['key'], label=cdef['label'], type=_N, pay=False, group='계산 칸',
                               expr=cdef['expr'])
    return fs


# ── 정의 다듬기 ─────────────────────────────────────
_DATE = re.compile(r'^\d{4}-\d{2}-\d{2}$')


def _date_or_none(s):
    s = (s or '').strip()[:10]
    if not _DATE.match(s):
        return None
    try:
        date.fromisoformat(s)
    except ValueError:
        return None
    return s


def clean_def(raw, extra=None):
    """화면이 보낸 정의 → 검사한 정의. (정의, 오류 목록)."""
    errs = []
    raw = raw if isinstance(raw, dict) else {}
    source = raw.get('source') if raw.get('source') in SOURCES else 'people'
    base = fields_of(source, extra)
    d = dict(source=source)

    calcs, seen = [], set()
    for i, cdef in enumerate((raw.get('calcs') or [])[:MAX_CALCS]):
        lab = str(cdef.get('label') or '').strip()[:30]
        expr = str(cdef.get('expr') or '').strip()[:300]
        if not lab and not expr:
            continue
        if not lab or not expr:
            errs.append('계산 칸은 이름과 식이 둘 다 있어야 합니다.')
            continue
        if lab in seen or any(f['label'] == lab for f in base.values()):
            errs.append("계산 칸 이름 '%s' 이 다른 칸과 겹칩니다." % lab)
            continue
        key = str(cdef.get('key') or '')
        if not re.match(r'^c\d{1,2}$', key) or key in {c['key'] for c in calcs}:
            key = 'c%d' % (len(calcs) + 1)
            while key in {c['key'] for c in calcs}:
                key = 'c%d' % (int(key[1:]) + 1)
        labels = {f['label']: k for k, f in base.items()}
        labels.update({c['label']: c['key'] for c in calcs})
        try:
            parse_expr(expr, labels)
        except ValueError as e:
            errs.append("계산 칸 '%s': %s" % (lab, e))
            continue
        seen.add(lab)
        calcs.append(dict(key=key, label=lab, expr=expr))
    d['calcs'] = calcs
    fs = _all_fields(d, extra)

    cols = [k for k in OrderedDict.fromkeys(raw.get('columns') or []) if k in fs and k != 'emp_no']
    if not cols:
        errs.append('보여 줄 칸을 하나 이상 고르세요.')
    d['columns'] = cols

    flt = []
    for f in (raw.get('filters') or [])[:MAX_FILTERS]:
        k, op = f.get('f'), f.get('op')
        if k not in fs or op not in OPS:
            continue
        v = str(f.get('v') if f.get('v') is not None else '').strip()[:100]
        if op not in ('empty', 'filled') and v == '':
            errs.append("거르기 '%s %s' 에 값이 없습니다." % (fs[k]['label'], OPS[op]))
            continue
        flt.append(dict(f=k, op=op, v=v))
    d['filters'] = flt

    pr = raw.get('prompts') or {}
    d['prompts'] = dict(on=bool(pr.get('on')) and source == 'people', dept=bool(pr.get('dept')),
                        period=bool(pr.get('period')))
    df = raw.get('defaults') or {}
    try:
        months = max(1, min(36, int(df.get('months') or 12)))
    except (TypeError, ValueError):
        months = 12
    try:
        dept = int(df.get('dept') or 0) or None
    except (TypeError, ValueError):
        dept = None
    d['defaults'] = dict(months=months, dept=dept)

    srt = raw.get('sort') or {}
    d['sort'] = dict(f=srt.get('f') if srt.get('f') in fs else '', dir='desc' if srt.get('dir') == 'desc' else 'asc')

    grp = raw.get('group') or {}
    by = [k for k in OrderedDict.fromkeys(grp.get('by') or []) if k in fs][:2]
    ms = []
    for m in (grp.get('measures') or [])[:6]:
        agg = m.get('agg')
        if agg not in AGGS:
            continue
        k = m.get('f') or ''
        if agg == 'count':
            k = ''
        elif k not in fs or fs[k]['type'] != _N:
            errs.append('합계·평균·최소·최대는 숫자 칸에만 쓸 수 있습니다.')
            continue
        if (agg, k) not in {(x['agg'], x['f']) for x in ms}:
            ms.append(dict(agg=agg, f=k))
    if by and not ms:
        ms = [dict(agg='count', f='')]
    d['group'] = dict(by=by, measures=ms if by else [])
    d['chart'] = 'bar' if by and raw.get('chart') == 'bar' else ''

    tr = raw.get('trend') or {}
    try:
        tm = max(3, min(24, int(tr.get('months') or 12)))
    except (TypeError, ValueError):
        tm = 12
    d['trend'] = dict(on=bool(tr.get('on')), months=tm)
    return d, errs


def load_def(row):
    try:
        d = json.loads(row['definition'] or '{}')
    except ValueError:
        d = {}
    d.setdefault('source', row['source'])
    return d


# ── 계산 칸 식 ─────────────────────────────────────
_TOK = re.compile(r'\s*(?:(\d+(?:\.\d+)?)|\[([^\]]{1,40})\]|(일수|년수|반올림)|([-+*/(),]))')


def parse_expr(expr, labels):
    """식 → 나무. labels = {칸 이름: key}. 틀리면 ValueError(읽는 말)."""
    toks, pos = [], 0
    expr = expr.strip()
    while pos < len(expr):
        m = _TOK.match(expr, pos)
        if not m or m.end() == pos:
            raise ValueError("'%s' 부분을 읽지 못했습니다." % expr[pos:pos + 10].strip())
        num, ref, fn, op = m.groups()
        if num:
            toks.append(('num', float(num)))
        elif ref:
            ref = ref.strip()
            if ref not in labels:
                raise ValueError("[%s] 칸이 없습니다." % ref)
            toks.append(('ref', labels[ref]))
        elif fn:
            toks.append(('fn', fn))
        else:
            toks.append(('op', op))
        pos = m.end()
        while pos < len(expr) and expr[pos].isspace():
            pos += 1
    if not toks:
        raise ValueError('식이 비었습니다.')
    i = [0]

    def peek():
        return toks[i[0]] if i[0] < len(toks) else (None, None)

    def take(kind=None, val=None):
        t = peek()
        if t[0] is None or (kind and t[0] != kind) or (val and t[1] != val):
            raise ValueError('식이 덜 끝났거나 순서가 틀렸습니다.')
        i[0] += 1
        return t

    def atom():
        k, v = peek()
        if k == 'num':
            take()
            return ('num', v)
        if k == 'ref':
            take()
            return ('ref', v)
        if k == 'fn':
            take()
            take('op', '(')
            a = add()
            b = None
            if v == '반올림':
                if peek() == ('op', ','):
                    take()
                    b = add()
            take('op', ')')
            return ('fn', v, a, b)
        if (k, v) == ('op', '('):
            take()
            e = add()
            take('op', ')')
            return e
        if (k, v) == ('op', '-'):
            take()
            return ('neg', atom())
        raise ValueError('식이 덜 끝났거나 순서가 틀렸습니다.')

    def mul():
        e = atom()
        while peek() in (('op', '*'), ('op', '/')):
            op = take()[1]
            e = ('bin', op, e, atom())
        return e

    def add():
        e = mul()
        while peek() in (('op', '+'), ('op', '-')):
            op = take()[1]
            e = ('bin', op, e, mul())
        return e

    tree = add()
    if i[0] != len(toks):
        raise ValueError('식이 덜 끝났거나 순서가 틀렸습니다.')
    return tree


def _days(v, on):
    try:
        return (date.fromisoformat(on) - date.fromisoformat(str(v)[:10])).days
    except (TypeError, ValueError):
        return None


def eval_expr(tree, row, on):
    k = tree[0]
    if k == 'num':
        return tree[1]
    if k == 'ref':
        v = row.get(tree[1])
        if v is None or v == '':
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    if k == 'neg':
        v = eval_expr(tree[1], row, on)
        return None if v is None else -v
    if k == 'fn':
        if tree[1] in ('일수', '년수'):
            src = tree[2]
            raw = row.get(src[1]) if src[0] == 'ref' else None
            dd = _days(raw, on)
            if dd is None:
                return None
            return dd if tree[1] == '일수' else round(dd / 365.25, 2)
        a = eval_expr(tree[2], row, on)
        n = eval_expr(tree[3], row, on) if tree[3] else 0
        return None if a is None else round(a, int(n or 0))
    a, b = eval_expr(tree[2], row, on), eval_expr(tree[3], row, on)
    if a is None or b is None:
        return None
    op = tree[1]
    if op == '+':
        return a + b
    if op == '-':
        return a - b
    if op == '*':
        return a * b
    return None if b == 0 else a / b


# ── 줄 읽기 ─────────────────────────────────────────
def _subtree(c, dept_id):
    kids = {}
    for i, p in c.execute('SELECT id, parent_id FROM departments'):
        kids.setdefault(p, []).append(i)
    out, stack = set(), [dept_id]
    while stack:
        x = stack.pop()
        if x in out:
            continue
        out.add(x)
        stack.extend(kids.get(x, []))
    return out


def _years(a, b):
    try:
        return round((date.fromisoformat(b[:10]) - date.fromisoformat(str(a)[:10])).days / 365.25, 1)
    except (TypeError, ValueError):
        return None


def _age(birth, on):
    try:
        b, o = date.fromisoformat(str(birth)[:10]), date.fromisoformat(on)
    except (TypeError, ValueError):
        return None
    return o.year - b.year - ((o.month, o.day) < (b.month, b.day))


_GENDER = {'M': '남', 'F': '여', 'male': '남', 'female': '여'}
_STATUS = {'active': '재직', 'resigned': '퇴사', 'inactive': '휴면', 'leave': '휴직'}
_PA_STATUS = {'pending': '결재 중', 'approved': '승인', 'rejected': '반려'}
_LV_STATUS = {'pending': '결재 중', 'approved': '승인', 'rejected': '반려', 'cancelled': '취소'}


def _depts(c):
    return {r[0]: dict(name=r[1], parent=r[2], code=r[3]) for r in
            c.execute('SELECT id, name, parent_id, code FROM departments')}


def _people_rows(c, on, dept_ids, h):
    """그날 재직자 줄. on = 'YYYY-MM-DD'."""
    today = date.today().isoformat()
    if on >= today:
        ids = {r[0] for r in c.execute("SELECT id FROM users WHERE status='active'")}
    else:
        ids = pe.employed_on(c, on)
    if not ids:
        return []
    q = ('SELECT u.id, u.emp_no, u.name, u.department_id, u.position_id, u.manager_id, u.employment_type, '
         'u.hire_date, u.birth_date, u.gender, u.job_title, u.work_type, u.status, u.termination_date, '
         'jf.name AS jf, es.base_salary FROM users u LEFT JOIN job_families jf ON jf.id=u.job_family_id '
         'LEFT JOIN employee_salary es ON es.user_id=u.id')
    base = [dict(r) for r in c.execute(q) if r['id'] in ids]
    vals = pe.as_of(c, on, ids) if on != today else {}
    deps = _depts(c)
    pos = {r[0]: r[1] for r in c.execute('SELECT id, name FROM positions')}
    names = {r[0]: r[1] for r in c.execute('SELECT id, name FROM users')}
    out = []
    for u in base:
        got = vals.get(u['id'], {})
        for f in pe.USER_FIELDS:
            if f in got:
                v = got[f]
                u[f] = int(v) if f in ('department_id', 'position_id', 'manager_id') and str(v).isdigit() else (v or None)
        sal = u['base_salary']
        if 'salary' in got:
            try:
                sal = int(float(got['salary']))
            except (TypeError, ValueError):
                pass
        if dept_ids is not None and u['department_id'] not in dept_ids:
            continue
        dp = deps.get(u['department_id']) or {}
        up = deps.get(dp.get('parent')) or {}
        out.append(dict(
            _id=u['id'], emp_no=u['emp_no'] or '', name=u['name'], dept=dp.get('name') or '',
            dept_code=dp.get('code') or '', upper=up.get('name') or '', position=pos.get(u['position_id']) or '',
            manager=names.get(u['manager_id']) or '', employment_type=pe._EMP.get(u['employment_type'] or '', u['employment_type'] or ''),
            salary=sal, hire_date=(u['hire_date'] or '')[:10], tenure=_years(u['hire_date'], on),
            age=_age(u['birth_date'], on), gender=_GENDER.get(u['gender'] or '', u['gender'] or ''),
            job_family=u['jf'] or '', job_title=u['job_title'] or '', work_type=u['work_type'] or '',
            status=_STATUS.get(u['status'], u['status'] or ''), termination_date=(u['termination_date'] or '')[:10]))
    return out


def _user_ctx(c):
    deps = _depts(c)
    users = {r[0]: dict(emp_no=r[1] or '', name=r[2], dept=(deps.get(r[3]) or {}).get('name') or '', dept_id=r[3])
             for r in c.execute('SELECT id, emp_no, name, department_id FROM users')}
    return deps, users


def _leave_rows(c, frm, to, dept_ids, h):
    _, users = _user_ctx(c)
    labels = h.get('leave_labels') or {}
    out = []
    for r in c.execute('SELECT user_id, type, start_date, end_date, days, status, created_at FROM leave_requests '
                       'WHERE start_date >= ? AND start_date <= ? ORDER BY start_date', (frm, to)):
        u = users.get(r[0]) or {}
        if dept_ids is not None and u.get('dept_id') not in dept_ids:
            continue
        out.append(dict(_id=r[0], emp_no=u.get('emp_no', ''), name=u.get('name', ''), dept=u.get('dept', ''),
                        leave_type=labels.get(r[1], r[1]), start_date=(r[2] or '')[:10], end_date=(r[3] or '')[:10],
                        days=r[4], leave_status=_LV_STATUS.get(r[5], r[5]), created=(r[6] or '')[:10]))
    return out


def _action_rows(c, frm, to, dept_ids, h, pay_ok):
    _, users = _user_ctx(c)
    labels = h.get('action_labels') or {}
    out = []
    for r in c.execute('SELECT user_id, action_type, from_value, to_value, effective_date, status, reason '
                       'FROM personnel_actions WHERE effective_date >= ? AND effective_date <= ? '
                       'ORDER BY effective_date', (frm, to)):
        u = users.get(r[0]) or {}
        if dept_ids is not None and u.get('dept_id') not in dept_ids:
            continue
        fv, tv = r[2] or '', r[3] or ''
        if '|' in tv:              # '이름|id' 꼴은 이름만
            tv = tv.split('|')[0]
        if r[1] == 'salary_change' and not pay_ok:
            fv = tv = '(급여 권한 필요)'
        out.append(dict(_id=r[0], emp_no=u.get('emp_no', ''), name=u.get('name', ''), dept=u.get('dept', ''),
                        action=labels.get(r[1], r[1]), from_value=fv, to_value=tv,
                        effective_date=(r[4] or '')[:10], pa_status=_PA_STATUS.get(r[5], r[5]), reason=r[6] or ''))
    return out


def _leaver_rows(c, frm, to, dept_ids, h):
    deps, _ = _user_ctx(c)
    pos = {r[0]: r[1] for r in c.execute('SELECT id, name FROM positions')}
    labels = h.get('exit_labels') or {}
    tr = {}
    try:
        for r in c.execute("SELECT user_id, exit_reason_category, reason_code, is_regrettable FROM termination_requests "
                           "WHERE status='completed' ORDER BY id"):
            tr[r[0]] = r
    except Exception:
        pass
    out = []
    for r in c.execute("SELECT id, emp_no, name, department_id, position_id, employment_type, hire_date, "
                       "termination_date, termination_reason FROM users WHERE status='resigned' "
                       "AND termination_date >= ? AND termination_date <= ? ORDER BY termination_date", (frm, to)):
        if dept_ids is not None and r[3] not in dept_ids:
            continue
        t = tr.get(r[0])
        cat = (t[1] or t[2]) if t else None
        regret = '' if not t or t[3] is None else ('예' if t[3] else '아니오')
        out.append(dict(_id=r[0], emp_no=r[1] or '', name=r[2], dept=(deps.get(r[3]) or {}).get('name') or '',
                        position=pos.get(r[4]) or '', employment_type=pe._EMP.get(r[5] or '', r[5] or ''),
                        hire_date=(r[6] or '')[:10], termination_date=(r[7] or '')[:10],
                        tenure=_years(r[6], r[7] or ''), exit_cat=labels.get(cat, cat or ''),
                        exit_text=(r[8] or '').strip(), regret=regret))
    return out


def _extra_values(c, rows, keys, frm, to, h):
    """옛 마법사 칸(x_…) 값을 사람 id 로 붙인다."""
    raw = [k[2:] for k in keys]
    ids = [r['_id'] for r in rows]
    if not raw or not ids:
        return
    sql, params, labels = h['build_query'](raw, {'date_from': frm, 'date_to': to}, limit=None, user_ids=ids)
    got = {}
    for r in c.execute(sql, params):
        got[r[0]] = r
    for row in rows:
        g = got.get(row['_id'])
        for j, k in enumerate(raw):
            row['x_' + k] = g[j + 2] if g is not None else None


# ── 돌리기 ─────────────────────────────────────────
def _num(v):
    if v is None or v == '':
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _match(row, f, fs):
    v = row.get(f['f'])
    op, want = f['op'], f['v']
    if op == 'empty':
        return v is None or v == ''
    if op == 'filled':
        return not (v is None or v == '')
    if v is None or v == '':
        return op == 'ne'
    t = fs[f['f']]['type']
    if op == 'has':
        return want.lower() in str(v).lower()
    if t == _N:
        a, b = _num(v), _num(want)
        if a is None or b is None:
            return op == 'ne'
    else:
        a, b = str(v), want
    return dict(eq=a == b, ne=a != b, gt=a > b, lt=a < b, ge=a >= b, le=a <= b)[op]


def _agg(vals, agg):
    if agg == 'count':
        return len(vals)
    xs = [x for x in (_num(v) for v in vals) if x is not None]
    if not xs:
        return None
    if agg == 'sum':
        return round(sum(xs), 2)
    if agg == 'avg':
        return round(sum(xs) / len(xs), 2)
    return min(xs) if agg == 'min' else max(xs)


def measure_label(m, fs):
    if m['agg'] == 'count':
        return '수'
    return '%s %s' % (fs[m['f']]['label'], AGGS[m['agg']])


def resolve_params(d, args, today=None):
    """열 때 고른 값(주소) + 기본값 → 실제로 쓸 기준일·부서·기간."""
    today = today or date.today().isoformat()
    pr, df = d.get('prompts') or {}, d.get('defaults') or {}
    on = _date_or_none(args.get('on')) if pr.get('on') else None
    on = on or today
    dept = df.get('dept')
    if pr.get('dept'):
        raw = args.get('dept')
        if raw is not None:
            try:
                dept = int(raw) or None
            except (TypeError, ValueError):
                dept = None
    months = df.get('months') or 12
    end = _date_or_none(args.get('to')) if pr.get('period') else None
    end = end or today
    frm = _date_or_none(args.get('from')) if pr.get('period') else None
    if not frm:
        e = date.fromisoformat(end)
        frm = (e - timedelta(days=int(months * 30.44))).isoformat()
    if frm > end:
        frm, end = end, frm
    return dict(on=on, dept=dept, frm=frm, to=end)


def rows_for(c, d, p, h, pay_ok, on=None):
    """원천에서 줄을 읽고 계산 칸까지 붙인다. on 을 주면 그날 기준(추이용)."""
    dept_ids = _subtree(c, p['dept']) if p.get('dept') else None
    s = d['source']
    if s == 'people':
        rows = _people_rows(c, on or p['on'], dept_ids, h)
    elif s == 'leave':
        rows = _leave_rows(c, p['frm'], p['to'], dept_ids, h)
    elif s == 'actions':
        rows = _action_rows(c, p['frm'], p['to'], dept_ids, h, pay_ok)
    else:
        rows = _leaver_rows(c, p['frm'], p['to'], dept_ids, h)
    fs = _all_fields(d, h.get('extra'))
    used = set(d['columns']) | {f['f'] for f in d['filters']} | set(d['group']['by']) | \
        {m['f'] for m in d['group']['measures'] if m['f']} | ({d['sort']['f']} if d['sort']['f'] else set())
    for cdef in d.get('calcs') or []:
        used |= set(re.findall(r'\[([^\]]+)\]', cdef['expr']))
    labels = {f['label']: k for k, f in fs.items()}
    used = {labels.get(k, k) for k in used}
    extra = [k for k in fs if k.startswith('x_') and k in used and (pay_ok or not fs[k]['pay'])]
    if extra and on is None:
        _extra_values(c, rows, extra, p['frm'], p['to'], h)
    if not pay_ok:
        for r in rows:
            for k, f in fs.items():
                if f['pay'] and k in r:
                    r[k] = None
    ref_on = on or (p['on'] if s == 'people' else p['to'])
    trees = []
    for cdef in d.get('calcs') or []:
        lab = {f['label']: k for k, f in fs.items() if k != cdef['key']}
        try:
            trees.append((cdef['key'], parse_expr(cdef['expr'], lab)))
        except ValueError:
            trees.append((cdef['key'], None))
    for r in rows:
        for k, t in trees:
            v = eval_expr(t, r, ref_on) if t else None
            r[k] = None if v is None else (int(v) if float(v).is_integer() else round(v, 2))
    rows = [r for r in rows if all(_match(r, f, fs) for f in d['filters'])]
    return rows, fs


def _sort_key(v, t):
    if v is None or v == '':
        return (1, 0, '')
    if t == _N:
        n = _num(v)
        return (0, n if n is not None else 0, '')
    return (0, 0, str(v))


def run(c, d, args, h, pay_ok, today=None):
    """리포트 결과: 칸·줄·묶음·추이·알림 글."""
    p = resolve_params(d, args, today)
    rows, fs = rows_for(c, d, p, h, pay_ok)
    notes = []
    hidden = [fs[k]['label'] for k in d['columns'] if k in fs and fs[k]['pay'] and not pay_ok]
    if hidden:
        notes.append('급여 칸은 권한이 없어 비워 두었습니다: ' + ', '.join(hidden))
    srt = d['sort']
    if srt['f'] in fs:
        rows.sort(key=lambda r: _sort_key(r.get(srt['f']), fs[srt['f']]['type']), reverse=srt['dir'] == 'desc')
        if srt['dir'] == 'desc':     # 빈 값은 늘 뒤로
            rows.sort(key=lambda r: r.get(srt['f']) in (None, ''))
    else:
        rows.sort(key=lambda r: (r.get('emp_no') or '', r.get('start_date') or r.get('effective_date') or ''))
    cols = [('emp_no', '사번')] + [(k, fs[k]['label']) for k in d['columns'] if k in fs]
    res = dict(params=p, columns=cols, rows=rows, total=len(rows), notes=notes, fields=fs, group=None, trend=None)
    if d['group']['by']:
        res['group'] = group(rows, d, fs)
    if d['trend']['on']:
        res['trend'] = trend(c, d, p, h, pay_ok)
    return res


def group(rows, d, fs):
    by, ms = d['group']['by'], d['group']['measures']
    buckets = OrderedDict()
    for r in rows:
        k = tuple('' if r.get(b) is None else str(r.get(b)) for b in by)
        buckets.setdefault(k, []).append(r)
    out = []
    for k, rs in buckets.items():
        out.append(dict(key=k, vals=[_agg([x.get(m['f']) for x in rs] if m['f'] else rs, m['agg']) for m in ms]))
    first = ms[0]
    out.sort(key=lambda g: (g['vals'][0] is None, -(g['vals'][0] or 0), g['key']))
    total = [_agg([x.get(m['f']) for x in rows] if m['f'] else rows, m['agg']) for m in ms]
    peak = max([g['vals'][0] or 0 for g in out] + [0])
    return dict(by=[fs[b]['label'] for b in by], measures=[measure_label(m, fs) for m in ms], rows=out,
                total=total, peak=peak, first=measure_label(first, fs))


def _month_ends(end, n):
    e = date.fromisoformat(end)
    pts = [e]
    y, m = e.year, e.month
    for _ in range(n - 1):
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
        nxt = date(y + (m == 12), (m % 12) + 1, 1)
        pts.append(nxt - timedelta(days=1))
    return [x.isoformat() for x in reversed(pts)]


def trend(c, d, p, h, pay_ok):
    """기간 추이: 직원 원천은 달마다 그날 기준으로 다시 세고, 나머지는 날짜 칸의 달로 나눈다.
    값 = 첫 묶음 측정(없으면 수). 묶음 칸이 있으면 첫 묶음 칸으로 줄을 나눈다(위 8개 + 기타)."""
    n = d['trend']['months']
    ms = d['group']['measures'] or [dict(agg='count', f='')]
    m = ms[0]
    by = d['group']['by'][0] if d['group']['by'] else None
    fs = _all_fields(d, h.get('extra'))
    end = p['on'] if d['source'] == 'people' else p['to']
    points = _month_ends(end, n)
    series = OrderedDict()     # 줄 이름 → {달: 값}
    labels = [x[:7] for x in points]

    def put(rs, lab):
        if by:
            bk = OrderedDict()
            for r in rs:
                bk.setdefault('' if r.get(by) is None else str(r.get(by)), []).append(r)
            for g, xs in bk.items():
                series.setdefault(g, {})[lab] = _agg([x.get(m['f']) for x in xs] if m['f'] else xs, m['agg'])
        series.setdefault('__all', {})[lab] = _agg([x.get(m['f']) for x in rs] if m['f'] else rs, m['agg'])

    if d['source'] == 'people':
        for on in points:
            rs, _ = rows_for(c, d, p, h, pay_ok, on=on)
            put(rs, on[:7])
    else:
        frm = points[0][:8] + '01'
        p2 = dict(p, frm=frm, to=end)
        rs, _ = rows_for(c, d, p2, h, pay_ok)
        dk = SOURCES[d['source']]['date']
        byk = OrderedDict((lab, []) for lab in labels)
        for r in rs:
            mo = (r.get(dk) or '')[:7]
            if mo in byk:
                byk[mo].append(r)
        for lab, xs in byk.items():
            put(xs, lab)
    total = series.pop('__all', {})
    lines = []
    if by:
        rank = sorted(series.items(), key=lambda kv: -(_num(kv[1].get(labels[-1])) or 0))
        for g, vals in rank[:8]:
            lines.append(dict(name=g or '(빈 값)', vals=[vals.get(l) for l in labels]))
        if len(rank) > 8:
            lines.append(dict(name='기타 %d개' % (len(rank) - 8), vals=[
                _sum_or_none([v.get(l) for _, v in rank[8:]]) if m['agg'] in ('count', 'sum') else None
                for l in labels]))
    tot = [total.get(l) if m['agg'] != 'count' else (total.get(l) or 0) for l in labels]
    return dict(labels=labels, lines=lines, total=tot, measure=measure_label(m, fs), by=fs[by]['label'] if by else '',
                svg=_svg(labels, tot))


def _sum_or_none(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs), 2) if xs else None


def _svg(labels, vals):
    """합계 꺾은선 한 줄(색 없음 — currentColor)."""
    xs = [v for v in vals if v is not None]
    if not xs:
        return ''
    w, hgt, pad = 640, 160, 24
    lo, hi = min(xs + [0]), max(xs)
    span = (hi - lo) or 1
    step = (w - 2 * pad) / max(1, len(vals) - 1)
    pts = []
    for i, v in enumerate(vals):
        if v is None:
            continue
        x = pad + i * step
        y = hgt - pad - (v - lo) / span * (hgt - 2 * pad)
        pts.append((round(x, 1), round(y, 1), v, labels[i]))
    path = ' '.join('%s%s,%s' % ('M' if i == 0 else 'L', x, y) for i, (x, y, _, _) in enumerate(pts))
    dots = ''.join('<circle cx="%s" cy="%s" r="3"><title>%s · %s</title></circle>' % (x, y, l, _fmt(v))
                   for x, y, v, l in pts)
    ticks = ''.join('<text x="%s" y="%s" text-anchor="middle">%s</text>' % (round(pad + i * step, 1), hgt - 4, l[2:])
                    for i, l in enumerate(labels) if len(labels) <= 12 or i % 2 == len(labels) % 2 - 1 or i == len(labels) - 1)
    return ('<svg class="rp-svg" viewBox="0 0 %d %d" role="img" aria-label="기간 추이">'
            '<line x1="%d" y1="%d" x2="%d" y2="%d" class="rp-axis"/>'
            '<path d="%s" fill="none" stroke="currentColor" stroke-width="2"/>'
            '<g fill="currentColor">%s</g><g class="rp-tick">%s</g>'
            '<text x="%d" y="14" class="rp-tick">%s</text></svg>'
            % (w, hgt, pad, hgt - pad, w - pad, hgt - pad, path, dots, ticks, pad, _fmt(hi)))


def _fmt(v):
    if v is None:
        return ''
    if isinstance(v, float):
        return '{:,}'.format(int(v)) if v.is_integer() else '{:,.2f}'.format(v)
    if isinstance(v, int):
        return '{:,}'.format(v)
    return str(v)


# ── 엑셀 ─────────────────────────────────────────
def to_xlsx(res, name, h):
    wb, ws = h['make_wb']('리포트')
    p = res['params']
    ws.append([name])
    ws.append(['기준일 %s · 기간 %s ~ %s · %d줄' % (p['on'], p['frm'], p['to'], res['total'])])
    labels = [lab for _, lab in res['columns']]
    h['write_header'](ws, labels, row=3)
    for i, r in enumerate(res['rows'], 4):
        h['write_row'](ws, i, [r.get(k) for k, _ in res['columns']])
    h['freeze_header'](ws, row=4)
    h['auto_width'](ws)
    if res['group']:
        g = res['group']
        ws2 = wb.create_sheet('묶음')
        h['write_header'](ws2, g['by'] + g['measures'])
        for i, row in enumerate(g['rows'], 2):
            h['write_row'](ws2, i, list(row['key']) + row['vals'])
        h['write_row'](ws2, len(g['rows']) + 2, ['합계'] + [''] * (len(g['by']) - 1) + g['total'], total=True)
        h['auto_width'](ws2)
    if res['trend']:
        t = res['trend']
        ws3 = wb.create_sheet('추이')
        h['write_header'](ws3, [t['by'] or '구분'] + t['labels'])
        i = 2
        for ln in t['lines']:
            h['write_row'](ws3, i, [ln['name']] + ln['vals'])
            i += 1
        h['write_row'](ws3, i, ['전체 ' + t['measure']] + t['total'], total=True)
        h['auto_width'](ws3)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ── 권한·보기 ─────────────────────────────────────────
def uses_pay(d, extra):
    fs = _all_fields(d, extra)
    keys = set(d.get('columns') or []) | {f['f'] for f in d.get('filters') or []} | set(d['group']['by']) | \
        {m['f'] for m in d['group']['measures'] if m['f']}
    labels = {f['label']: k for k, f in fs.items()}
    for cdef in d.get('calcs') or []:
        keys |= {labels.get(x) for x in re.findall(r'\[([^\]]+)\]', cdef['expr'])}
    return any(k in fs and fs[k]['pay'] for k in keys)


def holders(c, h):
    """리포트 권한이 있는 재직자 id(관리자 + 'reports' 묶음)."""
    ids = {r[0] for r in c.execute("SELECT id FROM users WHERE role='admin' AND status='active'")}
    ids |= set(h['perm_holders'](c, 'reports'))
    return ids


def pay_holders(c, h):
    ids = {r[0] for r in c.execute("SELECT id FROM users WHERE role='admin' AND status='active'")}
    ids |= set(h['perm_holders'](c, 'reports_pay'))
    return ids


def visible(c, uid, is_admin, rid=None):
    q = ('SELECT r.*, u.name AS owner_name, '
         '(SELECT COUNT(*) FROM report_shares s WHERE s.report_id=r.id) AS share_n, '
         '(SELECT 1 FROM report_shares s WHERE s.report_id=r.id AND s.user_id=?) AS to_me, '
         'sc.freq, sc.dow, sc.dom, sc.active AS sch_active '
         'FROM saved_reports r LEFT JOIN users u ON u.id=r.owner_id '
         'LEFT JOIN report_schedules sc ON sc.report_id=r.id WHERE r.deleted_at IS NULL')
    args = [uid]
    if rid is not None:
        q += ' AND r.id=?'
        args.append(rid)
    q += ' ORDER BY r.updated_at DESC, r.id DESC'
    out = []
    for r in c.execute(q, args):
        r = dict(r)
        mine = r['owner_id'] == uid
        if not (is_admin or mine or r['to_me'] or r['shared_all']):
            continue
        r['mine'] = mine
        r['can_edit'] = mine or is_admin
        out.append(r)
    return out


def schedule_text(r):
    if not r.get('freq') or not r.get('sch_active'):
        return ''
    if r['freq'] == 'weekly':
        return '매주 %s요일' % DOW[int(r['dow']) % 7]
    return '매달 %d일' % int(r['dom'])


def due_on(sc, today):
    """그 일정이 오늘(또는 놓친 날 이후) 보낼 차례인가. 가장 최근 보낼 날 ≤ 오늘 이 지난번 보낸 날보다 뒤면 보낸다."""
    t = date.fromisoformat(today)
    if sc['freq'] == 'weekly':
        last = t - timedelta(days=(t.weekday() - int(sc['dow'])) % 7)
    else:
        dom = max(1, min(28, int(sc['dom'])))
        last = t.replace(day=dom) if t.day >= dom else \
            (t.replace(day=1) - timedelta(days=1)).replace(day=dom)
    made = (sc['created_at'] or '')[:10]
    if made and last.isoformat() < made:
        return False
    return not sc['last_sent_on'] or sc['last_sent_on'] < last.isoformat()


def deliver(c, rep, h, kind='schedule', by=None, today=None):
    """리포트를 지금 돌려 엑셀을 표에 넣고 받는 사람에게 알린다. (run id, 받은 사람 수, 건너뛴 사람 수)."""
    ensure_schema(c)
    today = today or date.today().isoformat()
    d, _ = clean_def(load_def(rep), h.get('extra'))
    sc = c.execute('SELECT * FROM report_schedules WHERE report_id=?', (rep['id'],)).fetchone()
    want = [int(x) for x in ((sc['recipients'] if sc else '') or '').split(',') if x.isdigit()]
    if kind == 'test' and by:
        want = [by]
    ok_ids = holders(c, h)
    pay_need = uses_pay(d, h.get('extra'))
    pay_ids = pay_holders(c, h) if pay_need else set()
    send, skip = [], []
    for u in want:
        if u not in ok_ids or (pay_need and u not in pay_ids):
            skip.append(u)
        else:
            send.append(u)
    owner_pay = rep['owner_id'] in pay_holders(c, h)
    res = run(c, d, {}, h, owner_pay, today=today)
    blob = to_xlsx(res, rep['name'], h)
    cur = c.execute('INSERT INTO report_runs (report_id, ran_on, rows, recipients, skipped, file, kind, created_by) '
                    'VALUES (?,?,?,?,?,?,?,?)',
                    (rep['id'], today, res['total'], ','.join(map(str, send)), ','.join(map(str, skip)), blob, kind, by))
    run_id = cur.lastrowid
    for u in send:
        c.execute('INSERT INTO notifications (user_id, type, category, title, content, link) VALUES (?,?,?,?,?,?)',
                  (u, 'info', 'report', "리포트 '%s'" % rep['name'],
                   '%s 기준 %d줄 · 엑셀 내려받기' % (today, res['total']), '/reports/runs/%d' % run_id))
    if sc and kind == 'schedule':
        c.execute('UPDATE report_schedules SET last_sent_on=? WHERE id=?', (today, sc['id']))
    # 보낸 결과는 리포트마다 최근 12개만 남긴다
    c.execute('DELETE FROM report_runs WHERE report_id=? AND id NOT IN '
              '(SELECT id FROM report_runs WHERE report_id=? ORDER BY id DESC LIMIT 12)', (rep['id'], rep['id']))
    return run_id, len(send), len(skip)


def sweep(c, h, today=None):
    ensure_schema(c)
    today = today or date.today().isoformat()
    n = 0
    for sc in c.execute('SELECT sc.*, r.id AS rid FROM report_schedules sc JOIN saved_reports r ON r.id=sc.report_id '
                        'WHERE sc.active=1 AND r.deleted_at IS NULL').fetchall():
        if not due_on(sc, today):
            continue
        rep = c.execute('SELECT * FROM saved_reports WHERE id=?', (sc['rid'],)).fetchone()
        deliver(c, rep, h, today=today)
        n += 1
    return n


# ── 화면 ─────────────────────────────────────────
def init_app(app, h):
    from flask import Response, abort, flash, jsonify, redirect, render_template, request, session, url_for

    get_db = h['get_db']
    login_required = h['login_required']
    log_audit = h['log_audit']
    can = h.get('can') or (lambda code: session.get('user_role') == 'admin')
    plan_ok = h.get('plan_ok') or (lambda: True)
    extra = h.get('extra')

    def allowed():
        return plan_ok() and can('reports')

    def pay_ok():
        return can('reports_pay')

    app.jinja_env.globals['reports_can'] = allowed

    def is_admin():
        return session.get('user_role') == 'admin'

    def get_or_404(db, rid):
        rows = visible(db, session['user_id'], is_admin(), rid)
        if not rows:
            abort(404)
        return rows[0]

    def demo_block(back):
        if session.get('demo_mode'):
            flash('데모에서는 저장되지 않습니다.', 'warning')
            return redirect(back)
        return None

    def depts(db):
        return [dict(r) for r in db.execute(
            "SELECT id, name FROM departments WHERE COALESCE(status,'active')='active' ORDER BY name")]

    def field_menu(source):
        fs = fields_of(source, extra)
        out = OrderedDict()
        for k, f in fs.items():
            if f['pay'] and not pay_ok():
                continue
            out.setdefault(f['group'], []).append(f)
        return out

    swept = {}

    @app.before_request
    def reports_daily_sweep():
        """자동 보내기는 하루 한 번(첫 요청 때). 회사 설정에 날짜를 적어 워커·재시작에도 한 번."""
        if not session.get('user_id') or session.get('demo_mode') or request.endpoint in (None, 'static'):
            return
        try:
            db = get_db()
            k = _dbkey(db)
            today = date.today().isoformat()
            if swept.get(k) == today:
                return
            ensure_schema(db)
            r = db.execute("SELECT value FROM company_settings WHERE key='reports_sweep_date'").fetchone()
            if not r or r['value'] != today:
                db.execute("INSERT OR REPLACE INTO company_settings (key, value) VALUES ('reports_sweep_date', ?)",
                           (today,))
                db.commit()
                sweep(db, h, today)
                db.commit()
            swept[k] = today
        except Exception as e:   # 보내기 실패로 화면을 막지 않는다
            app.logger.error('reports sweep failed: %s', e)

    @app.route('/reports', endpoint='reports_list')
    @login_required
    def reports_list():
        if not allowed():
            abort(403)
        db = get_db()
        ensure_schema(db)
        tab = request.args.get('tab') or 'mine'
        rows = visible(db, session['user_id'], is_admin())
        counts = dict(mine=sum(1 for r in rows if r['mine']),
                      shared=sum(1 for r in rows if not r['mine'] and (r['to_me'] or r['shared_all'])),
                      all=len(rows))
        if tab == 'mine':
            rows = [r for r in rows if r['mine']]
        elif tab == 'shared':
            rows = [r for r in rows if not r['mine'] and (r['to_me'] or r['shared_all'])]
        else:
            tab = 'all'
        for r in rows:
            r['sched'] = schedule_text(r)
            r['source_label'] = SOURCES.get(r['source'], SOURCES['people'])['label']
        return render_template('reports/list.html', rows=rows, tab=tab, counts=counts, is_admin=is_admin(), active_page='reports')

    def editor(db, rep, d, errs=None):
        users = [dict(r) for r in db.execute(
            "SELECT id, emp_no, name FROM users WHERE status='active' AND id IN (%s) ORDER BY name"
            % ','.join(str(i) for i in holders(db, h) | {0}))]
        shares, sch = set(), None
        if rep:
            shares = {r[0] for r in db.execute('SELECT user_id FROM report_shares WHERE report_id=?', (rep['id'],))}
            sch = db.execute('SELECT * FROM report_schedules WHERE report_id=?', (rep['id'],)).fetchone()
        fs = _all_fields(d, extra)
        menu = OrderedDict((s, [dict(key=k, label=f['label'], type=f['type'], pay=f['pay'], group=f['group'])
                                for k, f in fields_of(s, extra).items() if pay_ok() or not f['pay']])
                           for s in SOURCES)
        return render_template('reports/edit.html', rep=rep, d=d, errs=errs or [], sources=SOURCES,
                               menu=menu, field_menu=field_menu(d['source']), fields=fs, ops=OPS, aggs=AGGS,
                               depts=depts(db), users=users, shares=shares, sch=sch, freq=FREQ, dow=DOW,
                               me=session['user_id'], pay_ok=pay_ok(), active_page='reports')

    def save(db, rep):
        f = request.form
        try:
            raw = json.loads(f.get('definition') or '{}')
        except ValueError:
            raw = {}
        d, errs = clean_def(raw, extra)
        if not pay_ok() and uses_pay(d, extra):
            errs.append('급여 칸은 급여 권한이 있어야 넣을 수 있습니다.')
        name = (f.get('name') or '').strip()[:60]
        if not name:
            errs.append('리포트 이름을 적으세요.')
        desc = (f.get('description') or '').strip()[:300]
        if errs:
            return None, d, errs
        uid = session['user_id']
        if rep:
            db.execute("UPDATE saved_reports SET name=?, description=?, source=?, definition=?, "
                       "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                       (name, desc, d['source'], json.dumps(d, ensure_ascii=False), rep['id']))
            rid = rep['id']
        else:
            rid = db.execute('INSERT INTO saved_reports (name, description, source, definition, owner_id) '
                             'VALUES (?,?,?,?,?)',
                             (name, desc, d['source'], json.dumps(d, ensure_ascii=False), uid)).lastrowid
        # 공유
        ok_ids = holders(db, h)
        db.execute('UPDATE saved_reports SET shared_all=? WHERE id=?', (1 if f.get('shared_all') else 0, rid))
        db.execute('DELETE FROM report_shares WHERE report_id=?', (rid,))
        for x in f.getlist('share'):
            if x.isdigit() and int(x) in ok_ids and int(x) != uid:
                db.execute('INSERT OR IGNORE INTO report_shares (report_id, user_id) VALUES (?,?)', (rid, int(x)))
        # 자동 보내기
        freq = f.get('freq') or ''
        if freq in FREQ:
            rc = [int(x) for x in f.getlist('recipient') if x.isdigit() and int(x) in ok_ids]
            if not rc:
                rc = [uid]
            dow = max(0, min(6, f.get('dow', type=int) or 0))
            dom = max(1, min(28, f.get('dom', type=int) or 1))
            if db.execute('SELECT 1 FROM report_schedules WHERE report_id=?', (rid,)).fetchone():
                db.execute('UPDATE report_schedules SET freq=?, dow=?, dom=?, recipients=?, active=1 WHERE report_id=?',
                           (freq, dow, dom, ','.join(map(str, rc)), rid))
            else:
                db.execute('INSERT INTO report_schedules (report_id, freq, dow, dom, recipients, created_by) '
                           'VALUES (?,?,?,?,?,?)', (rid, freq, dow, dom, ','.join(map(str, rc)), uid))
        else:
            db.execute('UPDATE report_schedules SET active=0 WHERE report_id=?', (rid,))
        db.commit()
        log_audit('update' if rep else 'create', 'export', None, '리포트 %d: %s' % (rid, name))
        return rid, d, []

    @app.route('/reports/new', methods=['GET', 'POST'], endpoint='reports_new')
    @login_required
    def reports_new():
        if not allowed():
            abort(403)
        db = get_db()
        ensure_schema(db)
        if request.method == 'POST':
            blk = demo_block(url_for('reports_list'))
            if blk:
                return blk
            rid, d, errs = save(db, None)
            if errs:
                return editor(db, None, d, errs)
            flash('리포트를 저장했습니다.', 'success')
            return redirect(url_for('reports_view', rid=rid))
        source = request.args.get('source') if request.args.get('source') in SOURCES else 'people'
        start = dict(people=['name', 'dept', 'position', 'hire_date'], leave=['name', 'dept', 'leave_type', 'start_date', 'days'],
                     actions=['name', 'action', 'from_value', 'to_value', 'effective_date'],
                     leavers=['name', 'dept', 'termination_date', 'exit_cat'])[source]
        d, _ = clean_def(dict(source=source, columns=start, prompts=dict(on=source == 'people', dept=True,
                                                                          period=source != 'people')), extra)
        return editor(db, None, d)

    @app.route('/reports/<int:rid>', endpoint='reports_view')
    @login_required
    def reports_view(rid):
        if not allowed():
            abort(403)
        db = get_db()
        ensure_schema(db)
        rep = get_or_404(db, rid)
        d, _ = clean_def(load_def(rep), extra)
        res = run(db, d, request.args, h, pay_ok())
        dept_name = ''
        if res['params']['dept']:
            r = db.execute('SELECT name FROM departments WHERE id=?', (res['params']['dept'],)).fetchone()
            dept_name = r[0] if r else ''
        runs = [dict(r) for r in db.execute(
            'SELECT id, ran_on, rows, recipients, skipped, kind, created_at FROM report_runs WHERE report_id=? '
            'ORDER BY id DESC LIMIT 5', (rid,))]
        for r in runs:
            r['n_to'] = len([x for x in r['recipients'].split(',') if x])
            r['n_skip'] = len([x for x in r['skipped'].split(',') if x])
        rep['sched'] = schedule_text(rep)
        return render_template('reports/view.html', rep=rep, d=d, res=res, depts=depts(db), dept_name=dept_name,
                               view_rows=res['rows'][:MAX_ROWS_VIEW], max_rows=MAX_ROWS_VIEW, fmt=_fmt,
                               source=SOURCES[d['source']], runs=runs, qs=request.query_string.decode(), active_page='reports')

    @app.route('/reports/<int:rid>/export', endpoint='reports_export')
    @login_required
    def reports_export(rid):
        if not allowed():
            abort(403)
        db = get_db()
        ensure_schema(db)
        rep = get_or_404(db, rid)
        d, _ = clean_def(load_def(rep), extra)
        res = run(db, d, request.args, h, pay_ok())
        blob = to_xlsx(res, rep['name'], h)
        log_audit('download', 'export', None, '리포트 %d: %s' % (rid, rep['name']))
        from urllib.parse import quote
        fname = quote('%s_%s.xlsx' % (rep['name'], res['params']['on']))
        return Response(blob, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                        headers={'Content-Disposition': "attachment; filename*=UTF-8''%s" % fname})

    @app.route('/reports/<int:rid>/edit', methods=['GET', 'POST'], endpoint='reports_edit')
    @login_required
    def reports_edit(rid):
        if not allowed():
            abort(403)
        db = get_db()
        ensure_schema(db)
        rep = get_or_404(db, rid)
        if not rep['can_edit']:
            abort(403)
        if request.method == 'POST':
            blk = demo_block(url_for('reports_view', rid=rid))
            if blk:
                return blk
            _, d, errs = save(db, rep)
            if errs:
                return editor(db, rep, d, errs)
            flash('리포트를 고쳤습니다.', 'success')
            return redirect(url_for('reports_view', rid=rid))
        d, _ = clean_def(load_def(rep), extra)
        if request.args.get('source') in SOURCES and request.args['source'] != d['source']:
            d, _ = clean_def(dict(d, source=request.args['source'], columns=['name']), extra)
        return editor(db, rep, d)

    @app.route('/reports/<int:rid>/copy', methods=['POST'], endpoint='reports_copy')
    @login_required
    def reports_copy(rid):
        if not allowed():
            abort(403)
        db = get_db()
        ensure_schema(db)
        rep = get_or_404(db, rid)
        blk = demo_block(url_for('reports_view', rid=rid))
        if blk:
            return blk
        d, _ = clean_def(load_def(rep), extra)
        if not pay_ok() and uses_pay(d, extra):
            flash('급여 칸이 들어 있어 급여 권한이 있어야 복사할 수 있습니다.', 'warning')
            return redirect(url_for('reports_view', rid=rid))
        new = db.execute('INSERT INTO saved_reports (name, description, source, definition, owner_id) '
                         'VALUES (?,?,?,?,?)', ((rep['name'] + ' 사본')[:60], rep['description'], rep['source'],
                                                json.dumps(d, ensure_ascii=False), session['user_id'])).lastrowid
        db.commit()
        log_audit('create', 'export', None, '리포트 %d 복사: %s' % (new, rep['name']))
        flash('사본을 만들었습니다. 공유·자동 보내기는 옮기지 않았습니다.', 'success')
        return redirect(url_for('reports_edit', rid=new))

    @app.route('/reports/<int:rid>/delete', methods=['POST'], endpoint='reports_delete')
    @login_required
    def reports_delete(rid):
        if not allowed():
            abort(403)
        db = get_db()
        ensure_schema(db)
        rep = get_or_404(db, rid)
        if not rep['can_edit']:
            abort(403)
        blk = demo_block(url_for('reports_view', rid=rid))
        if blk:
            return blk
        db.execute('UPDATE saved_reports SET deleted_at=CURRENT_TIMESTAMP WHERE id=?', (rid,))
        db.execute('UPDATE report_schedules SET active=0 WHERE report_id=?', (rid,))
        db.commit()
        log_audit('delete', 'export', None, '리포트 %d: %s' % (rid, rep['name']))
        flash("'%s' 리포트를 지웠습니다." % rep['name'], 'success')
        return redirect(url_for('reports_list'))

    @app.route('/reports/<int:rid>/send', methods=['POST'], endpoint='reports_send')
    @login_required
    def reports_send(rid):
        """[지금 보내 보기] — 받는 사람 전원에게 지금 한 번(일정 날짜는 그대로)."""
        if not allowed():
            abort(403)
        db = get_db()
        ensure_schema(db)
        rep = get_or_404(db, rid)
        if not rep['can_edit']:
            abort(403)
        blk = demo_block(url_for('reports_view', rid=rid))
        if blk:
            return blk
        if not db.execute('SELECT 1 FROM report_schedules WHERE report_id=? AND active=1', (rid,)).fetchone():
            flash('자동 보내기가 꺼져 있습니다. [고치기]에서 일정과 받는 사람을 먼저 정하세요.', 'warning')
            return redirect(url_for('reports_view', rid=rid))
        _, n, skip = deliver(db, db.execute('SELECT * FROM saved_reports WHERE id=?', (rid,)).fetchone(), h,
                             kind='manual', by=session['user_id'])
        db.commit()
        msg = '%d명에게 보냈습니다.' % n
        if skip:
            msg += ' %d명은 권한(리포트 또는 급여)이 없어 건너뛰었습니다.' % skip
        flash(msg, 'success' if n else 'warning')
        return redirect(url_for('reports_view', rid=rid))

    @app.route('/reports/runs/<int:run_id>', endpoint='reports_run_file')
    @login_required
    def reports_run_file(run_id):
        db = get_db()
        ensure_schema(db)
        r = db.execute('SELECT rr.*, sr.name, sr.deleted_at FROM report_runs rr JOIN saved_reports sr ON sr.id=rr.report_id '
                       'WHERE rr.id=?', (run_id,)).fetchone()
        if not r or r['deleted_at']:
            abort(404)
        uid = session['user_id']
        got = uid in [int(x) for x in r['recipients'].split(',') if x.isdigit()]
        if not (got and allowed()) and not (allowed() and visible(db, uid, is_admin(), r['report_id'])
                                            and (r['created_by'] == uid or is_admin())):
            abort(403)
        log_audit('download', 'export', None, '리포트 %d 보낸 결과 %d' % (r['report_id'], run_id))
        from urllib.parse import quote
        fname = quote('%s_%s.xlsx' % (r['name'], r['ran_on']))
        return Response(r['file'], mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                        headers={'Content-Disposition': "attachment; filename*=UTF-8''%s" % fname})
