/* ============ v9 第 3 期：密码本（保险柜） ============
 * 主口令只在提交那一刻发给服务端校验，前端不留存、不进 localStorage、不进任何缓存。
 * 服务端只存派生后的校验值，密文里永远不含明文；密码字段不在列表里，看密码是单独一次请求。
 */
var vfState = { setup: false, unlocked: false, items: [], view: 'list', editId: 0, hideTimer: null };
/* sid 只是「这台设备的解锁凭据」，不是主口令本身；派生密钥留在服务端内存里。
   放 sessionStorage：刷新还在，关掉标签页就没了，要重新输主口令。
   不按设备分的话，手机解锁一次，电脑上的同一账号也就跟着通了。 */
function vfSid(v) {
  if (v === undefined) return sessionStorage.getItem('vf-sid') || '';
  if (v === '') sessionStorage.removeItem('vf-sid'); else sessionStorage.setItem('vf-sid', v);
  return v;
}
function vfApi(url, opts) {
  opts = opts || {};
  opts.headers = opts.headers || {};
  opts.headers['X-Vault-Sid'] = vfSid();
  return api(url, opts);
}

function vfBind() {
  var b = document.getElementById('btnVault2');
  if (b && !b._bound) { b._bound = 1; b.onclick = function () { closeDrawer(); vfOpen(); }; }
}
document.addEventListener('DOMContentLoaded', vfBind);
setTimeout(vfBind, 300);

