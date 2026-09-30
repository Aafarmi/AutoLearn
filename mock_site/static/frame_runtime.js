/* 跨域 frame 内的题目运行时。
 *
 * 由 quiz.html 通过 <iframe src="http://127.0.0.1:8900/frame.html?..."> 加载 ——
 * 端口不同即**真跨域**（srcdoc / 同源 iframe 测不出视觉兜底路径）。
 *
 * 题目数据从主源（8899）的 /mock-api 接口用 CORS 拉取。
 * 「下一题」勾不到父文档，通过 postMessage 回传。
 *
 * URL 参数：?idx=30&seed=123&api=http%3A%2F%2F127.0.0.1%3A8899
 */
(function () {
  'use strict';

  var Q = window.MockQuiz;
  var params = new URLSearchParams(window.location.search);
  var IDX = parseInt(params.get('idx') || '0', 10);
  var SEED = parseInt(params.get('seed') || '20260925', 10);
  var API = params.get('api') || window.location.origin;
  var MOUNT = document.getElementById('frame-stage');

  function notifyParent() {
    try {
      window.parent.postMessage({ type: 'mock:next', idx: IDX }, '*');
    } catch (err) {
      /* 跨域受限时静默；主文档另有兜底导航 */
    }
  }

  async function boot() {
    var resp = await fetch(API + '/mock-api/question/' + IDX + '?seed=' + SEED,
      { credentials: 'omit', mode: 'cors' });
    var question = await resp.json();

    var layout = Q.layoutOptions(question, Q.rngFor(SEED, IDX, 2));
    var form = Q.buildForm(question, layout);
    Q.wireForm(form, notifyParent);
    MOUNT.appendChild(form);

    Q.fillOptions(form.querySelector('[data-quiz="options"]'), layout.items, question.qtype);

    if (Q.has(question.traps, 'canvas')) {
      Q.paintCanvas(form.querySelector('[data-quiz="stem-canvas"]'),
        [question.canvas_stem || question.stem], 520, 220);
    }
    if (Q.has(question.traps, 'cls')) {
      Q.randomizeClasses(form, Q.rngFor(SEED, IDX, 3));
    }

    document.title = 'frame · 题库序号 ' + IDX;
    window.__MOCK_FRAME__ = { index: IDX, origin: window.location.origin, api: API };
  }

  document.addEventListener('DOMContentLoaded', boot);
})();
