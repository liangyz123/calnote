#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""日历便签桌面入口用的跳转服务（只依赖 Python 标准库）。

它不访问你的任何数据，也不是便签本身：便签仍然跑在你原来的 Docker 容器里
（默认 http://NAS:8000）。这里只做一件事——把从飞牛桌面图标点开的请求，
转发到便签的实际地址，让你像用原生应用一样用便签。

地址配置：${TRIM_PKGETC}/portal.conf，一行一个 KEY=VALUE
  TARGET_PORT=8000      便签在 NAS 上的端口（Docker 映射出来的那个前端端口）
  TARGET_HOST=          留空=跟着浏览器地址栏的主机名走（内网/远程都能用）
                        填了就用固定的，比如 <你的飞牛外网域名>
  TARGET_SCHEME=http    https 就写 https
  PORT=8010             本跳转服务自己的端口（应用中心图标就开这个）
"""
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEF = {"TARGET_PORT": "8000", "TARGET_HOST": "", "TARGET_SCHEME": "http", "PORT": "8010"}


def conf():
    """读配置：文件不存在/写坏了就用默认值，绝不因为配置问题打不开。"""
    out = dict(DEF)
    path = os.path.join(os.environ.get("TRIM_PKGETC", os.path.expanduser("~")), "portal.conf")
    try:
        with io_open(path, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln or ln.startswith("#") or "=" not in ln:
                    continue
                k, v = ln.split("=", 1)
                k = k.strip().upper()
                if k in DEF:
                    out[k] = v.strip()
    except OSError:
        pass
    return out


def io_open(p, encoding=None):
    return open(p, "r", encoding=encoding or "utf-8")


def target_url(c, host_header, path):
    """算出该跳到便签哪个地址：c=配置，host_header=浏览器地址栏的 Host，path=原样带过去。
    留空 TARGET_HOST 时跟着浏览器主机名走 —— 内网 IP 和飞牛远程域名都能自适应。"""
    host = (c.get("TARGET_HOST") or "").strip()
    if not host:
        host = ((host_header or "127.0.0.1").split(":")[0]).strip() or "127.0.0.1"
    scheme = (c.get("TARGET_SCHEME") or "http").strip()
    port = str(c.get("TARGET_PORT") or "8000").strip()
    p = path or "/"
    if scheme == "https" and port == "443":
        port = ""
    elif scheme == "http" and port == "80":
        port = ""
    return "%s://%s%s%s" % (scheme, host, (":" + port) if port else "", p)


class H(BaseHTTPRequestHandler):
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        url = target_url(conf(), self.headers.get("Host"), self.path)
        body = ("便签入口 · 正在打开 <a href=\"%s\">%s</a>" % (url, url)).encode("utf-8")
        self.send_response(302)
        self.send_header("Location", url)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def log_message(self, *a):            # 别往 stderr 灌日志，交给 cmd/main 重定向
        pass


def main():
    port = int(conf()["PORT"])
    try:
        port = int(sys.argv[1]) if len(sys.argv) > 1 else port
    except ValueError:
        pass
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()


if __name__ == "__main__":
    main()
