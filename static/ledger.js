/* ============= v9 第 2 期：记账（账本 / 账户 / 分类 / 流水 / 月度统计） =============
 * 依赖 app.js 里的 esc()/api()/toast()；本文件在 app.js 之前加载，
 * 所以只在函数运行时使用它们（那时两边都加载完了）。
 * 金额一律「分」，显示时再除 100，前端也不做浮点累加。
 */
var ledState = { book: 0, month: '', setup: null, summary: null, list: [], books: [], open: false };

function ledFen(cents) {
  var v = Math.round((parseInt(cents || 0, 10)) / 100);
  if (Math.abs(v) >= 10000) return (v / 10000).toFixed(Math.abs(v) >= 100000 ? 0 : 1) + '万';
  return String(v);
}
function ledMoney(cents) {
  var c = parseInt(cents || 0, 10), neg = c < 0; c = Math.abs(c);
  return (neg ? '-' : '') + Math.floor(c / 100) + '.' + String(c % 100).padStart(2, '0');
}
/* 当天头卡下面那一行：今天花了多少、收了多少，顺手给个「记一笔」入口 */
/* app.js 里的 selectedDate/dayData 是 let 声明，不会挂到 window，所以状态靠参数传进来 */
function dayLedgerLine(day, dd) {
  var L = (dd && dd.ledger) || null;
  var d = day || new Date().toISOString().slice(0, 10);
  var btn = '<button class="mini-btn" onclick="ledQuick(\'' + d + '\')">＋记一笔</button>';
  if (!L || !L.count) return '<div class="dh-ledger"><span class="led-quiet">今天还没记帐</span>' + btn + '</div>';
  return '<div class="dh-ledger"><b class="led-out">支 ' + ledMoney(L.expense) + '</b>'
    + (L.income ? '<b class="led-in">收 ' + ledMoney(L.income) + '</b>' : '')
    + '<span class="led-quiet">' + L.count + ' 笔</span>' + btn
    + '<a class="led-more" onclick="ledOpen(\'' + d.slice(0, 7) + '\')">看本月</a></div>';
}
function ledBind() {
  var b = document.getElementById('btnLedger2');
  if (b && !b._bound) { b._bound = 1; b.onclick = function () { closeDrawer(); ledOpen(); }; }
}
document.addEventListener('DOMContentLoaded', ledBind);
setTimeout(ledBind, 300);
window.ledRefreshMonth = function () { if (ledState.open) ledLoad(); };

