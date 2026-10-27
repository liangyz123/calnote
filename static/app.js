/* ==================== 日历便签 v5 前端逻辑 ==================== */

// ---------- 状态 ----------
let curYear = 0, curMonth = 0;      // 当前显示的年月
let selectedDate = '';              // 当前选中的日期 YYYY-MM-DD
let monthItems = [];                // 当月条目（日历渲染）
let monthInfo = {};                 // 当月农历信息
let dayData = null;                 // 当天：内容 + 黄历 + 天气
let typeCounts = {};                // 各类型总数（抽屉里显示）
let editId = null;                  // 正在编辑的条目 id（null=新建）
let editType = 'note';
let folders = [];                 // v5.12 自建文件夹
let editFolder = 0;               // 正在编辑的条目归属文件夹
let itemCache = {};                 // 已加载条目缓存（openEdit 用）
let pendingFiles = [];              // 新选中的待上传图片
let pendingRefs = [];               // v6.0：等条目建好后挂上去的 NAS 引用路径
var nasState = { dir: '', q: '', only: '', sel: {}, entries: [], view: 'list', acc: [], shown: 0, more: false, tries: 0 };
let upQ = [];                       // v5.14：{file,key,ok,err} —— 按指纹去重，传过的不再传
function _fkey(f) { return f.name + '|' + f.size + '|' + (f.lastModified || 0); }
function queueFiles(files) {
  let added = 0;
  files.forEach(f => {
    const k = _fkey(f);
    if (upQ.some(x => x.key === k)) return;              // 同一份文件不重复上传
    if (f.size > 8 * 1024 * 1024 &&
        !confirm('「' + f.name + '」有 ' + Math.round(f.size / 1048576) + 'MB，用域名访问可能被拦。还是要传吗？')) return;
    upQ.push({ file: f, key: k, ok: false, err: '' });
    added++;
  });
  return added;
}
async function flushUploads(itemId) {
  const list = upQ.slice();
  if (!list.length) return { ok: 0, bad: 0 };
  let i = 0, done = 0, bad = 0;
  async function worker() {
    while (i < list.length) {
      const it = list[i++];
      try {
        const form = new FormData();
        form.append('file', it.file);
        const r = await fetch('/api/items/' + itemId + '/attachments', { method: 'POST', body: form });
        if (!r.ok) throw new Error('接口 ' + r.status);
        it.ok = true; done++;
      } catch (e) {
        it.err = String((e && e.message) || e).slice(0, 40); bad++;
      }
    }
  }
  await Promise.all([worker(), worker(), worker()]);        // 三个一批，不再一张一张排
  upQ = list.filter(x => !x.ok);                            // 只留失败的，可单独重试
  return { ok: done, bad: bad };
}
function retryUploads(itemId) {
  if (!itemId) { toast('先点完成保存，再重传附件'); return; }
  toast('重传中…');
  flushUploads(itemId).then(r => {
    if (r.bad) { toast('还有 ' + r.bad + ' 个传不上（多半是域名代理限制体积）'); }
    else { toast('附件全部传上了'); loadDay(); }
    renderPending();
  });
}
function renderPending() {                                   // 没传成功的排在附件行末尾
  const box = document.getElementById('attachList');
  if (!box) return;
  [...box.querySelectorAll('.up-pend')].forEach(n => n.remove());
  upQ.forEach(it => {
    const d = document.createElement('div');
    d.className = 'file-chip up-pend' + (it.err ? ' upfail' : '');
    const im = /^image\//.test(it.file.type || '');
    d.innerHTML = `<span class="fi">${im ? '🖼' : '📄'}</span><span class="fn">${esc(it.file.name)}</span>
      <span class="fs">${it.err ? ('✕ ' + esc(it.err)) : '待传'}</span>
      <button class="fd" onclick="event.stopPropagation();retryUploads(editId)">⟳</button>
      <button class="del" onclick="event.stopPropagation();this.parentNode.remove();upQ=upQ.filter(x=>x.key!=='${it.key}');syncAttachFold();">✕</button>`;
    box.appendChild(d);
  });
  syncAttachFold();
}
let collapsed = false;              // 日历是否折叠
let searchState = { q: '', type: '', total: 0, list: [], offset: 0 };

const LIMIT_NOTE_TITLES = 5200;     // 笔记标题用日期，日历格子里不必铺开

// ---------- 类型配置 ----------
const TYPES = [
  { key: 'schedule', name: '普通日程' },
  { key: 'birthday', name: '生日' },
  { key: 'anniversary', name: '纪念日' },
  { key: 'countdown', name: '倒数日' },
  { key: 'note', name: '笔记' },
  { key: 'todo', name: '待办' },
];
const TYPE_NAME = Object.fromEntries(TYPES.map(t => [t.key, t.name]));

// ---------- 小工具 ----------
function fmtDate(d) {
  const y = d.getFullYear(), m = String(d.getMonth() + 1).padStart(2, '0'), day = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
}
function todayStr() { return fmtDate(new Date()); }
function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
async function api(url, options) {
  const res = await fetch(url, options);
  if (res.status === 401) {   // 登录过期（默认 30 天）或被别人退出：直接去登录页
    location.href = '/login?next=' + encodeURIComponent(location.pathname + location.search);
    throw new Error('请先登录');
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || '请求失败');
  return data;
}
function cacheItems(list) { (list || []).forEach(i => { itemCache[i.id] = i; }); }
function itemDay(item) { return item.yearly ? item.this_year_date : item.date; }
function daysUntil(dateStr) {
  const t = new Date(dateStr + 'T00:00:00');
  const now = new Date(todayStr() + 'T00:00:00');
  return Math.round((t - now) / 86400000);
}
function wordCount(s) { return (s || '').replace(/\s/g, '').length; }

// ---------- 月份加载 ----------
async function loadMonth() {
  const monthStr = `${curYear}-${String(curMonth).padStart(2, '0')}`;
  document.getElementById('monthTitle').textContent = `${curYear}年${curMonth}月`;
  try {
    const [itemsRes, infoRes] = await Promise.all([
      api(`/api/items?month=${monthStr}`),
      api(`/api/calendar-info?month=${monthStr}`),
    ]);
    monthItems = itemsRes.items;
    monthInfo = infoRes.calendar || {};
    window.monthLedger = itemsRes.ledger_daily || {};   // v9 记账：当月每日支出映射
    try { if (window.ledRefreshMonth) window.ledRefreshMonth(); } catch (e) { }
    cacheItems(monthItems);
    renderMonth();
    await loadDay();
    loadTypeCounts();
  loadFolders();
  } catch (e) {
    alert('加载失败：' + e.message);
  }
}
async function loadTypeCounts() {
  try {
    const res = await api('/api/stats');          // v5.13：原来直接用 fetch 的 Response 当 JSON 用，计数永远是空的
    typeCounts = res.counts || {};
    renderCatList();          // v5.13：原来判断 display==='block'（实际是 flex）→ 计数永远不刷新
  } catch (e) { /* 抽屉没打开时忽略 */ }
}
async function loadDay(live) {
  try {
    dayData = await api('/api/day?date=' + selectedDate + (live ? '&live=1' : ''));
    cacheItems(dayData.items);
  } catch (e) {
    dayData = { items: [], almanac: null, weather: '', counts: {}, fetchable: 0 };
  }
  renderDayHead();
  renderDayContent();
}
function prevMonth() { curMonth--; if (curMonth < 1) { curMonth = 12; curYear--; } loadMonth(); }
function nextMonth() { curMonth++; if (curMonth > 12) { curMonth = 1; curYear++; } loadMonth(); }
function goToday() {
  const t = new Date();
  curYear = t.getFullYear(); curMonth = t.getMonth() + 1;
  selectedDate = todayStr();
  loadMonth();
  scrollToDay();
}

// ---------- 月视图渲染（周日起始）----------
function renderMonth() {
  const grid = document.getElementById('calendarGrid');
  const first = new Date(curYear, curMonth - 1, 1);
  const offset = first.getDay();
  const daysInMonth = new Date(curYear, curMonth, 0).getDate();
  const prevDays = new Date(curYear, curMonth - 1, 0).getDate();
  const cells = [];
  const now = todayStr();

  const byDay = {};
  monthItems.forEach(item => {
    const key = itemDay(item);
    if (!byDay[key]) byDay[key] = [];
    byDay[key].push(item);
  });

  for (let i = 0; i < 42; i++) {
    let d, inMonth = true;
    if (i < offset) { d = new Date(curYear, curMonth - 2, prevDays - offset + 1 + i); inMonth = false; }
    else if (i >= offset + daysInMonth) { d = new Date(curYear, curMonth, i - offset - daysInMonth + 1); inMonth = false; }
    else { d = new Date(curYear, curMonth - 1, i - offset + 1); }
    const ds = fmtDate(d);
    const info = monthInfo[ds] || {};
    const items = byDay[ds] || [];
    const isToday = ds === now;
    const isSelected = ds === selectedDate;

    // 农历 / 节日 / 节气（能显示完整：靠 CSS 的自动缩放 + 不裁切）
    let extra;
    if ((info.festivals || []).length) extra = `<div class="day-festival">${esc(info.festivals[0])}</div>`;
    else if (info.jieqi) extra = `<div class="day-jieqi">${esc(info.jieqi)}</div>`;
    else extra = `<div class="day-lunar">${esc(info.dayCn || '')}</div>`;

    // 格子里只放点：有内容就一个点，不同类型不同颜色（同类型只画一个），再多放个条数
    let itemHtml = '';
    const rep = items.some(i => i.yearly && i.type === 'birthday');
    if (items.length) {
      const kinds = [];
      items.forEach(i => { if (kinds.indexOf(i.type) < 0) kinds.push(i.type); });
      const dots = kinds.slice(0, 4).map(ty => `<i class="dot t-${ty}"></i>`).join('');
      itemHtml = `<div class="day-dots">${dots}${items.length > 4 ? `<span class="day-more">${items.length}</span>` : ''}</div>`;
    }
    const noteMark = items.some(i => i.type === 'note') ? '<span class="note-mark">笔</span>' : '';
    const led = (window.monthLedger || {})[ds];
    const ledMark = led && led.out ? `<span class="day-money">-${ledFen(led.out)}</span>`
                  : (led && led.in ? `<span class="day-money in">+${ledFen(led.in)}</span>` : '');

    cells.push(`
      <div class="day-cell ${inMonth ? '' : 'other-month'} ${isToday ? 'today' : ''} ${isSelected ? 'selected' : ''}" data-im="${inMonth ? 1 : 0}" onclick="pickDay('${ds}')">
        <div class="day-top"><span class="day-num">${d.getDate()}</span>${noteMark}${rep ? '<span class="bday-mark">生</span>' : ''}${ledMark}</div>
        ${extra}
        <div class="day-items">${itemHtml}</div>
      </div>`);
  }
  // v5.12：只显示「含本月日期」的周；月初不在周日就补空白格，日期才对得上星期列
  const weeks = [];
  for (let i = 0; i < cells.length; i += 7) {
    const wk = cells.slice(i, i + 7);
    if (wk.some(c => c.indexOf('data-im="1"') >= 0)) weeks.push(wk);
  }
  let out = [];
  weeks.forEach((wk, wi) => {
    const first = wk.findIndex(c => c.indexOf('data-im="1"') >= 0);
    if (wi === 0 && first > 0) {                    // 月初不在周日：前面补空白格，上月的格子丢掉
      for (let b = 0; b < first; b++) out.push('<div class="day-cell blank"></div>');
      wk = wk.slice(first);
    }
    out = out.concat(wk);                          // 首周 = 空白格 + 本月前几天，正好 7 格
  });
  grid.innerHTML = out.join('');
}

// ---------- 点日期：只看当天 ----------
function pickDay(dateStr) {
  const changed = dateStr !== selectedDate;
  selectedDate = dateStr;
  renderMonth();
  loadDay().then(() => { if (changed && window.innerWidth < 768) scrollToDay(); });
  if (searchViewOpen()) closeSearch();
}
function scrollToDay() {
  document.getElementById('dayTitle').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

// ---------- 当天头部卡：天气 + 黄历 ----------
function renderDayHead() {
  const box = document.getElementById('dayHead');
  if (!dayData) { box.innerHTML = ''; return; }
  const a = dayData.almanac || {};
  const d = new Date(selectedDate + 'T00:00:00');
  const weather = dayData.weather || '';
  const fest = (a.festivals || []).join('、');
  const chips = [];
  if (a.lunarMonthDay) chips.push(`农历${esc(a.lunarMonthDay)}`);
  if (a.ganzhiDay) chips.push(`${esc(a.ganzhiYear)}年 ${esc(a.ganzhiMonth)}月 ${esc(a.ganzhiDay)}日`);
  if (a.jieqi) chips.push(`节气·${esc(a.jieqi)}`);
  if (fest) chips.push(esc(fest));
  if (a.chong) chips.push(`冲${esc(a.chong)} 煞${esc(a.sha)}`);
  const bds = ((dayData && dayData.items) || []).filter(i => i.type === 'birthday');
  if (bds.length) chips.unshift(bds.map(i => {
    const t = i.age_text || (i.birth_year ? _ageOf(i.birth_year, i.this_year_date || i.date) + '岁' : '');
    return '🎂 ' + i.title + (t ? ' · ' + t : '');
  }).join('　'));
  // 天气：有就显示；没有但能联网取 → 给个「取天气」按钮
  let wBtn = '';
  const staleWx = !!weather && String(dayData.weather_src || '').indexOf('open-meteo') !== 0;   // 导入带来的旧文本
  if (weather) {
    if ((dayData.items || []).length && (dayData.items || []).every(i => !i.weather)) {
      wBtn = `<button class="mini-btn" onclick="saveDayWeather(event)">写进当天</button>`;
    } else {
      wBtn = `<button class="mini-btn${staleWx ? ' warn' : ''}" onclick="fetchDayWeather(event)">${staleWx ? '按实测刷新' : '重新校准'}</button>`;
    }
  } else if (dayData.fetchable) {
    wBtn = `<button class="mini-btn" onclick="fetchDayWeather(event)">取天气</button>`;
  }
  box.innerHTML = `
    <div class="dh-top">
      <div class="dh-date">${d.getMonth() + 1}月${d.getDate()}日 <span>${esc((a.week || '').replace('星期', '周'))}</span></div>
      <div class="dh-weather">${esc(weather || '没有天气记录')} ${wBtn}</div>
    </div>
    <div class="dh-chips">${chips.map(c => `<span class="chip">${c}</span>`).join('')}</div>
    ${dayLedgerLine(selectedDate, dayData)}
    ${a.yi || a.ji ? `<div class="dh-almanac">
      <div class="al"><b class="yi">宜</b><span>${(a.yi || []).map(esc).join(' · ')}</span></div>
      <div class="al"><b class="ji">忌</b><span>${(a.ji || []).map(esc).join(' · ')}</span></div>
    </div>` : ''}`;
}

/* ═══ v7.5：编辑框天气——自动按这一天填、可 ↻ 更新、可 ✕ 不用 ═══ */
let _wxManual = false;
function showWxSrc(t) { const e = document.getElementById('wxSrc'); if (e) e.textContent = t ? (' · ' + t) : ''; }
function autoWeather(day, force) {
  if (!day) return;
  const f = document.getElementById('fWeather');
  if (!f) return;
  if (_wxManual && !force) return;
  api('/api/daywx?date=' + encodeURIComponent(day)).then(r => {
    if (!r.weather) { showWxSrc(''); return; }
    const cur = document.getElementById('fWeather');
    if (!cur) return;
    if (!cur.value) cur.value = r.weather;
    showWxSrc((r.src || '自动') + ' · ' + day);
  }).catch(() => { showWxSrc(''); });
}
function wxRefresh() {
  const day = (document.getElementById('fDate') || {}).value || selectedDate;
  const f = document.getElementById('fWeather');
  if (!f) return;
  const old = f.value;
  toast('正在按 ' + day + ' 取天气…');
  f.value = '取天气中…'; f.disabled = true;
  showWxSrc('取中 · ' + day);
  api('/api/daywx?date=' + encodeURIComponent(day) + '&live=1').then(r => {
    f.disabled = false;
    if (!r.weather) { f.value = old; toast('没取到天气（NAS 要能上外网），可以手填'); return; }
    f.value = r.weather; showWxSrc('实测 · ' + day); _wxManual = false;
    toast(day + '：' + r.weather);
  }).catch(e => {
    f.disabled = false; f.value = old;
    showWxSrc('取不到：' + String(e.message).slice(0, 30) + '（后端若是旧 app.py 会缺 /api/daywx）');
    toast('没取到天气：NAS 要能上外网，且 app.py 得是本次整包换过');
  });
}
function wxClear() {
  const f = document.getElementById('fWeather');
  if (f) { f.value = ''; }
  _wxManual = true;
  showWxSrc('这条不写天气');
  toast('已清空，保存后这条不带天气');
}
(function () {
  const f = document.getElementById('fWeather');
  if (f) f.addEventListener('input', function () { _wxManual = true; showWxSrc('手填'); });
  const d = document.getElementById('fDate');
  if (d) d.addEventListener('change', function () {
    if (_wxManual) return;
    const f2 = document.getElementById('fWeather');
    if (f2) f2.value = '';
    autoWeather(d.value, true);
  });
})();

async function fetchDayWeather(e) {
  e.stopPropagation();
  const el = e.target;
  el.disabled = true; el.textContent = '获取中…';
  await loadDay(true);
  if (!dayData.weather) { alert('没取到天气（NAS 要能上外网），可以先手填'); return; }
  const src = (dayData.weather_src || '').replace('open-meteo-', '') || '本地档案';
  toast('已按实测校准：' + (dayData.weather.split(' ').slice(1).join(' ') || dayData.weather)
        + '（数据源 ' + src + (dayData.weather_mm !== undefined && dayData.weather_mm !== '' ? ' · 降水 ' + dayData.weather_mm + 'mm' : '') + '）');
}
async function saveDayWeather(e) {
  e.stopPropagation();
  try {
    const res = await api('/api/day/weather', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ date: selectedDate, weather: dayData.weather }),
    });
    alert(`已写入当天 ${res.updated} 条内容`);
    await loadDay();
  } catch (err) { alert('写入失败：' + err.message); }
}

