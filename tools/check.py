#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""仓库自检：语法 + 关键接口冒烟。
用法：python3 tools/check.py      （改完代码先跑一遍，再部署）
退出码 0 = 全部通过；非 0 = 有问题，脚本会打印哪一项失败。"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import io
import http.cookiejar
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAIL = []


def ok(msg):
    print('  \033[92m✓\033[0m %s' % msg)


def bad(msg):
    print('  \033[91m✗\033[0m %s' % msg)
    FAIL.append(msg)


def py_compile_all():
    import py_compile
    for f in ('app.py', 'ledger.py', 'vault.py'):
        p = os.path.join(ROOT, f)
        if not os.path.exists(p):
            bad('缺文件 ' + f)
            continue
        try:
            py_compile.compile(p, doraise=True)
            ok('py 编译：' + f)
        except Exception as e:
            bad('py 编译失败 %s：%s' % (f, e))


def js_check():
    """node 在就用 node --check，不在就用括号/引号粗略检查（不装 node 也能跑）。"""
    node = shutil.which('node')
    files = [os.path.join(ROOT, 'static', f) for f in
             ('app.js', 'ledger.js', 'vault.js', 'sw.js') if os.path.exists(os.path.join(ROOT, 'static', f))]
    for p in files:
        if node:
            r = subprocess.run([node, '--check', p], capture_output=True, text=True)
            (ok if r.returncode == 0 else bad)('js 语法：%s%s' % (os.path.basename(p),
                                                              ('  ' + r.stderr.strip().splitlines()[0]) if r.returncode else ''))
        else:
            s = io.open(p, encoding='utf-8').read()
            bal = s.count('{') - s.count('}')
            (ok if abs(bal) <= 2 else bad)('js 括号平衡（无 node，粗略）：%s 差 %d' % (os.path.basename(p), bal))


