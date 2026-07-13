/**
 * CSRF 토큰 자동 주입 (Phase A-2 보안 기준선)
 *
 * <meta name="csrf-token"> 값을 읽어 세 경로 모두에 자동 주입한다:
 *  1. 일반 폼 제출 (submit 이벤트 캡처)
 *  2. 프로그래매틱 form.submit() 호출 (프로토타입 패치 — submit 이벤트가 발생하지 않으므로 별도 처리)
 *  3. fetch() POST/PUT/PATCH/DELETE (X-CSRF-Token 헤더)
 */
(function () {
  var meta = document.querySelector('meta[name="csrf-token"]');
  var TOKEN = meta ? meta.getAttribute('content') : '';
  if (!TOKEN) return;

  function injectToken(form) {
    if (!form || form.method.toLowerCase() !== 'post') return;
    if (form.querySelector('input[name="csrf_token"]')) return;
    var input = document.createElement('input');
    input.type = 'hidden';
    input.name = 'csrf_token';
    input.value = TOKEN;
    form.appendChild(input);
  }

  // 1. 일반 제출 — 캡처 단계라 동적 생성 폼도 커버
  document.addEventListener('submit', function (e) {
    injectToken(e.target);
  }, true);

  // 2. form.submit() 직접 호출
  var nativeSubmit = HTMLFormElement.prototype.submit;
  HTMLFormElement.prototype.submit = function () {
    injectToken(this);
    return nativeSubmit.apply(this, arguments);
  };

  // 3. fetch AJAX — 같은 출처의 쓰기 요청에만 헤더 추가
  var nativeFetch = window.fetch;
  window.fetch = function (input, init) {
    init = init || {};
    var method = (init.method || (input && input.method) || 'GET').toUpperCase();
    var url = (typeof input === 'string') ? input : (input && input.url) || '';
    var sameOrigin = !/^https?:\/\//i.test(url) || url.indexOf(location.origin) === 0;
    if (sameOrigin && ['POST', 'PUT', 'PATCH', 'DELETE'].indexOf(method) !== -1) {
      if (init.headers instanceof Headers) {
        if (!init.headers.has('X-CSRF-Token')) init.headers.set('X-CSRF-Token', TOKEN);
      } else {
        init.headers = init.headers || {};
        if (!init.headers['X-CSRF-Token']) init.headers['X-CSRF-Token'] = TOKEN;
      }
    }
    return nativeFetch.call(this, input, init);
  };
})();