function ledOpen(month) {
  var mask = document.getElementById('ledMask');
  if (!mask) return;
  mask.style.display = 'flex';
  ledState.open = true;
  ledState.month = month || ledState.month || new Date().toISOString().slice(0, 7);
  ledLoad();
}
function ledClose() {
  document.getElementById('ledMask').style.display = 'none';
  ledState.open = false;
}
function ledShiftMonth(step) {
  var y = parseInt(ledState.month.slice(0, 4), 10), m = parseInt(ledState.month.slice(5, 7), 10) + step;
  if (m < 1) { m = 12; y--; } if (m > 12) { m = 1; y++; }
  ledState.month = y + '-' + String(m).padStart(2, '0');
  ledLoad();
}
function ledLoad() {
  var q = '?month=' + ledState.month + (ledState.book ? '&book=' + ledState.book : '');
  Promise.all([
    api('/api/ledger/books'),
    api('/api/ledger/summary' + q),
    api('/api/ledger/setup' + (ledState.book ? '?book=' + ledState.book : '')),
    api('/api/ledger/entries' + q + '&limit=120')
  ]).then(function (r) {
    ledState.books = r[0].books || [];
    var s = r[1]; ledState.book = s.book;
    ledState.setup = r[2]; ledState.summary = s; ledState.list = r[3].entries || [];
    ledRender();
  }).catch(function (e) { toast('记账读取出错：' + e.message); });  setTimeout(function () {              /* 数据回来了：管理面板开着就顺手重画，不用手动刷新 */
    try {
      var p = document.getElementById('ledPanel');
      if (p && getComputedStyle(p).display !== 'none') ledAdmin(_ledTab);
    } catch (e) { }
  }, 60);
}
function ledRender() {
  var s = ledState.summary, st = ledState.setup;
  var opt = ledState.books.map(function (b) {
    return '<option value="' + b.id + '"' + (b.id === ledState.book ? ' selected' : '') + '>' + esc(b.name) + '</option>';
  }).join('');
  var mom = s.mom_pct === null || s.mom_pct === undefined ? '' :
    ('<span class="led-mom ' + (s.mom_pct > 0 ? 'up' : 'down') + '">比上月' + (s.mom_pct > 0 ? '多' : '少') +
     Math.abs(s.mom_pct) + '%</span>');
  var accs = (st.accounts || []).map(function (a) {
    return '<div class="led-acc"><span>' + esc(a.name) + '</span><b>' + ledMoney(a.balance) + '</b></div>';
  }).join('');
  var cats = [];
  ((s.by_category || {}).expense || []).forEach(function (c) { cats.push({ c: c, kind: 'out' }); });
  ((s.by_category || {}).income || []).forEach(function (c) { cats.push({ c: c, kind: 'in' }); });
  var tot = Math.max(1, s.expense);
  var catHtml = cats.length ? cats.map(function (x) {
    var pct = x.kind === 'out' ? Math.round(x.c.cents * 100 / tot) : 0;
    return '<div class="led-catrow" onclick="ledCatDetail(' + x.c.id + ',\'' + (x.kind === 'in' ? 'in' : 'out') + '\')"><span class="led-catname">' + (x.kind === 'in' ? '收·' : '') + esc(x.c.name) + '</span>'
      + '<span class="led-bar"><i style="width:' + Math.min(100, pct) + '%"></i></span>'
      + '<b>' + ledMoney(x.c.cents) + '</b><small>' + x.c.count + '笔' + (pct ? ' ' + pct + '%' : '') + '</small></div>';
  }).join('') : '<div class="led-quiet">这个月还没有记过账</div>';
  var listHtml = ledState.list.map(function (e) {
    var cls = e.dir === 'in' ? 'led-in' : (e.dir === 'transfer' ? 'led-tr' : 'led-out');
    var sign = e.dir === 'in' ? '+' : (e.dir === 'transfer' ? '⇄' : '-');
    return '<div class="led-entry" onclick="ledEditEntry(' + e.id + ')"><span class="led-day">' + esc((e.day || '').slice(5)) + '</span>'
      + '<span class="led-cat">' + esc(e.dir === 'transfer'
        ? (e.account.name + '→' + e.to_account.name) : (e.category.name || '未分类')) + '</span>'
      + '<span class="led-note">' + esc(e.note || '') + (e.by && !e.mine ? ' <i>' + esc(e.by) + '</i>' : '') + '</span>'
      + '<b class="' + cls + '">' + sign + ledMoney(e.cents) + '</b>'
      + '<button class="led-op" title="改这一笔" onclick="event.stopPropagation();ledEditEntry(' + e.id + ')">\u270e</button>'
      + '<button class="led-op del" title="删掉（进记账垃圾桶，可恢复）" onclick="event.stopPropagation();ledDelEntry(' + e.id + ')">\u2715</button></div>';
  }).join('') || '<div class="led-quiet">本月没有流水</div>';

  /* v10.4：顶部＝账本下拉（点名称直接切换）+ ⚙管理（默认账户段）+ 小＋记一笔 */
  try {
    var hd = document.querySelector('.cal-ledopen #ledMask .led-modal .modal-head') || document.querySelector('#ledMask .modal-head');
    hd.innerHTML = '<button class="head-btn" onclick="ledClose()">关闭</button>'
      + '<select class="led-bookpick" id="ledBookSelTop" title="点一下切换账本；改名去账本段，删除用 \ud83d\uddd1">'
      + (ledState.books || []).map(function (x) {
          return '<option value="' + x.id + '"' + (x.id === ledState.book ? ' selected' : '') + '>'
            + esc(x.name) + '（' + (x.entries || 0) + '）</option>';
        }).join('')
      + '</select>'
      + '<span class="led-head-r"><button class="head-btn" onclick="ledAdmin(\'acc\')">⚙ 管理</button>'
      + '<button class="led-quickmini" onclick="ledQuick()" title="记一笔">＋</button></span>';
    var _s = document.getElementById('ledBookSelTop');
    if (_s) _s.onchange = function () {
      ledState.book = parseInt(this.value, 10);
      try { localStorage.setItem('calnote-ledger-book', this.value); } catch (e) { }
      ledChanged(null, '');
    };
  } catch (e) { }
  document.getElementById('ledBody').innerHTML =
    '<div class="led-top">'
    + '<div class="led-mon"><button class="icon-btn" onclick="ledShiftMonth(-1)">‹</button>'
    + '<span>' + esc(ledState.month) + '</span>'
    + '<button class="icon-btn" onclick="ledShiftMonth(1)">›</button>'
    + '<input type="month" id="ledMonthTop" class="led-sel" onchange="ledState.month=this.value;ledLoad()"></div>'
    + '</div>'
    + '<div class="led-sum"><div><span>支出</span><b class="led-out">' + ledMoney(s.expense) + '</b></div>'
    + '<div><span>收入</span><b class="led-in">' + ledMoney(s.income) + '</b></div>'
    + '<div><span>结余</span><b>' + ledMoney(s.balance) + '</b>' + mom + '</div></div>'
    + '<div class="led-accs">' + accs + '</div>'
    + ledBudgetHtml(s)
    + '<div class="led-cats">' + catHtml + '</div>'
    + ledChartsHtml(s)
    + '<div class="led-list">' + listHtml + '</div>';
  var _bs = document.getElementById('ledBookSel');
  if (_bs) _bs.onchange = function () { ledState.book = parseInt(this.value, 10); ledLoad(); };
  document.getElementById('ledMonthInput').value = ledState.month.slice(0, 7);
}
/* ---------- 记一笔 ---------- */
function ledQuick(day) {
  ledState.month = (day || ledState.month || '').slice(0, 7);
  ledQuickShow(day || (window.selectedDate || new Date().toISOString().slice(0, 10)));
}
function ledQuickShow(day, editing) {
  ledState.editing = editing || null;   // v9.6：带进来就是编辑这一笔
  var box = document.getElementById('ledQuickMask');
  if (!box) return;
  box.style.display = 'flex';
  Promise.all([api('/api/ledger/setup' + (ledState.book ? '?book=' + ledState.book : '')), api('/api/ledger/books')])
    .then(function (r) {
      ledState.setup = r[0]; ledState.books = r[1].books || []; ledState.book = r[0].book;
      var ed = ledState.editing;
      document.getElementById('lqDay').value = day;
      // v9.7：编辑这一笔时，把原来的值填回去（金额/备注/方向/分类/账户），不再让你重新输入
      document.getElementById('lqAmount').value = ed ? (ed.cents / 100).toFixed(2).replace(/\.00$/, '') : '';
      document.getElementById('lqNote').value = ed ? (ed.note || '') : '';
      ledState.dir = ed ? (ed.dir || 'out') : 'out';
      document.querySelectorAll('#ledQuickMask .lq-seg').forEach(function (b) {
        b.classList.toggle('on', b.dataset.dir === ledState.dir); });
      var dirCats = (r[0].categories || {})[ledState.dir === 'in' ? 'income' : 'expense'] || [];
      ledChips('lqCats', dirCats, null);
      ledChips('lqAccs', r[0].accounts, null);
      if (!ed) {
        ledDefaultPick('lqCats', '_cat');
        ledDefaultPick('lqAccs', '_acc');
      } else {
        var mark = function (elId, arr, obj) {
          var i = 0, hit = -1;
          (arr || []).forEach(function (x) { if (obj && x.id === obj.id) hit = i; i++; });
          var chips = document.querySelectorAll('#' + elId + ' .lq-chip');
          if (hit >= 0 && chips[hit]) { [].forEach.call(chips, function (b) { b.classList.remove('on'); });
            chips[hit].classList.add('on'); ledState[elId === 'lqCats' ? '_cat' : '_acc'] = obj.id; }
        };
        mark('lqCats', dirCats, ed.category);
        mark('lqAccs', r[0].accounts, ed.account);
        if (ledState.dir === 'transfer') { ledChips('lqToAccs', r[0].accounts, null);
          mark('lqToAccs', r[0].accounts, ed.to_account); }
        document.getElementById('lqToRow').style.display = ledState.dir === 'transfer' ? '' : 'none';
        document.getElementById('lqCatRow').style.display = ledState.dir === 'transfer' ? 'none' : '';
        return;                                            // 编辑模式的界面已就位
      }
      document.getElementById('lqToRow').style.display = 'none';
      document.getElementById('lqCatRow').style.display = '';
    }).catch(function (e) { toast('打记账面板失败：' + e.message); });
}
function ledChips(elId, arr, picked) {
  var el = document.getElementById(elId);
  if (!el) return;
  el.innerHTML = (arr || []).map(function (x, i) {
    var ic = x.icon ? '<span class="lq-ic">' + esc(x.icon) + '</span>'   /* 图标终于显示在记一笔的宫格里 */
                    : '<span class="lq-ic">' + esc(String(x.name || '?').slice(0, 1)) + '</span>';
    return '<button type="button" class="lq-chip' + ((picked != null && picked === i) ? ' on' : '') + '" data-id="' + x.id +
           '" onclick="ledPickChip(this,\'' + elId + '\')">' + ic + '<span class="lq-nm">' + esc(x.name) + '</span></button>';
  }).join('');
}
function ledDefaultPick(elId, field) {
  var first = document.querySelector('#' + elId + ' .lq-chip');
  if (!first) return;
  first.classList.add('on');
  ledState[field] = parseInt(first.dataset.id, 10);
}
function ledPickChip(btn, elId) {
  document.querySelectorAll('#' + elId + ' .lq-chip').forEach(function (b) { b.classList.remove('on'); });
  btn.classList.add('on');
  if (elId === 'lqCats') ledState._cat = parseInt(btn.dataset.id, 10);
  if (elId === 'lqAccs') ledState._acc = parseInt(btn.dataset.id, 10);
  if (elId === 'lqToAccs') ledState._to = parseInt(btn.dataset.id, 10);
}
function ledSeg(dir) {
  ledState.dir = dir;
  document.querySelectorAll('#ledQuickMask .lq-seg').forEach(function (b) {
    b.classList.toggle('on', b.dataset.dir === dir);
  });
  document.getElementById('lqToRow').style.display = dir === 'transfer' ? '' : 'none';
  document.getElementById('lqCatRow').style.display = dir === 'transfer' ? 'none' : '';
  if (dir === 'transfer') ledChips('lqToAccs', (ledState.setup || {}).accounts || [], null);
  else ledChips('lqCats', ((ledState.setup || {}).categories || {})[dir === 'in' ? 'income' : 'expense'] || [], null);
}
function ledQuickClose() {
  ledState.editing = null;
  var m = document.querySelector('#ledQuickMask .modal-title');
  if (m) m.textContent = '记一笔';
  var d = document.getElementById('lqDel'); if (d) d.style.display = 'none';
  document.getElementById('ledQuickMask').style.display = 'none';
}
function ledQuickSave() {
  var amount = document.getElementById('lqAmount').value.trim();
  var body = {
    book: ledState.book, dir: ledState.dir, amount: amount,
    day: document.getElementById('lqDay').value,
    account: ledState._acc || 0, note: document.getElementById('lqNote').value.trim()
  };
  if (ledState.dir === 'transfer') body.to_account = ledState._to || 0;
  else body.category = ledState._cat || 0;
  var ed = ledState.editing;
  var url = ed ? '/api/ledger/entries/' + ed.id : '/api/ledger/entries';
  api(url, { method: ed ? 'PUT' : 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
    .then(function (r) {
      ledQuickClose();
      toast(ed ? '这一笔改好了' : '已记 ' + ((r.entry || {}).amount || body.amount) + ' 元');
      ledState.month = (body.day || '').slice(0, 7) || ledState.month;
      if (typeof loadDay === 'function') loadDay();
      if (ledState.open) ledLoad();
    })
    .catch(function (e) { toast('没记上：' + e.message); });
}
function ledDelEntry(id) {
  if (!confirm('删掉这一笔？\n\n先进「记账垃圾桶」，30 天内能恢复。')) return;
  api('/api/ledger/entries/' + id, { method: 'DELETE' })
    .then(function () { ledLoad(); if (typeof loadDay === 'function') loadDay(); })
    .catch(function (e) { toast(e.message); });
}
function ledEdit(id) { return ledEditEntry(id); }        /* 老名字保留，别处还可能有引用 */
function ledEditEntry(id) {
  var e = (ledState.list || []).filter(function (x) { return x.id === id; })[0];
  if (!e) { toast('这笔不在当前列表里，翻下月份再点'); return; }
  ledQuickShow(e.day, e);        // v9.7：回填全部在 ledQuickShow 里完成（修掉"要重新输入"的毛病）
  var t = document.querySelector('#ledQuickMask .modal-title'); if (t) t.textContent = '改这一笔';
  var dl = document.getElementById('lqDel');
  if (dl) {
    dl.style.display = '';
    dl.onclick = function () { ledDelEntry(e.id); };
  } else {
    var foot = document.querySelector('#ledQuickMask .modal-foot') || document.querySelector('#ledQuickMask .modal-body');
    if (foot) {
      var b = document.createElement('button');
      b.id = 'lqDel'; b.type = 'button'; b.className = 'lq-del'; b.textContent = '\ud83d\uddd1 删掉这一笔';
      b.onclick = function () { ledDelEntry(e.id); };
      foot.appendChild(b);
    }
  }
}
/* ---------- 账户 / 分类 ---------- */
function ledAddThing() {
  var what = prompt('加什么？输入 1 = 账户，2 = 支出分类，3 = 收入分类：', '2');
  if (!what) return;
  var name = prompt('名字（比如「钓鱼」「信用卡」）：');
  if (!name || !name.trim()) return;
  if (what === '1') {
    var op = prompt('这个账户现在有多少钱（元，没有就 0）：', '0');
    api('/api/ledger/accounts', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ book: ledState.book, name: name.trim(), opening: (op || '0').trim() }) })
      .then(function () { ledChanged('acc', '账户加好了'); }).catch(function (e) { toast(e.message); });
  } else {
    api('/api/ledger/categories', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ book: ledState.book, name: name.trim(), kind: what === '3' ? 'income' : 'expense' }) })
      .then(function () { ledLoad(); toast('分类加好了'); }).catch(function (e) { toast(e.message); });
  }
}
function ledNewBook() {
  var name = prompt('新账本名字（比如「厂里」「学费」）：');
  if (!name || !name.trim()) return;
  api('/api/ledger/books', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: name.trim() }) })
    .then(function (r) { ledState.book = r.book.id; ledLoad(); toast('账本建好了'); })
    .catch(function (e) { toast(e.message); });
}