def api_smoke():
    """起一个临时实例（临时数据库 + 临时附件目录），打几个关键接口。"""
    tmp = tempfile.mkdtemp(prefix='calnote-check-')
    db = os.path.join(tmp, 'data', 'calnote.db')
    os.makedirs(os.path.dirname(db))
    import socket
    sk = socket.socket(); sk.bind(('127.0.0.1', 0)); port = str(sk.getsockname()[1]); sk.close()
    env = dict(os.environ, DB_PATH=db, PORT=port, AUTH_USER='admin', AUTH_PASS='check1234',
               WEATHER_CITY='兴安县', WEATHER_LAT='25.6', WEATHER_LON='110.6')
    logf = open(os.path.join(tmp, 'srv.log'), 'w+')
    p = subprocess.Popen([sys.executable, os.path.join(ROOT, 'app.py')], cwd=ROOT, env=env,
                         stdout=logf, stderr=logf)
    try:
        base = 'http://127.0.0.1:%s' % port
        deadline = time.time() + 30
        up = False
        while time.time() < deadline:               # 只要端口有 HTTP 反应就算起来了（不必 200）
            try:
                urllib.request.urlopen(base + '/api/health', timeout=3).read()
                up = True; break
            except urllib.error.HTTPError:
                up = True; break
            except Exception:
                time.sleep(0.5)
        if not up:
            bad('自检服务没起来，日志尾：\n' + io.open(os.path.join(tmp, 'srv.log'), encoding='utf-8', errors='ignore').read()[-400:])
            return

        jar = http.cookiejar.CookieJar()
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

        def call(path, obj=None, method=None):
            d = json.dumps(obj).encode() if obj is not None else None
            r = urllib.request.Request(base + path, data=d, method=method or ('POST' if d else 'GET'),
                                      headers={'Content-Type': 'application/json'} if d else {})
            try:
                res = op.open(r, timeout=20)
                t = res.read().decode()
                try:
                    return res.status, json.loads(t)
                except ValueError:
                    return res.status, t
            except urllib.error.HTTPError as e:
                return e.code, e.read().decode()[:120]

        try:
            op.open(urllib.request.Request(base + '/login',
                      data=urllib.parse.urlencode({'user': 'admin', 'pass': 'check1234', 'next': '/'}).encode(),
                      method='POST'), timeout=15)
        except Exception as e:
            bad('登录失败：%s' % e)
        (ok if call('/api/health')[0] == 200 else bad)('GET /api/health')
        (ok if call('/')[0] == 200 else bad)('GET / （首页 HTML）')
        st, j = call('/api/items', {'type': 'note', 'title': '自检笔记', 'date': time.strftime('%Y-%m-%d'),
                                    'content': 'x'})
        if 200 <= st < 300 and (j.get('item') or {}).get('id'):
            ok('POST /api/items 建记录')
            iid = j['item']['id']
        else:
            bad('POST /api/items 失败：%s' % str(j)[:120])
            return
        st, d = call('/api/items?date=' + time.strftime('%Y-%m-%d'))
        if 200 <= st < 300 and any(x['id'] == iid for x in d.get('items', [])):
            ok('GET /api/items 读得到')
        else:
            bad('GET /api/items 读不到刚建的记录')
        st, j2 = call('/api/items/%d' % iid, method='DELETE')
        st3, d3 = call('/api/items?date=' + time.strftime('%Y-%m-%d'))
        gone = all(x['id'] != iid for x in d3.get('items', []))
        (ok if j2.get('trashed') and gone else bad)('DELETE → 进垃圾桶（列表不再显示）%s' % ('' if gone else '，但列表里还在！'))
        st, t = call('/api/trash')
        (ok if t.get('count') and any(x['id'] == iid for x in t.get('items', [])) else bad)('GET /api/trash 列得出被删记录')
        st, r = call('/api/trash/restore', {'ids': [iid]})
        st4, d4 = call('/api/items?date=' + time.strftime('%Y-%m-%d'))
        (ok if r.get('restored') and any(x['id'] == iid for x in d4.get('items', [])) else bad)('恢复后回到日历')
        call('/api/items/%d' % iid, method='DELETE')
        st, p2 = call('/api/trash/purge', {'ids': [iid]})
        st5, d5 = call('/api/items?date=' + time.strftime('%Y-%m-%d'))
        (ok if p2.get('purged') and all(x['id'] != iid for x in d5.get('items', [])) else bad)('彻底删除生效')
        st, s1 = call('/api/ledger/setup')
        (ok if st == 200 and s1.get('book') else bad)('GET /api/ledger/setup（记账模块）')
        st, e1 = call('/api/ledger/entries', {'book': s1.get('book'), 'dir': 'out', 'amount': '12.3',
                                              'category': '自检类', 'account': (s1.get('accounts') or [{}])[0].get('id'),
                                              'day': time.strftime('%Y-%m-%d')})
        (ok if e1.get('entry') else bad)('POST /api/ledger/entries 记一笔 %s' % str(e1)[:60])
        st, cat = call('/api/ledger/categories', {'book': s1.get('book'), 'name': '自检分类', 'kind': 'expense', 'icon': '🧪'})
        (ok if st == 201 else bad)('POST /api/ledger/categories 加分类')
        cid = (cat.get('category') or {}).get('id')
        if cid:
            st, up = call('/api/ledger/categories/%d' % cid, {'name': '自检分类2', 'icon': '🧪'}, 'PUT')
            (ok if st == 200 else bad)('PUT /api/ledger/categories 改名/换图标')
            st, dl = call('/api/ledger/categories/%d?force=1' % cid, method='DELETE')
            (ok if st == 200 else bad)('DELETE 分类（有流水也能删）')
        st, tb = call('/api/ledger/trash')
        (ok if st == 200 else bad)('GET /api/ledger/trash 记账垃圾桶')
        st, sm = call('/api/ledger/summary?month=' + time.strftime('%Y-%m-%d')[:7])
        (ok if st == 200 and 'expense' in sm else bad)('GET /api/ledger/summary 汇总')
        st, ex = call('/api/ledger/export?book=%s' % s1.get('book'))
        (ok if st == 200 else bad)('GET /api/ledger/export 导出')
        st, pf = call('/api/ui')
        (ok if st == 200 else bad)('GET /api/ui 界面偏好')
        st, sv = call('/api/ui/save', {'cols': {'cal': 500, 'mid': 460, 'fsHead': 30, 'x': 9}}, 'POST')
        (ok if sv.get('cols') and sv['cols'].get('cal') == 500 and 'x' not in sv['cols'] else bad)('POST /api/ui/save 白名单存回读')
    finally:
        p.terminate()
        time.sleep(1)
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    print('\n日历便签仓库自检')
    print('─' * 46)
    print('[1/3] Python 语法')
    py_compile_all()
    print('[2/3] 前端 JS')
    js_check()
    print('[3/3] 接口冒烟（临时库，不碰你的数据）')
    api_smoke()
    print('─' * 46)
    if FAIL:
        print('\033[91m%d 项未通过\033[0m' % len(FAIL))
        for x in FAIL:
            print('   - ' + x)
        return 1
    print('\033[92m全部通过\033[0m')
    return 0


if __name__ == '__main__':
    sys.exit(main())
