"""TalentCore Copilot — 평가·보상 작성 보조 (C5).

제공자는 Anthropic Claude (Messages API). 서버 환경변수 ANTHROPIC_API_KEY 가 있을 때만 호출하고,
키가 없거나 호출이 실패하면 규칙 기반 결과를 돌려준다.
표현 점검·점수-코멘트 불일치·평가 패턴 같은 규칙 점검은 키와 무관하게 항상 실행한다.

원칙
- 점수는 제안하지 않는다 (초안이 평가자의 판단을 끌고 가지 않도록 코멘트 초안·근거만).
- 외부로 보내는 데이터에는 이름·사번·이메일·연락처를 넣지 않는다.
- 호출하는 쪽(app.py)이 열람 권한(피드백 공개 범위·동료 평가 익명 기준)을 통과한 근거만 넘긴다.
"""
import json
import os
import re
import urllib.request

API_URL = 'https://api.anthropic.com/v1/messages'
API_VERSION = '2023-06-01'
DEFAULT_MODEL = 'claude-sonnet-5'

SENT_FIELDS = {
    'review_draft': '확정 목표 제목·진행률·자기평가 코멘트·최근 체크인, 역량 이름·자기평가 코멘트, '
                    '평가자가 열람 가능한 피드백 본문, 익명 동료 평가 서술(3명 이상일 때)',
    'bias_check':   '작성 중인 평가 코멘트와 해당 점수',
    'raise_note':   '성과 등급·Compa·권장/적용 인상률·밴드 위치',
    'goal_assist':  '목표 제목·유형',
}
FEATURE_LABEL = {'review_draft': '평가 초안', 'bias_check': '표현 점검',
                 'raise_note': '인상 근거 초안', 'goal_assist': '목표 SMART 분석'}


def model():
    return os.environ.get('COPILOT_MODEL') or DEFAULT_MODEL


def connected():
    return bool(os.environ.get('ANTHROPIC_API_KEY'))


def status():
    return {'provider': 'Claude', 'model': model(), 'connected': connected(),
            'sent_fields': SENT_FIELDS, 'feature_label': FEATURE_LABEL}


