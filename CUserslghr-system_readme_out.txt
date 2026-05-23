=== hr-system/README.md ===
# CODING AGENTS: READ THIS FIRST

This is a **handoff bundle** from Claude Design (claude.ai/design).

A user mocked up designs in HTML/CSS/JS using an AI design tool, then exported this bundle so a coding agent can implement the designs for real.

## What you should do — IMPORTANT

**Read the chat transcripts first.** There are 2 chat transcript(s) in `hr-system/chats/`. The transcripts show the full back-and-forth between the user and the design assistant — they tell you **what the user actually wants** and **where they landed** after iterating. Don't skip them. The final HTML files are the output, but the chat is where the intent lives.

**Read `hr-system/project/Landing.html` in full.** The user had this file open when they triggered the handoff, so it's almost certainly the primary design they want built. Read it top to bottom — don't skim. Then **follow its imports**: open every file it pulls in (shared components, CSS, scripts) so you understand how the pieces fit together before you start implementing.

**If anything is ambiguous, ask the user to confirm before you start implementing.** It's much cheaper to clarify scope up front than to build the wrong thing.

## About the design files

The design medium is **HTML/CSS/JS** — these are prototypes, not production code. Your job is to **recreate them pixel-perfectly** in whatever technology makes sense for the target codebase (React, Vue, native, whatever fits). Match the visual output; don't copy the prototype's internal structure unless it happens to fit.

**Don't render these files in a browser or take screenshots unless the user asks you to.** Everything you need — dimensions, colors, layout rules — is spelled out in the source. Read the HTML and CSS directly; a screenshot won't tell you anything they don't.

## Bundle contents

- `hr-system/README.md` — this file
- `hr-system/chats/` — conversation transcripts (read these!)
- `hr-system/project/` — the `hr-system` project files (HTML prototypes, assets, components)


=== hr-system/project/flask-kit/README.md ===
# Flask + Jinja2 적용 가이드

## 📁 폴더 구조

본인 Flask 프로젝트는 이런 구조여야 해요:

```
your-project/
├── app.py
├── static/
│   ├── css/
│   │   └── design-system.css   ← 여기에 복사
│   └── js/
│       └── app.js               ← 여기에 복사
└── templates/
    ├── base.html                ← 모든 페이지가 상속
    ├── home.html
    └── ...
```

## 🚀 적용 순서 (한 단계씩)

### 1단계: 파일 복사
이 `flask-kit/` 폴더 안의 파일들을 본인 프로젝트의 같은 경로에 복사하세요.

- `flask-kit/static/css/design-system.css` → `your-project/static/css/design-system.css`
- `flask-kit/static/js/app.js` → `your-project/static/js/app.js` (기존 파일이 있으면 내용을 합치세요)
- `flask-kit/templates/base.html` → `your-project/templates/base.html`
- `flask-kit/templates/home.html` → 참고용 예시

### 2단계: 기존 템플릿을 base.html 상속으로 바꾸기

**Before** (기존 각 템플릿):
```html
<!doctype html>
<html>
<head>...</head>
<body>
  <h1>제목</h1>
  <p>내용</p>
</body>
</html>
```

**After**:
```html
{% extends "base.html" %}
{% block title %}제목{% endblock %}
{% block content %}
  <h1 class="display-md">제목</h1>
  <p class="body-lg">내용</p>
{% endblock %}
```

### 3단계: 기존 CSS 정리

`static/css/style.css` 에 있던 기존 스타일 중:
- **border: 1px solid ...** — 전부 삭제
- **color: #000 / black** — `var(--on-surface)` 로 교체
- **background: white / #fff** — 그대로 둬도 됨 (디자인 시스템이 같은 색 사용)
- **font-family** — 삭제 (body에 이미 Plus Jakarta Sans 적용됨)
- **border-radius** — 4px, 8px 같은 작은 값은 `var(--radius-sm)` 이상으로

기존 CSS를 완전히 버리기 싫으면 `design-system.css`를 **뒤에** 로드해서 덮어쓰게 하세요.