// ---------- 当天内容区（只显示当天）----------
function renderDayContent() {
  const box = document.getElementById('dayContent');
  if (!dayData) { box.innerHTML = ''; return; }
  const items = dayData.items || [];
  const n = new Date(selectedDate + 'T00:00:00');
  document.getElementById('dayTitle').innerHTML =
    `<span>当天内容（${items.length}）</span>
     <button class="mini-btn" onclick="openCreate('${selectedDate}','note')">＋ 记一笔</button>`;
  if (!items.length) {
    box.innerHTML = `<div class="empty-tip">这天没有记录<br>
      <span class="add-empty" onclick="openCreate('${selectedDate}','note')">＋ 增加笔记</span></div>`;
    return;
  }
  box.innerHTML = '<div class="day-items-list">' + items.map(itemCard).join('') + '</div>';
}

function itemCard(it) {
  const subParts = [];
  if (it.all_day) subParts.push('全天');
  else if (it.time) {
    subParts.push(it.time);
    if (it.end_time) subParts.push('~' + it.end_time);
  }
  subParts.push(TYPE_NAME[it.type] || it.type);
  if (it.yearly) subParts.push(it.lunar_label ? '每年·' + it.lunar_label : '每年');
  if (it.yearly && it.this_year_date && it.this_year_date !== it.date) {
    subParts.push('今年 ' + it.this_year_date.replace(/-/g, '.'));
  }
  if (it.location) subParts.push('📍' + it.location);
  if (it.weather) subParts.push('⛅' + it.weather);
  if (it.tags) subParts.push('#' + it.tags.split(/[,，]/).map(s => s.trim()).filter(Boolean).join(' #'));
  let cnt = it.content ? `<div class="cnt">${esc(it.content)}</div>` : '';
  if (it.type === 'countdown') {
    const left = daysUntil(it.date);
    cnt = `<div class="cnt">${left >= 0 ? `还有 ${left} 天` : `已过去 ${-left} 天`}</div>`;
  }
  if (it.type === 'todo') {
    const arr = it.todo_items || [];
    const sub = arr.length ? `<div class="todo-sub">${arr.slice(0, 5).map(t =>
      `<div class="${t.done ? 'todo-li done' : 'todo-li'}">${t.done ? '☑' : '☐'} ${esc(t.text || t.title || t.name || '')}</div>`
    ).join('')}${arr.length > 5 ? `<div class="todo-li">…还有 ${arr.length - 5} 项</div>` : ''}</div>` : '';
    cnt = `<div class="cnt">${it.done ? '✅ 已完成' : '⏳ 未完成'}</div>${sub}`;
  }
  let thumbs = '';
  if (it.attachments && it.attachments.length) {
    const im = it.attachments.filter(x => x.is_image).length;
    const fs = it.attachments.length - im;
    thumbs = '<div class="att-count">' + (im ? `\ud83d\uddbc ${im}` : '') + (im && fs ? '　' : '') + (fs ? `\ud83d\udcce ${fs}` : '') + '</div>';
  }
  if (it.age_text) subParts.push(it.age_text);
  if (it.folder_name) subParts.unshift('\ud83d\udcc1 ' + it.folder_name);
  const ops = `<div class="card-ops">
      <button type="button" class="op" title="复制文字（拷走标题+时间+正文）" onclick="event.stopPropagation();copyItemText(${it.id})">⧉</button>
      <button type="button" class="op" title="存一份副本（当天多一条，可立刻改）" onclick="event.stopPropagation();duplicateItem(${it.id},true)">⊕</button>
      <button type="button" class="op del" title="删除这条" onclick="event.stopPropagation();deleteItemById(${it.id})">✕</button>
    </div>`;
  return `
    <div class="day-item-card" onclick="openEdit(${it.id})">
      <div class="bar t-${it.type}"></div>
      <div style="flex:1;min-width:0">
        <div class="ti ${it.done ? 'done' : ''}">${esc(it.title)}</div>
        <div class="sub">${esc(subParts.join(' · '))}</div>
        ${cnt}
        ${thumbs}
      </div>
      ${ops}
    </div>`;
}

// ---------- 搜索（v5 新增）----------
function openSearch() {
  searchViewOpen(true);
  document.getElementById('searchInput').focus();
  renderSearchFilters();
  if (!searchState.list.length && !searchState.q) {
    searchState.type = '';
    runSearch('');
  }
}
function searchViewOpen(force) {
  const el = document.getElementById('searchView');
  if (force === true) { el.style.display = 'flex'; document.body.classList.add('searching'); }
  else if (force === false) { el.style.display = 'none'; document.body.classList.remove('searching'); }
  return el.style.display === 'flex';
}
function closeSearch() { searchViewOpen(false); }
function renderSearchFilters() {
  const kinds = [['', '全部'], ['note', '笔记'], ['schedule', '日程'], ['birthday', '生日'],
                 ['anniversary', '纪念日'], ['todo', '待办'], ['countdown', '倒数日']];
  document.getElementById('searchFilters').innerHTML = kinds.map(([k, name]) =>
    `<button class="sf ${searchState.type === k ? 'active' : ''}" onclick="setSearchType('${k}')">${name}</button>`).join('');
}
function setSearchType(k) { searchState.type = k; renderSearchFilters(); runSearch(searchState.q); }
async function runSearch(rawKw) {
  const kw = rawKw !== undefined ? rawKw : document.getElementById('searchInput').value.trim();
  if (rawKw === undefined) searchState.q = kw;
  const body = document.getElementById('searchBody');
  body.innerHTML = '<div class="empty-tip">搜索中…</div>';
  const params = new URLSearchParams();
  if (kw) params.set('q', kw);
  if (searchState.type) params.set('type', searchState.type);
  params.set('limit', '80');
  try {
    const res = await api('/api/items?' + params.toString());
    cacheItems(res.items);
    searchState.list = res.items;
    searchState.total = res.total || res.items.length;
    if (!res.items.length) {
      body.innerHTML = `<div class="empty-tip">${kw ? `没有匹配「${esc(kw)}」的内容` : '还没有内容'}</div>`;
      return;
    }
    body.innerHTML = `<div class="search-count">共 ${searchState.total} 条${res.total > res.items.length ? `（先显示 ${res.items.length} 条）` : ''}</div>`
      + res.items.map(searchCard).join('');
  } catch (e) {
    body.innerHTML = `<div class="empty-tip">搜索失败：${esc(e.message)}</div>`;
  }
}
function searchCard(it) {
  const kw = (searchState.q || '').trim();
  const hl = s => {
    const t = esc(s || '');
    if (!kw) return t;
    return t.replace(new RegExp(kw.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'g'), m => `<em>${m}</em>`);
  };
  const snippet = (() => {
    const c = (it.content || '').replace(/\s+/g, ' ');
    if (!kw) return c.slice(0, 80);
    const i = c.indexOf(kw);
    if (i < 0) return c.slice(0, 80);
    return (i > 24 ? '…' : '') + c.slice(Math.max(0, i - 24), i + 56);
  })();
  return `
    <div class="sr" onclick="jumpToItem(${it.id})">
      <div class="sr-date">${esc(it.date.replace(/-/g, '.'))}${it.time ? ' ' + esc(it.time) : ''}</div>
      <div class="sr-main">
        <div class="sr-t"><span class="dot t-${it.type}"></span>${hl(it.title)}</div>
        ${snippet ? `<div class="sr-c">${hl(snippet)}</div>` : ''}
      </div>
    </div>`;
}
function jumpToItem(id) {
  const it = itemCache[id];
  if (!it) return;
  closeSearch();
  const d = new Date(it.date + 'T00:00:00');
  curYear = d.getFullYear(); curMonth = d.getMonth() + 1;
  selectedDate = it.date;
  loadMonth().then(() => openEdit(id));
  scrollToDay();
}

// ---------- 抽屉：分类 / 笔记 / 导入导出 ----------
function openDrawer() {
  renderCatList();
  document.getElementById('drawerMask').style.display = 'block';
  document.getElementById('drawer').style.display = 'block';
  loadTypeCounts(); loadFolders();
}
function closeDrawer() {
  document.getElementById('drawerMask').style.display = 'none';
  document.getElementById('drawer').style.display = 'none';
}
function openTrash() {
  closeDrawer();
  document.getElementById('catViewTitle').textContent = '\ud83d\udd11 垃圾桶';
  document.getElementById('catView').style.display = 'flex';
  try { document.body.classList.add('cal-popened'); } catch (e) { }
  const add = document.getElementById('catAdd'); if (add) add.style.display = 'none';
  const list = document.getElementById('catViewList');
  list.innerHTML = '<div class="empty-tip">读取中…</div>';
  api('/api/trash').then(r => {
    const items = r.items || [], loose = r.loose || [];
    if (!items.length && !loose.length) {
      list.innerHTML = '<div class="empty-tip">垃圾桶是空的。<br>' +
        '<span style="font-size:11.5px">删除的内容会先存到这里 ' + (r.kept_days || 30) + ' 天，随时可以恢复。</span></div>';
      return;
    }
    let h = '<div class="search-count">共 ' + (items.length + loose.length) + ' 项 · 超过 ' +
      (r.kept_days || 30) + ' 天自动彻底删除</div>' +
      '<div class="trash-bar"><button type="button" class="tb" onclick="trashAll(true)">全部恢复</button>' +
      '<button type="button" class="tb danger" onclick="trashAll(false)">清空垃圾桶</button></div>';
    h += items.map(it => `<div class="day-item-card trashed">
        <div class="bar t-${it.type}"></div>
        <div style="flex:1;min-width:0">
          <div class="ti">${esc(it.title)}</div>
          <div class="sub">${esc(TYPE_NAME[it.type] || it.type)} · ${esc(it.date)}${it.time ? ' ' + esc(it.time) : ''}
            · 删掉于 ${esc((it.deleted_at || '').slice(0, 16))} · 还能留 ${it.left_days} 天${it.att_n ? ' · \ud83d\udcce' + it.att_n : ''}</div>
        </div>
        <div class="card-ops">
          <button type="button" class="op" title="放回日历" onclick="event.stopPropagation();trashRestore([${it.id}],[])">\u21a9</button>
          <button type="button" class="op del" title="彻底删除（不可恢复）" onclick="event.stopPropagation();trashPurge([${it.id}],[])">\ud83d\uddd1</button>
        </div></div>`).join('');
    if (loose.length) {
      h += '<div class="drawer-label" style="margin:10px 2px 4px">单独移出的附件</div>' + loose.map(a => `
        <div class="day-item-card trashed">
          <div class="bar t-note"></div>
          <div style="flex:1;min-width:0"><div class="ti">\ud83d\udcc4 ${esc(a.filename)}</div>
          <div class="sub">原来挂在 #${a.item_id} · 还能留 ${a.left_days} 天</div></div>
          <div class="card-ops">
            <button type="button" class="op" title="放回原记录" onclick="event.stopPropagation();trashRestore([],[${a.id}])">\u21a9</button>
            <button type="button" class="op del" title="彻底删除文件" onclick="event.stopPropagation();trashPurge([],[${a.id}])">\ud83d\uddd1</button>
          </div></div>`).join('');
    }
    list.innerHTML = h;
  }).catch(e => { list.innerHTML = '<div class="empty-tip">读不到：' + esc(e.message) + '</div>'; });
}
function trashRestore(ids, atts) {
  if (!confirm(ids.length && !atts.length ? ('把这 ' + ids.length + ' 条放回日历？') : '恢复选中的内容？')) return;
  api('/api/trash/restore', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ids: ids, att_ids: atts }) }).then(r => {
    toast(r.restored ? ('已恢复 ' + r.restored + ' 项') : '没恢复成（可能不是你的内容）');
    openTrash(); loadMonth(curYear, curMonth, true); loadDay();
  }).catch(e => toast('恢复失败：' + e.message));
}
function trashPurge(ids, atts) {
  if (!confirm('彻底删除？附件文件会一起删掉，**不能恢复**。')) return;
  api('/api/trash/purge', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ids: ids, att_ids: atts }) }).then(r => {
    toast(r.purged ? ('已彻底删除 ' + r.purged + ' 项') : '没删成');
    openTrash(); loadDay();
  }).catch(e => toast('删不了：' + e.message));
}
function trashAll(restore) {
  api('/api/trash').then(r => {
    const ids = (r.items || []).map(x => x.id), atts = (r.loose || []).map(x => x.id);
    if (!ids.length && !atts.length) { toast('垃圾桶已经空了'); return; }
    if (restore) trashRestore(ids, atts);
    else if (confirm('清空垃圾桶：' + (ids.length + atts.length) + ' 项会连带附件文件一起真删，无法恢复。确定？')) {
      api('/api/trash/clear', { method: 'POST' }).then(x => { toast('已清空 ' + (x.purged || 0) + ' 项'); openTrash(); loadDay(); })
        .catch(e => toast('清不掉：' + e.message));
    }
  });
}

