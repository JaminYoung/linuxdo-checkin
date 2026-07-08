"""
cron: 0 */6 * * *
new Env("Linux.Do 签到")
"""

import os
import re
import random
import time
import functools
from loguru import logger
from DrissionPage import ChromiumOptions, Chromium
from tabulate import tabulate
from curl_cffi import requests
from bs4 import BeautifulSoup
from notify import NotificationManager


def retry_decorator(retries=3, min_delay=5, max_delay=10):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if attempt == retries - 1:  # 最后一次尝试
                        logger.error(f"函数 {func.__name__} 最终执行失败: {str(e)}")
                    logger.warning(
                        f"函数 {func.__name__} 第 {attempt + 1}/{retries} 次尝试失败: {str(e)}"
                    )
                    if attempt < retries - 1:
                        sleep_s = random.uniform(min_delay, max_delay)
                        logger.info(
                            f"将在 {sleep_s:.2f}s 后重试 ({min_delay}-{max_delay}s 随机延迟)"
                        )
                        time.sleep(sleep_s)
            return None

        return wrapper

    return decorator


os.environ.pop("DISPLAY", None)
os.environ.pop("DYLD_LIBRARY_PATH", None)

USERNAME = os.environ.get("LINUXDO_USERNAME")
PASSWORD = os.environ.get("LINUXDO_PASSWORD")
COOKIES = os.environ.get("LINUXDO_COOKIES", "").strip()  # 手动设置的 Cookie 字符串，优先使用
BROWSE_ENABLED = os.environ.get("BROWSE_ENABLED", "true").strip().lower() not in [
    "false",
    "0",
    "off",
]
if not USERNAME:
    USERNAME = os.environ.get("USERNAME")
if not PASSWORD:
    PASSWORD = os.environ.get("PASSWORD")

HOME_URL = "https://linux.do/"
LOGIN_URL = "https://linux.do/login"
SESSION_URL = "https://linux.do/session"
CSRF_URL = "https://linux.do/session/csrf"
CURRENT_USER_URL = "https://linux.do/session/current.json"