### 4단계: Flask 라우트에 `active_page` 넘기기

사이드바 하이라이트를 위해 각 라우트에서:

```python
@app.route('/')
def home():
    return render_template('home.html', active_page='home', ...)

@app.route('/timeoff')
def timeoff():
    return render_template('timeoff.html', active_page='timeoff', ...)
```

## 🧩 자주 쓰는 컴포넌트 치트시트

### 버튼
```html
<button class="btn btn-primary">주요 액션</button>
<button class="btn btn-secondary">보조 액션</button>
<a href="#" class="btn btn-ghost">취소</a>
<button class="btn btn-primary btn-lg">큰 버튼</button>
```

### 카드
```html
<div class="card">
  <div class="label">라벨</div>
  <h3 class="headline-md">제목</h3>
  <p class="body-md">내용</p>
</div>

<!-- 톤 있는 카드 (sub-section용) -->
<div class="card inset">...</div>
```

### 폼 필드
```html
<div class="field">
  <label for="email">이메일</label>
  <input type="email" id="email" name="email" class="input" placeholder="you@example.com">
</div>
```

### 칩 (배지)
```html
<span class="chip">기본</span>
<span class="chip chip-primary">파랑</span>
<span class="chip chip-positive chip-dot">승인됨</span>
<span class="chip chip-warn chip-dot">대기중</span>
```

### 아바타 (이니셜)
```html
<div class="av" data-name="김지민">김지</div>
<!-- data-name 이 있으면 JS가 자동으로 색 지정 -->
```

### 모달
```html
<!-- 열기 버튼 -->
<button class="btn btn-primary" onclick="openModal('myModal')">열기</button>

<!-- 모달 -->
<div id="myModal" class="overlay" style="display: none;">
  <div class="modal" onclick="event.stopPropagation()">
    <div class="row-between" style="margin-bottom: 24px;">
      <h2 class="headline-lg">제목</h2>
      <button class="icon-btn" onclick="closeModal('myModal')">✕</button>
    </div>
    <p class="body-lg">내용…</p>
  </div>
</div>
```

### Flash 메시지 (Flask)
`base.html`에 이미 처리되어 있어요. 라우트에서:
```python
flash('저장했어요.', 'success')   # 초록
flash('오류가 났어요.', 'error')   # 주황
flash('참고하세요.', 'info')       # 파랑
```

## ✅ 자기점검

- [ ] `Plus Jakarta Sans` 폰트가 로딩됨 (개발자도구 Network 탭 확인)
- [ ] 모든 페이지가 `{% extends "base.html" %}` 사용
- [ ] `border: 1px solid` 가 없음 (`grep -r "border: 1px" static/` 로 확인)
- [ ] 검정색 텍스트 없음 (`grep -r "color: #000\|color: black" static/`)
- [ ] 카드/버튼이 radius 최소 0.75rem 이상

## 💡 Claude Code에게 보낼 프롬프트

본인 프로젝트 폴더에서 Claude Code 실행 후:

```
@flask-kit/README.md 를 읽고, 이 디자인 시스템을 내 기존 Flask 프로젝트에 적용해줘.

1. flask-kit/static/css/design-system.css 를 내 프로젝트 static/css/ 로 복사.
2. flask-kit/templates/base.html 을 참고해서 내 base.html 을 업데이트.
   (기존 네비게이션 링크는 유지, 디자인만 적용)
3. 내 templates/ 안의 모든 페이지를 base.html 상속 구조로 리팩토링.
4. 기존 static/css/style.css 에서 다음 항목 모두 제거/수정:
   - border: 1px solid → 전부 삭제
   - color: #000 / black → var(--on-surface) 로 교체
   - 작은 border-radius (4px, 8px) → 최소 var(--radius-sm) 이상으로
5. 기존 버튼/카드/폼을 design-system.css 의 클래스(.btn, .card, .input 등) 사용하도록 변환.
6. 내 라우트(@app.py)에 active_page 변수 전달 추가.
```