/* ================= v9 第 4 期：预算标色 / SVG 图表 / 导入导出 ================= */
/* 图表全部手写 SVG：NAS 经常断外网，引 CDN 或打包 ECharts 都会让图表整块白屏 */
function ledChartsHtml(sm) {
  var trend = sm.trend || [], cats = ((sm.by_category || {}).expense) || [];
  if (trend.length < 2 && cats.length < 2) return '';
  var out = '', w = 320, h = 92, bw = trend.length ? Math.max(6, Math.min(22, Math.floor(w / trend.length) - 6)) : 18;
  if (trend.length >= 2) {
    var max = Math.max.apply(null, trend.map(function (t) { return t.expense; }).concat([1]));
    var bars = trend.map(function (t, i) {
      var hh = Math.max(2, Math.round(t.expense * (h - 18) / max));
      var x = 6 + i * (bw + 6), y = h - hh - 14, cur = t.period === sm.month;
      return '<rect x="' + x + '" y="' + y + '" width="' + bw + '" height="' + hh + '" rx="2" fill="'
        + (cur ? '#2b6cb0' : '#a8c4e0') + '"><title>' + esc(t.period) + ' 支出 ' + ledMoney(t.expense) + '</title></rect>'
        + (i % 2 === 0 ? '<text x="' + (x + bw / 2) + '" y="' + (h - 3) + '" font-size="8" fill="#98a2ad" text-anchor="middle">'
          + esc(t.period.slice(5)) + '</text>' : '');
    }).join('');
    out += '<span class="led-chart"><svg class="led-svg" viewBox="0 0 ' + w + ' ' + h + '" role="img" '
      + 'aria-label="近' + trend.length + '个月支出柱状图">' + bars + '</svg>'
      + '<div class="led-cap">近 ' + trend.length + ' 个月支出（蓝＝本月）</div></span>';
  }
  if (cats.length > 1) {
    var tot = cats.reduce(function (a, b) { return a + b.cents; }, 0) || 1;
    var ang = -Math.PI / 2, cx = 46, cy = 46, rr = 40, paths = '';
    var PAL = ['#2b6cb0', '#4f9dd8', '#7fb8e6', '#f0a35e', '#d97b6f', '#8bc49a', '#c9a7e0', '#d9c26f', '#9aa7b5', '#6fa8b5'];
    cats.slice(0, 10).forEach(function (c, i) {
      var a2 = ang + (c.cents / tot) * Math.PI * 2, big = (a2 - ang) > Math.PI ? 1 : 0;
      var x1 = cx + rr * Math.cos(ang), y1 = cy + rr * Math.sin(ang);
      var x2 = cx + rr * Math.cos(a2), y2 = cy + rr * Math.sin(a2);
      paths += '<path d="M' + cx + ',' + cy + ' L' + x1.toFixed(1) + ',' + y1.toFixed(1) + ' A' + rr + ',' + rr
        + ' 0 ' + big + ',1 ' + x2.toFixed(1) + ',' + y2.toFixed(1) + ' Z" fill="' + PAL[i % PAL.length] + '">'
        + '<title>' + esc(c.name) + ' ' + ledMoney(c.cents) + '</title></path>';
      ang = a2;
    });
    out += '<span class="led-chart"><svg class="led-svg pie" viewBox="0 0 92 92" role="img" aria-label="本月分类占比饼图">'
      + paths + '</svg><div class="led-cap">本月分类占比</div></span>';
  }
  return '<div class="led-charts">' + out + '</div>';
}
/* 预算：用到 80% 标黄、超 100% 标红 */
function ledBudgetHtml(sm) {
  var bs = sm.budgets || [];
  if (!bs.length) return '';
  var warn = sm.warn_pct || 80;
  var rows = bs.map(function (b) {
    return '<div class="led-bud ' + b.state + '"><span class="bd-n">' + esc(b.name) + '</span>'
      + '<span class="bd-bar"><i style="width:' + Math.min(100, b.pct) + '%"></i></span>'
      + '<b>' + esc(b.used_text) + ' / ' + esc(b.limit_text) + '</b>'
      + '<small>' + b.pct + '%' + (b.state === 'over' ? ' 已超支' : (b.state === 'warn' ? ' 接近上限' : '')) + '</small>'
      + '<button class="mini-btn" onclick="ledDelBudget(' + b.id + ')">删</button></div>';
  }).join('');
  var tl = bs.reduce(function (a, b) { return a + b.limit; }, 0), tu = bs.reduce(function (a, b) { return a + b.used; }, 0);
  var tp = tl ? Math.round(tu * 1000 / tl) / 10 : 0;
  var cls = tp > 100 ? 'over' : (tp >= warn ? 'warn' : 'ok');
  return '<div class="led-buds"><div class="led-bud total ' + cls + '"><span class="bd-n">预算合计</span>'
    + '<span class="bd-bar"><i style="width:' + Math.min(100, tp) + '%"></i></span>'
    + '<b>' + ledMoney(tu) + ' / ' + ledMoney(tl) + '</b><small>' + tp + '%</small><span></span></div>' + rows + '</div>';
}
function ledSetBudget() { ledAdmin('bud'); }   /* v9.9：预算改成管理面板里的一个分段，不再弹系统输入框 */
function ledDelBudget(id) {
  if (!confirm('取消这条预算？流水不会被删。')) return;
  api('/api/ledger/budgets/' + id, { method: 'DELETE' }).then(ledLoad).catch(function (e) { toast(e.message); });
}
function ledDoExport() {
  window.location = '/api/ledger/export?book=' + ledState.book + (ledState.month ? '&month=' + ledState.month : '');
}
function ledImpTip(html, bad, rich) {
  var el = document.getElementById('liResult');
  if (el) { el.className = 'import-result' + (bad ? ' err' : ''); if (rich) el.innerHTML = html; else el.textContent = html; }
}
function ledImportDialog() {
  var box = document.getElementById('ledImpMask');
  var accs = (((ledState.setup || {}).accounts) || []).map(function (a) {
    return '<option value="' + a.id + '">' + esc(a.name) + '</option>';
  }).join('');
  if (!box) {
    box = document.createElement('div');
    box.id = 'ledImpMask'; box.className = 'modal-mask'; box.style.display = 'none';
    box.innerHTML = '<div class="modal modal-center led-quick"><div class="modal-head">'
      + '<button class="head-btn" onclick="ledImportClose()">取消</button>'
      + '<div class="modal-title">导入账单</div><div class="head-btn" style="visibility:hidden">占位</div></div>'
      + '<div class="modal-body"><p class="hint">三种文件都能认，自动判格式：<br>'
      + '<b>① 微信账单</b>（导出来是 <b>.xlsx</b>，直接选它就行：交易时间 / 交易类型 / 交易对方 / 金额(元) / 当前状态）<br>'
      + '<b>② 支付宝账单</b>（交易时间 / 交易分类 / 交易对方 / 金额 / 交易状态）<br>'
      + '<b>③ 本应用导出的 CSV</b>（日期,方向,金额,分类,账户,转入账户,备注）<br>'
      + '交易关闭、已全额退款的自动跳过；同日期+同金额+同方向不重复导入。'
      + '认不出的分类归到「其他」，但把原分类名留在备注里，不丢信息。</p>'
      + '<div class="field"><div class="field-label">账单文件（微信 .xlsx / 支付宝 .csv / 本应用 CSV）</div>'
      + '<input type="file" id="liFile" accept=".csv,.txt,.xlsx,text/csv,text/plain,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"></div>'
      + '<div class="field"><div class="field-label">记到哪个账户</div><select id="liAcc" class="led-sel">'
      + accs + '</select></div>'
      + '<div class="field"><label class="check"><input type="checkbox" id="liGo"> 直接写入（不勾＝只预览，不动数据）</label></div>'
      + '<button class="primary-btn" onclick="ledDoImport()">开始</button>'
      + '<div class="import-result" id="liResult"></div></div></div>';
    document.body.appendChild(box);
  }
  box.style.display = 'flex';
  ledImpTip('', false);
}
function ledImportClose() {
  var b = document.getElementById('ledImpMask'); if (b) b.style.display = 'none';
}
function ledDoImport() {
  var fi = document.getElementById('liFile');
  if (!fi || !fi.files || !fi.files[0]) { ledImpTip('先选一个账单文件', true); return; }
  var fd = new FormData();
  fd.append('file', fi.files[0]);
  fd.append('book', ledState.book);
  var acc = document.getElementById('liAcc');
  if (acc && acc.value) fd.append('account', acc.value);
  fd.append('dry_run', document.getElementById('liGo').checked ? '0' : '1');
  api('/api/ledger/import', { method: 'POST', body: fd }).then(function (r) {
    var head = '<b>识别为【' + esc(r.source) + '】' + (r.dry_run ? '可导入 ' : '已导入 ') + r.would_add
      + ' 笔</b>　重复跳过 ' + r.dup + ' · 不适用 ' + r.skipped + ' · 解析失败 ' + r.failed;
    var html = head + '<br>' + esc(r.note);
    if (r.samples && r.samples.length) {
      html += '<br>样例：' + r.samples.slice(0, 5).map(function (x) {
        return esc(x.day + ' ' + (x.dir === 'in' ? '收' : (x.dir === 'transfer' ? '转' : '支')) + x.amount + ' ' + x.category);
      }).join('；');
    }
    if (r.errors && r.errors.length) html += '<br><span class="err">解析失败的行：<br>' + r.errors.map(esc).join('<br>') + '</span>';
    ledImpTip(html, false, true);
    if (!r.dry_run) { ledImportClose(); ledLoad(); }
  }).catch(function (e) { ledImpTip('导入失败：' + esc(e.message), true); });
}
/* ================= v9.6：账本设置 / 管分类 / 记账垃圾桶 ================= */
function ledPanel(title, acts) {
  /* 兼容旧调用：把动作清单渲染进同一个面板 */
  var box = document.getElementById('ledPanel');
  if (!box) {
    box = document.createElement('div');
    box.id = 'ledPanel'; box.className = 'modal-mask'; box.style.display = 'none';
    box.innerHTML = '<div class="modal modal-center led-sheet"><div class="modal-head">'
      + '<button class="head-btn" onclick="ledPanelClose()">返回</button>'
      + '<div class="modal-title" id="ledPanelT"></div><div class="head-btn" style="visibility:hidden">·</div></div>'
      + '<div class="modal-body" id="ledPanelB"></div></div>';
    document.body.appendChild(box);
  }
  var _t2 = document.getElementById('ledPanelT'), _b2 = document.getElementById('ledPanelB');
  if (!_t2 || !_b2) {                      /* 容器是旧结构：整块重建一次 */
    box.innerHTML = '<div class="modal modal-center led-sheet"><div class="modal-head">'
      + '<button class="head-btn" onclick="ledPanelClose()">返回</button>'
      + '<div class="modal-title" id="ledPanelT"></div>'
      + '<button class="head-btn" id="ledPanelAdd" style="display:none">＋</button></div>'
      + '<div class="led-tabs" id="ledTabs" style="display:none"></div>'
      + '<div class="modal-body" id="ledPanelB"></div></div>';
    _t2 = document.getElementById('ledPanelT'); _b2 = document.getElementById('ledPanelB');
  }
  _t2.textContent = title;
  _b2.innerHTML = acts.map(function (a, i) {
    return '<button type="button" class="led-act' + (a[2] ? ' danger' : '') + '" data-i="' + i + '">' + esc(a[0]) + '</button>';
  }).join('');
  [].forEach.call(document.querySelectorAll('#ledPanelB .led-act'), function (b) {
    b.onclick = function () { var f = acts[parseInt(b.dataset.i, 10)][1]; ledPanelClose(); try { f(); } catch (e) { toast(e.message); } };
  });
  box.onclick = function (e) { if (e.target === box) ledPanelClose(); };
  box.style.display = 'flex';
}
function ledCatManager() {
  var st = ledState.setup || {};
  var cats = ((st.categories || {}).expense || []).concat((st.categories || {}).income || []);
  ledPanel('管分类（' + cats.length + ' 个）', cats.map(function (c) {
    return [(c.icon ? c.icon + ' ' : '') + c.name + '（' + (c.kind === 'income' ? '收入' : '支出') + ' · 第' + (c.sort == null ? 0 : c.sort) + '位）',
      function () { ledCatEdit(c); }];
  }).concat([['＋ 新加一个分类', function () { ledCatAdd(''); }]]));
}
function ledCatSave(c, patch) {
  var body = Object.assign({ name: c.name, icon: c.icon || '', sort: c.sort == null ? 99 : c.sort }, patch);
  api('/api/ledger/categories/' + c.id, { method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body) }).then(function () { ledChanged(_ledTab, '分类改好了'); }).catch(function (e) { toast(e.message); });
}

