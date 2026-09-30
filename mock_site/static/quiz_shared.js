/* AutoLearn 题目靶场共享构件。
 *
 * 主文档（quiz_runtime.js）与跨域 frame（frame_runtime.js）必须产出**完全一致**
 * 的锚点契约与答案口径。逻辑放在这里一次，避免两份实现悄悄漂移 —— 漂移会让
 * 「地面真值」失效，那是 P1 最不能出的问题。
 *
 * 对外只暴露 window.MockQuiz。
 */
(function (global) {
  'use strict';

  var LABELS = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'];

  /* ------------------------------------------------------------ 确定性随机 */

  function mulberry32(a) {
    return function () {
      a |= 0; a = (a + 0x6d2b79f5) | 0;
      var t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  function rngFor(seed, index, salt) {
    return mulberry32(seed + index * 7919 + (salt || 0) * 104729);
  }

  function rndInt(gen, lo, hi) {
    return lo + Math.floor(gen() * (hi - lo + 1));
  }

  function shuffled(gen, arr) {
    var a = arr.slice();
    for (var i = a.length - 1; i > 0; i--) {
      var j = Math.floor(gen() * (i + 1));
      var tmp = a[i]; a[i] = a[j]; a[j] = tmp;
    }
    return a;
  }

  function has(list, value) {
    return (list || []).indexOf(value) !== -1;
  }

  function el(tag, attrs, text) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) { node.setAttribute(k, attrs[k]); });
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function sleep(ms) {
    return new Promise(function (r) { setTimeout(r, ms); });
  }

  /* -------------------------------------------------------- 乱序与答案口径 */

  /**
   * 打乱后标号按**呈现位置**重新分配，data-answer 必须跟着重算。
   * 漏了这一步，MockProvider 读到的就是错答案。
   */
  function layoutOptions(question, gen) {
    var pairs = question.options.map(function (text, i) {
      return { text: text, correct: has(question.answer, i) };
    });
    var ordered = question.shuffle ? shuffled(gen, pairs) : pairs;
    var items = ordered.map(function (p, i) {
      return { label: LABELS[i], text: p.text, correct: p.correct, index: i };
    });
    var correct = items.filter(function (o) { return o.correct; });
    return {
      items: items,
      answerLabels: correct.map(function (o) { return o.label; }),
      answerTexts: correct.map(function (o) { return o.text; }),
    };
  }

  /* ------------------------------------------------------------- DOM 构造 */

  function buildForm(question, layout) {
    var form = el('form', {
      'class': 'qz-question',
      'data-quiz': 'question',
      'data-question-id': question.id,
      'data-qtype': question.qtype,
      'data-answer': layout.answerLabels.join(','),
      'data-answer-texts': JSON.stringify(layout.answerTexts),
      'data-traps': (question.traps || []).join(','),
      'data-flags': (question.flags || []).join(','),
      'data-shuffled': question.shuffle ? 'true' : 'false',
    });
    form.setAttribute('novalidate', 'novalidate');

    var fieldset = el('fieldset', { 'class': 'qz-question__body' });
    var legend = el('legend', { 'class': 'qz-stem', 'data-quiz': 'stem' });
    fieldset.appendChild(legend);

    if (has(question.traps, 'canvas')) {
      // canvas 题：legend 留空，正文画在画布上并镜像进 data-quiz-stem-text
      fieldset.appendChild(el('canvas', {
        'class': 'qz-stem-canvas',
        'data-quiz': 'stem-canvas',
        'data-quiz-stem-text': question.canvas_stem || question.stem,
        'width': '520',
        'height': '220',
      }));
    } else {
      legend.textContent = question.stem;
    }

    if (question.image_svg && !has(question.traps, 'canvas')) {
      var fig = el('div', { 'class': 'qz-figure', 'data-quiz': 'figure' });
      fig.innerHTML = question.image_svg;
      fieldset.appendChild(fig);
    }

    var list = el('ul', { 'class': 'qz-options', 'data-quiz': 'options' });
    fieldset.appendChild(list);

    var actions = el('div', { 'class': 'qz-actions' });
    actions.appendChild(el('button', {
      'type': 'submit', 'class': 'qz-submit', 'data-quiz': 'submit',
    }, '提交'));
    actions.appendChild(el('button', {
      'type': 'button', 'class': 'qz-next', 'data-quiz': 'next',
    }, '下一题'));
    fieldset.appendChild(actions);

    var result = el('div', {
      'class': 'qz-result', 'role': 'status', 'data-quiz': 'result',
    });
    result.hidden = true;
    fieldset.appendChild(result);

    form.appendChild(fieldset);
    return form;
  }

  function fillOptions(list, items, qtype) {
    var inputType = qtype === 'multiple' ? 'checkbox' : 'radio';
    items.forEach(function (item) {
      var li = el('li', {
        'class': 'qz-option', 'data-quiz': 'option',
        'data-index': String(item.index), 'data-label': item.label,
      });
      var id = 'opt-' + item.index;
      var label = el('label', { 'class': 'qz-option__label' });
      label.setAttribute('for', id);
      label.appendChild(el('input', {
        'type': inputType, 'name': 'q-choice', 'value': item.label,
        'data-quiz': 'input', 'id': id,
      }));
      label.appendChild(el('span', {
        'class': 'qz-option__text', 'data-quiz': 'option-text',
      }, item.text));
      li.appendChild(label);
      list.appendChild(li);
    });
  }

  /* ------------------------------------------------------------- 画布题干 */

  function paintCanvas(canvas, lines, width, height) {
    var ctx = canvas.getContext('2d');
    canvas.width = width;
    canvas.height = height;
    ctx.fillStyle = '#ffffff';
    ctx.fillRect(0, 0, width, height);

    // 方格底：图例类画布的常见底纹。同时保证「非背景像素占比」稳定高于
    // M0-5 要求的 5% —— 纯文字 + 细线只有 4.x%，太贴边，容易假阴性。
    ctx.strokeStyle = '#e3e9f2';
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (var gx = 40; gx < width; gx += 40) {
      ctx.moveTo(gx + 0.5, 0);
      ctx.lineTo(gx + 0.5, height);
    }
    for (var gy = 40; gy < height; gy += 40) {
      ctx.moveTo(0, gy + 0.5);
      ctx.lineTo(width, gy + 0.5);
    }
    ctx.stroke();

    ctx.strokeStyle = '#c9d3e4';
    ctx.strokeRect(0.5, 0.5, width - 1, height - 1);

    ctx.fillStyle = '#1c2430';
    ctx.font = 'bold 17px "Segoe UI", "Microsoft YaHei", sans-serif';

    var y = 34;
    var maxWidth = width - 32;
    (lines || []).forEach(function (line) {
      var text = String(line);
      while (text.length) {
        var take = text.length;
        while (take > 1 && ctx.measureText(text.slice(0, take)).width > maxWidth) take--;
        ctx.fillText(text.slice(0, take), 16, y);
        text = text.slice(take);
        y += 26;
      }
    });

    // 简易示意图：加粗线宽 + 加大实心节点，保证画面里确实有"东西"
    ctx.strokeStyle = '#2f6fed';
    ctx.lineWidth = 3;
    var bx = 52;
    var by = y + 22;
    ctx.beginPath();
    ctx.moveTo(bx + 46, by); ctx.lineTo(bx, by + 52);
    ctx.moveTo(bx + 46, by); ctx.lineTo(bx + 92, by + 52);
    ctx.stroke();
    ctx.fillStyle = '#2f6fed';
    [[bx + 46, by], [bx, by + 52], [bx + 92, by + 52]].forEach(function (p) {
      ctx.beginPath(); ctx.arc(p[0], p[1], 17, 0, Math.PI * 2); ctx.fill();
    });
    ctx.fillStyle = '#ffffff';
    ctx.font = 'bold 14px Consolas, monospace';
    [[bx + 46, by], [bx, by + 52], [bx + 92, by + 52]].forEach(function (p, i) {
      ctx.fillText(String(i + 1), p[0] - 4, p[1] + 5);
    });
  }

  /* --------------------------------------------------------- 坑：类名混淆 */

  var CLS_POOL = ['x7a', 'bq2f', 'n9', 'k3vz', 'w1', 'tr8d', 'e4m', 'uj6'];

  function randomizeClasses(root, gen) {
    var nodes = [root].concat(Array.prototype.slice.call(root.querySelectorAll('*')));
    var map = {};
    nodes.forEach(function (node) {
      var old = node.getAttribute('class');
      if (!old) return;
      node.setAttribute('class', old.split(/\s+/).map(function (name) {
        if (!map[name]) {
          map[name] = CLS_POOL[Math.floor(gen() * CLS_POOL.length)] + '_' +
            Math.floor(gen() * 10000).toString(36);
        }
        return map[name];
      }).join(' '));
    });
  }

  /* --------------------------------------------------------- 坑：弹窗遮罩 */

  function showModal(gen, host) {
    var overlay = el('div', { 'class': 'qz-modal', 'data-quiz': 'modal' });
    var panel = el('div', { 'class': 'qz-modal__panel', 'data-quiz': 'modal-panel' });
    panel.appendChild(el('h3', { 'class': 'qz-modal__title' }, '提示'));
    panel.appendChild(el('p', { 'class': 'qz-modal__body' },
      '本页正在使用新版答题界面，遮罩将在数秒后自动消失。'));
    overlay.appendChild(panel);
    host.appendChild(overlay);
    return new Promise(function (resolve) {
      setTimeout(function () {
        if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
        resolve();
      }, rndInt(gen, 1200, 1800));
    });
  }

  /* -------------------------------------------------------------- 提交逻辑 */

  function readSelectedLabels(form) {
    return Array.prototype.slice
      .call(form.querySelectorAll('[data-quiz="input"]'))
      .filter(function (input) { return input.checked; })
      .map(function (input) { return input.value; })
      .sort();
  }

  /** 挂上提交与翻页处理，返回「下一题」按钮。 */
  function wireForm(form, onNext) {
    form.addEventListener('submit', function (event) {
      event.preventDefault();
      if (form.getAttribute('data-submitted') === 'true') {
        return; // 幂等：重复提交不改状态、不产生新结果
      }
      var expected = (form.getAttribute('data-answer') || '')
        .split(',').filter(Boolean).sort();
      var got = readSelectedLabels(form);
      var ok = expected.length === got.length &&
        expected.every(function (v, i) { return v === got[i]; });

      form.setAttribute('data-submitted', 'true');
      form.setAttribute('data-submit-ok', ok ? 'true' : 'false');
      form.querySelector('[data-quiz="submit"]').setAttribute('disabled', 'disabled');

      var result = form.querySelector('[data-quiz="result"]');
      result.textContent = '回答' + (ok ? '正确' : '错误') +
        '（期望 ' + expected.join(',') + '，实得 ' + (got.join(',') || '未选') + '）';
      result.hidden = false;
      result.setAttribute('data-ok', ok ? 'true' : 'false');
    });

    var next = form.querySelector('[data-quiz="next"]');
    if (onNext) next.addEventListener('click', onNext);
    return next;
  }

  global.MockQuiz = {
    LABELS: LABELS,
    mulberry32: mulberry32,
    rngFor: rngFor,
    rndInt: rndInt,
    shuffled: shuffled,
    has: has,
    el: el,
    sleep: sleep,
    layoutOptions: layoutOptions,
    buildForm: buildForm,
    fillOptions: fillOptions,
    paintCanvas: paintCanvas,
    randomizeClasses: randomizeClasses,
    showModal: showModal,
    readSelectedLabels: readSelectedLabels,
    wireForm: wireForm,
  };
})(window);