function renderCatList() {
  const list = document.getElementById('catList');
  list.innerHTML = TYPES.map(t => `
    <div class="cat-item" onclick="openCatView('${t.key}')">
      <span class="cat-dot t-${t.key}"></span>
      <span class="cat-name">${t.name}</span>
      <span class="cat-count">${typeCounts[t.key] != null ? typeCounts[t.key] + ' 条' : ''}</span>
      <span class="cat-arrow">›</span>
    </div>`).join('') + (folders.length ? '<div class="drawer-label" style="margin-top:10px">文件夹</div>' + folders.map(f => `
    <div class="cat-item" onclick="openCatViewFolder(${f.id})">
      <span class="cat-dot" style="background:#8D99AE"></span>
      <span class="cat-name">\ud83d\udcc1 ${esc(f.name)}</span>
      <span class="cat-count">${f.count ? f.count + ' 条' : ''}</span>
      <button class="cat-op" title="重命名文件夹" onclick="event.stopPropagation();renameFolder(${f.id})">✎</button>
      <button class="cat-op" title="删掉文件夹（笔记不删）" onclick="event.stopPropagation();deleteFolder(${f.id})">✕</button>
      <span class="cat-arrow">›</span>
    </div>`).join('') + '<div class="cat-item" onclick="quickNewFolder()"><span class="cat-dot" style="background:#DCE7FF"></span><span class="cat-name">＋ 新建文件夹</span><span class="cat-arrow">›</span></div>'
    + '<div class="drawer-tip">文件夹只是归类，笔记仍然显示在当天日历里。</div>' : '');
}
let _cat = { key: '', off: 0, acc: [], total: 0, loading: false, bound: false };
const CAT_PAGE = 50;                                      // 首屏 50 条，滑到底自动续
function _catUrl(st) {
  return st.key.indexOf('folder:') === 0
    ? '/api/items?folder=' + st.key.slice(7)
    : '/api/items?type=' + st.key;
}
function catShow(title, key) {
  closeDrawer();
  document.getElementById('catViewTitle').textContent = title;
  document.getElementById('catView').style.display = 'flex';
  document.getElementById('catView').className = 'cat-view' + (key.indexOf('folder:') === 0 ? ' infolder' : '');
  const add = document.getElementById('catAdd');
  if (add) add.style.display = key.indexOf('folder:') === 0 ? '' : 'none';
  _cat.key = key; _cat.off = 0; _cat.acc = []; _cat.total = 0; _cat.loading = false;
  const list = document.getElementById('catViewList');
  list.innerHTML = '<div class="empty-tip">加载中…</div>';
  if (list && !_cat.bound) {
    _cat.bound = true;
    list.addEventListener('scroll', function () {
      if (list.scrollTop + list.clientHeight > list.scrollHeight - 260) catMore();
    }, { passive: true });
  }
  catFetch(true);
}
function catFetch(reset) {
  const st = _cat;
  if (st.loading) return;
  st.loading = true;
  const list = document.getElementById('catViewList');
  if (reset) list.innerHTML = '<div class="empty-tip">加载中…</div>';
  else {
    const m = list.querySelector('.cat-more');
    if (m) m.textContent = '加载中…';
  }
  api(_catUrl(st) + '&limit=' + CAT_PAGE + '&offset=' + st.off).then(res => {
    const items = res.items || [];
    cacheItems(items);
    if (reset) st.acc = items; else {
      const seen = {}; st.acc = st.acc.concat(items).filter(x => (seen[x.id] ? 0 : (seen[x.id] = 1)));
    }
    st.total = res.total || st.acc.length;
    st.off = st.acc.length;
    st.loading = false;
    catRender();
  }).catch(e => {
    st.loading = false;
    if (reset) list.innerHTML = '<div class="empty-tip">加载失败：' + esc(e.message) + '</div>';
    else catRender();
  });
}
function catRender() {
  const st = _cat, list = document.getElementById('catViewList');
  if (!list || list.firstChild === null) return;
  if (!st.acc.length) {
    const t = st.key.indexOf('folder:') === 0 ? '这个文件夹还是空的' : '这个分类还没有内容';
    const add = st.key.indexOf('folder:') === 0
      ? '<span class="add-empty" onclick="newInFolder()">＋ 在文件夹里新建</span>'
      : '<span class="add-empty" onclick="openCreate(\'' + todayStr() + '\',\'' + st.key + '\')">＋ 新建</span>';
    list.innerHTML = '<div class="empty-tip">' + t + '<br>' + add + '</div>';
    return;
  }
  const more = st.acc.length < st.total;
  list.innerHTML = '<div class="search-count">共 ' + st.total + ' 条 · 已显示 ' + st.acc.length + '</div>'
    + st.acc.map(itemCard).join('')
    + (more ? '<div class="cat-more" onclick="catMore()">继续加载（还剩 ' + (st.total - st.acc.length) + ' 条）· 或下滑自动加载</div>'
            : '<div class="cat-end">已全部显示 ' + st.total + ' 条</div>');
}
function catMore() {
  const st = _cat;
  if (st.loading || st.acc.length >= st.total) return;
  catFetch(false);
}
function openCatView(type) { catShow(TYPE_NAME[type], type); }
function closeCatView() { document.getElementById('catView').style.display = 'none'; _catFolder = 0; const add = document.getElementById('catAdd'); if (add) add.style.display = 'none'; }

// ---------- 年月跳转 ----------
function openJump() {
  const ySel = document.getElementById('jumpYear'), mSel = document.getElementById('jumpMonth');
  const nowY = new Date().getFullYear();
  ySel.innerHTML = Array.from({ length: 90 }, (_, i) => nowY - 70 + i)
    .map(y => `<option value="${y}" ${y === curYear ? 'selected' : ''}>${y} 年</option>`).join('');
  mSel.innerHTML = Array.from({ length: 12 }, (_, i) => i + 1)
    .map(m => `<option value="${m}" ${m === curMonth ? 'selected' : ''}>${m} 月</option>`).join('');
  document.getElementById('jumpYears').innerHTML = [nowY - 3, nowY - 2, nowY - 1, nowY, 2023, 2019, 2015, 2012]
    .filter((v, i, arr) => arr.indexOf(v) === i)
    .map(y => `<button class="sf" onclick="quickJump(${y})">${y}</button>`).join('');
  document.getElementById('jumpMask').style.display = 'flex';
}
function closeJump() { document.getElementById('jumpMask').style.display = 'none'; }
function quickJump(y) { document.getElementById('jumpYear').value = String(y); }
function doJump() {
  curYear = parseInt(document.getElementById('jumpYear').value, 10);
  curMonth = parseInt(document.getElementById('jumpMonth').value, 10);
  const last = new Date(curYear, curMonth - 1, 0).getDate();
  const day = Math.min(new Date().getDate(), last);
  selectedDate = fmtDate(new Date(curYear, curMonth - 1, curMonth === new Date().getMonth() + 1 ? day : 1));
  closeJump();
  loadMonth();
}

// ---------- 滑动：左右切月 / 上滑折叠 / 下滑展开 ----------
let lastSwipeAt = 0;          // 手势收尾时浏览器还会补一次 click，别当成点日期
function bindSwipe() {
  const area = document.getElementById('swipeArea');
  let sx = 0, sy = 0, dx = 0, dy = 0, tracking = false;
  area.addEventListener('pointerdown', e => {
    if (e.pointerType === 'mouse' && e.button !== 0) return;
    sx = e.clientX; sy = e.clientY; dx = dy = 0; tracking = true;
  }, { passive: true });
  area.addEventListener('pointermove', e => {
    if (!tracking) return;
    dx = e.clientX - sx; dy = e.clientY - sy;
    // 横向意图明显时，日历跟着手指轻微位移，给一点反馈
    if (Math.abs(dx) > 24 && Math.abs(dx) > Math.abs(dy)) {
      area.style.transition = 'none';
      area.style.transform = `translateX(${Math.max(-40, Math.min(40, dx * 0.18))}px)`;
    }
  }, { passive: true });
  const end = () => {
    if (!tracking) return;
    tracking = false;
    area.style.transition = '';
    area.style.transform = '';
    if (Math.abs(dx) > 60 && Math.abs(dx) > Math.abs(dy) * 1.4) {   // 左右滑 → 切月
      lastSwipeAt = Date.now();
      if (dx < 0) nextMonth(); else prevMonth();
      return;
    }
    if (Math.abs(dy) < 60 || Math.abs(dy) < Math.abs(dx)) return;    // 上下滑 → 折叠/展开
    lastSwipeAt = Date.now();
    if (dy < 0 && !collapsed) { collapsed = true; area.classList.add('collapsed'); }
    else if (dy > 0 && collapsed) { collapsed = false; area.classList.remove('collapsed'); }
  };
  area.addEventListener('pointerup', end, { passive: true });
  area.addEventListener('pointercancel', () => {
    tracking = false; area.style.transition = ''; area.style.transform = '';
  }, { passive: true });
  // 折叠状态下点日历区 = 展开（点日期格子时由 pickDay 处理，不抢事件）
  area.addEventListener('click', e => {
    if (Date.now() - lastSwipeAt < 500) return;
    if (!collapsed) return;
    if (e.target.closest('.day-cell')) return;
    collapsed = false;
    area.classList.remove('collapsed');
  });
}

// ---------- 全屏新建 / 编辑 ----------
function renderTypeDropdown() {
  document.getElementById('typeDropdown').innerHTML = TYPES.map(t =>
    `<div class="type-opt ${t.key === editType ? 'active' : ''}" onclick="setType('${t.key}')">${t.name}</div>`
  ).join('');
}
function setType(key) {
  editType = key;
  _markType();
  document.getElementById('typeValue').textContent = TYPE_NAME[key] + ' ▾';
  document.getElementById('typeDropdown').style.display = 'none';
  applyTypeFields();
  document.getElementById('editTitle').textContent = editId ? '编辑' : '新建';
}
function applyTypeFields() {
  const t = editType;
  const isSchedule = t === 'schedule';
  const isBirth = t === 'birthday' || t === 'anniversary';
  const isCount = t === 'countdown';
  const isNote = t === 'note';
  document.getElementById('allDayWrap').style.display = isSchedule ? '' : 'none';
  document.getElementById('doneWrap').style.display = t === 'todo' ? '' : 'none';
  document.getElementById('todoWrap').style.display = t === 'todo' ? '' : 'none';
  document.getElementById('endTimeWrap').style.display = isSchedule ? '' : 'none';
  document.getElementById('lunarWrap').style.display = isBirth ? '' : 'none';
  const bw = document.getElementById('birthWrap'); if (bw) bw.style.display = isBirth ? '' : 'none';
  document.getElementById('remindWrap').style.display = (isSchedule || isBirth || isCount) ? '' : 'none';
  document.getElementById('locWrap').style.display = isSchedule ? '' : 'none';
  document.getElementById('weatherWrap').style.display = (isSchedule || isNote) ? '' : 'none';
  document.getElementById('attachWrap').style.display = isNote ? '' : 'none';
  const ph = { schedule: '日程名称', birthday: '寿星姓名', anniversary: '纪念日名称',
               countdown: '倒数日名称', note: '标题（不写默认当天日期）', todo: '待办事项' };
  document.getElementById('fTitle').placeholder = ph[t];
  _fitRows();                                   // 收掉一行里全空的那几行（用 class，不靠 :has()）
  updateAgeTip();
  document.getElementById('timeWrap').style.display = (isSchedule && !document.getElementById('fAllDay').checked) ? '' : 'none';
}
function openCreate(dateStr, type) {
  editId = null;
  editType = type || 'note';
  selectedDate = dateStr || todayStr();
  const set = (id, v) => { const el = document.getElementById(id); if (el) el.value = v; };
  set('fTitle', ''); set('fDate', selectedDate); set('fTime', ''); set('fEndTime', '');
  document.getElementById('fAllDay').checked = false;
  set('fContent', ''); set('fTags', ''); set('fLocation', '');
  set('fWeather', dayData && dayData.weather ? dayData.weather : '');
  if (!dayData || !dayData.weather) autoWeather(dateStr, true); else showWxSrc(dayData.weather_src || '');
  _wxManual = false;
  set('fRemind', '0');
  set('fBirth', '');
  document.getElementById('editMask').dataset.occ = dateStr || todayStr();
  editFolder = 0; renderFolderSel(0);
  document.getElementById('fLunar').checked = false;
  document.getElementById('fYearly').checked = true;
  document.getElementById('fDone').checked = false;
  document.getElementById('todoList').innerHTML = '';
  document.getElementById('noteMeta').style.display = 'none';
  document.getElementById('btnDeleteWrap').style.display = 'none';
  document.getElementById('attachList').innerHTML = '';
  pendingFiles = [];
  document.getElementById('typeValue').textContent = TYPE_NAME[editType] + ' ▾';
  document.getElementById('typeDropdown').style.display = 'none';
  renderTypeDropdown();
  applyTypeFields();
  document.getElementById('editTitle').textContent = '新建';
  _markType();
  if (isWide()) { document.getElementById('fTitle').blur(); }   // 网页右栏：别抢光标把页面滚走
  pendingRefs = [];                                            // 上一条的待挂引用不能跟过来
  _foldTouched = null; _wxManual = false;
  try { syncAttachFold(); _syncPicRow(); } catch (e) { }        // 标题上的（🖼3 🔗1）当场归零
  showWxSrc(dayData && dayData.weather_src ? dayData.weather_src : '');
  document.getElementById('editMask').style.display = 'flex';
  document.getElementById('fTitle').focus();
}
function openEdit(id) {
  const cached = itemCache[id];
  // 列表里的正文是截断过的，编辑前拉一次完整内容
  api('/api/items/' + id).then(res => { fillEditForm(res.item); }).catch(() => {
    if (cached) fillEditForm(cached);
  });
}
function fillEditForm(item) {
  if (!item) return;
  itemCache[item.id] = item;
  editId = item.id;
  editType = item.type;
  const set = (id2, v) => { const el = document.getElementById(id2); if (el) el.value = v || ''; };
  set('fTitle', item.title); set('fDate', item.date); set('fTime', item.time);
  set('fEndTime', item.end_time); set('fContent', item.content); set('fTags', item.tags);
  set('fLocation', item.location); set('fWeather', item.weather || '');
  if (!item.weather) autoWeather(item.date, !item.weather); else showWxSrc('');
  _wxManual = false;
  set('fRemind', String(item.remind_minutes || 0));
  set('fBirth', item.birth_year || (item.type === 'birthday' || item.type === 'anniversary' ? (item.date || '').slice(0, 4) : ''));
  document.getElementById('editMask').dataset.occ = item.this_year_date || item.date || '';
  editFolder = item.folder_id || 0; renderFolderSel(editFolder);
  _foldTouched = null;   // 换记录：附件区重新按数量自动决定开合
  document.getElementById('fAllDay').checked = !!item.all_day;
  document.getElementById('fLunar').checked = !!item.is_lunar;
  document.getElementById('fYearly').checked = !!item.yearly;
  document.getElementById('fDone').checked = !!item.done;
  pendingFiles = [];
  document.getElementById('typeValue').textContent = TYPE_NAME[item.type] + ' ▾';
  document.getElementById('typeDropdown').style.display = 'none';
  renderTypeDropdown();
  applyTypeFields();
  const todoList = document.getElementById('todoList');
  todoList.innerHTML = '';
  (item.todo_items || []).forEach(t => addTodoRow(t.text || t.title || t.name || '', !!t.done));
  if (item.type === 'note') {
    const when = (item.created_at || '').slice(0, 16).replace(/-/g, '/');
    document.getElementById('noteMeta').style.display = '';
    document.getElementById('noteMeta').textContent =
      `${when} | ${wordCount(item.content)}字 | ${item.tags || '未分类'}`;
  } else {
    document.getElementById('noteMeta').style.display = 'none';
  }
  _renderAttach(item);
  document.getElementById('editTitle').textContent = '编辑';
  _markType();
  document.getElementById('editMask').style.display = 'flex';
}
function closeEdit() { const _d0 = document.getElementById('typeDropdown'); if (_d0) _d0.style.display = 'none'; document.getElementById('editMask').style.display = 'none'; }

// ---------- 待办子项 ----------
function addTodoRow(text, done) {
  const box = document.getElementById('todoList');
  const row = document.createElement('div');
  row.className = 'todo-row';
  row.innerHTML = `
    <label class="check"><input type="checkbox" ${done ? 'checked' : ''}></label>
    <input type="text" class="line-input todo-text" value="${esc(text || '')}" placeholder="子项内容">
    <button type="button" class="del" onclick="this.parentNode.remove()">✕</button>`;
  box.appendChild(row);
}
function collectTodoItems() {
  const items = [];
  document.querySelectorAll('#todoList .todo-row').forEach(row => {
    const text = row.querySelector('.todo-text').value.trim();
    if (!text) return;
    items.push({ text, done: row.querySelector('input[type=checkbox]').checked ? 1 : 0 });
  });
  return items;
}

