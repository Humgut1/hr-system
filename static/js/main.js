// =========================================================
// app.js — Vanilla JS 헬퍼
// 모달 열기/닫기, 토스트 표시 등 공통 인터랙션
// =========================================================

// ---- Modal 제어 ----
function openModal(modalId) {
  const overlay = document.getElementById(modalId);
  if (overlay) overlay.style.display = 'grid';
}

function closeModal(modalId) {
  const overlay = document.getElementById(modalId);
  if (overlay) overlay.style.display = 'none';
}

// overlay 클릭 시 닫기
document.addEventListener('click', (e) => {
  if (e.target.classList.contains('overlay')) {
    e.target.style.display = 'none';
  }
});

// ESC로 닫기
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    document.querySelectorAll('.overlay').forEach(el => el.style.display = 'none');
  }
});

// ---- Toast ----
function showToast(message) {
  let stack = document.querySelector('.toast-stack');
  if (!stack) {
    stack = document.createElement('div');
    stack.className = 'toast-stack';
    document.body.appendChild(stack);
  }
  const toast = document.createElement('div');
  toast.className = 'toast';
  toast.textContent = message;
  stack.appendChild(toast);
  setTimeout(() => toast.remove(), 3200);
}

// ---- 햄버거 메뉴 (모바일 사이드바) ----
(function () {
  var hamburger = document.getElementById('hamburgerBtn');
  var sidebar   = document.querySelector('.sidebar');
  var overlay   = document.getElementById('sidebarOverlay');

  function openSidebar() {
    if (sidebar)  sidebar.classList.add('open');
    if (overlay)  overlay.classList.add('active');
    document.body.style.overflow = 'hidden';
  }

  function closeSidebar() {
    if (sidebar)  sidebar.classList.remove('open');
    if (overlay)  overlay.classList.remove('active');
    document.body.style.overflow = '';
  }

  if (hamburger) hamburger.addEventListener('click', openSidebar);
  if (overlay)   overlay.addEventListener('click', closeSidebar);

  // 사이드바 내 링크 클릭 시 자동 닫기 (모바일)
  if (sidebar) {
    sidebar.querySelectorAll('a, button').forEach(function (el) {
      el.addEventListener('click', function () {
        if (window.innerWidth <= 768) closeSidebar();
      });
    });
  }
}());

// ---- 이니셜 아바타 색 자동 지정 ----
const AV_PALETTES = [
  ['#dfe7ff', '#15306b'],
  ['#e5efd7', '#1f3b10'],
  ['#fce8cf', '#4a2f00'],
  ['#e7deff', '#2f1f5a'],
  ['#d7eef1', '#0e3a43'],
];
document.querySelectorAll('.av[data-name]').forEach(el => {
  const name = el.dataset.name;
  let h = 0;
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) | 0;
  const [bg, fg] = AV_PALETTES[Math.abs(h) % AV_PALETTES.length];
  el.style.background = bg;
  el.style.color = fg;
});