def claude_json(system, user, max_tokens=900):
    """Claude 호출 → 응답 텍스트의 JSON 객체. 키 없음·네트워크 오류·형식 오류는 모두 None."""
    key = os.environ.get('ANTHROPIC_API_KEY', '')
    if not key:
        return None
    body = json.dumps({'model': model(), 'max_tokens': max_tokens, 'system': system,
                       'messages': [{'role': 'user', 'content': user}]}).encode()
    req = urllib.request.Request(API_URL, data=body, method='POST', headers={
        'x-api-key': key, 'anthropic-version': API_VERSION, 'content-type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            resp = json.loads(r.read())
        text = ''.join(b.get('text', '') for b in resp.get('content', []) if b.get('type') == 'text')
        s, e = text.find('{'), text.rfind('}')
        if s < 0 or e <= s:
            return None
        out = json.loads(text[s:e + 1])
        return out if isinstance(out, dict) else None
    except Exception:
        return None


def _clip(s, n):
    s = re.sub(r'\s+', ' ', str(s or '')).strip()
    return s if len(s) <= n else s[:n - 1].rstrip() + '…'


def _strs(v, n=160, limit=5):
    return [_clip(x, n) for x in (v or []) if isinstance(x, str) and x.strip()][:limit]


# ── 목표 SMART 분석 ─────────────────────────────────────────────
def _goal_rule(title, category):
    smart = {
        'S': any(w in title for w in ['달성', '개선', '완료', '구축', '구현', '감소', '증가', '확보', '작성', '수립']),
        'M': bool(re.search(r'\d', title)) or '%' in title,
        'A': len(title) > 5,
        'R': True,
        'T': any(w in title for w in ['분기', '반기', '월', '주', '연간', 'Q1', 'Q2', 'Q3', 'Q4', '상반기', '하반기', '까지', '이내']),
    }
    missing = [k for k, v in smart.items() if not v]
    tips = []
    if not smart['M']:
        tips.append('측정 가능한 수치를 추가하세요. 예: "20% 향상", "3건 완료", "90점 이상"')
    if not smart['T']:
        tips.append('기간을 명시하세요. 예: "Q2 말까지", "6월 30일까지", "상반기 내"')
    if not smart['S']:
        tips.append('구체적인 행동 동사를 사용하세요. 예: "달성", "구축", "개선", "완료"')
    if not tips:
        tips.append('목표가 비교적 잘 작성되었습니다. 측정 기준을 설명란에 구체적으로 적어보세요.')
    improved = title
    if not smart['M']:
        improved += ' (수치 목표 추가 필요)'
    if not smart['T']:
        improved += ' — Q2 말까지' if category == 'KPI' else ' — 상반기 내'
    return {'improved_title': improved,
            'reason': ('·'.join(missing) + ' 기준이 부족합니다.') if missing else 'SMART 기준을 대체로 충족합니다.',
            'smart_check': smart, 'tips': tips, 'source': 'rule'}


def goal_assist(title, category='KPI', job=''):
    rule = _goal_rule(title, category)
    res = claude_json(
        '당신은 한국 기업의 성과관리 담당자입니다. 직원이 쓴 목표를 SMART 기준에 맞게 다듬습니다. '
        '과장하지 말고 원래 의도를 유지하세요. 반드시 JSON 객체 하나만 출력합니다.',
        f'직무: {_clip(job, 60) or "미입력"}\n목표 유형: {category}\n현재 목표: {_clip(title, 300)}\n\n'
        '형식: {"improved_title": "...", "reason": "1~2문장", '
        '"smart_check": {"S": true, "M": true, "A": true, "R": true, "T": true}, "tips": ["...", "..."]}',
        max_tokens=600)
    if res and isinstance(res.get('improved_title'), str) and res['improved_title'].strip():
        sc = res.get('smart_check') if isinstance(res.get('smart_check'), dict) else {}
        return {'improved_title': _clip(res['improved_title'], 300),
                'reason': _clip(res.get('reason'), 300) or rule['reason'],
                'smart_check': {k: bool(sc.get(k, rule['smart_check'][k])) for k in 'SMART'},
                'tips': _strs(res.get('tips'), 200, 4) or rule['tips'], 'source': 'claude'}
    return rule


# ── 평가 초안 ──────────────────────────────────────────────────
CHECKIN_WORD = {'on_track': '순항', 'at_risk': '주의', 'off_track': '위험'}


def _progress_sentence(p):
    p = int(p or 0)
    if p >= 100:
        return f'목표를 달성했습니다(진행률 {p}%).'
    if p >= 80:
        return f'목표 대부분을 달성했습니다(진행률 {p}%).'
    if p >= 50:
        return f'목표를 부분 달성했습니다(진행률 {p}%).'
    return f'목표 달성이 미흡합니다(진행률 {p}%).'


def _review_rule(ev):
    goals, strengths, growth = [], [], []
    for g in ev.get('goals', []):
        parts = [_progress_sentence(g.get('progress'))]
        ck = g.get('checkin')
        if ck and ck.get('status') in ('at_risk', 'off_track'):
            s = f'최근 체크인에서 {CHECKIN_WORD[ck["status"]]}로 보고되었습니다'
            if ck.get('comment'):
                s += f' — {_clip(ck["comment"], 60)}'
            parts.append(s + '.')
            growth.append({'text': f'「{_clip(g["title"], 40)}」 {CHECKIN_WORD[ck["status"]]} 보고',
                           'basis': f'목표 체크인 · {(ck.get("date") or "")[:10]}'})
        if g.get('self_comment'):
            parts.append(f'본인 서술: “{_clip(g["self_comment"], 80)}”')
        parts.append('관찰한 결과물·사례를 한 줄 덧붙이세요.')
        goals.append({'id': g['id'], 'draft': ' '.join(parts)})
        if int(g.get('progress') or 0) >= 100:
            strengths.append({'text': f'「{_clip(g["title"], 40)}」 목표 달성', 'basis': '진행률 100%'})
    comps = []
    for c in ev.get('competencies', []):
        s = f'본인 서술: “{_clip(c["self_comment"], 80)}” ' if c.get('self_comment') else ''
        comps.append({'key': c['key'], 'draft': s + f'「{c["name"]}」이 드러난 구체적 행동과 그 결과를 적으세요.'})
    for f in ev.get('feedback', []):
        item = {'text': _clip(f['content'], 90)}
        if f['kind'] == 'praise':
            strengths.append({**item, 'basis': '칭찬 피드백'})
        elif f['kind'] == 'suggest':
            growth.append({**item, 'basis': '개선 제안 피드백'})
    for p in ev.get('peer', []):
        if p.get('strength'):
            strengths.append({'text': _clip(p['strength'], 90), 'basis': f'동료 평가 · {ev.get("peer_labels", ["", "", ""])[0] or "강점"} · 익명'})
        if p.get('improvement'):
            growth.append({'text': _clip(p['improvement'], 90), 'basis': f'동료 평가 · {ev.get("peer_labels", ["", "", ""])[2] or "개선"} · 익명'})
    notes = []
    if not ev.get('feedback') and not ev.get('peer'):
        notes.append('이번 주기에 열람 가능한 피드백·동료 평가가 없어 목표 진행 기록만으로 작성했습니다.')
    if ev.get('peer_hidden'):
        notes.append(f'동료 평가 {ev["peer_hidden"]}건은 익명 기준(3명 이상) 미달로 제외했습니다.')
    if ev.get('open_actions'):
        notes.append(f'1:1 미완료 액션 {ev["open_actions"]}건이 있습니다.')
    return {'goals': goals, 'competencies': comps, 'strengths': strengths[:5], 'growth': growth[:5],
            'notes': notes, 'source': 'rule'}


def review_draft(ev):
    """ev: app.py 가 권한 범위 안에서 모은 근거 (이름 없음). 점수 제안 없음."""
    rule = _review_rule(ev)
    payload = {
        'goals': [{'id': g['id'], 'title': _clip(g['title'], 120), 'progress': g.get('progress'),
                   'self_comment': _clip(g.get('self_comment'), 300),
                   'checkin': g.get('checkin')} for g in ev.get('goals', [])],
        'competencies': [{'key': c['key'], 'name': c['name'], 'desc': c.get('desc', ''),
                          'self_comment': _clip(c.get('self_comment'), 300)} for c in ev.get('competencies', [])],
        'feedback': [{'kind': f['kind'], 'content': _clip(f['content'], 300)} for f in ev.get('feedback', [])][:20],
        'peer': [{k: _clip(p.get(k), 300) for k in ('strength', 'comment', 'improvement')} for p in ev.get('peer', [])][:10],
        'peer_labels': ev.get('peer_labels'),
    }
    res = claude_json(
        '당신은 한국 기업의 평가 코치입니다. 매니저가 팀원 평가 코멘트를 쓰도록 근거 기반 초안을 만듭니다. '
        '규칙: 점수·등급을 제안하지 않는다. 근거에 없는 사실을 만들지 않는다. 성격·외모·성별·나이·가족 관련 표현을 쓰지 않는다. '
        '관찰 가능한 행동과 결과로 쓴다. 각 초안은 2~3문장, 존댓말 아닌 평서체(~했습니다). JSON 객체 하나만 출력한다.',
        '근거(JSON):\n' + json.dumps(payload, ensure_ascii=False) + '\n\n'
        '형식: {"goals": [{"id": 목표id, "draft": "..."}], "competencies": [{"key": "...", "draft": "..."}], '
        '"strengths": [{"text": "...", "basis": "근거 출처"}], "growth": [{"text": "...", "basis": "근거 출처"}]}',
        max_tokens=1600)
    if not res:
        return rule
    gids = {g['id'] for g in ev.get('goals', [])}
    ckeys = {c['key'] for c in ev.get('competencies', [])}
    goals = [{'id': int(x['id']), 'draft': _clip(x.get('draft'), 600)} for x in res.get('goals') or []
             if isinstance(x, dict) and str(x.get('id', '')).isdigit() and int(x['id']) in gids and x.get('draft')]
    comps = [{'key': x['key'], 'draft': _clip(x.get('draft'), 600)} for x in res.get('competencies') or []
             if isinstance(x, dict) and x.get('key') in ckeys and x.get('draft')]
    if not goals and gids:
        return rule

    def _items(v):
        return [{'text': _clip(x.get('text'), 160), 'basis': _clip(x.get('basis'), 60)}
                for x in (v or []) if isinstance(x, dict) and x.get('text')][:5]
    have_g = {g['id'] for g in goals}
    have_c = {c['key'] for c in comps}
    return {'goals': goals + [g for g in rule['goals'] if g['id'] not in have_g],
            'competencies': comps + [c for c in rule['competencies'] if c['key'] not in have_c],
            'strengths': _items(res.get('strengths')) or rule['strengths'],
            'growth': _items(res.get('growth')) or rule['growth'],
            'notes': rule['notes'], 'source': 'claude'}


# ── 표현 점검 ──────────────────────────────────────────────────
BIAS_RULES = [
    ('personal', 'late', '개인 특성(성별·나이·외모·가족)을 가리키는 표현입니다. 업무 행동과 결과로 바꾸세요.',
     ['여자', '여성', '남자', '남성', '아줌마', '아저씨', '애교', '싹싹', '외모', '예쁘', '잘생', '나이', '젊은', '늙',
      '어린 ', '결혼', '출산', '육아', '임신', '엄마', '아빠', '애 엄마', '군필', '여직원', '남직원']),
    ('personality', 'wait', '성격을 판단하는 표현입니다. 관찰한 행동으로 서술하세요.',
     ['성격', '감정적', '예민', '드세', '공격적', '소극적', '까칠', '고집', '이기적', '눈치', '기가 세']),
    ('absolute', 'wait', '단정하는 표현입니다. 빈도·사례로 바꾸세요.',
     ['항상', '절대', '전혀', '매번', '한 번도', '늘 ']),
]
VAGUE_TERMS = ['열심히', '성실', '태도', '노력', '적극적', '무난', '최선', '책임감', '잘함', '좋음']
NEG_TERMS = ['미흡', '부족', '아쉽', '못했', '못 했', '저조', '실패', '개선 필요', '개선이 필요', '지연', '누락']
POS_TERMS = ['우수', '탁월', '훌륭', '뛰어', '잘했', '성공', '초과 달성', '기대 이상', '모범']


def _hits(text, terms):
    return [t.strip() for t in terms if t in text]


def _check_rule(items):
    warns = []
    for it in items:
        text, field = (it.get('text') or '').strip(), it.get('field')
        score = it.get('score')
        for kind, level, msg, terms in BIAS_RULES:
            h = _hits(text, terms)
            if h:
                warns.append({'field': field, 'kind': kind, 'level': level, 'msg': msg, 'hits': h})
        v = _hits(text, VAGUE_TERMS)
        if v and not re.search(r'\d', text):
            warns.append({'field': field, 'kind': 'vague', 'level': 'wait', 'hits': v,
                          'msg': '근거 없이 일반적인 표현입니다. 결과·사례·수치를 덧붙이세요.'})
        if score:
            neg, pos = _hits(text, NEG_TERMS), _hits(text, POS_TERMS)
            if score >= 4 and neg:
                warns.append({'field': field, 'kind': 'mismatch', 'level': 'late', 'hits': neg,
                              'msg': f'{score}점인데 코멘트가 부정적입니다. 점수와 코멘트 중 하나를 맞추세요.'})
            elif score <= 2 and pos and not neg:
                warns.append({'field': field, 'kind': 'mismatch', 'level': 'late', 'hits': pos,
                              'msg': f'{score}점인데 코멘트가 긍정적입니다. 점수와 코멘트 중 하나를 맞추세요.'})
            if score in (1, 5) and len(text) < 20:
                warns.append({'field': field, 'kind': 'extreme', 'level': 'wait', 'hits': [],
                              'msg': f'{score}점은 보정 회의에서 설명할 근거가 필요합니다. 코멘트를 20자 이상 적으세요.'})
    return warns


def rating_pattern(rows, min_n=3, gap=0.5):
    """이 평가자가 이번 주기에 저장한 팀원별 평균 점수 → 성별 평균 차이. rows: [{'gender': 'M'|'F', 'avg': float}]"""
    groups = {}
    for r in rows:
        if r.get('gender') in ('M', 'F') and r.get('avg') is not None:
            groups.setdefault(r['gender'], []).append(float(r['avg']))
    out = {k: {'n': len(v), 'avg': round(sum(v) / len(v), 2)} for k, v in groups.items()}
    warn = (all(out.get(k, {}).get('n', 0) >= min_n for k in 'MF')
            and abs(out['M']['avg'] - out['F']['avg']) >= gap)
    msg = ''
    if warn:
        msg = (f'이번 주기 내 저장된 평가 평균 — 남성 {out["M"]["avg"]:.1f}점({out["M"]["n"]}명) · '
               f'여성 {out["F"]["avg"]:.1f}점({out["F"]["n"]}명), 차이 {abs(out["M"]["avg"] - out["F"]["avg"]):.1f}점. '
               '팀원별 근거를 다시 확인하세요.')
    return {'groups': out, 'warn': warn, 'msg': msg}


def bias_check(items):
    """items: [{'field', 'label', 'text', 'score'}] — 규칙 점검 + (연결 시) Claude 문장 제안."""
    items = [it for it in items if (it.get('text') or '').strip() or it.get('score') in (1, 5)]
    warns = _check_rule(items)
    source = 'rule'
    texts = [{'field': it['field'], 'score': it.get('score'), 'text': _clip(it['text'], 600)}
             for it in items if (it.get('text') or '').strip()]
    if texts:
        res = claude_json(
            '당신은 인사평가 문장 검토자입니다. 코멘트에서 편향(성별·나이·외모·가족·성격 판단), 근거 없는 모호함, '
            '점수와 어긋나는 톤을 찾고, 같은 뜻을 관찰 가능한 행동·결과로 바꾼 문장을 제안합니다. '
            '문제가 없으면 목록에 넣지 않습니다. JSON 객체 하나만 출력합니다.',
            json.dumps(texts, ensure_ascii=False) + '\n\n형식: {"warnings": [{"field": "...", "msg": "무엇이 문제인지 한 문장", '
            '"suggestion": "바꾼 문장"}]}', max_tokens=1200)
        if res is not None:
            source = 'claude'
            fields = {t['field'] for t in texts}
            for x in res.get('warnings') or []:
                if not isinstance(x, dict) or x.get('field') not in fields:
                    continue
                sug = _clip(x.get('suggestion'), 600)
                same = next((w for w in warns if w['field'] == x['field'] and not w.get('suggestion')), None)
                if same and sug:
                    same['suggestion'] = sug
                elif x.get('msg'):
                    warns.append({'field': x['field'], 'kind': 'claude', 'level': 'wait', 'hits': [],
                                  'msg': _clip(x['msg'], 200), 'suggestion': sug})
    return {'warnings': warns, 'source': source}


# ── 인상 근거 초안 ──────────────────────────────────────────────
def _band_word(compa):
    if compa is None:
        return '밴드 미등록'
    return '밴드 하단' if compa < 0.9 else ('밴드 상단' if compa > 1.1 else '밴드 중간')


def _raise_rule(c):
    pct, sug = float(c.get('pct') or 0), c.get('suggested_pct')
    parts = [f'성과 등급 {c["grade"]}' if c.get('grade') else '성과 등급 없음']
    if c.get('compa'):
        parts.append(f'현재 Compa {c["compa"]:.2f}({_band_word(c["compa"])})')
    if sug is not None:
        parts.append(f'인상 매트릭스 권장 {sug:+.1f}% 대비 {pct:+.1f}% 적용' if abs(pct - sug) > 0.05
                     else f'인상 매트릭스 권장 {pct:+.1f}% 적용')
    else:
        parts.append(f'{pct:+.1f}% 적용')
    if c.get('compa_new'):
        parts.append(f'인상 후 Compa {c["compa_new"]:.2f}')
    warns = []
    if sug is not None and abs(pct - sug) > 1.0 and len((c.get('note') or '').strip()) < 10:
        warns.append({'kind': 'deviation', 'level': 'late',
                      'msg': f'권장 대비 {pct - sug:+.1f}%p 차이 — 조정 사유(성과 사례·시장 수준·역할 변화)를 구체적으로 적으세요.'})
    annual = int(c.get('new_salary') or 0) * 12
    if c.get('band_max') and annual > c['band_max']:
        warns.append({'kind': 'band_max', 'level': 'late', 'msg': '인상 후 연봉이 직급 밴드 상한을 넘습니다.'})
    if c.get('band_min') and annual and annual < c['band_min']:
        warns.append({'kind': 'band_min', 'level': 'wait', 'msg': '인상 후에도 연봉이 직급 밴드 하한 미만입니다.'})
    return {'text': ' · '.join(parts), 'warnings': warns, 'source': 'rule'}


def raise_note(ctx):
    rule = _raise_rule(ctx)
    payload = {k: ctx.get(k) for k in ('grade', 'compa', 'compa_new', 'pct', 'suggested_pct', 'note')}
    res = claude_json(
        '당신은 보상 담당자입니다. 부서장이 HR에 제출할 인상률 사유를 한 문장(90자 이내)으로 씁니다. '
        '주어진 수치만 쓰고 이름·추측을 넣지 않습니다. JSON 객체 하나만 출력합니다.',
        json.dumps(payload, ensure_ascii=False) + '\n\n형식: {"text": "..."}', max_tokens=300)
    if res and isinstance(res.get('text'), str) and res['text'].strip():
        return {**rule, 'text': _clip(res['text'], 200), 'source': 'claude'}
    return rule