// ---------- 保存 / 删除 ----------
async function saveItem() {
  const val = id => (document.getElementById(id).value || '').trim();
  const body = {
    type: editType,
    title: val('fTitle'),
    date: val('fDate') || todayStr(),
    time: val('fTime'),
    end_time: val('fEndTime'),
    all_day: document.getElementById('fAllDay').checked ? 1 : 0,
    location: val('fLocation'),
    weather: val('fWeather'),
    todo_items: collectTodoItems(),
    content: document.getElementById('fContent').value,
    tags: val('fTags'),
    remind_minutes: parseInt(val('fRemind'), 10) || 0,
    is_lunar: document.getElementById('fLunar').checked ? 1 : 0,
    yearly: document.getElementById('fYearly').checked ? 1 : 0,
    done: document.getElementById('fDone').checked ? 1 : 0,
    folder_id: folderIdNow(), birth_year: parseInt(val('fBirth'), 10) || 0,
  };
  const btn = document.getElementById('btnSave');
  btn.disabled = true;
  try {
    let itemId = editId;
    if (editId) {
      await api('/api/items/' + editId, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    } else {
      const res = await api('/api/items', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      itemId = res.item.id;
    }
    selectedDate = body.date;
    const d = new Date(body.date + 'T00:00:00');
    curYear = d.getFullYear(); curMonth = d.getMonth() + 1;
    if (pendingRefs.length) {
      const rr = await flushRefs(itemId);
      if (rr.added.length) toast('已保存，并挂上 ' + rr.added.length + ' 个 NAS 文件');
      if (rr.skipped.length) toast((rr.added.length ? '' : '已保存；') + rr.skipped.length + ' 个 NAS 引用没挂上：' + ((rr.skipped[0] || {}).error || ''));
    }
    if (!upQ.length) { pendingFiles = []; closeEdit(); refreshAll(); }
    else {
      btn.textContent = '传附件…';                        // 正文已存好，附件单独传，不绑在一起
      const r = await flushUploads(itemId);
      pendingFiles = []; editId = itemId;
      if (r.bad) { toast('已保存正文；附件 ' + r.ok + ' 个成功、' + r.bad + ' 个没传上（点 ⟳ 可重试）'); renderPending(); openAttachFold(); }
      else { toast('已保存（含 ' + r.ok + ' 个附件）'); closeEdit(); }
      refreshAll();
    }
  } catch (e) { toast('保存失败：' + e.message); alert('保存失败：' + e.message); }
  /* v5.16 关键修复：原来无附件分支用 return 提前离开，btn.disabled 一直是 true，
     导致「保存一条后再新建就点不动保存」，只能刷新页面。finally 保证按钮一定恢复。 */
  btn.disabled = false;
  btn.textContent = '完成';
}
async function deleteItem() {
  if (!editId) return;
  if (!confirm('确定删除这条内容吗？')) return;
  try {
    await api('/api/items/' + editId, { method: 'DELETE' });
    closeEdit();
    refreshAll();
  } catch (e) { alert('删除失败：' + e.message); }
}
async function removeExistingAttach(attId) {
  if (!confirm('删除这张图片？')) return;
  try {
    await api('/api/attachments/' + attId, { method: 'DELETE' });
    const item = itemCache[editId];
    if (item) item.attachments = (item.attachments || []).filter(a => a.id !== attId);
    _attachSig = ''; _renderAttach(item);                    // 删图后强制重画一次
  } catch (e) { alert('删除失败：' + e.message); }
}
function refreshAll() { loadMonth(); }

// ---------- 批量导入 ----------
function openImport() { document.getElementById('importMask').style.display = 'flex'; document.getElementById('importResult').innerHTML = ''; }
function closeImport() { document.getElementById('importMask').style.display = 'none'; }
async function importSubmit() {
  const result = document.getElementById('importResult');
  const file = document.getElementById('fCsvFile').files[0];
  const csvText = document.getElementById('fCsv').value;
  const since = (document.getElementById('fSince').value || '').trim();
  const fd = new FormData();
  if (since) fd.append('since', since);
  if (!document.getElementById('fDedupe').checked) fd.append('dedupe', '0');
  if (file) {
    fd.append('file', file);
    submitImport(fd, result, file.name);
    return;
  }
  if (!csvText.trim()) { alert('请选择文件或粘贴内容'); return; }
  fd.append('csv', csvText);
  submitImport(fd, result, '粘贴的内容');
}
async function submitImport(fd, result, name) {
  result.className = 'import-result';
  result.textContent = `正在导入 ${name} …（几千条也就十几秒，别关页面）`;
  try {
    const res = await fetch('/api/import', { method: 'POST', body: fd });
    showImportResult(await res.json(), result, name);
  } catch (e) { result.className = 'import-result err'; result.textContent = '导入失败：' + e.message; }
}
function showImportResult(data, el, name) {
  if (data.error) { el.className = 'import-result err'; el.textContent = data.error; return; }
  el.className = data.fail ? 'import-result err' : 'import-result ok';
  el.innerHTML = `${esc(name)}：导入 <b>${data.ok}</b> 条`
    + (data.skip ? `，跳过重复/无效 ${data.skip} 条` : '')
    + (data.fail ? `，失败 ${data.fail} 条：` + (data.errors || []).join('；') : '');
  document.getElementById('fCsvFile').value = '';
  document.getElementById('fCsv').value = '';
  refreshAll();
}

// ---------- 提醒检查 ----------
async function checkReminders() {
  if (sessionStorage.getItem('calnote-remind-shown')) return;
  try {
    const res = await api('/api/reminders');
    if (res.reminders && res.reminders.length) {
      document.getElementById('remindBody').innerHTML = res.reminders.slice(0, 10).map(it => {
        const sub = [TYPE_NAME[it.type] || it.type];
        if (it.type === 'countdown') sub.push(it.days_left >= 0 ? `还有 ${it.days_left} 天` : `已过去 ${-it.days_left} 天`);
        else if (it.days_left === 0) sub.push('今天');
        else if (it.days_left === 1) sub.push('明天');
        if (it.time) sub.push(it.time);
        itemCache[it.id] = it;
        return `
          <div class="remind-item" onclick="closeRemind(); jumpToItem(${it.id})">
            <span class="dot t-${it.type}"></span>
            <div class="main">
              <div class="ti">${esc(it.title)}</div>
              <div class="sub">${esc(sub.join(' · '))}</div>
            </div>
          </div>`;
      }).join('');
      document.getElementById('remindMask').style.display = 'flex';
      sessionStorage.setItem('calnote-remind-shown', '1');
    }
  } catch (e) { /* 忽略提醒错误 */ }
}
function closeRemind() { document.getElementById('remindMask').style.display = 'none'; }

// ---------- 初始化 ----------
document.getElementById('btnPrev').onclick = prevMonth;
document.getElementById('btnNext').onclick = nextMonth;
document.getElementById('btnToday').onclick = goToday;
document.getElementById('monthTitle').onclick = openJump;
document.getElementById('btnSearch').onclick = openSearch;
document.getElementById('btnDrawer').onclick = openDrawer;
document.getElementById('btnImport2').onclick = () => { closeDrawer(); openImport(); };
document.getElementById('btnExport2').onclick = () => { window.location = '/api/export'; };
document.getElementById('btnAdd').onclick = () => openCreate(selectedDate || todayStr(), 'note');
document.getElementById('btnDelete').onclick = deleteItem;
document.getElementById('searchInput').addEventListener('keydown', e => {
  if (e.key === 'Enter') { searchState.q = e.target.value.trim(); runSearch(e.target.value.trim()); }
  if (e.key === 'Escape') closeSearch();
});
document.getElementById('searchInput').addEventListener('input', e => {
  clearTimeout(window.__sTimer);
  const v = e.target.value.trim();
  window.__sTimer = setTimeout(() => { searchState.q = v; runSearch(v); }, 450);
});
let _metaT = null;
document.getElementById('fContent').oninput = e => {
  if (!editId) return;
  clearTimeout(_metaT);                                  // 防抖：不每敲一个字就重排整块
  _metaT = setTimeout(() => {
    const el = document.getElementById('noteMeta');
    if (el) el.textContent = (el.textContent || '').replace(/\d+字/, wordCount(e.target.value) + '字');
  }, 500);
};
document.getElementById('typeValue').onclick = (e) => {   // v5.13：只有点类型胶囊本身才弹类型浮层
  e.stopPropagation();
  const dd = document.getElementById('typeDropdown');
  dd.style.display = dd.style.display === 'none' ? '' : 'none';
};
document.getElementById('fAllDay').onchange = e => {
  document.getElementById('timeWrap').style.display = e.target.checked ? 'none' : '';
};
document.getElementById('btnAttach').onclick = () => document.getElementById('fFile').click();
function openAttach(attId, el) {
  const node = el || document.querySelector('[data-att="' + attId + '"]');
  const url = (node && node.dataset && node.dataset.url) || ('/api/attachments/' + attId);
  let mine = {};
  if (node && node.dataset) {
    mine = { filename: node.dataset.name || '', mime: node.dataset.mime || '', path: node.dataset.full || '', missing: node.dataset.missing === '1' };
  } else {
    mine = (((itemCache[editId] || {}).attachments) || []).filter(x => x.id === attId)[0] || {};
  }
  const ct = mine.mime || '';
  if (mine.missing) { viewPic(url, mine.filename || '', ct, mine.path || '', true); return; }
  if (/^(image|video|audio)\//.test(ct) || ct.indexOf('pdf') > 0 || ct.indexOf('text/') === 0) viewPic(url, mine.filename || '', ct, mine.path || '');
  else viewPic(url, mine.filename || '', 'doc', mine.path || '');
}
function askRemoveAttach(attId, kind) {
  if (!confirm(kind === 'link' ? '取消这个引用？（NAS 上的原文件不会被删除）\n\n取消后先进「垃圾桶」，可以恢复。' : '把这个附件移出这条记录？\n\n文件不会被删，先进「垃圾桶」，可以恢复。')) return;
  removeExistingAttach(attId);
}
function solidifyAttach(attId) {
  if (!confirm('复制一份到应用存储？以后原文件被删/改名也不影响这条笔记。')) return;
  api('/api/attachments/' + attId + '/solidify', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }).then(() => {
    toast('已存一份进应用存储');
    if (editId) api('/api/items/' + editId).then(res => { _attachSig = ''; _renderAttach(res.item); });
  }).catch(e => toast('存不了：' + e.message));
}
function _addPickedFiles(files) {
  const list = document.getElementById('attachList');
  if (!list) return;
  files.forEach(function (file) {
    if (upQ.some(function (x) { return x.key === _fkey(file); })) return;
    pendingFiles.push(file);
    const url = URL.createObjectURL(file);
    const fk = _fkey(file), isImg = isImgName(file);
    const k = _asAdd({ pend: 'file', url: url, name: file.name, mime: file.type || (isImg ? 'image/' : ''), copy: file.name,
      rm: function () {
        upQ = upQ.filter(function (x) { return x.key !== fk; });
        pendingFiles = pendingFiles.filter(function (f) { return f !== file; });
        const node = list.querySelector('[data-fkey="' + fk + '"]');
        if (node) node.remove();
        _syncPicRow();
      } });
    const more = '<button type="button" class="amore" onclick="attachSheet(\'' + k + '\',event);return false">\u22ef</button>';
    const row = document.createElement('div');
    row.className = 'pend-wrap';
    row.setAttribute('data-fkey', fk);
    row.innerHTML = isImg
      ? '<div class="imgcell" data-name="' + esc(file.name) + '"><a href="' + url + '" onclick="return openPendPick(event,\'' + k + '\')"><img src="' + url + '" alt=""></a>' + more + '</div>'
      : '<a class="file-chip" href="' + url + '" onclick="return openPendPick(event,\'' + k + '\')">' +
        '<span class="fi">' + nasIconExt(file.name) + '</span><span class="fn">' + esc(file.name) + '</span>' +
        '<span class="fs">' + prettySize(file.size) + '</span><span class="fs">\u5f85\u4f20</span>' + more + '</a>';
    list.appendChild(row);
  });
  if (queueFiles(files)) openAttachFold();
  _syncPicRow();
}
function openPendPick(e, k) {
  const it = _asMap[k]; if (!it) return true;
  try { viewPic(it.url, it.name, it.mime, '', false); } catch (err) { return true; }
  const box = document.getElementById('picView');
  if (!box || !box.style.display || box.style.display === 'none') return true;
  if (e && e.preventDefault) e.preventDefault();
  return false;
}
document.getElementById('fFile').onchange = e => {
  const files = Array.from(e.target.files || []);
  e.target.value = '';
  if (!files.length) return;
  _addPickedFiles(files);
};
document.getElementById('fAnyFile').onchange = e => {
  const files = Array.from(e.target.files || []);
  e.target.value = '';
  if (!files.length) return;
  _addPickedFiles(files);
};

// 键盘 ← → 切月（桌面端）
document.addEventListener('keydown', e => {
  if (document.getElementById('editMask').style.display === 'flex') return;
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
  if (e.key === 'ArrowLeft') prevMonth();
  if (e.key === 'ArrowRight') nextMonth();
  if (e.key === 't' || e.key === 'T') goToday();
  if (e.key === '/') { e.preventDefault(); openSearch(); }
});

const weekNames = ['日', '一', '二', '三', '四', '五', '六'];
document.getElementById('weekRow').innerHTML = weekNames.map((w, i) =>
  `<div class="${i === 0 || i === 6 ? 'weekend' : ''}">${w}</div>`).join('');

(function init() {
  const t = new Date();
  curYear = t.getFullYear();
  curMonth = t.getMonth() + 1;
  selectedDate = todayStr();
  loadMonth();
  bindSwipe();
  checkReminders();
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js').catch(() => {});
  }
})();

/* ================= 皮肤 v5.7 新增（不影响原来的增删改查逻辑） ================= */
let __toastTimer = null;
function toast(msg) {
  let el = document.getElementById('qwToast');
  if (!el) {
    el = document.createElement('div'); el.id = 'qwToast'; el.className = 'qw-toast';
    el.onclick = () => el.classList.remove('on');
    document.body.appendChild(el);
  }
  el.textContent = msg; el.classList.add('on');
  clearTimeout(__toastTimer);
  __toastTimer = setTimeout(() => el.classList.remove('on'), 1900);
}
function fullItem(id) {
  return api('/api/items/' + id).then(r => r.item).catch(() => itemCache[id] || null);
}
function itemToText(it) {
  const bits = [];
  if (it.title) bits.push(it.title);
  const when = [it.date];
  if (it.all_day) when.push('全天'); else if (it.time) when.push(it.time + (it.end_time ? '-' + it.end_time : ''));
  bits.push(when.join(' '));
  if (it.location) bits.push('地点：' + it.location);
  if (it.weather) bits.push('天气：' + it.weather);
  (it.todo_items || []).forEach(t => bits.push((t.done ? '☑ ' : '☐ ') + (t.text || '')));
  if (it.content) bits.push(it.content);
  if (it.tags) bits.push('#' + String(it.tags).split(/[,，]/).filter(x => x.trim()).join(' #'));
  return bits.join('\n');
}
function copyText(txt) {
  if (navigator.clipboard && window.isSecureContext !== false) {
    return navigator.clipboard.writeText(txt).catch(() => copyFallback(txt));
  }
  return Promise.resolve(copyFallback(txt));
}
function copyFallback(txt) {
  const ta = document.createElement('textarea');
  ta.value = txt; ta.setAttribute('readonly', '');
  ta.style.cssText = 'position:fixed;left:-9999px;top:0';
  document.body.appendChild(ta); ta.select();
  let ok = false;
  try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
  ta.remove();
  return ok ? Promise.resolve() : Promise.reject(new Error('这台设备不让复制'));
}
function copyItemText(id) {
  fullItem(id).then(it => {
    if (!it) { toast('没读到这条内容'); return; }
    copyText(itemToText(it)).then(() => toast('文字已复制：' + (it.title || '').slice(0, 12) + '…（去微信/备忘录粘贴）'),
      e => toast('这台设备不让复制：' + e.message));
  });
}
function duplicateItem(id, openIt) {
  fullItem(id).then(it => {
    if (!it) { toast('没读到这条内容'); return; }
    const body = {
      type: it.type, title: it.title, content: it.content, date: selectedDate || it.date,
      time: it.time, end_time: it.end_time, all_day: it.all_day, location: it.location,
      weather: it.weather, tags: it.tags, remind_minutes: it.remind_minutes,
      is_lunar: it.is_lunar, yearly: it.yearly, done: 0, todo_items: (it.todo_items || []),
    };
    api('/api/items', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
      .then(res => {
        toast('已生成副本，可直接改内容/日期');
        loadMonth(); loadDay();
        if (openIt && res && res.item) setTimeout(() => openEdit(res.item.id), 400);   // 副本马上打开来改
      })
      .catch(e => toast('复制失败：' + e.message));
  });
}
function deleteItemById(id) {
  if (!confirm('删除这条？删了就找不回来了')) return;
  api('/api/items/' + id, { method: 'DELETE' })
    .then(() => { toast('已删除'); loadMonth(); loadDay(); if (editId === id) { editId = null; closeEdit(); } })
    .catch(e => toast('删除失败：' + e.message));
}
let _attachSig = '';
document.addEventListener('click', e => {            // 点浮层外面就收起，别让它赖在页面上
  const d = document.getElementById('typeDropdown');
  if (!d || d.style.display === 'none') return;
  if (d.contains(e.target) || (e.target.closest && e.target.closest('#typeValue'))) return;
  d.style.display = 'none';
}, true);
let _foldOpen = true;                            // v6.7：默认就展开，附件行必须看得见点得着
function _foldCounts() {
  const box = document.getElementById('attachList');
  const n = { img: 0, file: 0, ref: 0, pend: 0 };
  if (box) {
    n.img = box.querySelectorAll('.attach-thumb, .imgcell:not(.pend)').length;
    n.ref = box.querySelectorAll('.file-chip.ref:not(.pend-ref)').length;
    n.file = box.querySelectorAll('.file-chip:not(.ref):not(.pend-ref)').length;
    n.pend = box.querySelectorAll('.pend-ref, .up-pend').length + pendingRefs.length + upQ.length;
  }
  return n;
}
function syncAttachFold() {
  const w = document.getElementById('attachWrap'), t = document.getElementById('attFoldN');
  if (!w) return;
  const c = _foldCounts(), bits = [];
  if (c.img) bits.push('\ud83d\uddbc' + c.img);
  if (c.ref) bits.push('\ud83d\udd17' + c.ref);
  if (c.file) bits.push('\ud83d\udcc4' + c.file);
  if (c.pend) bits.push('待处理' + c.pend);
  const total = c.img + c.ref + c.file + c.pend;
  if (t) t.textContent = bits.length ? ('（' + bits.join(' ') + '）') : '';
  const isEmpty = !total && editType !== 'note';
  w.classList.toggle('row-off', isEmpty);                    // 真空着（且不是笔记）才不占位
  w.classList.toggle('is-empty', isEmpty);
  try { localStorage.removeItem('calnote-attach-fold'); } catch (e0) { }   // 清掉旧版"记住"，否则永远展开
  if (_foldTouched === null) _foldOpen = total <= 2;          // ≤2 自动摊开，多了收起
  w.classList.toggle('open', _foldOpen && !!total);
  const ar = w.querySelector('.fold-hd .ar'); if (ar) ar.textContent = _foldOpen ? '\u25be' : '\u25b8';
}

let _foldTouched = null;
function toggleAttachFold(e) {
  if (e) e.stopPropagation();
  const w = document.getElementById('attachWrap');
  _foldOpen = !(w && w.classList.contains('open'));
  _foldTouched = 1;                                          // 手动点过就不再自动改
  syncAttachFold();
}
function resetAttachFold() { _foldTouched = null; syncAttachFold(); }
function openAttachFold() { _foldOpen = true; syncAttachFold(); }
function _syncPicRow() {                           // 附件行：是笔记就常驻（要留着放 📁 从NAS选），列表按内容展开
  const pic = document.getElementById('attachWrap');
  if (!pic) return;
  const has = !!pic.querySelector('.attach-thumb, .file-chip, .up-pend, .pend-ref');
  pic.classList.toggle('row-off', !has && editType !== 'note');   // 只有真空着才不占位
  /* 不要再回调 syncAttachFold（会和它互相调用爆栈） */
}
function _ageOf(by, occ) {
  const d = occ ? String(occ).split('-') : [];
  by = parseInt(by, 10);
  if (!by || d.length !== 3) return '';
  let age = parseInt(d[0], 10) - by;
  const t = new Date();
  const passed = (t.getMonth() + 1 > parseInt(d[1], 10)) ||
    (t.getMonth() + 1 === parseInt(d[1], 10) && t.getDate() >= parseInt(d[2], 10));
  if (!passed) age -= 1;
  return age >= 0 ? age : '';
}
function updateAgeTip() {                          // 编辑框里的实时年龄
  const tip = document.getElementById('ageTip');
  if (!tip) return;
  const bt = document.getElementById('birthWrap');
  if (bt && bt.style.display === 'none') { tip.textContent = ''; return; }
  const by = document.getElementById('fBirth') ? document.getElementById('fBirth').value : '';
  const occ = document.getElementById('editMask').dataset.occ || document.getElementById('fDate').value;
  const age = _ageOf(by, occ);
  tip.textContent = (age === '') ? '' : (editType === 'birthday' ? ('今年 ' + age + ' 岁') : ('第 ' + (age + 1) + ' 年'));
}
document.addEventListener('input', e => { if (e.target && (e.target.id === 'fBirth' || e.target.id === 'fDate')) updateAgeTip(); }, true);
function _fitRows() {                            // 一行里没有一个可见控件就整行隐藏；没图的图片行同理
  document.querySelectorAll('.edit-panel .mline, .edit-panel .checks').forEach(r => {
    let on = false;
    Array.prototype.forEach.call(r.children, el => {
      if (el.id === 'allDayHint') return;
      if (getComputedStyle(el).display !== 'none') on = true;
    });
    r.classList.toggle('hide-row', !on);
    r.classList.toggle('row-off', !on);
  });
  _syncPicRow();
}
function _renderAttach(item) {                         // 只有这条的附件真的变了才重建 DOM（省掉重复下载/解码）
  const isPic = (x) => /\.(jpg|jpeg|png|gif|webp|bmp|heic|svg)$/i.test(x.filename || '') || /^image/.test(x.mime || '');
  const list = (item.attachments || []).slice().sort(function (x, y) {   // 分组：图片一排、文件一排
    const ix = isPic(x) ? 0 : 1, iy = isPic(y) ? 0 : 1;
    if (ix !== iy) return ix - iy;
    return String(x.filename || '').localeCompare(String(y.filename || ''));
  });
  const sig = item.id + '|' + list.map(a => a.id).join(',');
  const box = document.getElementById('attachList');
  _attachSig = sig;
  if (!box) return;   // 同一条记录内重渲染（存一份、挂引用）保持当前开合
  box.innerHTML = list.map(renderAttachRow).join('');
  syncAttachFold();
}
function isWide() { return window.innerWidth >= 1000; }
function _syncVH() {                              // v5.15：让编辑层跟随键盘（可视视口），正文不再被键盘压住
  const vv = window.visualViewport;
  const h = Math.round(vv ? Math.min(vv.height, window.innerHeight) : window.innerHeight);
  document.documentElement.style.setProperty('--vvh', h + 'px');
}
(function () {
  _syncVH();
  if (window.visualViewport) {
    window.visualViewport.addEventListener('resize', _syncVH);
    window.visualViewport.addEventListener('scroll', _syncVH);
  }
  window.addEventListener('resize', _syncVH);
})();
function _markType() {                            // 面板上标当前类型：笔记=收行，日程/生日等=摊行
  const m = document.getElementById('editMask');
  if (m) m.dataset.t = editType;
}
const __origCloseEdit = closeEdit;
closeEdit = function () {
  if (isWide()) { resetDraft(); return; }             // 网页版右栏常驻：取消＝清成空白草稿（不抢光标）
  __origCloseEdit();
};
function resetDraft() {                               // 清成一条空白新建草稿，但不 focus（免得把页面滚走）
  const keep = selectedDate;
  openCreate(keep, 'note');
  const t = document.getElementById('fTitle');
  if (t && t.blur) t.blur();
  document.getElementById('editMask').style.setProperty('display', 'flex', 'important');
}
const __origInit = null;
function showEditorDraft() {
  const m = document.getElementById('editMask');
  if (!m) return;
  m.style.display = 'flex';
}
(function editorDockOnLoad() {
  const apply = () => {
    document.body.classList.toggle('qw-wide', isWide());
    var m = document.getElementById('editMask');
    var 正在编辑 = m && m.style && m.style.display === 'flex';
    if (isWide() && !正在编辑) resetDraft();     // 只在没开编辑框时铺一个空草稿；正在写的内容绝不清
  };
  apply();
  window.addEventListener('resize', () => {
    const was = document.body.classList.contains('qw-wide');
    apply();
    if (was !== isWide()) { _syncVH(); try { syncAttachFold(); } catch (e) { } }   // 只重算布局，不关框（否则正在写的内容会丢）
  });
})();

/* ================= v5.12：文件夹 + 任意附件 + 看原图 ================= */
async function loadFolders() {
  try {
    const r = await api('/api/folders');
    folders = r.folders || [];
  } catch (e) { folders = []; }
  renderFolderSel(document.getElementById('fFolder') ? document.getElementById('fFolder').value : editFolder);
  renderCatList();
}
function renderFolderSel(sel) {
  const el = document.getElementById('fFolder');
  if (!el) return;
  const cur = parseInt(sel != null && sel !== '' ? sel : editFolder, 10) || 0;
  el.innerHTML = '<option value="0">未归类</option>' +
    folders.map(f => `<option value="${f.id}"${f.id === cur ? ' selected' : ''}>\ud83d\udcc1 ${esc(f.name)}${f.count ? ' · ' + f.count : ''}</option>`).join('') +
    (cur ? `<option value="__ren">✎ 重命名「${esc((folders.find(x => x.id === cur) || {}).name || '')}」…</option>
    <option value="__del">🗑 删掉这个文件夹（笔记变未归类）</option>` : '') +
    '<option value="__new">＋ 新建文件夹…</option>';
  editFolder = cur;
}
function persistFolderNow() {                        // 已存在的条目：改归类立刻落库（也能改回未归类）
  if (!editId) return;
  const f = folderIdNow();
  api('/api/items/' + editId + '/meta', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ folder_id: f }) })
    .then(() => { const c = itemCache[editId]; if (c) { c.folder_id = f; c.folder_name = (folders.filter(x => x.id === f)[0] || {}).name || ''; }
      toast(f ? ('已归到「' + (folders.filter(x => x.id === f)[0] || {}).name + '」，笔记仍在当天日历') : '已改成未归类'); })
    .catch(e => toast('归类没保存：' + e.message));
}
function folderIdNow() {
  const el = document.getElementById('fFolder');
  if (!el) return editFolder || 0;
  const v = el.value;
  if (v === '__new' || v === '') return editFolder || 0;
  return parseInt(v, 10) || 0;
}
async function onFolderPick(el) {
  const v = el.value;
  if (v !== '__new' && v !== '__ren' && v !== '__del') { editFolder = parseInt(v, 10) || 0; persistFolderNow(); return; }
  if (v === '__ren') { el.value = String(editFolder || 0); return renameFolder(editFolder); }
  if (v === '__del') { el.value = String(editFolder || 0); return deleteFolder(editFolder); }
  const name = (prompt('新文件夹名字（如 工作、日记、生日）') || '').trim();
  el.value = String(editFolder || 0);
  if (!name) return;
  try {
    const r = await api('/api/folders', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }) });
    folders.push(Object.assign({ count: 0 }, r.folder));
    editFolder = r.folder.id;
    renderFolderSel(editFolder);
    toast('已建文件夹：' + r.folder.name);
  } catch (e) { toast('建不了：' + e.message); }
}
async function renameFolder(id) {
  const f = folders.filter(x => x.id === id)[0];
  if (!f) { toast('先选一个文件夹'); return; }
  const name = (prompt('文件夹改名（原来写在笔记上的名字会一起变）', f.name) || '').trim();
  if (!name || name === f.name) return;
  try {
    await api('/api/folders/' + id, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }) });
    f.name = name;
    renderFolderSel(editFolder); renderCatList();
    if (nasState.dir && _catFolder === id) closeCatView(), openCatViewFolder(id);
    toast('已改名：' + name);
  } catch (e) { toast('改不了：' + e.message); }
}
async function deleteFolder(id) {
  const f = folders.filter(x => x.id === id)[0];
  if (!f) return;
  if (!confirm('删掉文件夹「' + f.name + '」？里面的笔记不会被删，只是变回「未归类」。')) return;
  try {
    await api('/api/folders/' + id, { method: 'DELETE' });
    folders.forEach(x => { if (x.id === id) x._gone = 1; }); folders = folders.filter(x => !x._gone);
    folders = folders.filter(x => x.id !== id);
    if (editFolder === id) editFolder = 0;
    if (_catFolder === id) closeCatView();
    renderFolderSel(editFolder); renderCatList();
    toast('文件夹已删，笔记还在');
  } catch (e) { toast('删不了：' + e.message); }
}
var _catFolder = 0;
async function quickNewFolder() {
  const name = (prompt('新文件夹名字（工作 / 日记 / 生日 / 日程 / 待办…）') || '').trim();
  if (!name) return;
  try {
    const r = await api('/api/folders', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }) });
    folders.push(Object.assign({ count: 0 }, r.folder));
    editFolder = r.folder.id; renderFolderSel(editFolder); renderCatList();
    toast('已建：' + r.folder.name);
  } catch (e) { toast('建不了：' + e.message); }
}
function newInFolder() {
  openCreate(selectedDate || todayStr(), 'note');
  editFolder = _catFolder || editFolder;
  renderFolderSel(editFolder);
}
function openCatViewFolder(id) {
  const f = folders.filter(x => x.id === id)[0];
  if (!f) return;
  _catFolder = id;
  catShow('文件夹 · ' + f.name, 'folder:' + id);   // 与分类共用：50 条起、下滑续、底部显示总数
}