function ledTrash() {
  api('/api/ledger/trash').then(function (r) {
    var items = r.items || [];
    if (!items.length) { toast('记账垃圾桶是空的（删掉的流水会先进这里 ' + (r.kept_days || 30) + ' 天）'); return; }
    ledPanel('记账垃圾桶 · ' + items.length + ' 项', items.map(function (x) {
      return ['↩ 恢复：' + (x.label || x.kind) + '（还剩 ' + x.left_days + ' 天）', function () {
        api('/api/ledger/trash/restore', { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ ids: [x.id] }) }).then(function (y) { toast('恢复了 ' + y.restored + ' 项'); ledLoad(); })
          .catch(function (e) { toast(e.message); });
      }];
    }).concat([['🗑 全部彻底删除（不可恢复）', function () {
      if (!confirm('真删？这些快照删掉后就再也恢复不了了。')) return;
      api('/api/ledger/trash/purge', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids: [0] }) }).then(function (y) { toast('清了 ' + y.purged + ' 项'); }).catch(function (e) { toast(e.message); });
    }, true]]));
  }).catch(function (e) { toast('读不到：' + e.message); });
}
/* ================= v9.7：⚙ 管理（账户/分类/账本/预算）+ 小表单卡片 + emoji 宫格 ================= */
var LED_EMOJI = ['\ud83c\udf5c', '\ud83c\udf5b', '\ud83c\udf71', '\u2615', '\ud83d\ude95', '\u26fd', '\ud83d\ude97',
  '\ud83d\udeb2', '\ud83d\uded2', '\ud83c\udf6d', '\ud83d\udcb0', '\ud83d\udcb3', '\ud83c\udfe0', '\ud83d\udcbc',
  '\ud83d\udc8a', '\ud83d\udcf1', '\ud83c\udfb2', '\ud83c\udfa3', '\u26bd', '\ud83c\udfae', '\ud83d\udcd6',
  '\u2708\ufe0f', '\ud83d\ude0a', '\ud83c\udf81', '\ud83d\udc36', '\ud83d\udd0b', '\ud83d\udee0', '\ud83c\udf93'];

