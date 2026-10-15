"""직원 개인 정보·연락처(B2).

  - users 에 칸을 더한다: 부르는 이름·직함·사진·회사 전화·개인 이메일·자리·시간대·계정 잠금.
  - 비상 연락처는 여러 명(emergency_contacts). 첫째(대표)는 옛 칸 users.emergency_* 에도 같이 적는다
    (다른 화면·내보내기가 아직 옛 칸을 읽는다).
  - 모든 사람은 사번(emp_no)으로 찾는다 — 사진 파일 이름·사진 주소·엑셀 맞춤 열쇠 전부 사번.
  - 테넌트 DB 는 init_db 를 거치지 않으므로 ensure_schema() 를 연결마다(경로당 한 번) 부른다.
"""
import os
import secrets
import sqlite3

USER_COLS = (
    ('preferred_name', 'TEXT'),
    ('job_title', 'TEXT'),
    ('photo', 'TEXT'),
    ('work_phone', 'TEXT'),
    ('personal_email', 'TEXT'),
    ('seat_location', 'TEXT'),
    ('timezone', "TEXT DEFAULT 'Asia/Seoul'"),
    ('locked_at', 'TEXT'),
    ('lock_reason', 'TEXT'),
    ('failed_logins', 'INTEGER DEFAULT 0'),
)

_DDL = (
    '''CREATE TABLE IF NOT EXISTS emergency_contacts (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        name        TEXT NOT NULL,
        relation    TEXT,
        phone       TEXT NOT NULL,
        sort        INTEGER NOT NULL DEFAULT 0,
        created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )''',
    'CREATE INDEX IF NOT EXISTS idx_emergency_contacts_user ON emergency_contacts(user_id, sort)',
)

MAX_EMERGENCY = 3
LOCK_AFTER = 10          # 비밀번호 연속 실패 이만큼이면 잠금

TIMEZONES = (
    ('Asia/Seoul', '한국 (서울)'), ('Asia/Tokyo', '일본 (도쿄)'), ('Asia/Shanghai', '중국 (상하이)'),
    ('Asia/Singapore', '싱가포르'), ('Asia/Ho_Chi_Minh', '베트남 (호찌민)'), ('Asia/Kolkata', '인도'),
    ('Europe/London', '영국 (런던)'), ('Europe/Berlin', '독일 (베를린)'),
    ('America/New_York', '미국 동부'), ('America/Los_Angeles', '미국 서부'),
)
TZ_LABEL = dict(TIMEZONES)

# 동료에게 보일 수 있는 칸(공개 범위 설정). 이름·부서·사진·회사 이메일은 늘 보인다.
PUBLIC_CHOICES = (
    ('job_title', '직함'), ('work_phone', '회사 전화'), ('seat_location', '자리'),
    ('phone', '휴대폰'), ('personal_email', '개인 이메일'), ('birth_date', '생년월일'),
    ('timezone', '시간대'),
)
PUBLIC_DEFAULT = ('job_title', 'work_phone', 'seat_location')

_done = set()


def ensure_schema(c, key=None):
    """칸·표가 없으면 만든다. 같은 DB 는 프로세스당 한 번만 확인(key = DB 경로)."""
    if key is not None and key in _done:
        return
    have = {r[1] for r in c.execute('PRAGMA table_info(users)').fetchall()}
    if not have:
        return
    for col, typ in USER_COLS:
        if col in have:
            continue
        try:
            c.execute('ALTER TABLE users ADD COLUMN %s %s' % (col, typ))
        except sqlite3.OperationalError as e:
            # 워커 두 개가 동시에 뜨면 한쪽이 먼저 넣는다.
            if 'duplicate column' not in str(e).lower():
                raise
    for q in _DDL:
        c.execute(q)
    c.commit()
    if key is not None:
        _done.add(key)


# ── 비상 연락처 ─────────────────────────────────────────────
def emergency_list(c, uid):
    rows = c.execute('SELECT id, name, relation, phone FROM emergency_contacts '
                     'WHERE user_id=? ORDER BY sort, id', (uid,)).fetchall()
    out = [dict(id=r[0], name=r[1], relation=r[2] or '', phone=r[3]) for r in rows]
    if not out:
        # 옛 칸에만 있는 사람(이 기능 전 입력)
        r = c.execute('SELECT emergency_name, emergency_relation, emergency_phone FROM users WHERE id=?',
                      (uid,)).fetchone()
        if r and (r[0] or r[2]):
            out.append(dict(id=None, name=r[0] or '', relation=r[1] or '', phone=r[2] or ''))
    return out


def emergency_from_form(form):
    """폼의 ec_name/ec_relation/ec_phone 여러 줄 → 목록. (목록, 오류문)"""
    names = form.getlist('ec_name')
    rels = form.getlist('ec_relation')
    phones = form.getlist('ec_phone')
    out = []
    for i, n in enumerate(names):
        n = (n or '').strip()
        rel = (rels[i] if i < len(rels) else '').strip()
        ph = (phones[i] if i < len(phones) else '').strip()
        if not n and not ph:
            continue
        if not n or not ph:
            return None, '비상 연락처는 이름과 번호를 함께 적어 주세요.'
        out.append(dict(name=n[:40], relation=rel[:20], phone=ph[:30]))
    if len(out) > MAX_EMERGENCY:
        return None, '비상 연락처는 %d명까지 적을 수 있습니다.' % MAX_EMERGENCY
    return out, None