/* 任意文件：选、传、列成一行 */
let _srcKind = 'img';
function pickSource(e, kind) {
  if (e) e.stopPropagation();
  _srcKind = kind;
  const pop = document.getElementById('srcPop');
  if (!pop) { (kind === 'img' ? document.getElementById('fFile') : document.getElementById('fAnyFile')).click(); return; }
  pop.style.display = pop.style.display === 'none' ? 'flex' : 'none';
  const b = (e && e.target && e.target.getBoundingClientRect) ? e.target.getBoundingClientRect() : { top: 120, left: 20 };
  pop.style.top = Math.round(b.bottom + 6) + 'px';
  pop.style.left = Math.max(8, Math.min(innerWidth - 210, Math.round(b.left - 8))) + 'px';
}
function srcGo(where) {
  const pop = document.getElementById('srcPop'); if (pop) pop.style.display = 'none';
  if (where === 'nas') { openNasPicker(_srcKind); return; }
  const i = document.getElementById(_srcKind === 'img' ? 'fFile' : 'fAnyFile');
  if (i) i.click();
}
function pickAnyFile() { const i = document.getElementById('fAnyFile'); if (i) i.click(); }
function isImgName(f) { return /^image\//.test(f.type || '') || /\.(png|jpe?g|gif|webp|bmp|svg)$/i.test(f.name || ''); }
function prettySize(n) { return n > 1048576 ? (n / 1048576).toFixed(1) + 'MB' : (n > 1024 ? Math.round(n / 1024) + 'KB' : (n || 0) + 'B'); }

/* 看原图 / 预览附件 */
function viewPic(url, name, ct, path, missing) {
  const box = document.getElementById('picView');
  if (!box) { location.href = url; return; }          // 内置浏览器常拦 window.open，直接本页打开
  [].slice.call(box.querySelectorAll('img,video,audio,iframe,.doc-view,.txt-view')).forEach(function (n) {
    try { if (n.pause) n.pause(); } catch (e) { }      // 视频/音频先停，别留着继续播
    n.remove();
  });
  const nm = document.getElementById('picName');
  if (nm) nm.textContent = (name || '') + (path ? ('　·　' + path) : '');
  document.addEventListener('click', function (ev) {
    const p = ev.target && ev.target.closest ? ev.target.closest('.fpath') : null;
    if (!p) return;
    const holder = p.closest('[data-full]');
    const box = document.getElementById('qwToast');
    const txt = (holder && holder.dataset.full) || '没有路径信息';
    if (box) { box.textContent = txt; box.classList.add('on'); clearTimeout(window.__tT); window.__tT = setTimeout(function(){box.classList.remove('on');}, 4000); }
    else { alert(txt); }
  }, true);
  const dl = document.getElementById('picDl');
  if (dl) dl.href = url + '?dl=1';
  const op = document.getElementById('picOpen');
  if (op) { op.href = url; op.style.display = ''; }
  let el;
  if (missing) {
    el = document.createElement('div'); el.className = 'doc-view';
    el.innerHTML = '<div class="dv">⚠</div><div class="dn">原文件读不到（被移动或改名）</div><div class="dp">' + esc(path || '') + '</div>';
  } else if (/^video\//.test(ct)) {
    el = document.createElement('video'); el.controls = true; el.autoplay = true; el.setAttribute('playsinline', ''); el.src = url;
  } else if (/^audio\//.test(ct)) {
    el = document.createElement('audio'); el.controls = true; el.autoplay = true; el.src = url;
  } else if (ct.indexOf('pdf') > 0) {
    el = document.createElement('iframe'); el.src = url; el.style.cssText = 'width:96vw;height:82vh;border:none;background:#fff';
  } else if (ct.indexOf('text/') === 0) {
    viewText(url, name, path);
    return;
  } else if (/^image\//.test(ct) || ct === '') {
    el = document.createElement('img'); el.src = url; el.style.cursor = 'zoom-in'; el.dataset.zoom = '0';
    el.onerror = function () { el.replaceWith(Object.assign(document.createElement('div'), { className: 'doc-view', innerHTML: '<div class="dv">🚫</div><div class="dn">这个格式不能直接预览</div><div class="dp">' + esc(path || '') + '</div>' })); };
    if (ct.indexOf('text/') === 0) { /* 文本走 img 会花屏，交给下载 */ }
    el.ondblclick = function (ev) { ev.stopPropagation(); zoomPic(el); };
  } else {
    el = document.createElement('div'); el.className = 'doc-view';
    el.innerHTML = '<div class="dv">📄</div><div class="dn">' + esc(name || '文件') + '</div>'
      + '<div class="dp">' + esc(path || '') + '</div>'
      + '<a class="db" href="' + url + '?dl=1">点这里下载 / 打开</a>';
    el.onclick = function (ev) { ev.stopPropagation(); };
  }
  box.insertBefore(el, box.firstChild);
  box.style.display = 'flex';
  try { if (!window.__picPush) { history.pushState({ pic: 1 }, ''); window.__picPush = 1; } } catch (e) { }
}
function closePic() {
  const b = document.getElementById('picView');
  if (b) b.style.display = 'none';
  closeAsSheet();
}
window.addEventListener('popstate', function () {
  const b = document.getElementById('picView');
  if (b && b.style.display && b.style.display !== 'none') { b.style.display = 'none'; window.__picPush = 0; }
  closeAsSheet();
});
function zoomPic(im) {
  const on = im.dataset.zoom !== '1';
  im.style.transform = on ? 'scale(1.9)' : 'scale(1)';
  im.style.cursor = on ? 'zoom-out' : 'zoom-in';
  im.dataset.zoom = on ? '1' : '0';
}

/* 编辑框里那一行：图片缩略图 + 文件条目（都能点开看） */
const _asMap = {}; let _asSeq = 0;
function _asAdd(info) { const k = 'a' + (++_asSeq); _asMap[k] = info; return k; }
function closeAsSheet() { const s = document.getElementById('attSheet'); if (s) s.style.display = 'none'; }
window.addEventListener('resize', closeAsSheet, { passive: true });
document.addEventListener('scroll', closeAsSheet, true);

/* ═══ v7.9：三层复制（clipboard → execCommand → 屏幕上给你长按）+ 下载三级降级 + 系统分享 ═══ */
function fallbackCopy(txt) {
  var ta = document.getElementById('copyFallback');
  if (!ta) {
    ta = document.createElement('textarea');
    ta.id = 'copyFallback';
    ta.setAttribute('readonly', '');
    ta.style.cssText = 'position:fixed;left:10px;right:10px;top:12%;z-index:10005;height:96px;font-size:14px;padding:10px;' +
      'border-radius:12px;border:1px solid #CDDAFE;box-sizing:border-box;background:#fff;color:#1B2430';
    document.body.appendChild(ta);
  }
  ta.value = txt;
  ta.style.display = 'block';
  try { ta.focus(); ta.select(); ta.setSelectionRange(0, txt.length); } catch (e) { }
  toast('这浏览器不让程序复制：已放在上面的框里，长按全选即可');
}
function doCopy(txt, label) {
  txt = String(txt == null ? '' : txt);
  function way1() {
    return (navigator.clipboard && navigator.clipboard.writeText)
      ? navigator.clipboard.writeText(txt) : Promise.reject(new Error('无剪贴板'));
  }
  function way2() {
    return new Promise(function (res, rej) {
      try {
        var t = document.createElement('textarea');
        t.value = txt; t.setAttribute('readonly', '');
        t.style.cssText = 'position:fixed;left:-9999px;top:0';
        document.body.appendChild(t); t.focus(); t.select();
        var ok = document.execCommand('copy');
        t.parentNode.removeChild(t);
        ok ? res(true) : rej(new Error('execCommand 不生效'));
      } catch (e) { rej(e); }
    });
  }
  way1().catch(way2).then(function () {
    toast('已复制：' + String(label || txt).split('\n')[0].slice(0, 18));
  }).catch(function () { fallbackCopy(txt); });
}
function attHref(it, dl) {
  return it.url + (dl ? ((it.url.indexOf('?') < 0 ? '?' : '&') + 'dl=1') : '');
}
function dlIframe(url) {
  var f = document.getElementById('dlFrame');
  if (!f) {
    f = document.createElement('iframe');
    f.id = 'dlFrame';
    f.style.cssText = 'position:fixed;width:0;height:0;border:0;visibility:hidden';
    document.body.appendChild(f);
  }
  f.src = url;
  setTimeout(function () { try { f.src = 'about:blank'; } catch (e) { } }, 25000);
}
function saveViaAnchor(url, name) {
  try {
    var el = document.createElement('a');
    el.href = url; el.download = name || ''; el.target = '_blank'; el.rel = 'noopener';
    document.body.appendChild(el); el.click();
    setTimeout(function () { el.remove(); }, 1500);
    toast('已把文件交给浏览器，请在通知栏/下载页里选保存位置');
  } catch (e) { dlIframe(url); toast('换了一种方式触发，去通知栏看有没有'); }
}
function shareAtt(it) {
  var url = attHref(it, false);
  if (it.pend === 'file') { saveViaAnchor(it.url, it.name); toast('还没上传，先保存这条记录再分享'); return; }
  fetch(url, { cache: 'no-store' }).then(function (r) {
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.blob();
  }).then(function (b) {
    if (!(navigator.canShare && navigator.share)) { saveViaAnchor(url, it.name); toast('这浏览器没有系统分享，已改成下载'); return; }
    var file;
    try { file = new File([b], it.name || '文件', { type: b.type || 'application/octet-stream' }); }
    catch (e) { saveViaAnchor(url, it.name); return; }
    var can = true;
    try { can = navigator.canShare({ files: [file] }); } catch (e) { can = false; }
    if (!can) { saveViaAnchor(url, it.name); toast('这种文件不让直接分享，已改成下载'); return; }
    toast('已调起系统面板：选「文件管理 / 另存为 / WPS / 微信」');
    navigator.share({ files: [file], title: it.name || '', text: it.path || '' }).catch(function () { });
  }).catch(function (e) {
    dlIframe(url);
    toast('调系统面板失败，已改为交给浏览器：' + String(e.message).slice(0, 22));
  });
}
function downloadAtt(it) {
  var url = attHref(it, true);
  fetch(url, { cache: 'no-store' }).then(function (r) {
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.blob();
  }).then(function (b) {
    var ok = false;
    try {
      if (navigator.canShare && navigator.share) {
        var file = new File([b], it.name || '文件', { type: b.type || 'application/octet-stream' });
        if (navigator.canShare({ files: [file] })) {
          ok = true;
          toast('已调起系统保存/分享面板');
          navigator.share({ files: [file], title: it.name || '' }).catch(function () { saveViaAnchor(url, it.name); });
        }
      }
    } catch (e) { ok = false; }
    if (!ok) saveViaAnchor(url, it.name);
  }).catch(function (e) {
    dlIframe(url);
    toast('浏览器接管了下载（去通知栏/下载页看）。没动静的话点「用手机应用打开」' );
  });
}

function attachSheet(k, ev) {
  if (ev) { ev.stopPropagation(); ev.preventDefault(); }   // 两个都要：文件条的 ⋯ 在 <a href> 里面，只停传播会走默认下载
  const it = _asMap[k]; if (!it) return;
  const acts = [];
  if (it.pend === 'ref') {
    acts.push(['\ud83d\udd0d 预览（不用先保存）', function(){ viewPic(it.url, it.name, it.mime, it.path, false); }]);
    acts.push(['\u2913 下载原文件', function(){ downloadAtt(it); }]);
    acts.push(['\u2197 用手机应用打开 / 分享', function(){ shareAtt(it); }]);
    acts.push(['\u29c9 复制文件名+路径', function(){ doCopy(it.copy, it.name); }]);
    acts.push(['\u2715 从待挂里去掉', function(){ pendingRefs = pendingRefs.filter(function(x){return x!==it.path;}); renderPendingRefs(); }]);
  } else if (it.pend === 'file') {
    acts.push(['\ud83d\udd0d 先看一眼', function(){ viewPic(it.url, it.name, it.mime, '', false); }]);
    acts.push(['\u29c9 复制文件名', function(){ doCopy(it.name, it.name); }]);
    acts.push(['\u2715 不传这个', function(){ if (it.rm) it.rm(); }]);
  } else {
    acts.push(['\ud83d\udd0d 预览 / 看大图', function(){ viewPic(it.url, it.name, it.mime, it.path, it.missing); }]);
    acts.push(['\u2913 下载原文件', function(){ downloadAtt(it); }]);
    acts.push(['\u2197 用手机应用打开 / 分享', function(){ shareAtt(it); }]);
    acts.push(['\u29c9 复制' + (it.path ? '文件名+路径' : '下载链接'), function(){ doCopy(it.copy, it.name); }]);
    if (it.link) acts.push(['\ud83d\udcbe 存一份到应用（防原文件被删）', function(){ solidifyAttach(it.id); }]);
    acts.push(['\u2715 从这条记录移出', function(){ askRemoveAttach(it.id, it.link ? 'link' : 'upload'); }]);
  }
  const nm = document.getElementById('asName'), ls = document.getElementById('asList'), sh = document.getElementById('attSheet');
  if (!nm || !ls || !sh) return;
  const _r = (ev && ev.target && ev.target.getBoundingClientRect) ? ev.target.getBoundingClientRect() : null;
  nm.textContent = it.name + (it.path ? ('\u3000\u00b7\u3000' + it.path) : '');
  ls.innerHTML = acts.map(function(x,t){ return '<button type="button" class="as-item' + (x[0].charAt(0)==='\u2715' ? ' del' : '') + '" data-t="' + t + '">' + esc(x[0]) + '</button>'; }).join('');
  [].slice.call(ls.querySelectorAll('.as-item')).forEach(function(b){
    b.onclick = function(){ closeAsSheet(); try { acts[parseInt(b.dataset.t,10)][1](); } catch (e) { toast('做不了：' + e.message); } };
  });
  sh.style.display = 'flex';
  _asPlace(sh, _r);
}
function _asPlace(sh, r) {
  const box = sh.querySelector('.as-box'); if (!box) return;
  const bw = 208, bh = box.offsetHeight || 200, pad = 8;
  let left = r ? (r.left - 150) : ((innerWidth - bw) / 2);
  left = Math.max(pad, Math.min(left, innerWidth - bw - pad));
  let top = r ? (r.bottom + 5) : (innerHeight / 2 - bh / 2);
  if (top + bh > innerHeight - pad) top = Math.max(pad, (r ? r.top : 80) - bh - 5);   // 下面放不下就往上翻
  if (top < pad) top = pad;
  box.style.left = Math.round(left) + 'px';
  box.style.top = Math.round(top) + 'px';
}
function renderAttachRow(att) {
  const url = att.url || ('/api/attachments/' + att.id);
  const nm = att.filename || '\u6587\u4ef6';
  const isImg = /\.(jpg|jpeg|png|gif|webp|bmp|heic|svg)$/i.test(nm) || /^image/.test(att.mime || '');   // 不看 thumb：后端对所有上传件都给 thumb
  const link = att.kind === 'link';
  const k = _asAdd({ url: url, name: nm, mime: att.mime || (isImg ? 'image/' : ''), path: att.path || '',
                     missing: !!att.missing, id: att.id, link: link,
                     copy: att.path ? (nm + '\n' + att.path) : (location.origin + '/api/attachments/' + att.id) });
  const more = '<button type="button" class="amore" onclick="attachSheet(\'' + k + '\',event);return false" aria-label="\u66f4\u591a">\u22ef</button>';
  const dat = 'data-att="' + att.id + '" data-name="' + esc(nm) + '" data-full="' + esc(att.path || '') + '" data-mime="' + esc(att.mime || '') + '" data-url="' + esc(url) + '" data-missing="' + (att.missing ? 1 : 0) + '"';
  if (link && isImg) {
    return '<div class="imgcell ref' + (att.missing ? ' miss' : '') + '" ' + dat + '>' +
      '<a href="' + esc(url) + '" onclick="return openLink(event,' + att.id + ',this)">' +
      (att.missing ? '<span class="warn">\u26a0</span>' : '<img loading="lazy" decoding="async" src="' + esc(att.thumb || url) + '" alt="">') +
      '</a>' + more + '</div>';
  }
  if (link) {
    return '<a class="file-chip ref' + (att.missing ? ' miss' : '') + '" ' + dat + ' href="' + esc(url) + '" onclick="return openLink(event,' + att.id + ',this)">' +
      '<span class="fi">' + (att.missing ? '\u26a0' : nasIconExt(nm)) + '</span><span class="fn">' + esc(nm) + '</span>' +
      '<span class="fs">' + (att.missing ? '\u539f\u6587\u4ef6\u4e0d\u5728' : prettySize(att.size)) + '</span>' + more + '</a>';
  }
  if (isImg) {
    return '<div class="imgcell" ' + dat + '><a href="' + esc(url) + '" onclick="return openLink(event,' + att.id + ',this)">' +
      '<img loading="lazy" decoding="async" src="' + esc(url) + '" alt=""></a>' + more + '</div>';
  }
  return '<a class="file-chip" ' + dat + ' href="' + esc(url) + '" onclick="return openLink(event,' + att.id + ',this)">' +
    '<span class="fi">' + nasIconExt(nm) + '</span><span class="fn">' + esc(nm) + '</span>' +
    '<span class="fs">' + prettySize(att.size) + '</span>' + more + '</a>';
}
function openLink(e, attId, el) {
  const holder = (el && el.closest) ? (el.matches && el.matches('[data-att]') ? el : el.closest('[data-att]')) : null;
  try { openAttach(attId, holder || el); } catch (err) { return true; }      // 出错：直接放行给浏览器
  const box = document.getElementById('picView');
  const t = e && e.target;
  if (t && t.closest && t.closest('button, .fd, .fx, .amore')) {      // 点行内按钮也不跳页
    if (e && e.preventDefault) e.preventDefault();
    return false;
  }
  if (e && e.preventDefault) e.preventDefault();     // 绝不跳页：预览打不开也留在应用里
  return false;
}

/* ================= v6.0：NAS 文件选择器（引用，不上传） ================= */
function nasUrl(o) {
  const p = [];
  if (o.dir) p.push('dir=' + encodeURIComponent(o.dir));
  if (o.sort && o.sort !== 'name') p.push('sort=' + o.sort);
  if (o.desc) p.push('desc=1');
  nasState._sort = o.sort || 'name'; nasState._desc = !!o.desc;
  if (o.q) p.push('q=' + encodeURIComponent(o.q));
  if (o.only) p.push('only=' + o.only);
  if (o.off) p.push('off=' + o.off);
  return '/api/nas/ls' + (p.length ? '?' + p.join('&') : '');
}
function openNasPicker(kind) {
  if (!document.getElementById('nasMask')) return;
  nasState.kind = kind || 'img';
  nasState.only = '';                       // 不再默认藏掉视频/文档
  try { nasState.sort = localStorage.getItem('calnote-nas-sort') || 'name';
        nasState.desc = localStorage.getItem('calnote-nas-desc') === '1'; } catch (e) { nasState.sort = 'name'; }
  try { document.getElementById('nasAll').classList.toggle('on', !nasState.only);
        document.getElementById('nasImg').classList.toggle('on', nasState.only === 'img'); } catch (e) { }
  nasState.sel = {};
  try { nasState.last = localStorage.getItem('calnote-nas-dir') || ''; } catch (e) { nasState.last = ''; }
  try { nasState.view = localStorage.getItem('calnote-nas-view') || 'list'; } catch (e) { nasState.view = 'list'; }
  _nasViewBtns();
  document.getElementById('nasMask').style.display = 'flex';
  nasLoad(nasState.last || '');
}
function nasHome() { nasLoad(''); }
function _nasViewBtns() {
  const g = document.getElementById('nasGridBtn'), l = document.getElementById('nasListBtn');
  if (g) g.classList.toggle('on', nasState.view === 'grid');
  if (l) l.classList.toggle('on', nasState.view === 'list');
}
function nasCycleSort() {
  const order = ['name', 'time', 'size'];
  nasState.sort = order[(order.indexOf(nasState.sort || 'name') + 1) % 3];
  try { localStorage.setItem('calnote-nas-sort', nasState.sort); } catch (e) { }
  nasLoad(nasState.dir, 0, true);
}
function nasFlipSort() {
  nasState.desc = !nasState.desc;
  try { localStorage.setItem('calnote-nas-desc', nasState.desc ? '1' : ''); } catch (e) { }
  nasLoad(nasState.dir, 0, true);
}
function _nasSortBtns() {
  const b = document.getElementById('nasSort'), f = document.getElementById('nasDesc');
  const nm = { name: '名称', time: '时间', size: '大小' }[nasState.sort || 'name'];
  if (b) b.textContent = '排序：' + nm;
  if (f) { f.textContent = nasState.desc ? '↓' : '↑'; f.title = nasState.desc ? '当前从新/大到旧/小' : '当前从旧/小到新/大'; }
}
function setNasView(v) {
  nasState.view = v;
  try { localStorage.setItem('calnote-nas-view', v); } catch (e) { }
  _nasViewBtns();
  nasLoad(nasState.dir, 0, true);
}
function nasIcon(e) {
  const n = (e.name || '').toLowerCase();
  if (/\.(jpg|jpeg|png|gif|webp|bmp|heic|svg)$/.test(n)) return '🖼';
  if (/\.(mp4|mov|avi|mkv|webm|3gp)$/.test(n)) return '🎬';
  if (/\.(mp3|m4a|wav|aac|flac)$/.test(n)) return '🎵';
  if (/\.pdf$/.test(n)) return '📕';
  if (/\.(doc|docx|xls|xlsx|ppt|pptx)$/.test(n)) return '📘';
  if (/\.(zip|rar|7z|gz)$/.test(n)) return '🗜';
  if (/\.(txt|md|csv|log|json)$/.test(n)) return '📝';
  return '📄';
}
function nasDate(t) {
  try { const d = new Date(t * 1000); return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0'); }
  catch (e) { return ''; }
}
function nasMoreClick() { if (nasState.more) nasLoad(nasState.dir, nasState.shown || 0, true); }
function nasWatchMore() {
  if (!('IntersectionObserver' in window)) return;
  const el = document.querySelector('#nasList .nas-more');
  if (!el) return;
  if (nasState.io) nasState.io.disconnect();
  nasState.io = new IntersectionObserver(es => {
    es.forEach(en => { if (en.isIntersecting) nasMoreClick(); });
  }, { root: document.getElementById('nasList'), threshold: 0.01 });
  nasState.io.observe(el);
}
function nasAutoMore() {
  const box = document.getElementById('nasList');
  if (!box || nasState.loading || !nasState.more) return;
  if (box.scrollTop + box.clientHeight < box.scrollHeight - 260) return;
  nasLoad(nasState.dir, nasState.shown || 0, true);
}
function nasCrumb(dir) {
  const box = document.getElementById('nasPath');
  if (!box) return;
  if (!dir) { box.innerHTML = '<span class="crumb on">🏠 选择来源</span>'; return; }
  const parts = dir.split('/');
  let html = '<span class="crumb" onclick="nasHome()">🏠</span>';
  let acc = '';
  parts.forEach((seg) => {
    acc = acc ? acc + '/' + seg : seg;
    const p = acc.replace(/'/g, '');
    html += '<span class="crumb-sep">/</span><span class="crumb" onclick="nasLoad(\'' + p.replace(/'/g, "\'") + '\')">' + esc(seg) + '</span>';
  });
  box.innerHTML = html;
}
function closeNasPicker() { document.getElementById('nasMask').style.display = 'none'; }
function nasEnter(path) { nasLoad(path); }
function nasUp() {
  const d = nasState.dir;
  nasLoad(d.indexOf('/') < 0 ? '' : d.slice(0, d.lastIndexOf('/')));
}
let _nasT = null;
function nasSearchSoon() { clearTimeout(_nasT); _nasT = setTimeout(() => nasLoad(nasState.dir, 0, true), 420); }
function setNasOnly(v) {
  nasState.only = v;
  document.getElementById('nasAll').classList.toggle('on', !v);
  document.getElementById('nasImg').classList.toggle('on', v === 'img');
  nasLoad(nasState.dir);
}
function nasLoad(dir, off, keepQ) {
  nasState.loading = true;
  if (!keepQ) off = 0;
  nasState.dir = dir || '';
  if (!keepQ) nasState.q = '';
  const qi = document.getElementById('nasQ');
  if (qi && !keepQ) qi.value = '';
  try { localStorage.setItem('calnote-nas-dir', nasState.dir); } catch (e2) { }
  document.getElementById('nasUp').style.visibility = nasState.dir ? 'visible' : 'hidden';
  nasCrumb(nasState.dir);
  api(nasUrl({ dir: nasState.dir, q: nasState.q, only: nasState.only, off: off || 0, sort: nasState.sort, desc: nasState.desc })).then(r => {
    const list = document.getElementById('nasList');
    if (list && !list._bound) { list._bound = 1; list.addEventListener('scroll', nasAutoMore, { passive: true }); }
    if (!off) nasState.acc = [];
    nasState.acc = nasState.acc.concat(r.entries || []);
    // 分组：文件夹 → 图片/视频 → 其它文件（组内保持后端给的排序）
    const dirs = nasState.acc.filter(e => e.is_dir), images = nasState.acc.filter(e => !e.is_dir && e.is_image), files = nasState.acc.filter(e => !e.is_dir && !e.is_image);
    if (!off) nasState.grouped = dirs.concat(images, files);
    nasState.shown = nasState.acc.length;
    nasState.more = (r.total || 0) > nasState.shown;
    nasState.loading = false;
    if (!nasState.acc.length) { list.innerHTML = '<div class="nas-empty">' + esc(r.error || '这个目录没有可读的文件') + '</div>'; nasFoot(); return; }
    list.className = nasState.view === 'grid' ? 'nas-grid' : 'nas-rows';
    const grid = nasState.view === 'grid';
    let html = '', lastG = '';
    nasState.grouped.forEach(e => {
      const g = e.is_dir ? 'dir' : (e.is_image ? 'img' : 'doc');
      if (g !== lastG) {
        const label = g === 'dir' ? '文件夹' : (g === 'img' ? '图片 / 视频' : '文件');
        const cnt = g === 'img' ? images.length : (g === 'doc' ? files.length : dirs.length);
        html += `<div class="grp-tip">${label}　${cnt}</div>`;
        lastG = g;
      }
      html += grid ? nasCell(e) : nasLine(e);
    });
    html += nasState.more
      ? `<div class="nas-more" onclick="nasMoreClick()">继续加载 · 已显示 ${nasState.shown} / ${r.total || 0}（滑到底自动，也可点这里）</div>`
      : `<div class="nas-more">已全部加载 ${r.total || 0} 项</div>`;
    list.innerHTML = html;
    nasFoot(); _nasSortBtns(); nasWatchMore();
    if (nasState.more && !grid && (nasState.tries || 0) < 3) {
      if (list.scrollHeight <= list.clientHeight + 10) { nasState.tries++; nasMoreClick(); }
    }
    nasState.tries = 0;
  }).catch(e => {
    nasState.loading = false;
    const list = document.getElementById('nasList');
    if (list) list.innerHTML = '<div class="nas-empty">读不到：' + esc(e.message) + '</div>';
  });
}
function _nesc(v) { return String(v == null ? '' : v).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'); }
function nasLine(e) {
  const key = e.path, on = nasState.sel[key] ? ' on' : '';
  const sub = [(e.size ? prettySize(e.size) : ''), (e.mtime ? nasDate(e.mtime) : '')].filter(Boolean).join(' · ');
  if (e.is_dir) return `<div class="nas-row dir" onclick="nasLoad('${_nesc(key)}')"><span class="ri">\ud83d\udcc1</span><span class="rn">${_nesc(e.name)}</span><span class="rm">${e.n || 0}${e.n_more ? '+' : ''} 项</span><span class="go">›</span></div>`;
  const mini = e.is_image
    ? `<img class="minithumb" loading="lazy" src="/api/nas/thumb?path=${encodeURIComponent(key)}&w=72" onerror="this.outerHTML='<span class=ri>\ud83d\uddbc</span>'">`
    : `<span class="ri">${nasIcon(e)}</span>`;
  return `<div class="nas-row${on}" data-p="${_nesc(key)}" onclick="nasToggleSel(this)">
    <span class="rth">${mini}</span>
    <span class="rmain"><span class="rn">${_nesc(e.name)}</span><span class="rsub">${sub}</span></span>
    <span class="ck">✓</span></div>`;
}
function nasCell(e) {
  const key = e.path, on = nasState.sel[key] ? ' on' : '';
  const sub = [(e.size ? prettySize(e.size) : ''), (e.mtime ? nasDate(e.mtime) : '')].filter(Boolean).join(' · ');
  if (e.is_dir) return `<div class="nas-cell dir" onclick="nasLoad('${_nesc(key)}')"><span class="ri">\ud83d\udcc1</span><span class="rn">${_nesc(e.name)}</span><span class="rm">${e.n || 0}${e.n_more ? '+' : ''} 项</span><span class="go">›</span></div>`;
  const th = e.is_image
    ? `<img loading="lazy" decoding="async" src="/api/nas/thumb?path=${encodeURIComponent(key)}&w=140" onerror="this.parentNode.classList.add('nothumb')">`
    : `<span class="ficon">${nasIcon(e)}</span>`;
  return `<div class="nas-cell${on}" data-p="${_nesc(key)}" onclick="nasToggleSel(this)">
    <span class="th">${th}<span class="ck">✓</span></span>
    <span class="cnm" title="${_nesc(e.name)}">${_nesc(e.name)}</span><span class="cmt">${sub}</span></div>`;
}
function nasToggleSel(el) {
  const p = el.getAttribute('data-p');
  if (nasState.sel[p]) delete nasState.sel[p]; else nasState.sel[p] = true;
  el.classList.toggle('on', !!nasState.sel[p]);
  nasFoot();
}
function nasRowClick(el) { return nasToggleSel(el); }
function nasFoot() {
  const n = Object.keys(nasState.sel).length;
  document.getElementById('nasSelN').textContent = n ? ('已选 ' + n + ' 个') : '未选择';
}
function renderPendingRefs() {
  const box = document.getElementById('attachList');
  if (!box) return;
  [].slice.call(box.querySelectorAll('.pend-wrap')).forEach(function (n) { n.remove(); });
  const IMGRE = /\.(jpg|jpeg|png|gif|webp|bmp|heic|svg)$/i;
  pendingRefs.slice().reverse().forEach(function (p) {
    const name = String(p).split('/').pop() || p, isImg = IMGRE.test(name);
    const pv = '/api/nas/preview?path=' + encodeURIComponent(p);
    const k = _asAdd({ pend: 'ref', url: pv, name: name, mime: isImg ? 'image/' : '', path: p, copy: name + '\n' + p });
    const more = '<button type="button" class="amore" onclick="attachSheet(\'' + k + '\',event);return false">\u22ef</button>';
    const d = document.createElement('div');
    d.className = 'pend-wrap';
    d.setAttribute('data-full', p);
    const jq = encodeURIComponent(p).replace(/'/g, '');
    d.innerHTML = isImg
      ? '<div class="imgcell ref pend" data-name="' + esc(name) + '" data-full="' + esc(p) + '"><a href="' + pv + '" onclick="return openPendRef2(event,' + jq + '">' +
        '<img loading="lazy" src="/api/nas/thumb?path=' + encodeURIComponent(p) + '&w=60" alt=""></a>' + more + '</div>'
      : '<a class="file-chip ref pend" data-name="' + esc(name) + '" data-full="' + esc(p) + '" href="' + pv + '" onclick="return openPendRef2(event,' + jq + '">' +
        '<span class="fi">' + nasIconExt(name) + '</span><span class="fn">' + esc(name) + '</span><span class="fs">\u5f85\u6302</span>' + more + '</a>';
    box.appendChild(d);
  });
  _foldTouched = null;
  _syncPicRow();
}
function openPendRef2(e, enc) { return openPendRef(e, decodeURIComponent(enc)); }
function openPendRef(e, p) {
  const name = String(p).split('/').pop() || p;
  const isImg = /\.(jpg|jpeg|png|gif|webp|bmp|heic|svg)$/i.test(name);
  const pv = '/api/nas/preview?path=' + encodeURIComponent(p);
  try { viewPic(pv, name, isImg ? 'image/' : '', p, false); } catch (err) { return true; }
  const box = document.getElementById('picView');
  if (!box || !box.style.display || box.style.display === 'none') return true;
  if (e && e.preventDefault) e.preventDefault();
  return false;
}
function nasIconExt(name) {
  const n = String(name || '').toLowerCase();
  if (/\.(mp4|mov|avi|mkv|webm|3gp)$/.test(n)) return '\ud83c\udfac';
  if (/\.(mp3|m4a|wav|aac|flac)$/.test(n)) return '\ud83c\udfb5';
  if (/\.pdf$/.test(n)) return '\ud83d\udcd5';
  if (/\.(doc|docx|xls|xlsx|ppt|pptx)$/.test(n)) return '\ud83d\udcd8';
  if (/\.(zip|rar|7z|gz)$/.test(n)) return '\ud83d\udcdc';
  if (/\.(txt|md|csv|log|json)$/.test(n)) return '\ud83d\udcdd';
  return '\ud83d\udcc4';
}
function copyPendRef(p, el) {
  const name = (String(p).split('/').pop() || p);
  copyText(name + '\n' + p).then(() => toast('已复制：' + name), () => fallbackCopy(name + '\n' + p));
}
function nasConfirm() {
  const paths = Object.keys(nasState.sel);
  if (!paths.length) { toast('先点文件勾上再挂'); return; }
  closeNasPicker();
  if (!editId) {                                   // 还没保存的草稿：先排队 + 立刻显示出来
    paths.forEach(p => { if (pendingRefs.indexOf(p) < 0) pendingRefs.push(p); });
    renderPendingRefs();
    toast('已选 ' + paths.length + ' 个 NAS 文件（点「完成」保存时挂上）');
    return;
  }
  api('/api/items/' + editId + '/refs', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ paths }) })
    .then(r => {
      toast(r.added.length ? ('挂上 ' + r.added.length + ' 个' + (r.skipped.length ? ('，' + r.skipped.length + ' 个跳过') : '')) : ('没挂上：' + ((r.skipped[0] || {}).error || '未知')));
      return api('/api/items/' + editId);
    }).then(res => { _attachSig = ''; _renderAttach(res.item); }).catch(e => toast('挂不上：' + e.message));
}
function flushRefs(itemId) {
  const paths = pendingRefs.slice();
  pendingRefs = [];
  if (!paths.length) return Promise.resolve({ added: [], skipped: [] });
  return api('/api/items/' + itemId + '/refs', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ paths }) })
    .catch(() => ({ added: [], skipped: paths.map(p => ({ path: p, error: '没挂上' })) }));   // 引用失败绝不拖累保存
}