/* 通用小表单卡片（替代系统 prompt）：fields=[{id,label,type,value,ph,opts}]，cb(values) */
function ledAsk(title, fields, cb, opt) {
  opt = opt || {};
  fields = fields || [];
  var box = document.getElementById('ledAsk');
  if (!box) {
    box = document.createElement('div');
    box.id = 'ledAsk'; box.className = 'modal-mask'; box.style.display = 'none';
    box.innerHTML = '<div class="modal modal-center led-quick"><div class="modal-head">'
      + '<button class="head-btn" onclick="ledAskClose()">\u53d6\u6d88</button>'
      + '<div class="modal-title" id="ledAskT"></div><div class="head-btn" style="visibility:hidden">\u00b7</div></div>'
      + '<div class="modal-body" id="ledAskB"></div>'
      + '<div class="modal-foot" id="ledAskF"><button class="primary-btn" id="ledAskOk">\u786e\u5b9a</button></div></div>';
    document.body.appendChild(box);
  }
  /* 每次打开都复位：确定按钮的文字/数量、功能键不累积 */
  var foot = document.getElementById('ledAskF');
  [].forEach.call(foot.querySelectorAll('button'), function (b) { if (b.id !== 'ledAskOk') b.remove(); });
  var ok = document.getElementById('ledAskOk');
  ok.textContent = '\u786e\u5b9a';
  document.getElementById('ledAskT').textContent = title;
  document.getElementById('ledAskB').innerHTML = fields.map(function (f) {
    if (f.type === 'emoji') {
      return '<div class="field"><div class="field-label">' + f.label + '</div>'
        + '<input type="hidden" id="' + f.id + '" value="' + (f.value || '') + '">'
        + '<div class="lq-chips emoji" id="' + f.id + 'Pick">' + LED_EMOJI.map(function (e) {
            return '<button type="button" class="lq-chip' + (f.value === e ? ' on' : '') + '" data-v="' + e + '">'
              + e + '</button>';
          }).join('') + '</div></div>';
    }
    if (f.type === 'select') {
      return '<div class="field"><div class="field-label">' + f.label + '</div>'
        + '<select id="' + f.id + '" class="line-input">' + (f.opts || []).map(function (o) {
            return '<option value="' + o[0] + '"' + (f.value === o[0] ? ' selected' : '') + '>' + o[1] + '</option>';
          }).join('') + '</select></div>';
    }
    return '<div class="field"><div class="field-label">' + f.label + '</div>'
      + '<input type="' + (f.type || 'text') + '" id="' + f.id + '" class="line-input" value="' + (f.value || '') + '"'
      + ' placeholder="' + (f.ph || '') + '" autocomplete="off"></div>';
  }).join('');
  [].forEach.call(document.querySelectorAll('#ledAskB .lq-chip'), function (b) {
    b.onclick = function () {
      [].forEach.call(document.querySelectorAll('#ledAskB .lq-chip'), function (x) { x.classList.remove('on'); });
      b.classList.add('on');
      var hid = document.getElementById((b.parentNode.id || '').replace('Pick', ''));
      if (hid) hid.value = b.dataset.v || '';
    };
  });
  /* 底部功能键：foot=[{t,fn,danger,confirm}]，extra=[[文字,fn,危险]]，arch/wipe 老写法 —— 统一渲染一次 */
  var acts = (opt.foot || []).map(function (x) { return [x.t, x.fn, x.danger, x.confirm]; }).concat(
             (opt.extra || []).map(function (x) { return [x[0], x[1], x[2], null]; }));
  if (opt.arch) acts.push([opt.archLabel || '\u25cb \u5f52\u6863', opt.arch, 0, null]);
  if (opt.wipe) acts.push([opt.wipeLabel || '\ud83d\uddd1 \u5f7b\u5e95\u5220\u9664', opt.wipe, 1, opt.wipeConfirm || null]);
  acts.forEach(function (a) {
    var b = document.createElement('button');
    b.type = 'button'; b.className = 'led-ask-x' + (a[2] ? ' danger' : ''); b.textContent = a[0];
    b.onclick = function () {
      if (a[3] && !confirm(a[3])) return;
      ledAskClose();
      try { a[1](); } catch (e) { toast(e.message); }
    };
    foot.appendChild(b);
  });
  box.style.display = 'flex';
  ok.onclick = function () {
    if (opt.readOnly) { ledAskClose(); if (opt.after) { try { opt.after(); } catch (e) { } } return; }
    var out = {};
    fields.forEach(function (f) { var el = document.getElementById(f.id); if (el) out[f.id] = el.value; });
    if (!fields.length) { ledAskClose(); return; }
    if (opt.check && !opt.check(out)) return;
    ledAskClose();
    try { cb(out); } catch (e) { toast(e.message); }
  };
}
function ledAskClose() { var b = document.getElementById('ledAsk'); if (b) b.style.display = 'none'; }

/* ⚙ 管理：账户 / 分类 / 账本 / 预算，一个面板全收 */
/* ══ v9.9：⚙ 管理 = 分段标签页（左右滑或点标签切换），下面只显示当前段的条目 ══ */
var LED_TABS = [['acc', '账户'], ['exp', '支出分类'], ['inc', '收入分类'], ['book', '账本'], ['data', '数据'], ['bud', '预算']];
var LED_TAB_HIDE = ['bud'];            /* 预算段暂时收起（代码留着，去掉这行就回来） */
var _ledTab = 'acc';

