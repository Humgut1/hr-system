/**
 * 직원 사진 줄이기 (B2) — 서버에 이미지 라이브러리가 없어서 브라우저에서 줄여 보낸다.
 * <input type="file" data-photo-resize [multiple] [data-autosubmit]>
 *  - 고른 사진을 긴 변 480px JPEG 로 바꿔 같은 칸에 다시 넣는다(파일 이름의 사번은 그대로, 확장자만 .jpg).
 *  - data-autosubmit 이면 다 줄인 뒤 폼을 바로 보낸다.
 */
(function () {
  var MAX = 480, Q = 0.85;

  function shrink(file) {
    return new Promise(function (ok) {
      if (!/^image\/(jpeg|png|webp)$/.test(file.type)) return ok(file);
      var url = URL.createObjectURL(file), img = new Image();
      img.onload = function () {
        var s = Math.min(1, MAX / Math.max(img.width, img.height));
        var c = document.createElement('canvas');
        c.width = Math.round(img.width * s); c.height = Math.round(img.height * s);
        var x = c.getContext('2d');
        x.fillStyle = '#fff'; x.fillRect(0, 0, c.width, c.height);
        x.drawImage(img, 0, 0, c.width, c.height);
        URL.revokeObjectURL(url);
        c.toBlob(function (b) {
          if (!b) return ok(file);
          var stem = file.name.replace(/\.[^.]+$/, '');
          ok(new File([b], stem + '.jpg', { type: 'image/jpeg' }));
        }, 'image/jpeg', Q);
      };
      img.onerror = function () { URL.revokeObjectURL(url); ok(file); };
      img.src = url;
    });
  }

  document.addEventListener('change', function (e) {
    var inp = e.target;
    if (!inp.matches || !inp.matches('input[type=file][data-photo-resize]') || inp.dataset.busy) return;
    var files = Array.prototype.slice.call(inp.files || []);
    if (!files.length) return;
    inp.dataset.busy = '1';
    var note = inp.form && inp.form.querySelector('[data-photo-note]');
    if (note) note.textContent = '사진을 줄이는 중…';
    Promise.all(files.map(shrink)).then(function (out) {
      try {
        var dt = new DataTransfer();
        out.forEach(function (f) { dt.items.add(f); });
        inp.files = dt.files;
      } catch (err) { /* 못 바꾸면 원본 그대로 보낸다(서버가 크기를 다시 본다) */ }
      delete inp.dataset.busy;
      if (note) note.textContent = out.length + '장 준비됨';
      if (inp.hasAttribute('data-autosubmit') && inp.form) inp.form.submit();
    });
  });
})();
