"""인사 분석 새 지표 3개(B13): 관리 폭 · 내부 이동률 · 퇴사 사유.

  - 새 테이블 없음. 이미 있는 users · field_changes(B1) · personnel_actions · termination_requests 를 읽기만 한다.
  - 날짜는 파이썬 오늘(KST)을 넘긴다 — SQLite date('now') 는 UTC.
  - 사람 목록은 사번(emp_no)을 같이 내보낸다 — 다른 표·엑셀과 사번으로 맞춰 볼 수 있게.
"""
from datetime import timedelta

import people_history

# 관리 폭 기준 — 회사마다 바꿀 일이 생기면 여기만 고친다
SPAN_WIDE = 13      # 직속 13명 이상 = 넓음
SPAN_NARROW = 2     # 직속 1~2명 = 좁음
SPAN_BUCKETS = (('1–2명', 1, 2), ('3–5명', 3, 5), ('6–8명', 6, 8), ('9–12명', 9, 12), ('13명 이상', 13, 10 ** 6))

# 회사가 고른 퇴사 유형 · 자유 글 사유에서 비자발로 읽는 말
_INVOL_TYPES = {'dismissal', 'contract', 'retirement'}
_INVOL_WORDS = ('계약 만료', '계약만료', '권고', '해고', '정년', '징계', '경영상')