function ledAdmin(tab) {
  if (tab) _ledTab = tab;
  var box = document.getElementById('ledPanel');
  if (!box) {
    box = document.createElement('div');
    box.id = 'ledPanel'; box.className = 'modal-mask'; box.style.display = 'none';
    box.innerHTML = '<div class="modal modal-center led-sheet"><div class="modal-head">'
      + '<button class="head-btn" onclick="ledPanelClose()">返回</button>'
      + '<div class="modal-title">管理</div>'
      + '<button class="head-btn" id="ledPanelAdd">＋</button></div>'
      + '<div class="led-tabs" id="ledTabs"></div>'
      + '<div class="modal-body" id="ledPanelB"></div></div>';
    document.body.appendChild(box);
    var x0 = 0;
    box.addEventListener('touchstart', function (e) { x0 = (e.touches[0] || {}).clientX || 0; }, { passive: true });
    box.addEventListener('touchend', function (e) {
      var d = ((e.changedTouches[0] || {}).clientX || 0) - x0;
      if (Math.abs(d) < 46) return;
      var i = LED_TABS.map(function (t) { return t[0]; }).indexOf(_ledTab);
      i = Math.max(0, Math.min(LED_TABS.length - 1, i + (d < 0 ? 1 : -1)));
      ledAdmin(LED_TABS[i][0]);
    }, { passive: true });
  }
  ledTabsEl().innerHTML = LED_TABS.filter(function (t) { return LED_TAB_HIDE.indexOf(t[0]) < 0; }).map(function (t) {
    return '<button type="button" class="led-tab' + (t[0] === _ledTab ? ' on' : '') + '" data-t="' + t[0] + '">'
      + t[1 + 0] + '</button>';
  }).join('');
  [].forEach.call(document.querySelectorAll('#ledTabs .led-tab'), function (b) {
    b.onclick = function () {
      ledAdmin(b.dataset.t);
    };
  });
  document.getElementById('ledPanelB').innerHTML = ledTabHtml();
  var add = document.getElementById('ledPanelAdd');
  add.onclick = ledTabAdd;
  add.classList.toggle('showadd', ['acc', 'exp', 'inc', 'book'].indexOf(_ledTab) >= 0);
  box.style.display = 'flex';
  box.onclick = function (e) { if (e.target === box) ledPanelClose(); };
}

function ledTabHtml() {
  var st = ledState.setup || {};
  var sm = ledState.summary || {};
  var bud = {};
  (sm.budgets || []).forEach(function (b) { bud[b.name] = b; });
  var used = {};
  (((sm.by_category || {}).expense) || []).forEach(function (c) { used[c.name] = c; });
  (((sm.by_category || {}).income) || []).forEach(function (c) { used[c.name] = c; });
  var row = function (ic, nm, right, key, acts) {
    return '<div class="am-row" data-k="' + key + '"><span class="am-ic">' + (ic || '') + '</span>'
      + '<span class="am-nm">' + esc(nm) + '</span><span class="am-r">' + (right || '') + '</span>'
      + (acts || '') + '</div>';
  };
  var first = function (s2) { return '<i>' + esc(String(s2 || '?').slice(0, 1)) + '</i>'; };
  var h = '';
  if (_ledTab === 'acc') {
    h = (st.accounts || []).map(function (a) {
      return row(a.icon ? esc(a.icon) : first(a.name), a.name + (a.archived ? '\uff08\u5df2\u505c\u7528\uff09' : ''),
                 ledMoney(a.balance), 'A' + a.id);
    }).join('') || '<div class="led-quiet">\u8fd8\u6ca1\u6709\u8d26\u6237\uff0c\u70b9\u53f3\u4e0a\u89d2 \uff0b \u52a0\u4e00\u4e2a</div>';
  } else if (_ledTab === 'exp' || _ledTab === 'inc') {
    var kind = _ledTab === 'inc' ? 'income' : 'expense';
    h = ((st.categories || {})[kind] || []).map(function (c) {
      var bits = [];
      var b = c.name in bud ? bud[c.name] : (kind === 'expense' ? null : null);
      if (b) bits.push('\u9884\u7b97' + b.limit_text + ' \u00b7 \u7528' + b.used_text);
      else if (c.name in used) bits.push('\u672c\u6708 ' + ledMoney(used[c.name].cents));
      bits.push('\u7b2c' + (c.sort == null ? 0 : c.sort) + '\u4f4d');
      return row(c.icon ? esc(c.icon) : first(c.name), c.name, bits.join(' '), 'C' + c.id);
    }).join('') || '<div class="led-quiet">\u8fd8\u6ca1\u6709\u8fd9\u4e00\u7c7b\uff0c\u70b9\u53f3\u4e0a\u89d2 \uff0b \u52a0</div>';
  } else if (_ledTab === 'book') {
    h = (ledState.books || []).map(function (b) {
      return row(b.id === ledState.book ? '\u2713' : '\u00b7', b.name + (b.archived ? '\uff08\u5df2\u5f52\u6863\uff09' : ''),
                 (b.entries || 0) + ' \u7b14', '',
                 '<span class="am-acts"><button class="am-go" data-bk="' + b.id + '">\u270e \u6539\u540d</button>'
                 + '<button class="am-go danger" data-del="' + b.id + '">\ud83d\uddd1 \u5220\u8fd9\u672c</button></span>');
    }).join('') || '<div class="led-quiet">\u8fd8\u6ca1\u6709\u8d26\u672c\uff0c\u70b9\u53f3\u4e0a\u89d2 \uff0b \u65b0\u5efa</div>';
  } else {
    h = row('\u2b07', '\u5bfc\u5165\u8d26\u5355', '\u5fae\u4fe1 xlsx / \u652f\u4ed8\u5b9d csv', 'Ximp')
      + row('\u2b06', '\u5bfc\u51fa CSV', 'Excel / WPS \u80fd\u76f4\u63a5\u5f00', 'Xexp')
      + row('\ud83d\uddd1', '\u8bb0\u8d26\u5783\u573e\u6876', '\u6d41\u6c34 / \u8d26\u6237 / \u5206\u7c7b / \u6574\u672c\u8d26', 'Xtrsh');
  }
  setTimeout(function () {
    [].forEach.call(document.querySelectorAll('#ledPanelB .am-go'), function (btn) {
      btn.onclick = function (ev) {
        ev.stopPropagation();
        if (btn.dataset.del) {                        /* 删这本：整本先进记账垃圾桶 30 天 */
          var bid = parseInt(btn.dataset.del, 10);
          var bb = (ledState.books || []).filter(function (x) { return x.id === bid; })[0] || {};
          if (!confirm('删掉账本「' + (bb.name || '') + '」？\n\n整本（流水/分类/账户）先存进垃圾桶 30 天，可以整本恢复。')) return;
          api('/api/ledger/books/' + bid + '?wipe=1', { method: 'DELETE' })
            .then(function (r) { if (ledState.book === bid) ledState.book = 0;
              ledChanged('book', '删了（' + (r.entries || 0) + ' 笔进桶，「数据 → 记账垃圾桶」可整本恢复）'); })
            .catch(function (e) { toast(e.message); });
          return;
        }
        ledBookEdit(parseInt(btn.dataset.bk, 10));
      };
    });
    [].forEach.call(document.querySelectorAll('#ledPanelB .am-row[data-bk]'), function (r) {
      r.onclick = function () {                        /* 点这一行＝切到这本 */
        ledState.book = parseInt(r.dataset.bk, 10);
        try { localStorage.setItem('calnote-ledger-book', String(ledState.book)); } catch (e) { }
        ledChanged('book', '');
      };
    });
    [].forEach.call(document.querySelectorAll('#ledPanelB .am-row'), function (r) {
      r.onclick = function () {
        var k = r.dataset.k || '';
        if (k.charAt(0) === 'A') {
          var a = (st.accounts || []).filter(function (x) { return 'A' + x.id === k; })[0];
          if (a) ledAccEdit(a);
          return;
        }
        if (k.charAt(0) === 'C') {
          var all = ((st.categories || {}).expense || []).concat((st.categories || {}).income || []);
          var c = all.filter(function (x) { return 'C' + x.id === k; })[0];
          if (c) ledCatEdit(c);
          return;
        }
        if (k === 'Ximp') { ledImportDialog(); return; }
        if (k === 'Xexp') { ledDoExport(); return; }
        if (k === 'Xtrsh') { ledTrash(); return; }
      };
    });
  }, 0);
  return h;
}
function ledTabItems() {
  var st = ledState.setup || {};
  if (_ledTab === 'acc') return st.accounts || [];
  if (_ledTab === 'exp') return (st.categories || {}).expense || [];
  if (_ledTab === 'inc') return (st.categories || {}).income || [];
  if (_ledTab === 'bud') return ((ledState.summary || {}).by_category || {}).expense || [];
  return [];
}
function ledTabAdd() {
  /* ＋ 按段新增：账户 / 支出分类 / 收入分类 / 账本（都走同一套：提交→ledChanged→自动重画） */
  if (_ledTab === 'book') {
    ledAsk('\u65b0\u8d26\u672c', [{ id: 'n', label: '\u540d\u5b57\uff08\u5982\uff1a\u5382\u91cc / \u5b66\u8d39\uff09' },
      { id: 'note', label: '\u5907\u6ce8\uff08\u53ef\u7a7a\uff09' }], function (v) {
      if (!v.n || !v.n.trim()) return;
      api('/api/ledger/books', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: v.n.trim(), note: (v.note || '').trim() }) })
        .then(function (r) { ledState.book = (r.book || {}).id || ledState.book; ledChanged('book', '\u8d26\u672c\u5efa\u597d\u4e86'); })
        .catch(function (e) { toast(e.message); });
    });
    return;
  }
  if (_ledTab === 'acc') {
    ledAsk('\u65b0\u8d26\u6237', [
      { id: 'n', label: '\u540d\u5b57\uff08\u5982\uff1a\u5de5\u8d44\u5361\uff09' },
      { id: 'ic', label: '\u56fe\u6807\uff08\u70b9\u4e00\u4e2a emoji\uff09', type: 'emoji', value: '' },
      { id: 'op', label: '\u671f\u521d\u4f59\u989d\uff08\u73b0\u5728\u91cc\u9762\u6709\u591a\u5c11\u94b1\uff0c\u5143\uff09', value: '0' }], function (v) {
      if (!v.n || !v.n.trim()) return;
      api('/api/ledger/accounts', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ book: ledState.book, name: v.n.trim(), opening: v.op, icon: v.ic }) })
        .then(function () { ledChanged('acc', '\u8d26\u6237\u52a0\u597d\u4e86'); }).catch(function (e) { toast(e.message); });
    });
    return;
  }
  if (_ledTab === 'exp' || _ledTab === 'inc') {
    var kind = _ledTab === 'inc' ? 'income' : 'expense';
    ledAsk('\u65b0' + (kind === 'income' ? '\u6536\u5165' : '\u652f\u51fa') + '\u5206\u7c7b', [
      { id: 'n', label: '\u540d\u5b57\uff08\u5982\uff1a\u9493\u9c7c / \u62a5\u9500\uff09' },
      { id: 'ic', label: '\u56fe\u6807\uff08\u70b9\u4e00\u4e2a emoji\uff0c\u53ef\u4e0d\u9009\uff09', type: 'emoji', value: '' }], function (v) {
      if (!v.n || !v.n.trim()) return;
      api('/api/ledger/categories', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ book: ledState.book, name: v.n.trim(), kind: kind, icon: v.ic || '' }) })
        .then(function () { ledChanged(_ledTab, '\u5206\u7c7b\u52a0\u597d\u4e86'); })
        .catch(function (e) { toast('\u52a0\u5206\u7c7b\u5931\u8d25\uff1a' + e.message); });
    });
    return;
  }
}