/* v7.5：txt / md / csv / 日志 读进应用里显示（一定有关闭按钮，不再是浏览器整页） */
function viewText(url, name, path) {
  const box = document.getElementById('picView');
  if (!box) { location.href = url; return; }
  fetch(url, { cache: 'no-store' }).then(r => r.text()).then(t => {
    [].slice.call(box.querySelectorAll('img,video,audio,iframe,.doc-view,.txt-view')).forEach(function (n) {
      try { if (n.pause) n.pause(); } catch (e) { }
      n.remove();
    });
    const el = document.createElement('div');
    el.className = 'txt-view';
    const head = document.createElement('div');
    head.className = 'tv-hd';
    head.textContent = (name || '文本') + (path ? ('　·　' + path) : '');
    const pre = document.createElement('pre');
    pre.textContent = t.length > 200000 ? t.slice(0, 200000) + '\n…（太长，只到这里，用 ⋯ 里的下载看全文）' : t;
    el.appendChild(head); el.appendChild(pre);
    box.insertBefore(el, box.firstChild);
    box.style.display = 'flex';
    try { if (!window.__picPush) { history.pushState({ pic: 1 }, ''); window.__picPush = 1; } } catch (e) { }
  }).catch(() => { try { viewPic(url + (url.indexOf('?') < 0 ? '?' : '&') + 'dl=1', name, 'doc', path, false); } catch (e) { toast('这个文件读不出来'); } });
}