def _ec_text(lst):
    return ' / '.join('%s(%s) %s' % (x['name'], x['relation'] or '-', x['phone']) if x.get('relation')
                      else '%s %s' % (x['name'], x['phone']) for x in lst)


def emergency_save(c, uid, lst):
    """목록을 통째로 바꾼다. 첫째는 옛 칸에도. 바뀌었으면 (전, 후) 글, 아니면 None."""
    before = emergency_list(c, uid)
    old, new = _ec_text(before), _ec_text(lst)
    c.execute('DELETE FROM emergency_contacts WHERE user_id=?', (uid,))
    for i, x in enumerate(lst):
        c.execute('INSERT INTO emergency_contacts (user_id, name, relation, phone, sort) VALUES (?,?,?,?,?)',
                  (uid, x['name'], x['relation'] or None, x['phone'], i))
    first = lst[0] if lst else {}
    c.execute('UPDATE users SET emergency_name=?, emergency_relation=?, emergency_phone=? WHERE id=?',
              (first.get('name') or None, first.get('relation') or None, first.get('phone') or None, uid))
    return (old, new) if old != new else None


# ── 사진 ────────────────────────────────────────────────────
PHOTO_MAX = 400 * 1024


def photo_dir(base, tenant_id):
    d = os.path.join(base, 'private_uploads', 'photos', 't%d' % int(tenant_id))
    os.makedirs(d, exist_ok=True)
    return d


def photo_kind(data):
    """JPEG/PNG 만. 확장자 또는 None."""
    if data[:3] == b'\xff\xd8\xff':
        return 'jpg'
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return 'png'
    return None


def photo_save(c, base, tenant_id, uid, data):
    """검사 후 저장, users.photo 갱신. (파일 이름, 오류문)"""
    if not data:
        return None, '사진 파일이 비어 있습니다.'
    if len(data) > PHOTO_MAX:
        return None, '사진이 너무 큽니다(400KB 이하).'
    ext = photo_kind(data)
    if not ext:
        return None, 'JPG 또는 PNG 사진만 올릴 수 있습니다.'
    d = photo_dir(base, tenant_id)
    name = '%d_%s.%s' % (uid, secrets.token_hex(6), ext)
    with open(os.path.join(d, name), 'wb') as f:
        f.write(data)
    old = c.execute('SELECT photo FROM users WHERE id=?', (uid,)).fetchone()
    c.execute('UPDATE users SET photo=? WHERE id=?', (name, uid))
    if old and old[0] and old[0] != name:
        try:
            os.remove(os.path.join(d, os.path.basename(old[0])))
        except OSError:
            pass
    return name, None


def photo_remove(c, base, tenant_id, uid):
    old = c.execute('SELECT photo FROM users WHERE id=?', (uid,)).fetchone()
    if not old or not old[0]:
        return False
    c.execute('UPDATE users SET photo=NULL WHERE id=?', (uid,))
    try:
        os.remove(os.path.join(photo_dir(base, tenant_id), os.path.basename(old[0])))
    except OSError:
        pass
    return True


# ── 공개 범위 ───────────────────────────────────────────────
def public_fields(raw):
    if raw is None:
        return set(PUBLIC_DEFAULT)
    ok = {k for k, _ in PUBLIC_CHOICES}
    return {x.strip() for x in str(raw).split(',') if x.strip() in ok}


# ── 폼 → 칸 ────────────────────────────────────────────────
_TEXT = (('preferred_name', 40), ('work_phone', 30), ('personal_email', 120), ('seat_location', 60))


def parse_form(form, admin):
    """폼에서 B2 칸을 읽는다. 폼에 없는 칸은 건드리지 않는다. ({칸: 값}, 비상 연락처 목록 또는 None, 오류문)"""
    fields = {}
    for k, n in _TEXT:
        if k in form:
            fields[k] = (form.get(k) or '').strip()[:n] or None
    if admin and 'job_title' in form:
        fields['job_title'] = (form.get('job_title') or '').strip()[:60] or None
    if 'timezone' in form:
        tz = form.get('timezone') or ''
        fields['timezone'] = tz if tz in TZ_LABEL else 'Asia/Seoul'
    pe = fields.get('personal_email')
    if pe and ('@' not in pe or ' ' in pe):
        return None, None, '개인 이메일 형식이 올바르지 않습니다.'
    ecs = None
    if form.get('ec_present'):
        ecs, err = emergency_from_form(form)
        if err:
            return None, None, err
    return fields, ecs, None


def apply_fields(c, uid, fields):
    if fields:
        keys = list(fields)
        c.execute('UPDATE users SET %s WHERE id=?' % ', '.join('%s=?' % k for k in keys),
                  [fields[k] for k in keys] + [uid])
