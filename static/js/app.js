document.addEventListener('DOMContentLoaded', function () {
  // 현재 경로에 맞게 nav 활성화
  const path = window.location.pathname;
  document.querySelectorAll('.nav-item[data-path]').forEach(function (el) {
    if (el.dataset.path && path.startsWith(el.dataset.path)) {
      el.classList.add('active');
    }
  });

  // ── 모바일 사이드바 토글 ──────────────────────────
  var sidebar  = document.querySelector('.sidebar');
  var overlay  = document.getElementById('sidebarOverlay');
  var hamburger = document.getElementById('hamburgerBtn');

  function openSidebar() {
    if (sidebar)  sidebar.classList.add('open');
    if (overlay)  overlay.classList.add('active');
  }

  function closeSidebar() {
    if (sidebar)  sidebar.classList.remove('open');
    if (overlay)  overlay.classList.remove('active');
  }

  if (hamburger) {
    hamburger.addEventListener('click', function () {
      if (sidebar && sidebar.classList.contains('open')) {
        closeSidebar();
      } else {
        openSidebar();
      }
    });
  }

  if (overlay) {
    overlay.addEventListener('click', closeSidebar);
  }

  // 사이드바 내 링크 클릭 시 모바일에서 자동 닫기
  if (sidebar) {
    sidebar.querySelectorAll('.nav-item').forEach(function (link) {
      link.addEventListener('click', function () {
        if (window.innerWidth <= 768) closeSidebar();
      });
    });
  }
});
