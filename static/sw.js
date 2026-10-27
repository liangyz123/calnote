/* ==================== 日历便签 Service Worker（PWA） ====================
 * v2 策略：网络优先 + 自动清理旧缓存
 * - 每次打开页面都先问服务器要最新文件，服务器连不上才用缓存（离线兜底）
 * - 安装新版时自动把旧版缓存的页面全部删掉，不会再出现"更新了还显示旧界面"
 * ======================================================================= */
const CACHE = 'calnote-v10.5';

self.addEventListener('install', (e) => {
  e.waitUntil(
    // 先把所有旧缓存删干净（包括 v1 留下的旧页面快照）
    caches.keys()
      .then((keys) => Promise.all(keys.map((k) => caches.delete(k))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  // 接口请求不走缓存，永远拿最新数据
  if (url.pathname.startsWith('/api/')) return;
  // 静态资源：先问网络（最新），失败才用缓存（离线兜底）
  e.respondWith(
    fetch(e.request).then((res) => {
      const clone = res.clone();
      caches.open(CACHE).then((c) => c.put(e.request, clone));
      return res;
    }).catch(() =>
      caches.match(e.request).then((hit) => hit || caches.match('./index.html'))
    )
  );
});