/* ═══════════════ v8.2①②：布局可拖，宽度/字号存服务器（手机固定）═══════════════ */
var LAYOUT_DEF = { cal: 452, mid: 452, fsHead: 14 };
var COL_MIN = 240;
var uiPrefs = null;
function cssVar(name, px) {
  try { document.documentElement.style.setProperty(name, px + 'px'); } catch (e) { }
}
function prefSave(part) {
  try { localStorage.setItem('calnote-prefs', JSON.stringify(Object.assign(readPrefsCache(), part))); } catch (e) { }
  var xhr = new XMLHttpRequest();
  xhr.open('POST', '/api/ui/save', true);
  xhr.setRequestHeader('Content-Type', 'application/json');
  xhr.onerror = function () { try { toast('这个数只在这台设备上记住了'); } catch (e) { } };
  try { xhr.send(JSON.stringify({ cols: part })); } catch (e) { }
}
function readPrefsCache() { try { return JSON.parse(localStorage.getItem('calnote-prefs') || '{}'); } catch (e) { return {}; } }
function prefApply(p) {
  if (!p) return;
  if (isWide()) {
    if (p.cal) cssVar('--w-cal', p.cal);
    if (p.mid) cssVar('--w-mid', p.mid);
  }
  if (p.fsHead) cssVar('--dh-fs', p.fsHead);
}
function prefNow(which) {
  try { return parseInt(getComputedStyle(document.documentElement).getPropertyValue(which), 10) || 0; } catch (e) { return 0; }
}
function makeGrip(targetVar, cls, label) {
  var g = document.createElement('div');
  g.className = 'col-grip ' + cls;
  g.title = label;
  var anchor = document.querySelector('.maincol');
  if (!anchor) return null;
  if (cls === 'grip-cal') anchor.parentNode.insertBefore(g, anchor);
  else {
    var cv = document.querySelector('.cat-view') || document.querySelector('.edit-mask');
    (cv || anchor).parentNode.insertBefore(g, cv || anchor);
  }
  var x0 = 0, w0 = 0, moved = false;
  function down(e) {
    if (!isWide()) return;
    var p = e.touches ? e.touches[0] : e;
    x0 = p.clientX; moved = false;
    w0 = prefNow('--w-' + (cls === 'grip-cal' ? 'cal' : 'mid')) || LAYOUT_DEF[cls === 'grip-cal' ? 'cal' : 'mid'];
    g.classList.add('dragging');
    try { if (e.cancelable) e.preventDefault(); } catch (err) { }
    document.addEventListener('mousemove', move, true);
    document.addEventListener('mouseup', up, true);
    document.addEventListener('touchmove', move, { passive: false, capture: true });
    document.addEventListener('touchend', up, true);
  }
  function move(e) {
    var p = e.touches ? e.touches[0] : e;
    var d = p.clientX - x0;
    if (Math.abs(d) > 2) moved = true;
    var key = cls === 'grip-cal' ? 'cal' : 'mid';
    var v = Math.round(w0 + (key === 'cal' ? d : d));
    var max = Math.floor(window.innerWidth * (key === 'cal' ? 0.46 : 0.52));
    v = Math.max(COL_MIN, Math.min(v, Math.max(COL_MIN + 40, max)));
    cssVar('--w-' + key, v);
    if (e.cancelable) { try { e.preventDefault(); } catch (err) { } }
  }
  function up() {
    g.classList.remove('dragging');
    document.removeEventListener('mousemove', move, true);
    document.removeEventListener('mouseup', up, true);
    document.removeEventListener('touchmove', move, true);
    document.removeEventListener('touchend', up, true);
    if (!moved) return;
    var key = cls === 'grip-cal' ? 'cal' : 'mid';
    var part = {}; part[key] = prefNow('--w-' + key);
    prefSave(part);
    try { toast((key === 'cal' ? '日历列' : '侧栏列') + ' ' + part[key] + 'px，已存服务器'); } catch (e) { }
  }
  g.addEventListener('mousedown', down);
  g.addEventListener('touchstart', down, { passive: true });
  g.addEventListener('dblclick', function () {
    var key = cls === 'grip-cal' ? 'cal' : 'mid';
    cssVar('--w-' + key, LAYOUT_DEF[key]);
    var part = {}; part[key] = LAYOUT_DEF[key];
    prefSave(part);
    try { toast((key === 'cal' ? '日历列' : '侧栏列') + '已回到 ' + LAYOUT_DEF[key] + 'px'); } catch (e) { }
  });
  return g;
}
function initHeadFontDrag() {
  var t = document.querySelector('.day-head-card .dh-date') || document.querySelector('.day-section-title');
  if (!t || t._fsBound) return;
  t._fsBound = 1;
  t.title = '按住左右拖＝改这行字号，双击＝回 14px';
  var y0 = 0, f0 = 0, moved = false;
  function down(e) {
    var p = e.touches ? e.touches[0] : e;
    y0 = p.clientX; moved = false;
    f0 = prefNow('--dh-fs') || LAYOUT_DEF.fsHead;
    document.addEventListener('mousemove', move, true);
    document.addEventListener('mouseup', up, true);
    document.addEventListener('touchmove', move, { passive: true, capture: true });
    document.addEventListener('touchend', up, true);
  }
  function move(e) {
    var p = e.touches ? e.touches[0] : e;
    var d = Math.round((p.clientX - y0) / 10);
    if (Math.abs(p.clientX - y0) > 4) moved = true;
    cssVar('--dh-fs', Math.max(11, Math.min(32, f0 + d)));
  }
  function up(e) {
    document.removeEventListener('mousemove', move, true);
    document.removeEventListener('mouseup', up, true);
    document.removeEventListener('touchmove', move, true);
    document.removeEventListener('touchend', up, true);
    if (!moved) return;
    var part = { fsHead: prefNow('--dh-fs') };
    prefSave(part);
    try { toast('这行字号 ' + part.fsHead + 'px，已存服务器'); } catch (err) { }
  }
  t.addEventListener('mousedown', down);
  t.addEventListener('touchstart', down, { passive: true });
  t.addEventListener('dblclick', function () {
    cssVar('--dh-fs', LAYOUT_DEF.fsHead);
    prefSave({ fsHead: LAYOUT_DEF.fsHead });
  });
}
function resetLayout() {
  cssVar('--w-cal', LAYOUT_DEF.cal); cssVar('--w-mid', LAYOUT_DEF.mid); cssVar('--dh-fs', LAYOUT_DEF.fsHead);
  prefSave({ cal: LAYOUT_DEF.cal, mid: LAYOUT_DEF.mid, fsHead: LAYOUT_DEF.fsHead });
  try { toast('已恢复默认：日历列 452 · 侧栏列 452 · 标题 14px'); } catch (e) { }
}
function initLayoutUI() {
  // ④ 手机端不给拖：非网页版直接收掉手柄和小胶囊，宽度也打回默认
  if (!isWide()) {
    dropGrips();
    var pill0 = document.getElementById('layoutPill');
    if (pill0) pill0.style.display = 'none';
    cssVar('--w-cal', LAYOUT_DEF.cal); cssVar('--w-mid', LAYOUT_DEF.mid);
    return;
  }
  if (!window._gripCal) window._gripCal = makeGrip('--w-cal', 'grip-cal', '按住左右拖＝改日历列宽，双击＝回 452');
  if (!window._gripMid) window._gripMid = makeGrip('--w-mid', 'grip-mid', '按住左右拖＝改侧栏列宽，双击＝回 452');
  var pill = document.getElementById('layoutPill');
  if (!pill) {
    pill = document.createElement('button');
    pill.id = 'layoutPill'; pill.type = 'button';
    pill.textContent = '↔ 布局';
    pill.title = '点＝复位列宽/字号（只影响网页版，手机端本来就没有这两条拖线）';
    pill.onclick = function (e) {
      if (e) e.stopPropagation();
      resetLayout();
      pill.classList.add('flash');
      setTimeout(function () { pill.classList.remove('flash'); }, 1200);
    };
    document.body.appendChild(pill);
    clearTimeout(window.__pillT);
    window.__pillT = setTimeout(function () { pill.classList.add('dim'); }, 4000);   // 4 秒后淡成小圆点，不挡眼
    pill.addEventListener('mouseenter', function () { pill.classList.remove('dim'); });
  }
  initHeadFontDrag();
}
function dropGrips() {
  ['_gripCal', '_gripMid'].forEach(function (k) {
    if (window[k] && window[k].parentNode) window[k].parentNode.removeChild(window[k]);
    window[k] = null;
  });
}
(function markCatOpen() {
  ['openCatView', 'openCatViewFolder'].forEach(function (nm) {
    var orig = window[nm];
    if (!orig || orig._wrapped) return;
    var w = function () {
      var r = orig.apply(this, arguments);
      try { document.body.classList.add('cal-popened'); } catch (e) { }
      setTimeout(initLayoutUI, 60);
      return r;
    };
    w._wrapped = 1; window[nm] = w;
  });
  if (window.closeCatView && !window.closeCatView._wrapped) {
    var oc = window.closeCatView;
    var cw = function () {
      try { document.body.classList.remove('cal-popened'); } catch (e) { }
      return oc.apply(this, arguments);
    };
    cw._wrapped = 1; window.closeCatView = cw;
  }
})();
(function bootLayout() {
  prefApply(readPrefsCache());                       // 先按本机缓存，页面一开就是对的
  fetch('/api/ui', { cache: 'no-store' }).then(function (r) { return r.json(); }).then(function (j) {
    uiPrefs = (j && j.cols) || null;
    if (uiPrefs) { prefApply(uiPrefs); try { localStorage.setItem('calnote-prefs', JSON.stringify(uiPrefs)); } catch (e) { } }
  }).catch(function (e) { });
  document.addEventListener('DOMContentLoaded', function () { setTimeout(initLayoutUI, 150); });
  setTimeout(initLayoutUI, 300);
  window.addEventListener('orientationchange', function () { setTimeout(initLayoutUI, 400); });
  window.addEventListener('resize', function () { clearTimeout(window.__rzT); window.__rzT = setTimeout(initLayoutUI, 400); });
  window.addEventListener('resize', function () {
    if (!isWide()) { cssVar('--w-cal', LAYOUT_DEF.cal); cssVar('--w-mid', LAYOUT_DEF.mid); }
    else prefApply(uiPrefs || readPrefsCache());
  });
})();