class LinuxDoBrowser:
    def __init__(self) -> None:
        from sys import platform

        if platform == "linux" or platform == "linux2":
            platformIdentifier = "X11; Linux x86_64"
        elif platform == "darwin":
            platformIdentifier = "Macintosh; Intel Mac OS X 10_15_7"
        elif platform == "win32":
            platformIdentifier = "Windows NT 10.0; Win64; x64"
        else:
            platformIdentifier = "X11; Linux x86_64"

        co = (
            ChromiumOptions()
            .headless(True)
            .incognito(True)
            .set_argument("--no-sandbox")
        )
        co.set_user_agent(
            f"Mozilla/5.0 ({platformIdentifier}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
        )
        self.browser = Chromium(co)
        self.page = self.browser.new_tab()
        self.session = requests.Session()
        # 读取浏览器真实 UA，让 curl_cffi 会话与浏览器 UA 保持一致。
        # Cloudflare 的 cf_clearance 与 UA 绑定，一致才能在 curl_cffi/浏览器之间复用放行状态。
        try:
            browser_ua = self.page.run_js("return navigator.userAgent")
        except Exception:
            browser_ua = None
        self.user_agent = browser_ua or (
            f"Mozilla/5.0 ({platformIdentifier}) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
        )
        self.session.headers.update(
            {
                "User-Agent": self.user_agent,
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Accept-Language": "zh-CN,zh;q=0.9",
            }
        )
        # 登录后记录用户名（Cookie 登录模式下 USERNAME 可能为空）
        self.username = None
        # 初始化通知管理器
        self.notifier = NotificationManager()

    @staticmethod
    def parse_cookie_string(cookie_str: str) -> list[dict]:
        """
        解析浏览器复制的 Cookie 字符串格式: "name1=value1; name2=value2"
        返回 DrissionPage 所需的 cookie 列表格式。
        """
        cookies = []
        for part in cookie_str.strip().split(";"):
            part = part.strip()
            if "=" in part:
                name, _, value = part.partition("=")
                cookies.append(
                    {
                        "name": name.strip(),
                        "value": value.strip(),
                        "domain": ".linux.do",
                        "path": "/",
                    }
                )
        return cookies

    def login_with_cookies(self, cookie_str: str) -> bool:
        """使用手动设置的 Cookie 直接登录，跳过账号密码流程。

        两条路径任一成功即视为登录成功：
        - 浏览器路径：真实（无头）Chromium 能执行 JS，可尝试自行通过 Cloudflare 挑战；
        - API 路径：curl_cffi + impersonate 直接命中 Discourse 接口（IP 未被 Cloudflare 挑战时可用）。
        纯 HTTP 客户端无法解 Cloudflare 的 JS 挑战，故被硬拦的 IP 上只能靠浏览器路径。
        """
        logger.info("检测到手动 Cookie，尝试 Cookie 登录...")
        dp_cookies = self.parse_cookie_string(cookie_str)
        if not dp_cookies:
            logger.error("Cookie 解析失败或为空，无法使用 Cookie 登录")
            return False

        logger.info(
            f"成功解析 {len(dp_cookies)} 个 Cookie 条目: {[c['name'] for c in dp_cookies]}"
        )

        # 同步到 curl_cffi 会话；域用 .linux.do 以便 connect.linux.do 等子域也能带上 Cookie
        for ck in dp_cookies:
            self.session.cookies.set(ck["name"], ck["value"], domain=".linux.do")

        # --- 路径 A：浏览器（可执行 JS，尝试自行通过 Cloudflare 挑战）---
        browser_ok = False
        try:
            logger.info("浏览器路径：先匿名访问，让浏览器自行通过 Cloudflare...")
            self.page.get(HOME_URL)
            self._wait_cloudflare_cleared()
            logger.info("注入登录 Cookie 并刷新...")
            self.page.set.cookies(dp_cookies)
            self.page.get(HOME_URL)
            self._wait_cloudflare_cleared()
            browser_ok = self._browser_logged_in()
        except Exception as e:
            logger.warning(f"浏览器路径异常: {e}")

        # 把浏览器现场拿到的 Cookie（可能含 cf_clearance）回灌到 curl_cffi，提升 API 命中率
        self._sync_browser_cookies_to_session()

        # --- 路径 B：curl_cffi API 校验 ---
        api_ok, username = self.verify_login_via_api()

        if browser_ok or api_ok:
            self.username = username or self.username
            who = self.username or "(用户名未知)"
            logger.success(
                f"Cookie 登录成功（浏览器={'✓' if browser_ok else '✗'} / "
                f"API={'✓' if api_ok else '✗'}），已登录为: {who}"
            )
            return True

        logger.error("Cookie 登录失败：浏览器与 API 均未确认登录")
        self._dump_page_state()
        return False

    def _sync_browser_cookies_to_session(self) -> None:
        """把浏览器当前 Cookie（含 Cloudflare 放行 Cookie）同步到 curl_cffi 会话。"""
        browser_cookies = None
        try:
            browser_cookies = self.page.cookies(as_dict=True)
        except Exception:
            try:
                browser_cookies = {
                    c.get("name"): c.get("value")
                    for c in (self.page.cookies() or [])
                    if isinstance(c, dict) and c.get("name")
                }
            except Exception as e:
                logger.warning(f"读取浏览器 Cookie 失败: {e}")
                return
        try:
            for name, value in (browser_cookies or {}).items():
                if name:
                    self.session.cookies.set(name, value, domain=".linux.do")
        except Exception as e:
            logger.warning(f"同步浏览器 Cookie 到会话失败: {e}")

    def verify_login_via_api(self) -> tuple[bool, str]:
        """用 curl_cffi + impersonate 走 API 验证 Cookie 是否有效（可穿透 Cloudflare）。

        返回 (是否已登录, 用户名)。主接口 /session/current.json，失败时回退首页 HTML 解析。
        """
        # 主接口：/session/current.json → { "current_user": { "username": ... } }
        try:
            resp = self.session.get(
                CURRENT_USER_URL,
                impersonate="chrome136",
                headers={"Accept": "application/json", "Referer": HOME_URL},
            )
            logger.info(f"/session/current.json 状态码: {resp.status_code}")
            if resp.status_code == 200:
                try:
                    data = resp.json()
                except Exception:
                    data = {}
                current_user = (data or {}).get("current_user") or {}
                username = current_user.get("username")
                if username:
                    return True, username
                logger.info("/session/current.json 未包含 current_user，尝试兜底校验")
        except Exception as e:
            logger.warning(f"API 验证登录异常: {e}")

        # 兜底：取首页 HTML，检测登录痕迹（用户名 / logout 链接 / 内联 current_user）
        try:
            resp = self.session.get(
                HOME_URL,
                impersonate="chrome136",
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
                },
            )
            html = resp.text or ""
            if self._is_cloudflare_html(html):
                logger.warning("API 兜底：首页被 Cloudflare 拦截，无法据此判断登录状态")
                return False, ""
            m = re.search(
                r'"current_user"\s*:\s*\{[^{}]*?"username"\s*:\s*"([^"]+)"', html
            )
            if m:
                return True, m.group(1)
            if "/logout" in html or 'id="current-user"' in html:
                return True, ""
        except Exception as e:
            logger.warning(f"API 兜底验证异常: {e}")
        return False, ""

    @staticmethod
    def _is_cloudflare_html(html: str) -> bool:
        """判断一段 HTML 是否是 Cloudflare 人机验证页。"""
        if not html:
            return False
        markers = [
            "Just a moment",
            "Checking your browser",
            "challenge-platform",
            "cf-chl",
            "cf_chl",
            "__cf_",
            "Enable JavaScript and cookies to continue",
        ]
        return any(mk in html for mk in markers)

    def _wait_cloudflare_cleared(self, timeout: int = 20) -> bool:
        """轮询等待 Cloudflare 清场：标题不再是 "Just a moment" 且出现主内容或用户入口。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                title = self.page.title or ""
            except Exception:
                title = ""
            challenging = ("Just a moment" in title) or ("请稍候" in title)
            has_app = False
            try:
                has_app = bool(
                    self.page.ele("@id=main-outlet", timeout=0.5)
                    or self.page.ele("@id=current-user", timeout=0.5)
                )
            except Exception:
                has_app = False
            if not challenging and has_app:
                return True
            time.sleep(1)
        return False

    def _browser_logged_in(self) -> bool:
        """浏览器端登录判定：优先 #current-user，回退 avatar 关键字。"""
        try:
            if self.page.ele("@id=current-user", timeout=8):
                return True
        except Exception:
            pass
        try:
            return "avatar" in self.page.html
        except Exception:
            return False

    def _dump_page_state(self) -> None:
        """失败诊断：打印页面 title/url，并把"被 Cloudflare 拦截"与"Cookie 过期"区分开。"""
        try:
            logger.info(f"当前页面 title={self.page.title!r} url={self.page.url!r}")
        except Exception as e:
            logger.warning(f"读取页面 title/url 失败: {e}")
        try:
            if self._is_cloudflare_html(self.page.html):
                logger.error(
                    "检测到 Cloudflare 人机验证页面：是无头浏览器被拦截，并非 Cookie 过期"
                )
        except Exception as e:
            logger.warning(f"读取页面 HTML 失败: {e}")

    def login(self):
        logger.info("开始账号密码登录")
        # Step 1: Get CSRF Token
        logger.info("获取 CSRF token...")
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36 Edg/142.0.0.0",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": LOGIN_URL,
        }
        resp_csrf = self.session.get(CSRF_URL, headers=headers, impersonate="firefox135")
        if resp_csrf.status_code != 200:
            logger.error(f"获取 CSRF token 失败: {resp_csrf.status_code}")
            return False        
        csrf_data = resp_csrf.json()
        csrf_token = csrf_data.get("csrf")
        logger.info(f"CSRF Token obtained: {csrf_token[:10]}...")

        # Step 2: Login
        logger.info("正在登录...")
        headers.update(
            {
                "X-CSRF-Token": csrf_token,
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Origin": "https://linux.do",
            }
        )

        data = {
            "login": USERNAME,
            "password": PASSWORD,
            "second_factor_method": "1",
            "timezone": "Asia/Shanghai",
        }

        try:
            resp_login = self.session.post(
                SESSION_URL, data=data, impersonate="chrome136", headers=headers
            )

            if resp_login.status_code == 200:
                response_json = resp_login.json()
                if response_json.get("error"):
                    logger.error(f"登录失败: {response_json.get('error')}")
                    return False
                logger.info("登录成功!")
            else:
                logger.error(f"登录失败，状态码: {resp_login.status_code}")
                logger.error(resp_login.text)
                return False
        except Exception as e:
            logger.error(f"登录请求异常: {e}")
            return False

        # Step 3: Pass cookies to DrissionPage
        logger.info("同步 Cookie 到 DrissionPage...")

        cookies_dict = self.session.cookies.get_dict()

        dp_cookies = []
        for name, value in cookies_dict.items():
            dp_cookies.append(
                {
                    "name": name,
                    "value": value,
                    "domain": ".linux.do",
                    "path": "/",
                }
            )

        self.page.set.cookies(dp_cookies)

        logger.info("Cookie 设置完成，导航至 linux.do...")
        self.page.get(HOME_URL)

        time.sleep(5)
        try:
            user_ele = self.page.ele("@id=current-user")
        except Exception as e:
            logger.warning(f"登录验证失败: {str(e)}")
            return True
        if not user_ele:
            # Fallback check for avatar
            if "avatar" in self.page.html:
                logger.info("登录验证成功 (通过 avatar)")
                return True
            logger.error("登录验证失败 (未找到 current-user)")
            return False
        else:
            logger.info("登录验证成功")
            return True

    def click_topic(self):
        topic_list = self.page.ele("@id=list-area").eles(".:title")
        if not topic_list:
            logger.error("未找到主题帖")
            return False
        logger.info(f"发现 {len(topic_list)} 个主题帖，随机选择10个")
        for topic in random.sample(topic_list, 10):
            self.click_one_topic(topic.attr("href"))
        return True

    @retry_decorator()
    def click_one_topic(self, topic_url):
        new_page = self.browser.new_tab()
        try:
            new_page.get(topic_url)
            if random.random() < 0.3:  # 0.3 * 30 = 9
                self.click_like(new_page)
            self.browse_post(new_page)
        finally:
            try:
                new_page.close()
            except Exception:
                pass

    def browse_post(self, page):
        prev_url = None
        # 开始自动滚动，最多滚动10次
        for _ in range(10):
            # 随机滚动一段距离
            scroll_distance = random.randint(550, 650)  # 随机滚动 550-650 像素
            logger.info(f"向下滚动 {scroll_distance} 像素...")
            page.run_js(f"window.scrollBy(0, {scroll_distance})")
            logger.info(f"已加载页面: {page.url}")

            if random.random() < 0.03:  # 33 * 4 = 132
                logger.success("随机退出浏览")
                break

            # 检查是否到达页面底部
            at_bottom = page.run_js(
                "window.scrollY + window.innerHeight >= document.body.scrollHeight"
            )
            current_url = page.url
            if current_url != prev_url:
                prev_url = current_url
            elif at_bottom and prev_url == current_url:
                logger.success("已到达页面底部，退出浏览")
                break

            # 动态随机等待
            wait_time = random.uniform(2, 4)  # 随机等待 2-4 秒
            logger.info(f"等待 {wait_time:.2f} 秒...")
            time.sleep(wait_time)

    def run(self):
        try:
            # 优先使用手动 Cookie 登录，没有再使用账号密码
            if COOKIES:
                login_res = self.login_with_cookies(COOKIES)
                if not login_res:
                    logger.warning("Cookie 登录失败，尝试账号密码登录...")
                    login_res = self.login()
            else:
                login_res = self.login()
            if not login_res:  # 登录
                logger.warning("登录验证失败")

            browse_done = False
            if BROWSE_ENABLED:
                try:
                    if self.click_topic():  # 点击主题
                        logger.info("完成浏览任务")
                        browse_done = True
                    else:
                        logger.warning(
                            "未找到主题帖或浏览失败（可能被 Cloudflare 拦截），"
                            "跳过浏览，继续签到流程"
                        )
                except Exception as e:
                    logger.warning(f"浏览任务异常，跳过：{e}")
            self.print_connect_info()  # 打印连接信息
            self.send_notifications(browse_done)  # 发送通知
        finally:
            try:
                self.page.close()
            except Exception:
                pass
            try:
                self.browser.quit()
            except Exception:
                pass

    def click_like(self, page):
        try:
            # 专门查找未点赞的按钮
            like_button = page.ele(".discourse-reactions-reaction-button")
            if like_button:
                logger.info("找到未点赞的帖子，准备点赞")
                like_button.click()
                logger.info("点赞成功")
                time.sleep(random.uniform(1, 2))
            else:
                logger.info("帖子可能已经点过赞了")
        except Exception as e:
            logger.error(f"点赞失败: {str(e)}")

    def print_connect_info(self):
        logger.info("获取连接信息")
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        }
        resp = self.session.get(
            "https://connect.linux.do/", headers=headers, impersonate="chrome136"
        )
        soup = BeautifulSoup(resp.text, "html.parser")
        rows = soup.select("table tr")
        info = []

        for row in rows:
            cells = row.select("td")
            if len(cells) >= 3:
                project = cells[0].text.strip()
                current = cells[1].text.strip() if cells[1].text.strip() else "0"
                requirement = cells[2].text.strip() if cells[2].text.strip() else "0"
                info.append([project, current, requirement])

        logger.info("--------------Connect Info-----------------")
        logger.info("\n" + tabulate(info, headers=["项目", "当前", "要求"], tablefmt="pretty"))

    def send_notifications(self, browse_enabled):
        """发送签到通知"""
        who = self.username or USERNAME or "LinuxDo 用户"
        status_msg = f"✅每日登录成功: {who}"
        if browse_enabled:
            status_msg += " + 浏览任务完成"

        # 使用通知管理器发送所有通知
        self.notifier.send_all("LINUX DO", status_msg)


if __name__ == "__main__":
    if not COOKIES and (not USERNAME or not PASSWORD):
        print("请设置 LINUXDO_COOKIES（Cookie 登录），或同时设置 USERNAME 和 PASSWORD（账号密码登录）")
        exit(1)
    browser = LinuxDoBrowser()
    browser.run()
