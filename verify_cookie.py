"""
独立验证 LINUXDO_COOKIES 是否有效——仅用 curl_cffi 走 API，不启动浏览器。

用途：把「Cookie 本身是否过期」与「无头浏览器是否被 Cloudflare 拦截」彻底分开，
便于在 main.py 报 "Cookie 可能已过期" 时快速定位到底是哪一类问题。

用法：
    # 方式一：读环境变量
    #   PowerShell:  $env:LINUXDO_COOKIES="_t=xxx; _forum_session=yyy"; python verify_cookie.py
    #   bash:        LINUXDO_COOKIES="_t=xxx; _forum_session=yyy" python verify_cookie.py
    # 方式二：作为命令行参数
    #   python verify_cookie.py "_t=xxx; _forum_session=yyy"

退出码：0=有效  1=未提供 Cookie  2=请求异常  3=被 Cloudflare 拦截  4=Cookie 无效/过期
"""

import os
import sys

from curl_cffi import requests

HOME_URL = "https://linux.do/"
CURRENT_USER_URL = "https://linux.do/session/current.json"
CF_MARKERS = ["Just a moment", "Checking your browser", "cf-chl", "challenge-platform"]


def parse_cookie_string(cookie_str):
    pairs = []
    for part in cookie_str.strip().split(";"):
        part = part.strip()
        if "=" in part:
            name, _, value = part.partition("=")
            pairs.append((name.strip(), value.strip()))
    return pairs


def main():
    cookie_str = (
        sys.argv[1] if len(sys.argv) > 1 else os.environ.get("LINUXDO_COOKIES", "")
    ).strip()
    if not cookie_str:
        print("未提供 Cookie：请设置 LINUXDO_COOKIES 环境变量，或作为命令行参数传入")
        sys.exit(1)

    pairs = parse_cookie_string(cookie_str)
    print(f"解析到 {len(pairs)} 个 Cookie: {[n for n, _ in pairs]}")
    if not any(n == "_t" for n, _ in pairs):
        print("⚠️  未发现 _t：Discourse 的持久登录令牌是 _t，缺了它基本无法登录")

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
    )
    for name, value in pairs:
        session.cookies.set(name, value, domain=".linux.do")

    # 若本机需要代理才能访问 linux.do（如科学上网），从环境变量读取代理
    proxy = (
        os.environ.get("LINUXDO_PROXY")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("https_proxy")
        or os.environ.get("ALL_PROXY")
        or os.environ.get("all_proxy")
    )
    request_kwargs = dict(
        impersonate="chrome136",
        headers={"Accept": "application/json", "Referer": HOME_URL},
        timeout=30,
    )
    if proxy:
        print(f"使用代理: {proxy}")
        request_kwargs["proxies"] = {"https": proxy, "http": proxy}

    try:
        resp = session.get(CURRENT_USER_URL, **request_kwargs)
    except Exception as e:
        print(f"请求异常（网络/代理/Cloudflare 问题）：{e}")
        print("  → 若本机需科学上网/代理才能访问 linux.do，请设置 LINUXDO_PROXY 后重试，例如：")
        print("     LINUXDO_PROXY=http://127.0.0.1:7890 python linuxdo-checkin/verify_cookie.py \"_t=...\"")
        sys.exit(2)

    print(f"HTTP 状态码: {resp.status_code}")
    body = resp.text or ""
    if any(mk in body for mk in CF_MARKERS):
        print("❌ 被 Cloudflare 拦截 —— 不是 Cookie 的问题，是 TLS/IP 指纹问题")
        sys.exit(3)

    if resp.status_code == 200:
        try:
            data = resp.json()
        except Exception:
            data = {}
        current_user = (data or {}).get("current_user") or {}
        if current_user.get("username"):
            print(f"✅ Cookie 有效，已登录为: {current_user['username']}")
            sys.exit(0)

    print("❌ Cookie 无效或已过期（未返回 current_user）")
    print("   → 请重新从浏览器 F12 → Application → Cookies 复制最新的 _t")
    print("返回片段:", body[:300].replace("\n", " "))
    sys.exit(4)


if __name__ == "__main__":
    main()