function ledBudgetFor(c) {
  ledAsk('「' + c.name + '」本月预算', [{ id: 'amt', label: '预算多少元（填 0 = 取消预算）', value: '' }], function (v) {
    api('/api/ledger/budgets', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ book: ledState.book, category_name: c.name, amount: String(v.amt || '0').trim() }) })
      .then(function (r) { toast(c.name + ' 预算 ' + (r.amount || v.amt) + ' 元'); ledLoad(); ledAdmin('bud'); })
      .catch(function (e) { toast('设预算失败：' + e.message); });
  });
}function ledPanelClose() { var b = document.getElementById('ledPanel'); if (b) b.style.display = 'none'; }

function ledAccEdit(a) {
  ledAsk('账户「' + a.name + '」', [
    { id: 'n', label: '名字', value: a.name },
    { id: 'ic', label: '图标（点一个）', type: 'emoji', value: a.icon || '' },
    { id: 'op', label: '期初余额（现在里面有多少钱，元）', value: (a.opening != null ? a.opening : 0) }],
    function (v) {
      var body = {};
      if (v.n && v.n.trim() && v.n.trim() !== a.name) body.name = v.n.trim();
      if (v.ic !== undefined && v.ic !== (a.icon || '')) body.icon = v.ic;
      if (v.op !== '' && v.op != null) body.opening = v.op;
      if (!Object.keys(body).length) return;
      api('/api/ledger/accounts/' + a.id, { method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body) }).then(function () { ledLoad(); ledAdmin('acc'); toast('账户改好了'); })
        .catch(function (e) { toast(e.message); });
    }, { extra: [
      ['○ 停用（下拉里不再出现，账全留）', function () {
        api('/api/ledger/accounts/' + a.id, { method: 'DELETE' })
          .then(function () { ledLoad(); ledAdmin('acc'); toast('已停用'); }).catch(function (e) { toast(e.message); });
      }],
      ['🗑 删除账户', function () {
        if (!confirm('删掉账户「' + a.name + '」？\n\n里面的流水不会被删，只把它们的账户留空；账户本身进垃圾桶 30 天，可恢复。')) return;
        api('/api/ledger/accounts/' + a.id + '?wipe=1', { method: 'DELETE' })
          .then(function () { ledLoad(); ledAdmin('acc'); toast('删了（进垃圾桶，可恢复）'); })
          .catch(function (e) { toast(e.message); });
      }, true]
    ] });
}function ledCatEdit(c) {
  ledAsk('分类「' + c.name + '」', [
    { id: 'n', label: '名字', value: c.name },
    { id: 'ic', label: '图标（点一个）', type: 'emoji', value: c.icon || '' },
    { id: 'so', label: '排第几（小的在前）', type: 'text', value: c.sort == null ? 99 : c.sort }], function (v) {
    var body = {};
    if (v.n && v.n.trim() && v.n.trim() !== c.name) body.name = v.n.trim();
    if (v.ic !== undefined && v.ic !== (c.icon || '')) body.icon = v.ic;
    if (v.so !== '' && v.so != null) body.sort = parseInt(v.so, 10) || 99;
    if (!Object.keys(body).length) return;
    api('/api/ledger/categories/' + c.id, { method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body) }).then(function () { ledChanged(_ledTab, '分类改好了'); })
      .catch(function (e) { toast(e.message); });
  }, { extra: [['🗑 删除这个分类', function () {
    if (!confirm('删掉分类「' + c.name + '」？\n\n它下面的流水不会丢，会自动挂到「其他」；分类本身进垃圾桶 30 天，可恢复。')) return;
    api('/api/ledger/categories/' + c.id + '?force=1', { method: 'DELETE' })
      .then(function () { ledLoad(); toast('删了（流水已挪到其他分类）'); }).catch(function (e) { toast(e.message); });
  }, true]] });
}function ledCatAdd(kind) {
  ledAsk('\u65b0\u5206\u7c7b', [
    { id: 'n', label: '\u540d\u5b57\uff08\u5982\uff1a\u9493\u9c7c / \u62a5\u9500\uff09' },
    { id: 'ic', label: '\u56fe\u6807\uff08\u70b9\u4e00\u4e2a\uff0c\u53ef\u4e0d\u9009\uff09', type: 'emoji', value: '' },
    { id: 'k', label: '\u6536\u652f\u65b9\u5411', type: 'select', value: kind || 'expense',
      opts: [['expense', '\u652f\u51fa'], ['income', '\u6536\u5165']] }], function (v) {
    if (!v.n || !v.n.trim()) return;
    api('/api/ledger/categories', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ book: ledState.book, name: v.n.trim(), kind: v.k || 'expense', icon: v.ic }) })
      .then(function () { ledLoad(); toast('\u52a0\u597d\u4e86'); }).catch(function (e) { toast(e.message); });
  });
}

