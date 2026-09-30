/* AutoLearn 网课靶场运行时。
 *
 * 三条不可动摇的约定：
 *   1. 弹题弹窗**绝不暂停视频**；弹题不是媒体态，覆盖层不会让 paused 变真。
 *   2. ?interrupt_at=end 时弹题与 ended **同刻**到达，用于验「ended 优先」。
 *   3. 每集埋 data-vid 与 data-duration，vid 与 core/vid.py 完全一致。
 *
 * URL 参数：?interrupt_at=30|end  ?dur=8  ?ep=3  ?quiz=21  ?seed=123
 */
(function () {
  'use strict';

  var Q = window.MockQuiz;
  var DEFAULT_SEED = 20260925;

  var params = new URLSearchParams(window.location.search);
  var RAW_INTERRUPT = params.get('interrupt_at');
  var INTERRUPT_END = RAW_INTERRUPT === 'end';
  var INTERRUPT_AT = INTERRUPT_END ? NaN : parseInt(RAW_INTERRUPT || '', 10);
  var DUR_OVERRIDE = parseInt(params.get('dur') || '', 10);
  var START_EP = parseInt(params.get('ep') || '1', 10);
  var QUIZ_INDEX = parseInt(params.get('quiz') || '21', 10);
  var SEED = parseInt(params.get('seed') || String(DEFAULT_SEED), 10);

  var video = document.querySelector('[data-media="video"]');
  var overlay = document.querySelector('[data-media="overlay"]');
  var listEl = document.querySelector('[data-media="episode-list"]');
  var playBtn = document.querySelector('[data-media="play-button"]');
  var nextBtn = document.querySelector('[data-media="next"]');
  var progressEl = document.querySelector('[data-media="progress"]');
  var statusEl = document.querySelector('[data-media="status"]');

  var course = null;
  var questions = null;
  var currentIndex = 1;
  var interruptShown = false; // 弹窗此刻是否开着
  var interruptFired = false; // 本集是否已经弹过（关了也不许再弹）
  var interruptEl = null;

  /* ------------------------------------------------------------- 小工具 */

  function episodeOf(index) {
    return course.episodes.filter(function (e) { return e.episode_index === index; })[0] || null;
  }

  function totalEpisodes() { return course.episodes.length; }

  function durationOf(ep) {
    return DUR_OVERRIDE > 0 ? DUR_OVERRIDE : ep.duration;
  }

  function fmt(sec) {
    if (!isFinite(sec) || sec < 0) sec = 0;
    var m = Math.floor(sec / 60);
    var s = Math.floor(sec % 60);
    return ('0' + m).slice(-2) + ':' + ('0' + s).slice(-2);
  }

  function mediaStateText() {
    if (video.ended) return '已结束';
    if (video.paused) return '已暂停';
    return '播放中';
  }

  /* --------------------------------------------------------- 叠加时间码 */

  function drawOverlay() {
    var ctx = overlay.getContext('2d');
    var w = overlay.width;
    var h = overlay.height;
    ctx.fillStyle = '#10161f';
    ctx.fillRect(0, 0, w, h);

    var ratio = video.duration ? video.currentTime / video.duration : 0;
    ctx.fillStyle = '#1d2735';
    ctx.fillRect(16, h - 20, w - 32, 8);
    ctx.fillStyle = video.ended ? '#1f9d63' : '#4c8dff';
    ctx.fillRect(16, h - 20, Math.max(0, Math.min(1, ratio)) * (w - 32), 8);

    ctx.fillStyle = '#e8eefc';
    ctx.font = '15px Consolas, "Courier New", monospace';
    ctx.fillText(
      'EP ' + currentIndex + '/' + totalEpisodes() +
      '   ' + fmt(video.currentTime) + ' / ' + fmt(video.duration) +
      '   ' + mediaStateText(),
      16, 26
    );

    // 十分秒帧号：给截图差分一个一眼可辨的变化量
    ctx.font = 'bold 22px Consolas, "Courier New", monospace';
    ctx.fillStyle = '#7fb0ff';
    ctx.fillText('#' + Math.floor(video.currentTime * 10), 16, 56);

    ctx.font = '13px Consolas, monospace';
    ctx.fillStyle = '#5b6675';
    ctx.fillText('interrupt_at=' + (RAW_INTERRUPT || 'off'), 160, 56);
  }

  function refreshProgress() {
    if (progressEl) {
      progressEl.textContent = fmt(video.currentTime) + ' / ' + fmt(video.duration);
    }
    drawOverlay();
  }

  /* ----------------------------------------------------------- 弹题弹窗 */

  function buildInterruptQuestion() {
    var base = questions.filter(function (q) { return q.index === QUIZ_INDEX; })[0];
    if (!base) return null;
    // 弹题保持干净：不带坑，避免与「嵌套中断」本身的验证纠缠
    return Object.assign({}, base, { traps: [] });
  }

  /**
   * 显示弹题。
   *
   * **刻意不调用 video.pause()** —— 弹题不是媒体态。系统必须自己显式暂停，
   * 这正是 M5-2「挂起期间必须显式暂停媒体」要测的东西。
   */
  function showInterrupt(mode) {
    if (interruptFired) return;
    var question = buildInterruptQuestion();
    if (!question) return;

    interruptShown = true;
    interruptFired = true;
    document.body.setAttribute('data-interrupt-mode', mode);

    interruptEl = Q.el('div', { 'class': 'cs-interrupt', 'data-media': 'interrupt' });
    var panel = Q.el('div', { 'class': 'cs-interrupt__panel', 'data-media': 'interrupt-panel' });
    panel.appendChild(Q.el('h2', { 'class': 'cs-interrupt__title' },
      '课中弹题 · 播放已继续（系统需自行暂停）'));

    var layout = Q.layoutOptions(question, Q.rngFor(SEED, question.index, 5));
    var form = Q.buildForm(question, layout);
    Q.fillOptions(form.querySelector('[data-quiz="options"]'), layout.items, question.qtype);
    Q.wireForm(form, null);
    form.addEventListener('submit', function () {
      setTimeout(hideInterrupt, 400);
    });
    panel.appendChild(form);

    interruptEl.appendChild(panel);
    document.body.appendChild(interruptEl);

    if (statusEl) {
      statusEl.textContent = '弹题已出现（mode=' + mode + '），视频仍在播放，paused=' + video.paused;
    }
    drawOverlay();
  }

  function hideInterrupt() {
    if (interruptEl && interruptEl.parentNode) interruptEl.parentNode.removeChild(interruptEl);
    interruptEl = null;
    interruptShown = false;
    if (statusEl) statusEl.textContent = '弹题已关闭 · 第 ' + currentIndex + ' 集';
  }
  /* ------------------------------------------------------------ 分集切换 */

  function renderEpisodeList() {
    listEl.innerHTML = '';
    course.episodes.forEach(function (ep) {
      var li = Q.el('li', {
        'class': 'cs-episode',
        'data-media': 'episode',
        'data-episode-index': String(ep.episode_index),
        'data-vid': ep.vid,
        'data-duration': String(durationOf(ep)),
        'data-title': ep.title,
        'data-active': ep.episode_index === currentIndex ? 'true' : 'false',
      });
      li.appendChild(document.createTextNode(ep.title));
      li.appendChild(Q.el('span', { 'class': 'cs-episode__meta' },
        ep.vid + ' · ' + durationOf(ep) + 's'));
      li.addEventListener('click', function () { loadEpisode(ep.episode_index); });
      listEl.appendChild(li);
    });
  }

  function loadEpisode(index) {
    var ep = episodeOf(index);
    if (!ep) return;
    currentIndex = index;
    hideInterrupt();
    interruptFired = false; // 换了集数才允许再弹

    var dur = durationOf(ep);
    video.src = '/media/' + ep.media + '?d=' + dur + '&ep=' + ep.episode_index;
    video.load();

    document.body.setAttribute('data-current-episode', String(index));
    document.body.setAttribute('data-episode-vid', ep.vid);

    Array.prototype.slice.call(listEl.children).forEach(function (li) {
      li.setAttribute('data-active',
        li.getAttribute('data-episode-index') === String(index) ? 'true' : 'false');
    });

    if (statusEl) statusEl.textContent = '第 ' + index + ' 集 · ' + ep.title + ' · ' + dur + 's';
    refreshProgress();
  }

  /* --------------------------------------------------------------- 事件 */

  function onEnded() {
    refreshProgress();
    // ?interrupt_at=end：与 ended **同刻**到达，优先级测试就靠这个用例
    if (interruptFired) return;
    if (INTERRUPT_END || (!isNaN(INTERRUPT_AT) && INTERRUPT_AT >= video.duration)) {
      showInterrupt('at-end');
    }
  }

  function onTimeUpdate() {
    refreshProgress();
    // interruptFired 是关键：弹题被关掉之后**不许再弹**，否则永远关不干净
    if (interruptFired || video.ended) return;
    if (!isNaN(INTERRUPT_AT) && video.currentTime >= INTERRUPT_AT) {
      showInterrupt('mid');
    }
  }

  function wireVideo() {
    ['timeupdate', 'play', 'pause', 'ended', 'loadedmetadata', 'seeked']
      .forEach(function (name) { video.addEventListener(name, refreshProgress); });

    video.addEventListener('ended', onEnded);
    video.addEventListener('timeupdate', onTimeUpdate);
    video.addEventListener('error', function () {
      video.setAttribute('data-media-error', String(video.error && video.error.code));
    });

    // 4Hz 轮询兜底：timeupdate 在极短素材上可能太稀疏
    setInterval(function () {
      if (!video.paused && !video.ended) refreshProgress();
    }, 250);
  }

  function wireControls() {
    playBtn.addEventListener('click', function () {
      if (video.paused) {
        var p = video.play();
        if (p && p.catch) {
          p.catch(function (err) {
            video.setAttribute('data-media-error', String(err && err.name));
            if (statusEl) statusEl.textContent = '播放被拒：' + (err && err.name);
          });
        }
      } else {
        video.pause();
      }
    });

    video.addEventListener('play', function () { playBtn.textContent = '暂停'; });
    video.addEventListener('pause', function () { playBtn.textContent = '播放'; });
    video.addEventListener('ended', function () { playBtn.textContent = '重播'; });

    nextBtn.addEventListener('click', function () {
      if (currentIndex < totalEpisodes()) loadEpisode(currentIndex + 1);
      else if (statusEl) statusEl.textContent = '已是最后一集';
    });
  }

  /* --------------------------------------------------------------- 启动 */

  async function boot() {
    var results = await Promise.all([
      fetch('static/course.json', { credentials: 'omit' }).then(function (r) { return r.json(); }),
      fetch('static/questions.json', { credentials: 'omit' }).then(function (r) { return r.json(); }),
    ]);
    course = results[0];
    questions = results[1].questions;

    document.body.setAttribute('data-course-id', course.meta.course_id);
    document.body.setAttribute('data-interrupt-at', RAW_INTERRUPT || 'off');

    renderEpisodeList();
    wireVideo();
    wireControls();

    window.__MOCK_COURSE__ = {
      courseId: course.meta.course_id,
      seed: SEED,
      interruptAt: RAW_INTERRUPT || 'off',
      durationOverride: DUR_OVERRIDE > 0 ? DUR_OVERRIDE : null,
      episodes: course.episodes.map(function (e) { return e.episode_index; }),
    };

    loadEpisode(episodeOf(START_EP) ? START_EP : 1);
  }

  document.addEventListener('DOMContentLoaded', boot);
})();