def _median(xs):
    xs = sorted(xs)
    n = len(xs)
    if not n:
        return 0
    return xs[n // 2] if n % 2 else round((xs[n // 2 - 1] + xs[n // 2]) / 2.0, 1)


def span_of_control(db):
    """매니저(직속 재직자가 1명 이상인 재직자)마다 직속 인원 · 아래 전체 인원."""
    people = db.execute(
        "SELECT u.id, u.name, u.emp_no, u.manager_id, d.name AS dept "
        "FROM users u LEFT JOIN departments d ON d.id = u.department_id "
        "WHERE u.status='active' AND u.role!='guest'").fetchall()
    by_id = {p['id']: p for p in people}
    kids = {}
    for p in people:
        if p['manager_id'] and p['manager_id'] in by_id and p['manager_id'] != p['id']:
            kids.setdefault(p['manager_id'], []).append(p['id'])

    def _below(mid):
        seen, stack = set(), list(kids.get(mid, []))
        while stack:
            x = stack.pop()
            if x in seen or x == mid:
                continue
            seen.add(x)
            stack.extend(kids.get(x, []))
        return len(seen)

    rows = []
    for mid, ks in kids.items():
        m = by_id[mid]
        n = len(ks)
        rows.append({'id': mid, 'name': m['name'], 'emp_no': m['emp_no'] or '', 'dept': m['dept'] or '—',
                     'direct': n, 'total': _below(mid),
                     'flag': 'wide' if n >= SPAN_WIDE else 'narrow' if n <= SPAN_NARROW else ''})
    rows.sort(key=lambda r: (-r['direct'], r['name']))
    ds = [r['direct'] for r in rows]
    buckets = [(lb, sum(1 for d in ds if lo <= d <= hi)) for lb, lo, hi in SPAN_BUCKETS]
    no_mgr = sum(1 for p in people if not p['manager_id'] or p['manager_id'] not in by_id)
    return {
        'managers': len(rows), 'staff': len(people),
        'avg': round(sum(ds) * 1.0 / len(ds), 1) if ds else 0, 'median': _median(ds),
        'max': max(ds) if ds else 0,
        'wide': [r for r in rows if r['flag'] == 'wide'],
        'narrow': [r for r in rows if r['flag'] == 'narrow'],
        'buckets': buckets, 'rows': rows, 'no_manager': no_mgr,
        'wide_at': SPAN_WIDE, 'narrow_at': SPAN_NARROW,
    }


def mobility(db, today, avg_headcount):
    """최근 12개월 부서 이동 · 직급 변경.

    출처 두 곳: 변경 이력(field_changes, B1 부터) + 승인된 인사 발령(personnel_actions, 그 전 것).
    같은 발령이 두 곳에 다 있으면(ref 'pa:<id>') 한 번만 센다. 신규 입사 때 처음 채운 값은 이동이 아니다.
    """
    people_history.ensure_schema(db)
    y0 = (today - timedelta(days=365)).isoformat()
    y1 = (today + timedelta(days=1)).isoformat()
    moves = []
    linked = set()
    for r in db.execute(
            "SELECT fc.user_id, fc.field, fc.old_value, fc.new_value, fc.created_at, fc.ref, "
            "u.name, u.emp_no, u.status FROM field_changes fc JOIN users u ON u.id = fc.user_id "
            "WHERE fc.field IN ('department_id','position_id') AND fc.created_at >= ? AND fc.created_at < ? "
            "AND COALESCE(fc.old_value,'') != '' AND COALESCE(fc.new_value,'') != '' "
            "AND fc.old_value != fc.new_value", (y0, y1)).fetchall():
        if r['ref'] and r['ref'].startswith('pa:'):
            linked.add(r['ref'])
        moves.append({'uid': r['user_id'], 'kind': 'dept' if r['field'] == 'department_id' else 'grade',
                      'old': r['old_value'], 'new': r['new_value'], 'at': (r['created_at'] or '')[:10],
                      'name': r['name'], 'emp_no': r['emp_no'] or '', 'left': r['status'] != 'active'})
    try:
        pas = db.execute(
            "SELECT pa.id, pa.user_id, pa.action_type, pa.from_value, pa.to_value, pa.effective_date, "
            "u.name, u.emp_no, u.status FROM personnel_actions pa JOIN users u ON u.id = pa.user_id "
            "WHERE pa.status='approved' AND pa.action_type IN ('dept_change','position_change') "
            "AND pa.effective_date >= ? AND pa.effective_date < ?", (y0, y1)).fetchall()
    except Exception:
        pas = []
    for r in pas:
        if 'pa:%s' % r['id'] in linked:
            continue
        f = 'department_id' if r['action_type'] == 'dept_change' else 'position_id'
        new = (r['to_value'] or '').split('|')[0]
        moves.append({'uid': r['user_id'], 'kind': 'dept' if f == 'department_id' else 'grade',
                      'old': people_history.readable(db, f, r['from_value'] or ''),
                      'new': people_history.readable(db, f, new),
                      'at': (r['effective_date'] or '')[:10],
                      'name': r['name'], 'emp_no': r['emp_no'] or '', 'left': r['status'] != 'active'})
    moves.sort(key=lambda m: m['at'], reverse=True)
    movers = {m['uid'] for m in moves}
    dept_p = {m['uid'] for m in moves if m['kind'] == 'dept'}
    grade_p = {m['uid'] for m in moves if m['kind'] == 'grade'}
    rate = round(len(movers) * 100.0 / avg_headcount, 1) if avg_headcount else 0.0
    try:
        since = db.execute('SELECT MIN(created_at) FROM field_changes').fetchone()[0]
    except Exception:
        since = None
    return {'movers': len(movers), 'dept': len(dept_p), 'grade': len(grade_p),
            'events': len(moves), 'rate': rate, 'recent': moves[:12],
            'since': (since or '')[:10]}


def _invol(rtype, cat, text):
    if rtype in _INVOL_TYPES or cat == 'involuntary':
        return True
    if rtype in ('voluntary', 'mutual'):
        return False
    t = text or ''
    return any(w in t for w in _INVOL_WORDS)


def exit_reasons(db, today, labels, months=24):
    """최근 N개월 퇴사자 사유.

    퇴사자 기준은 users(status='resigned' + 퇴사일) — 퇴직 신청(termination_requests)을 거치지 않고
    처리된 퇴사·옛 기록도 빠지지 않게. 신청서가 있으면 그 원인 분류를 먼저 쓰고, 없으면 퇴사 사유 글을 쓴다.
    """
    y0 = (today - timedelta(days=int(months * 30.44))).isoformat()
    y1 = today.isoformat()
    rows = db.execute(
        "SELECT u.id, u.name, u.emp_no, u.hire_date, u.termination_date, u.termination_reason, "
        "d.name AS dept FROM users u LEFT JOIN departments d ON d.id = u.department_id "
        "WHERE u.status='resigned' AND u.termination_date > ? AND u.termination_date <= ?",
        (y0, y1)).fetchall()
    tr = {}
    try:
        for r in db.execute(
                "SELECT user_id, request_type, exit_reason_category, reason_code, is_regrettable "
                "FROM termination_requests WHERE status='completed' ORDER BY id"):
            tr[r['user_id']] = r
    except Exception:
        pass
    groups, people = {}, []
    vol = invol = early = blank = 0
    for u in rows:
        t = tr.get(u['id'])
        cat = (t['exit_reason_category'] or t['reason_code']) if t else None
        text = (u['termination_reason'] or '').strip()
        if cat:
            label = labels.get(cat, cat)
        elif text:
            label = text[:24]
        else:
            label = '사유 미기재'
            blank += 1
        iv = _invol(t['request_type'] if t else None, cat, text)
        months_in = None
        if u['hire_date'] and u['termination_date']:
            try:
                from datetime import date
                hd, td = date.fromisoformat(u['hire_date'][:10]), date.fromisoformat(u['termination_date'][:10])
                months_in = (td.year - hd.year) * 12 + (td.month - hd.month)
            except ValueError:
                pass
        if months_in is not None and months_in < 12:
            early += 1
        if iv:
            invol += 1
        else:
            vol += 1
        g = groups.setdefault(label, {'label': label, 'cnt': 0, 'invol': iv, 'early': 0, 'tenure': []})
        g['cnt'] += 1
        if months_in is not None:
            g['tenure'].append(months_in)
            if months_in < 12:
                g['early'] += 1
        people.append({'name': u['name'], 'emp_no': u['emp_no'] or '', 'dept': u['dept'] or '—',
                       'date': (u['termination_date'] or '')[:10], 'reason': label,
                       'invol': iv, 'months': months_in})
    total = len(rows)
    out = []
    for g in sorted(groups.values(), key=lambda g: (-g['cnt'], g['label'])):
        ts = g.pop('tenure')
        g['avg_months'] = round(sum(ts) * 1.0 / len(ts)) if ts else None
        g['pct'] = round(g['cnt'] * 100.0 / total) if total else 0
        out.append(g)
    people.sort(key=lambda p: p['date'], reverse=True)
    return {'total': total, 'months': months, 'vol': vol, 'invol': invol, 'early': early,
            'blank': blank, 'groups': out, 'people': people,
            'early_pct': round(early * 100.0 / total) if total else 0}