function vfOpen() {
  var mask = document.getElementById('vaultMask');
  if (!mask) return;
  mask.style.display = 'flex';
  vfRefresh();
}
function vfClose() {
  clearTimeout(vfState.hideTimer);
  document.getElementById('vaultMask').style.display = 'none';
}
function vfTip(msg, bad) {
  var el = document.getElementById('vfResult');
  if (el) { el.className = 'import-result' + (bad ? ' err' : ''); el.textContent = msg || ''; }
}
function vfRefresh() {
  vfApi('/api/vault/status').then(function (st) {
    vfState.setup = !!st.setup; vfState.unlocked = !!st.unlocked;
    if (!st.available) { document.getElementById('vfBody').innerHTML =
      '<p class="hint">密码本需要 <code>cryptography</code> 库。包里已经写进 requirements，'
      + '重新构建镜像（<code>docker compose up -d --build</code>）后就能用。</p>'; return; }
    if (!vfState.setup) return vfRenderSetMaster();
    if (!vfState.unlocked) return vfRenderUnlock();
    return vfApi('/api/vault/items').then(function (r) {
      vfState.items = r.items || [];
      vfRenderList();
    });
  }).catch(function (e) {
    document.getElementById('vfBody').innerHTML = '<p class="hint">读不到密码本：' + esc(e.message) + '</p>';
  });
}
/* ---------- 设主口令 / 解锁 ---------- */
function vfRenderSetMaster() {
  document.getElementById('vfTitle').textContent = '设置主口令';
  document.getElementById('vfBody').innerHTML =
    '<p class="hint"><b>主口令是打开这台机器上所有密码的唯一钥匙。</b><br>'
    + '· 它<b>不写进数据库、不写进配置文件</b>，库里只存派生后的校验值<br>'
    + '· <b>忘了就是永久打不开</b>，没有任何找回通道，服务器上也解不出来<br>'
    + '· 所以请选一个你肯定记得住、又不是别处用过的；建议再抄一份纸质的收好<br>'
    + '· 备份 = <code>data/calnote.db</code> + 你的主口令，两样缺一不可</p>'
    + '<div class="field"><div class="field-label">主口令（至少 8 位）</div>'
    + '<input type="password" id="vfM1" autocomplete="new-password"></div>'
    + '<div class="field"><div class="field-label">再输一次确认</div>'
    + '<input type="password" id="vfM2" autocomplete="new-password"></div>'
    + '<button class="primary-btn" onclick="vfDoSetup()">设好并进入</button>'
    + '<div class="import-result" id="vfResult"></div>';
}
function vfDoSetup() {
  var a = document.getElementById('vfM1').value, b = document.getElementById('vfM2').value;
  if (a !== b) { vfTip('两次输的不一样', true); return; }
  api('/api/vault/setup', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ master: a }) })
    .then(function (r) { vfSid(r.sid); document.getElementById('vfM1').value = ''; document.getElementById('vfM2').value = '';
      vfTip('主口令设好了（当前已解锁，约 30 分钟后要重输）'); vfRefresh(); })
    .catch(function (e) { vfTip(e.message, true); });
}
function vfRenderUnlock() {
  document.getElementById('vfTitle').textContent = '解锁密码本';
  document.getElementById('vfBody').innerHTML =
    '<p class="hint">登录只是进了这台便签，密码本还要再输一次<b>主口令</b>。'
    + '解锁后约 ' + 30 + ' 分钟内不用再输，离开页面或超时会自动上锁。</p>'
    + '<div class="field"><div class="field-label">主口令</div>'
    + '<input type="password" id="vfMaster" autocomplete="current-password" '
    + 'onkeydown="if(event.key===\'Enter\')vfDoUnlock()"></div>'
    + '<button class="primary-btn" onclick="vfDoUnlock()">解锁</button>'
    + '<div class="import-result" id="vfResult"></div>';
  setTimeout(function () { var e = document.getElementById('vfMaster'); if (e) e.focus(); }, 60);
}
function vfDoUnlock() {
  var m = document.getElementById('vfMaster').value;
  api('/api/vault/unlock', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ master: m }) })
    .then(function (r) { vfSid(r.sid); document.getElementById('vfMaster').value = ''; vfRefresh(); })
    .catch(function (e) { vfTip(e.message, true); });
}
function vfLock() {
  vfApi('/api/vault/lock', { method: 'POST', body: '{}' }).then(function () { vfSid(''); vfRefresh(); }).catch(function (e) { vfTip(e.message, true); });
}
/* ---------- 列表 ---------- */
function vfRenderList() {
  document.getElementById('vfTitle').textContent = '密码本';
  var rows = vfState.items.map(function (it) {
    var who = it.mine ? '' : '<i class="vf-share">共享 · 来自别人</i>';
    var url = it.url ? '<a class="vf-url" href="' + esc(vfHref(it.url)) + '" target="_blank" rel="noopener noreferrer">'
      + esc(it.url) + '</a>' : '<span class="vf-none">没有网址</span>';
    if (it.corrupt) {
      return '<div class="vf-row bad"><span class="vf-name">' + esc(it.title) + '</span>'
        + '<span class="vf-note">密文解不开（可能换过主口令或数据被动过），建议删掉重记</span></div>';
    }
    return '<div class="vf-row">'
      + '<div class="vf-top"><b class="vf-name">' + (it.favorite ? '★ ' : '') + esc(it.title) + '</b>' + who
      + '<span class="vf-ops">'
      + '<button class="op" onclick="vfShow(' + it.id + ',this)">显示密码</button>'
      + '<button class="op" onclick="vfCopyUrl(' + it.id + ')">复制网址</button>'
      + (it.mine ? '<button class="op" onclick="vfEditForm(' + it.id + ')">改</button>'
        + '<button class="op del" onclick="vfDel(' + it.id + ')">删</button>' : '')
      + '</span></div>'
      + '<div class="vf-line">' + url + '</div>'
      + (it.user ? '<div class="vf-line">账号 ' + esc(it.user) + '</div>' : '')
      + '<div class="vf-pass" id="vp-' + it.id + '" style="display:none"></div>'
      + '</div>';
  }).join('');
  document.getElementById('vfBody').innerHTML =
    '<div class="vf-bar"><button class="primary-btn" onclick="vfEditForm(0)">＋ 新一条</button>'
    + '<button class="op" onclick="vfLogs()">谁看过我的</button>'
    + '<button class="op" onclick="vfMasterForm()">改主口令</button>'
    + '<button class="op" onclick="vfLock()">立即上锁</button>'
    + '<button class="op" onclick="vfRefresh()">刷新</button></div>'
    + (vfState.items.length ? rows : '<p class="hint">还没有存任何东西。点「＋ 新一条」记下第一个网站账号密码。</p>')
    + '<div class="import-result" id="vfResult"></div>';
}
function vfHref(u) {
  var s = String(u || '').trim();
  if (/^https?:\/\//i.test(s)) return s;
  if (/^[\w.-]+\.[a-z]{2,}/i.test(s)) return 'https://' + s;
  return '#';
}
function vfShow(id, btn) {
  vfApi('/api/vault/items/' + id + '/reveal', { method: 'POST', body: '{}' }).then(function (r) {
    var box = document.getElementById('vp-' + id);
    if (!box) return;
    box.style.display = '';
    box.innerHTML = '<span class="vf-pass-txt">' + esc(r.password) + '</span>'
      + '<button class="op" onclick="vfCopyText(' + id + ')">复制</button>'
      + '<span class="vf-cd" id="vc-' + id + '">30 秒后自动收起</span>';
    clearTimeout(vfState.hideTimer);
    var left = 30;
    vfState.hideTimer = setInterval(function () {
      left--;
      var cd = document.getElementById('vc-' + id);
      if (cd) cd.textContent = left + ' 秒后自动收起';
      if (left <= 0) { clearInterval(vfState.hideTimer); var b2 = document.getElementById('vp-' + id);
        if (b2) { b2.style.display = 'none'; b2.innerHTML = ''; } }
    }, 1000);
  }).catch(function (e) { vfTip(e.message, true); });
}
/* 剪贴板：https/localhost 用 navigator.clipboard，内网 http 退回 textarea+execCommand。
   浏览器不允许网页定时清剪贴板，所以只能提示，不假装能做到。 */
function vfCopy(text) {
  if (navigator.clipboard && window.isSecureContext) {
    return navigator.clipboard.writeText(text).then(function () { vfTip('已复制到剪贴板——密码在剪贴板里不会被自动清掉，用完记得清一下'); });
  }
  var ta = document.createElement('textarea');
  ta.value = text; ta.style.position = 'fixed'; ta.style.left = '-9999px';
  document.body.appendChild(ta); ta.select();
  var ok = false;
  try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
  document.body.removeChild(ta);
  if (ok) vfTip('已复制到剪贴板——用完记得清掉（浏览器不允许网页代你清）');
  else vfTip('这个浏览器不让网页复制，请手抄：' + text, true);
  return Promise.resolve();
}
function vfCopyText(id) {
  var el = document.querySelector('#vp-' + id + ' .vf-pass-txt');
  if (el) vfCopy(el.textContent);
}
function vfCopyUrl(id) {
  var it = vfState.items.filter(function (x) { return x.id === id; })[0];
  if (it) vfCopy(it.url || it.title);
}
/* ---------- 新增 / 编辑 ---------- */
function vfEditForm(id) {
  var it = id ? vfState.items.filter(function (x) { return x.id === id; })[0] : null;
  if (id && !it) return;
  vfState.editId = id || 0;
  document.getElementById('vfTitle').textContent = id ? '改这一条' : '新一条';
  document.getElementById('vfBody').innerHTML =
    '<div class="field"><div class="field-label">名字（网站或用途）</div><input id="vfTitle_" value="' + esc(it ? it.title : '') + '"></div>'
    + '<div class="field"><div class="field-label">网址</div><input id="vfUrl" value="' + esc(it ? it.url : '') + '" placeholder="baidu.com"></div>'
    + '<div class="field"><div class="field-label">账号</div><input id="vfUser" value="' + esc(it ? it.user : '') + '"></div>'
    + '<div class="field"><div class="field-label">密码' + (id ? '（留空＝不改）' : '') + '</div><input id="vfPass" type="text" autocomplete="off"></div>'
    + '<div class="field"><div class="field-label">备注（安全问题、有效期之类）</div><input id="vfNote" value=""></div>'
    + '<label class="check"><input type="checkbox" id="vfShare"' + (it && it.shared ? ' checked' : '') + '> '
    + '共享给家里其他人（<b>勾了就等于全家可见</b>：这类条目改由服务器密钥加密，不再受你的主口令保护）</label>'
    + '<label class="check"><input type="checkbox" id="vfFav"' + (it && it.favorite ? ' checked' : '') + '> 标星（排在最前）</label>'
    + '<button class="primary-btn" onclick="vfSave()">' + (id ? '保存' : '存进密码本') + '</button> '
    + '<button class="op" onclick="vfRefresh()">取消</button>'
    + '<div class="import-result" id="vfResult"></div>';
  if (!id) { var t = document.getElementById('vfTitle_'); if (t) t.focus(); }
}
function vfSave() {
  var body = {
    title: document.getElementById('vfTitle_').value.trim(),
    url: document.getElementById('vfUrl').value.trim(),
    user: document.getElementById('vfUser').value.trim(),
    note: document.getElementById('vfNote').value.trim(),
    shared: document.getElementById('vfShare').checked,
    favorite: document.getElementById('vfFav').checked
  };
  var p = document.getElementById('vfPass').value;
  if (body.shared) body.favorite = false;                 // 星标只对自己有意义
  var creating = !vfState.editId;
  if (p !== '' || creating) body.pass = p;
  if (!body.title) { vfTip('名字总得写一个', true); return; }
  var url = creating ? '/api/vault/items' : '/api/vault/items/' + vfState.editId;
  vfApi(url, { method: creating ? 'POST' : 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body) })
    .then(function () { vfTip(creating ? '存好了' : '改好了'); document.getElementById('vfPass') && (document.getElementById('vfPass').value = '');
      setTimeout(vfRefresh, 400); })
    .catch(function (e) { vfTip(e.message, true); });
}
function vfDel(id) {
  if (!confirm('删掉这一条？删了就找不回来了。')) return;
  vfApi('/api/vault/items/' + id, { method: 'DELETE' })
    .then(function () { vfRefresh(); }).catch(function (e) { vfTip(e.message, true); });
}
/* ---------- 留痕 / 改主口令 ---------- */
function vfLogs() {
  vfApi('/api/vault/logs').then(function (r) {
    document.getElementById('vfTitle').textContent = '谁看过我的共享条目';
    document.getElementById('vfBody').innerHTML = (r.logs && r.logs.length)
      ? r.logs.map(function (l) { return '<div class="vf-row"><div class="vf-line">' + esc(l.at)
          + ' · <b>' + esc(l.viewer) + '</b> 看了你共享出去的条目 #' + l.item + '</div></div>'; }).join('')
        + '<button class="op" onclick="vfRefresh()">返回</button>'
      : '<p class="hint">还没有人看过你共享的条目（只有自己的一条也说明不了什么）。'
        + '注：你看自己的不记，只记别人看你的。</p><button class="op" onclick="vfRefresh()">返回</button>';
  }).catch(function (e) { vfTip(e.message, true); });
}
function vfMasterForm() {
  document.getElementById('vfTitle').textContent = '改主口令';
  document.getElementById('vfBody').innerHTML =
    '<p class="hint">改主口令会把你的全部条目用新口令重新加密一遍，中途出错会整体中止、不动数据。'
    + '改完别忘了把新口令也记在安全的地方——它一样没有找回通道。</p>'
    + '<div class="field"><div class="field-label">当前主口令</div><input type="password" id="vmOld"></div>'
    + '<div class="field"><div class="field-label">新主口令（至少 8 位）</div><input type="password" id="vmNew"></div>'
    + '<div class="field"><div class="field-label">确认新主口令</div><input type="password" id="vmNew2"></div>'
    + '<button class="primary-btn" onclick="vfDoMaster()">改</button> '
    + '<button class="op" onclick="vfRefresh()">取消</button>'
    + '<div class="import-result" id="vfResult"></div>';
}
function vfDoMaster() {
  var o = document.getElementById('vmOld').value, n = document.getElementById('vmNew').value, n2 = document.getElementById('vmNew2').value;
  if (n !== n2) { vfTip('两次新口令不一样', true); return; }
  api('/api/vault/master', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ old: o, new: n }) })
    .then(function (r) { vfSid(r.sid); vfTip('改好了，重加密 ' + (r.re_encrypted || 0) + ' 条'); setTimeout(vfRefresh, 600); })
    .catch(function (e) { vfTip(e.message, true); });
}