/* ================= v9 多用户：身份显示与成员管理 ================= */
var _me = null;
function initAuthUI() {
  fetch('/api/me', { cache: 'no-store' }).then(function (r) { return r.status === 200 ? r.json() : null; })
    .then(function (j) {
      var u = j && j.user; if (!u) return; _me = u;
      if (u.single) return;                              // 没开账号体系：界面保持原样
      var who = document.getElementById('btnWho');
      if (who) { who.style.display = ''; who.textContent = (u.display_name || u.username).slice(0, 4);
                 who.onclick = openMembers; who.title = (u.display_name || u.username) +
                   (u.role === 'admin' ? '（管理员）' : '') + ' · 点此管理成员'; }
      var sec = document.getElementById('secAccount'); if (sec) sec.style.display = '';
      if (u.starter) toast('这个账号还在用初始口令，建议现在改掉');
    }).catch(function () { });
}
function memErr(msg) {
  var el = document.getElementById('memResult');
  if (el) { el.className = 'import-result err'; el.textContent = msg; }
}
function openMembers() {
  document.getElementById('membersMask').style.display = 'flex';
  var el = document.getElementById('memResult'); if (el) { el.textContent = ''; el.className = 'import-result'; }
  closeDrawer();
  api('/api/members').then(function (j) {
    var list = j.members || [], me = j.me, admin = !!j.admin;
    var btn = document.getElementById('memAddBtn'); if (btn) btn.style.display = admin ? '' : 'none';
    var add = document.getElementById('memAddBox'); if (add) add.style.display = 'none';
    var ROLE = { admin: '管理员', member: '成员' };
    document.getElementById('memList').innerHTML = list.map(function (m) {
      var tag = m.id === me ? '（我）' : '';
      var warn = m.starter ? ' <span class="warn">初始口令未改</span>' : '';
      var off = m.active ? '' : ' <span class="warn">已停用</span>';
      var ops = admin && m.id !== me
        ? '<button class="op" title="重置他的口令" onclick="adminResetPass(' + m.id + ',\'' + esc(m.username) + '\')">改口令</button>' +
          '<button class="op" title="' + (m.active ? '停用（数据保留，可再启用）' : '重新启用') + '" onclick="toggleMember(' + m.id + ',' + (m.active ? 0 : 1) + ')">' + (m.active ? '停用' : '启用') + '</button>'
        : '';
      return '<div class="cat-item"><span class="cat-dot" style="background:' +
        (m.role === 'admin' ? '#FFE1C4' : '#DCE7FF') + '"></span><span class="cat-name">' +
        esc(m.display_name) + tag + ' · ' + esc(m.username) +
        ' <small>' + (ROLE[m.role] || m.role) + '</small>' + warn + off + '</span>' +
        '<span class="cat-count">' + (m.items || 0) + ' 条</span>' + ops + '</div>';
    }).join('');
  }).catch(function (e) { memErr('读成员列表失败：' + e.message); });
}
function closeMembers() { document.getElementById('membersMask').style.display = 'none'; }
function addMember() {
  var username = (document.getElementById('mName').value || '').trim();
  var display_name = (document.getElementById('mDisp').value || '').trim();
  var password = (document.getElementById('mPass').value || '').trim();
  if (!username || password.length < 6) { memErr('账号名别空着，口令至少 6 位'); return; }
  api('/api/members', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: username, display_name: display_name, password: password }) })
    .then(function () { memResult('已添加 ' + username + '，把口令发给他，让他登录后自己改'); openMembers(); })
    .catch(function (e) { memErr(e.message); });
}
function memResult(msg) {
  var el = document.getElementById('memResult');
  if (el) { el.className = 'import-result'; el.textContent = msg; }
}
function adminResetPass(uid, name) {
  var p = prompt('给 ' + name + ' 设一个新口令（至少 6 位，他会用这个登录）：');
  if (p === null) return;
  if (p.trim().length < 6) { memErr('口令至少 6 位'); return; }
  api('/api/members/' + uid + '/password', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ password: p.trim() }) })
    .then(function () { memResult(name + ' 的口令已改，他下次要用新口令登录'); openMembers(); })
    .catch(function (e) { memErr(e.message); });
}
function toggleMember(uid, on) {
  api('/api/members/' + uid, { method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ active: !!on }
    ) }).then(function () { openMembers(); }).catch(function (e) { memErr(e.message); });
}
function changeMyPass() {
  var oldp = prompt('当前口令：'); if (oldp === null) return;
  var newp = prompt('新口令（至少 6 位，改完会用新口令重新登录一次）：'); if (newp === null) return;
  if (newp.trim().length < 6) { memErr('新口令至少 6 位'); return; }
  api('/api/members/' + (_me ? _me.id : 1) + '/password', { method: 'POST',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ old: oldp, password: newp.trim() }) })
    .then(function () { toast('口令已改，请用新口令重新登录'); setTimeout(function () { location.href = '/login'; }, 900); })
    .catch(function (e) { memErr(e.message); });
}
initAuthUI();

/* v9.5①：抽屉里加一行「🗑 垃圾桶」（用包一层的方式注入，不改动原函数文本；带条数） */
(function () {
  if (typeof renderCatList !== 'function') return;
  var orig = renderCatList;
  window.renderCatList = function () {
    var r = orig.apply(this, arguments);
    try {
      var list = document.getElementById('catList');
      if (list && !list.querySelector('.trash-item')) {
        var row = document.createElement('div');
        row.className = 'cat-item trash-item';
        row.innerHTML = '<span class="cat-dot" style="background:#EEF1F5"></span>' +
          '<span class="cat-name">🗑 垃圾桶</span><span class="cat-count"></span><span class="cat-arrow">›</span>';
        row.onclick = function (e) { if (e) e.stopPropagation(); openTrash(); };
        list.appendChild(row);
        api('/api/trash').then(function (j) {
          var n = row.querySelector('.cat-count');
          if (n) n.textContent = (j.count ? j.count + ' 项' : '空');
        }).catch(function () { });
      }
    } catch (e) { }
    return r;
  };
})();
