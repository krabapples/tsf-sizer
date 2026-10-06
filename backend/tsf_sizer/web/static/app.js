// TSF Sizer UI behaviour: theme toggle, drop zones, submit state, report tabs, print.
(function () {
  'use strict';

  // Theme: system by default; the toggle cycles to the opposite of what is shown and remembers it.
  var root = document.documentElement;
  var toggle = document.getElementById('theme-toggle');
  if (toggle) {
    toggle.addEventListener('click', function () {
      var dark = root.dataset.theme
        ? root.dataset.theme === 'dark'
        : window.matchMedia('(prefers-color-scheme: dark)').matches;
      var next = dark ? 'light' : 'dark';
      root.dataset.theme = next;
      try { localStorage.setItem('tsf-theme', next); } catch (e) {}
    });
  }

  // Drop zones: show the chosen file, highlight while dragging.
  function fmtSize(bytes) {
    if (bytes >= 1048576) return (bytes / 1048576).toFixed(1) + ' MB';
    if (bytes >= 1024) return Math.round(bytes / 1024) + ' KB';
    return bytes + ' B';
  }
  document.querySelectorAll('.dropzone').forEach(function (zone) {
    var input = zone.querySelector('input[type="file"]');
    var title = zone.querySelector('[data-title]');
    var sub = zone.querySelector('[data-sub]');
    var original = { title: title && title.innerHTML, sub: sub && sub.innerHTML };
    function update() {
      var f = input.files && input.files[0];
      zone.classList.toggle('has-file', !!f);
      if (f) {
        title.innerHTML = '';
        var name = document.createElement('span');
        name.className = 'file-name';
        name.textContent = f.name;
        title.appendChild(name);
        sub.textContent = fmtSize(f.size) + ' · click or drop to replace';
      } else {
        title.innerHTML = original.title;
        sub.innerHTML = original.sub;
      }
    }
    input.addEventListener('change', update);
    ['dragenter', 'dragover'].forEach(function (ev) {
      zone.addEventListener(ev, function () { zone.classList.add('is-drag'); });
    });
    ['dragleave', 'drop'].forEach(function (ev) {
      zone.addEventListener(ev, function () { zone.classList.remove('is-drag'); });
    });
    update();
  });

  // Upload form: busy state on submit.
  var form = document.getElementById('upload-form');
  if (form) {
    form.addEventListener('submit', function () {
      var b = document.getElementById('submit-btn');
      if (b) { b.disabled = true; b.querySelector('span').textContent = 'Uploading…'; }
      var s = document.getElementById('upload-status');
      if (s) s.textContent = 'Large TSFs can take a minute to upload.';
    });
  }

  // Report: highlight the tab of the section in view.
  var tabs = document.querySelectorAll('.tabs a[href^="#"]');
  if (tabs.length && 'IntersectionObserver' in window) {
    var byId = {};
    tabs.forEach(function (a) { byId[a.getAttribute('href').slice(1)] = a; });
    var obs = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (e.isIntersecting && byId[e.target.id]) {
          tabs.forEach(function (a) { a.classList.remove('active'); });
          byId[e.target.id].classList.add('active');
        }
      });
    }, { rootMargin: '-90px 0px -60% 0px' });
    Object.keys(byId).forEach(function (id) {
      var el = document.getElementById(id);
      if (el) obs.observe(el);
    });
  }

  // Print: collapsed sections don't print, so open the marked ones and restore afterwards.
  var opened = [];
  window.addEventListener('beforeprint', function () {
    document.querySelectorAll('details[data-print-open]:not([open])').forEach(function (d) {
      d.open = true; opened.push(d);
    });
  });
  window.addEventListener('afterprint', function () {
    opened.forEach(function (d) { d.open = false; });
    opened = [];
  });
})();