/* ═══ 宽屏：记账插到日历列和编辑栏中间（可拖宽），手机照旧弹层 ═══ */
(function () {
  var o = window.ledOpen;
  if (o && !o._w) {
    window.ledOpen = function () { var r = o.apply(this, arguments);
      try { if (typeof isWide === 'function' && isWide()) { document.body.classList.add('cal-ledopen');
        document.body.classList.remove('cal-popened');          // 中间一栏一次只放一个：开记账就收分类
        setTimeout(function () { try { initLayoutUI(); } catch (e) { } }, 80); } } catch (e) { } return r; };
    window.ledOpen._w = 1;
  }
  var c = window.ledClose;
  if (c && !c._w) {
    window.ledClose = function () { var r = c.apply(this, arguments);
      try { document.body.classList.remove('cal-ledopen');
        setTimeout(function () { try { initLayoutUI(); } catch (e) { } }, 80); } catch (e) { } return r; };
    window.ledClose._w = 1;
  }
})();
/* 点分类统计条 → 这个分类本月的每一笔（点其中一笔可直接改） */
function ledCatDetail(cid, kind) {
  var rows = (ledState.list || []).filter(function (e) { return e.category && e.category.id === cid; });
  var st = ledState.setup || {};
  var all = ((st.categories || {})[kind === 'in' ? 'income' : 'expense'] || []);
  var nm = (all.filter(function (x) { return x.id === cid; })[0] || {}).name || '这一类';
  ledAsk('「' + nm + '」本月 ' + rows.length + ' 笔', [], function () { ledLoad(); });
  var body = document.getElementById('ledAskB');
  body.innerHTML = (rows.length ? rows.map(function (e) {
    return '<div class="led-entry sm" onclick="ledAskClose();ledEditEntry(' + e.id + ')">'
      + '<span class="led-day">' + esc((e.day || '').slice(8)) + '</span>'
      + '<span class="led-cat">' + esc(e.note || nm) + '</span>'
      + '<b class="' + (e.dir === 'in' ? 'led-in' : 'led-out') + '">' + ledMoney(e.cents) + '</b></div>';
  }).join('') : '<div class="led-quiet">这个分类这个月没有可显示的流水（或超出已加载条数）</div>');
  var ok = document.getElementById('ledAskOk');
  if (ok) { ok.textContent = '关闭'; ok.onclick = function () { ledAskClose(); }; }
}
/* 点标题位选账本（当前打 ✓，最后一项是新建） */
function ledBookPick() { ledAdmin('book'); }     /* 标题位选账本 = 管理的「账本」段（点名字切换、✎ 设置、＋ 新建） */
function ledPanel2(title, acts) {
  ledAdmin('book');
  var b = document.getElementById('ledPanel'); if (!b) return;
  var _t2 = document.getElementById('ledPanelT'), _b2 = document.getElementById('ledPanelB');
  if (!_t2 || !_b2) {                      /* 容器是旧结构：整块重建一次 */
    box.innerHTML = '<div class="modal modal-center led-sheet"><div class="modal-head">'
      + '<button class="head-btn" onclick="ledPanelClose()">返回</button>'
      + '<div class="modal-title" id="ledPanelT"></div>'
      + '<button class="head-btn" id="ledPanelAdd" style="display:none">＋</button></div>'
      + '<div class="led-tabs" id="ledTabs" style="display:none"></div>'
      + '<div class="modal-body" id="ledPanelB"></div></div>';
    _t2 = document.getElementById('ledPanelT'); _b2 = document.getElementById('ledPanelB');
  }
  _t2.textContent = title;
  _b2.innerHTML = acts.map(function (a, i) {
    return '<button type="button" class="led-act" data-i="' + i + '">' + esc(a[0]) + '</button>';
  }).join('');
  [].forEach.call(b.querySelectorAll('#ledPanelB .led-act'), function (el) {
    el.onclick = function () { ledPanelClose(); try { acts[parseInt(el.dataset.i, 10)][1](); } catch (e) { toast(e.message); } };
  });
}
/* 某个账本的设置：改名/备注/共享/归档/彻底删除 */
function ledBookEdit(bid) {
  /* 一本账的设置：名字/备注 + 归档 + 彻底删除（删除永远只有最底这一个） */
  var b = (ledState.books || []).filter(function (x) { return x.id === bid; })[0] || {};
  ledAsk('\u8d26\u672c\u300c' + (b.name || '') + '\u300d\uff08' + (b.entries || 0) + ' \u7b14\uff09', [
    { id: 'n', label: '\u540d\u5b57', value: b.name || '' },
    { id: 'note', label: '\u5907\u6ce8\uff08\u53ef\u7a7a\uff09', value: b.note || '' }], function (v) {
    var o = {};
    if (v.n && v.n.trim() && v.n.trim() !== b.name) o.name = v.n.trim();
    if (v.note !== undefined) o.note = v.note.trim();
    if (!Object.keys(o).length) return;
    api('/api/ledger/books/' + bid, { method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(o) }).then(function () { ledChanged('book', '\u8d26\u672c\u6539\u597d\u4e86'); })
      .catch(function (e) { toast(e.message); });
  }, { foot: [
    { t: '\u25cb \u5f52\u6863\u8fd9\u672c\uff08\u4ece\u5217\u8868\u9690\u85cf\uff0c\u8d26\u90fd\u7559\uff09', fn: function () {
        api('/api/ledger/books/' + bid, { method: 'DELETE' })
          .then(function () { ledState.book = 0; ledChanged('book', '\u5df2\u5f52\u6863'); }).catch(function (e) { toast(e.message); });
      } },
    { t: '\ud83d\uddd1 \u5f7b\u5e95\u5220\u9664\u8fd9\u672c\uff08\u6574\u672c\u8fdb\u5783\u573e\u6876 30 \u5929\uff09', danger: 1,
      confirm: '\u628a\u300c' + (b.name || '') + '\u300d\u6574\u672c\u5220\u6389\uff1f\n\n\u6d41\u6c34/\u5206\u7c7b/\u8d26\u6237\u4f1a\u5148\u5b58\u8fdb\u5783\u573e\u6876 30 \u5929\uff0c\u53ef\u6574\u672c\u6062\u590d\u3002',
      fn: function () {
        api('/api/ledger/books/' + bid + '?wipe=1', { method: 'DELETE' })
          .then(function (r) { ledState.book = 0; ledChanged('book', '\u5220\u4e86\uff08' + (r.entries || 0) + ' \u7b14\u5df2\u8fdb\u6876\uff09'); })
          .catch(function (e) { toast(e.message); });
      } }
  ] });
}

function ledTabsEl() {
  var t = document.getElementById('ledTabs');
  if (t) return t;
  var box = document.getElementById('ledPanel');
  if (!box) return null;
  var head = box.querySelector('.modal-head') || box;
  t = document.createElement('div');
  t.id = 'ledTabs'; t.className = 'led-tabs';
  head.insertAdjacentElement('afterend', t);
  return t;
}

/* v9.5：所有增/删/改之后统一走这里（之前这函数定义被去重误删，导致点了没反应、必须手动刷新）*/
function ledChanged(tab, msg) {
  if (msg) toast(msg);
  if (tab) _ledTab = tab;
  var box = document.getElementById('ledPanel');
  var wasOpen = !!box && getComputedStyle(box).display !== 'none';
  var fin = function () { if (wasOpen) { try { ledAdmin(_ledTab); } catch (e) { } } };
  var done = 0, need = 3;
  var step = function () { if (++done >= need) fin(); };
  api('/api/ledger/setup' + (ledState.book ? '?book=' + ledState.book : ''))
    .then(function (r) { ledState.setup = r; }).catch(function () { }).then(step);
  api('/api/ledger/summary?month=' + (((ledState.summary || {}).month) || todayStr().slice(0, 7)) +
      (ledState.book ? '&book=' + ledState.book : ''))
    .then(function (r) { ledState.summary = r; }).catch(function () { }).then(step);
  api('/api/ledger/books').then(function (r) { ledState.books = r.books || []; })
    .catch(function () { }).then(step);
  try { if (typeof ledLoad === 'function') ledLoad(); } catch (e) { }
  return wasOpen;
}
