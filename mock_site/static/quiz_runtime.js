/* AutoLearn 题目靶场页面运行时 —— 负责编排、注入坑、翻页。
 *
 * 构件（乱序 / 表单 / 画布 / 遮罩）全部来自 quiz_shared.js。
 *
 * 坑的实现位置：
 *   spa     延迟挂载整题
 *   lazy    选项等 ul 进入视口后才填充（另有 3s 兜底）
 *   canvas  题干画在 canvas 上，legend 留空
 *   iframe  整题搬进跨域 frame
 *   cls     题内类名全部随机化
 *   modal   选项渲染后 250ms 弹遮罩，1.2~1.8s 后自动消失
 *   xhr     题干改由 /mock-api/question/<n> 下发
 *   next_after_scroll
 *           「下一题」被 1200px 占位推到首屏之外，**滚到才出现**；
 *           若这是本次计划的最后一题，则**永不出现**并显示「已是最后一题」
 *
 * URL 参数：?seq=21,22,23  ?seed=123  ?q=21
 */
(function () {
  'use strict';

  var Q = window.MockQuiz;
  var DEFAULT_SEED = 20260925;
  var LAZY_SPACER_PX = 1100;
  //: 「下一题」前方的占位高度：必须大于一个视口，否则按钮一开始就在首屏里，
  //: 那这个坑就什么都没测到。
  var NEXT_SPACER_PX = 1200;

  var params = new URLSearchParams(window.location.search);
  var SEED = parseInt(params.get('seed') || String(DEFAULT_SEED), 10);
  var META = document.querySelector('meta[name="mock-frame-origin"]');
  var FRAME_ORIGIN = META ? META.getAttribute('content') : '';
  var MAIN_ORIGIN = window.location.origin;
  var SEQ = (params.get('seq') || '')
    .split(',')
    .map(function (s) { return parseInt(s.trim(), 10); })
    .filter(function (n) { return !isNaN(n); });

  var stage = document.getElementById('quiz-stage');
  var progressIndex = document.querySelector('[data-quiz="progress-index"]');
  var progressTotal = document.querySelector('[data-quiz="progress-total"]');
  var statusLine = document.querySelector('[data-quiz="status"]');

  var plan = [];
  var cursor = 0;

  function updateProgress() {
    var q = plan[cursor];
    if (progressIndex) progressIndex.textContent = String(cursor + 1);
    if (progressTotal) progressTotal.textContent = String(plan.length);
    if (statusLine) {
      statusLine.textContent = '第 ' + (cursor + 1) + '/' + plan.length +
        ' 题 · 题库序号 ' + q.index +
        ' · 坑 [' + ((q.traps || []).join('+') || '无') + ']';
    }
  }

  function onNextClick() {
    if (cursor + 1 >= plan.length) {
      stage.appendChild(Q.el('div', { 'class': 'qz-done', 'data-quiz': 'done' },
        '全部 ' + plan.length + ' 题已完成。'));
      return;
    }
    cursor += 1;
    void renderQuestion(plan[cursor]);
  }

  // 跨域 frame 内的「下一题」勾不到主文档，走 postMessage 回传
  window.addEventListener('message', function (event) {
    if (!FRAME_ORIGIN || event.origin !== FRAME_ORIGIN) return;
    var data = event.data || {};
    if (data.type === 'mock:next') onNextClick();
  });

  async function renderQuestion(question) {
    var traps = question.traps || [];
    stage.innerHTML = '';

    // ---- 坑 spa：延迟挂载，读早了就是空页面
    if (Q.has(traps, 'spa')) {
      stage.appendChild(Q.el('div', { 'class': 'qz-placeholder', 'data-quiz': 'placeholder' },
        '正在加载题目…'));
      await Q.sleep(Q.rndInt(Q.rngFor(SEED, question.index, 1), 300, 900));
    }

    // ---- 坑 xhr：题干由接口下发
    var payload = question;
    if (Q.has(traps, 'xhr')) {
      try {
        var resp = await fetch('/mock-api/question/' + question.index + '?seed=' + SEED,
          { credentials: 'omit' });
        payload = await resp.json();
      } catch (err) {
        payload = question; // 接口失败退回本地数据，保证靶场仍可用
      }
    }

    var layout = Q.layoutOptions(payload, Q.rngFor(SEED, question.index, 2));
    stage.innerHTML = '';

    // ---- 坑 iframe：整题搬进跨域 frame，主文档没有 [data-quiz=question]
    if (Q.has(traps, 'iframe')) {
      stage.appendChild(Q.el('iframe', {
        'class': 'qz-frame',
        'data-quiz': 'frame',
        'data-traps': traps.join(','),
        'src': FRAME_ORIGIN + '/frame.html?idx=' + question.index +
          '&seed=' + SEED + '&api=' + encodeURIComponent(MAIN_ORIGIN),
        'width': '100%',
        'height': '620',
      }));
      updateProgress();
      return;
    }

    var form = Q.buildForm(payload, layout);
    var list = form.querySelector('[data-quiz="options"]');
    Q.wireForm(form, onNextClick);
    stage.appendChild(form);

    if (Q.has(traps, 'lazy')) {
      // 长题干把选项推到首屏之外；滚动进视口后才填充
      insertLazySpacer(form);
      var fill = function () {
        if (list.getAttribute('data-loaded') === 'true') return;
        list.setAttribute('data-loaded', 'true');
        Q.fillOptions(list, layout.items, payload.qtype);
      };
      if ('IntersectionObserver' in window) {
        var io = new IntersectionObserver(function (entries) {
          entries.forEach(function (entry) {
            if (entry.isIntersecting) { fill(); io.disconnect(); }
          });
        }, { threshold: 0.01 });
        io.observe(list);
      }
      setTimeout(fill, 3000); // 兜底：最终一定可答
    } else {
      Q.fillOptions(list, layout.items, payload.qtype);
    }

    if (Q.has(traps, 'canvas')) {
      Q.paintCanvas(form.querySelector('[data-quiz="stem-canvas"]'),
        [payload.canvas_stem || payload.stem], 520, 220);
    }

    if (Q.has(traps, 'cls')) {
      Q.randomizeClasses(form, Q.rngFor(SEED, question.index, 3));
    }

    if (Q.has(traps, 'next_after_scroll')) {
      installScrolledNext(form, cursor + 1 >= plan.length);
    }

    if (Q.has(traps, 'modal')) {
      // 遮罩在选项渲染后 250ms 出现并自动消失。期间强制点击会被遮罩吞掉，
      // 回读机制必须能把这一步捞回来 —— 这正是该坑要测的东西。
      setTimeout(function () {
        void Q.showModal(Q.rngFor(SEED, question.index, 4), stage);
      }, 250);
    }

    updateProgress();
  }

  /**
   * 坑 next_after_scroll：把「下一题」推到首屏之外，**滚到才出现**。
   *
   * 为什么要专门做这个坑：真实站点上「找不到下一题」有两种含义 ——
   * 「还没滚到」与「真的没有了」—— 而它们在画面上长得一模一样。分不清的代价
   * 极不对称：把「还没滚到」当成「跑完了」= 后面所有题都不会被作答。
   *
   * 只推「下一题」，**不动提交按钮**：否则这个坑会顺带改变提交路径的行为，
   * 那就不是「一个坑测一件事」了。
   *
   * 若是本次计划的**最后一题**，按钮**永不出现**并显示「已是最后一题」——
   * 这就是「真的没有了」那一半的靶子。
   */
  function installScrolledNext(form, isLast) {
    var actions = form.querySelector('.qz-actions');
    var next = form.querySelector('[data-quiz="next"]');
    if (!actions || !next) return;

    if (isLast) {
      actions.replaceChild(
        Q.el('span', { 'class': 'qz-last', 'data-quiz': 'last-note' }, '已是最后一题'),
        next
      );
      return;
    }

    var tail = Q.el('div', { 'class': 'qz-next-tail', 'data-quiz': 'next-tail' });
    tail.appendChild(Q.el('div', {
      'class': 'qz-spacer',
      'data-quiz': 'next-spacer',
      'style': 'height:' + NEXT_SPACER_PX + 'px',
    }));
    next.style.display = 'none';
    actions.removeChild(next);
    tail.appendChild(next);
    (form.querySelector('fieldset') || form).appendChild(tail);

    // 文档压根滚不动时（极短的页面）直接放出来 —— 否则这个坑会变成一个
    // 「永远找不到」的死结，而不是「滚一下就有」。
    if (document.documentElement.scrollHeight <= window.innerHeight + 10) {
      next.style.display = '';
      return;
    }

    var revealed = false;
    var reveal = function () {
      if (revealed) return;
      // 「按钮那一行真的进了视口」才放出来 —— 用 next 自己的位置判断，
      // 比用占位块的位置准（占位块可能一开始就有一半在首屏里）。
      if (next.getBoundingClientRect().top < window.innerHeight - 20) {
        revealed = true;
        next.style.display = '';
        window.removeEventListener('scroll', reveal, true);
      }
    };
    // capture=true：内层滚动容器的事件也要能收到（很多答题页把内容放在
    // overflow:auto 的 div 里，滚 window 是滚不动的）。
    window.addEventListener('scroll', reveal, true);
  }

  /** 懒加载题：在题干与选项之间插入一段占位高度，把选项推到首屏之下。 */
  function insertLazySpacer(form) {    var body = form.querySelector('.qz-question__body') ||
      form.querySelector('fieldset');
    var list = form.querySelector('[data-quiz="options"]');
    if (!body || !list) return;
    body.insertBefore(Q.el('div', {
      'class': 'qz-spacer',
      'data-quiz': 'spacer',
      'style': 'height:' + LAZY_SPACER_PX + 'px',
    }), list);
  }

  async function boot() {
    var resp = await fetch('static/questions.json', { credentials: 'omit' });
    var data = await resp.json();
    plan = SEQ.length
      ? data.questions.filter(function (q) { return Q.has(SEQ, q.index); })
      : data.questions.slice();

    var start = parseInt(params.get('q') || '', 10);
    if (!isNaN(start)) {
      var at = plan.findIndex(function (q) { return q.index === start; });
      if (at >= 0) cursor = at;
    }

    window.__MOCK__ = {
      seed: SEED,
      frameOrigin: FRAME_ORIGIN,
      mainOrigin: MAIN_ORIGIN,
      total: plan.length,
      sequence: plan.map(function (q) { return q.index; }),
    };

    await renderQuestion(plan[cursor]);
  }

  document.addEventListener('DOMContentLoaded', boot);
})();
