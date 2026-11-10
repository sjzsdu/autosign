#!/usr/bin/env python3
"""豆包「天天领额度」每日签到（浏览器版）。

为什么不能像 Trae / Qoder 那样纯 HTTP：豆包的写接口（签到、领奖）强制
`a_bogus` 动态签名，签名由豆包网页 JS 在真实浏览器里现场生成，函数锁在网页
闭包里，脚本无法复刻；只带 Cookie 裸调会被服务端拒绝。所以这里用 Playwright
驱动一个真实 Chromium：页面自己发请求、自己签名，我们只负责点「立即签到」。

流程：
  1. 首次使用：`doubao_signin.py login`
     - 打开可见浏览器（普通 Chrome UA）→ 豆包首页 → 点「登录」→ 手机扫码；
     - 登录态（Cookie + localStorage）写入 ~/.cache/autosign/doubao.state.json（0600）。
  2. 日常：`doubao_signin.py auto`
     - 用保存的登录态、SamanthaDoubao/<客户端版本> UA 无头打开签到页
       https://www.doubao.com/pc/agent-explore；
     - 页面显示「立即签到」才点击，显示「已签到」直接返回 ALREADY（幂等）；
     - 领取后再复核页面状态，避免误报。

依赖：Playwright（`pip install playwright`，浏览器用本机已安装的 Chrome）。
没有 Playwright / 没登录时不报硬失败，输出 NO_AUTH 并提示怎么补，方便 cron 跳过。

用法：
  python3 doubao_signin.py login     # 一次性扫码登录（有窗口）
  python3 doubao_signin.py auto      # 签到（幂等，日常用这个）
  python3 doubao_signin.py status    # 只查状态，不领取
  python3 doubao_signin.py claim     # 只尝试领取

输出一行 JSON，人话在 report 字段。任何情况下都不打印 Cookie / token。
退出码：0 正常（含 CLAIMED/ALREADY/INACTIVE/PENDING）、1 硬失败、2 凭据/依赖问题。
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time

CHECKIN_URL = os.environ.get("DOUBAO_CHECKIN_URL", "https://www.doubao.com/pc/agent-explore")
HOME_URL = "https://www.doubao.com/chat/"
STATE_DIR = os.path.expanduser("~/.cache/autosign")
STATE_FILE = os.environ.get("DOUBAO_STATE_FILE", os.path.join(STATE_DIR, "doubao.state.json"))
ERROR_SHOT = os.path.join(STATE_DIR, "doubao-error.png")
PAGE_TIMEOUT = int(os.environ.get("DOUBAO_TIMEOUT", "60"))
LOGIN_TIMEOUT = int(os.environ.get("DOUBAO_LOGIN_TIMEOUT", "300"))
# 页面数据是 SPA + 活动接口，load 之后再等几秒更稳
SETTLE_MS = int(os.environ.get("DOUBAO_SETTLE_MS", "6000"))


def emit(out):
    print(json.dumps(out, ensure_ascii=False, default=str), flush=True)


def _readable(path):
    try:
        with open(path, "rb") as f:
            f.read(1)
        return True
    except Exception:
        return False


# ------------------------------------------------------------------ 客户端信息

def _plist_value(plist, key):
    try:
        return subprocess.run(
            ["defaults", "read", plist, key], capture_output=True, text=True, timeout=5
        ).stdout.strip()
    except Exception:
        return ""


def app_version():
    """豆包桌面端版本号 → SamanthaDoubao/<version>。"""
    v = os.environ.get("DOUBAO_APP_VERSION")
    if v:
        return v
    plist = "/Applications/Doubao.app/Contents/Info.plist"
    if os.path.exists(plist):
        v = _plist_value(plist, "CFBundleShortVersionString")
        if v:
            return v
    # 兜底：页面只校验版本不能低于当前客户端，留一个较新的值
    return "2.34.12"


def chromium_major():
    """豆包客户端内置 Chromium 的 major 版本，用于让 UA 更像客户端。"""
    v = os.environ.get("DOUBAO_CHROME_VERSION")
    if v:
        return v
    base = ("/Applications/Doubao.app/Contents/Helpers/Doubao Browser.app/Contents/Frameworks/"
            "Doubao Browser Framework.framework/Versions")
    try:
        dirs = [d for d in os.listdir(base) if d[0].isdigit()]
        if dirs:
            return max(dirs, key=lambda d: int(d.split(".")[0])).split(".")[0]
    except Exception:
        pass
    return "147"


def samantha_ua():
    return ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/%s.0.0.0 Safari/537.36 SamanthaDoubao/%s"
            % (chromium_major(), app_version()))


def diag():
    return {
        "app_version": app_version(),
        "ua_samantha": samantha_ua(),
        "state_file": STATE_FILE,
        "state_exists": _readable(STATE_FILE),
        "headless": not bool(os.environ.get("DOUBAO_HEADED")),
    }


# ------------------------------------------------------------------ 浏览器

def browser_launch_options():
    """优先用本机已安装的 Chrome/Edge，避免额外下载 Chromium。"""
    channel = os.environ.get("DOUBAO_BROWSER_CHANNEL")
    if channel:
        return {"channel": channel}
    candidates = [
        ("chrome", "/Applications/Google Chrome.app"),
        ("msedge", "/Applications/Microsoft Edge.app"),
        ("chrome", r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        ("msedge", r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    ]
    for name, path in candidates:
        if os.path.exists(path):
            return {"channel": name}
    for exe, name in (("google-chrome", "chrome"), ("chromium", None), ("microsoft-edge", "msedge")):
        if shutil.which(exe):
            return {"channel": name} if name else {}
    return {}


def _playwright():
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
        return sync_playwright
    except Exception:
        return None


def _launch(p, headless):
    opts = browser_launch_options()
    try:
        return p.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
            **opts,
        )
    except Exception:
        # 指定 channel 失败（没装 Chrome）时回退到 Playwright 自带 Chromium
        if opts:
            return p.chromium.launch(
                headless=headless,
                args=["--disable-blink-features=AutomationControlled"],
            )
        raise


def _save_state(ctx):
    os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
    ctx.storage_state(path=STATE_FILE)
    try:
        os.chmod(STATE_FILE, 0o600)
    except Exception:
        pass


def _cookie_names(ctx):
    try:
        return {c.get("name") for c in ctx.cookies()}
    except Exception:
        return set()


# ------------------------------------------------------------------ 登录

def do_login():
    sync_playwright = _playwright()
    if not sync_playwright:
        return 2, {"result": "NO_AUTH", "diag": diag(),
                   "report": "未安装 Playwright，先执行：pip install playwright"
                             "（本项目用本机 Chrome，无需 playwright install chromium）"}

    with sync_playwright() as p:
        browser = _launch(p, headless=False)
        # 登录必须用普通 Chrome UA：SamanthaDoubao UA 下不显示登录入口
        ctx = browser.new_context(locale="zh-CN", viewport={"width": 1280, "height": 900})
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(HOME_URL, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT * 1000)
        page.wait_for_timeout(4000)
        try:
            page.bring_to_front()
        except Exception:
            pass

        # 关掉「下载豆包电脑版」等遮挡弹窗
        try:
            for dlg in page.query_selector_all("[role='dialog'][data-state='open']"):
                btn = dlg.query_selector("button[aria-label='关闭'], [class*='close']")
                if btn:
                    btn.click(timeout=3000)
                else:
                    page.keyboard.press("Escape")
                page.wait_for_timeout(800)
        except Exception:
            pass

        # 打开扫码登录框
        btn = page.query_selector("button[data-testid='to_login_button']")
        print("页面: %s | 登录按钮: %s" % (page.url, "找到" if btn else "未找到"), flush=True)
        if btn:
            try:
                btn.click(timeout=8000)
            except Exception:
                try:
                    btn.click(force=True, timeout=8000)
                except Exception:
                    pass
            page.wait_for_timeout(3000)

        try:
            modal = None
            for dlg in page.query_selector_all("[role='dialog'][data-state='open']"):
                txt = dlg.inner_text()
                if "登录" in txt or "扫码" in txt:
                    modal = txt
                    break
            print("登录弹窗: %s" % ("已出现，请扫码" if modal else "未出现，请手动点右上角「登录」"),
                  flush=True)
        except Exception:
            pass

        shot = os.environ.get("DOUBAO_DEBUG_SHOT")
        if shot:
            try:
                page.screenshot(path=shot)
                print("调试截图: %s" % shot, flush=True)
            except Exception:
                pass

        print("请在打开的浏览器窗口里用豆包 App 扫码登录，脚本会自动检测登录结果…",
              flush=True)

        deadline = time.time() + LOGIN_TIMEOUT
        logged_in = False
        while time.time() < deadline:
            names = _cookie_names(ctx)
            if "sessionid" in names or "sessionid_ss" in names:
                logged_in = True
                break
            try:
                page.wait_for_timeout(2000)
            except Exception:
                break

        if not logged_in:
            browser.close()
            return 2, {"result": "NO_AUTH", "diag": diag(),
                       "report": "等待 %d 秒仍未检测到豆包登录，请重试 ./run.sh doubao-login"
                                 % LOGIN_TIMEOUT}

        _save_state(ctx)
        # 用 SamanthaDoubao UA 复核签到页是否可见
        ctx.close()
        verify = None
        verify_error = None
        try:
            ctx2 = browser.new_context(
                storage_state=STATE_FILE, user_agent=samantha_ua(),
                locale="zh-CN", viewport={"width": 1280, "height": 900})
            page2 = ctx2.new_page()
            page2.goto(CHECKIN_URL, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT * 1000)
            page2.wait_for_timeout(SETTLE_MS)
            body = page2.inner_text("body")
            verify = "立即签到" in body or "每日签到" in body or "已签到" in body
            ctx2.close()
        except Exception as e:
            # 复核没做成不能当成功：把异常记进 diag 并按失败返回
            verify_error = "%s: %s" % (type(e).__name__, e)
        browser.close()

    out_diag = diag()
    out_diag["verify"] = verify
    if verify_error:
        out_diag["verify_error"] = verify_error

    if verify is not True:
        if verify is False:
            report = "登录成功，但签到页未显示签到模块（可能活动未开放或 UA 版本需更新）"
        else:
            report = ("登录成功、登录态已保存，但签到页复核未完成（%s）；"
                      "请运行 ./run.sh status 确认" % verify_error)
        return 1, {"result": "ERROR", "diag": out_diag, "logged_in": True,
                   "report": report}
    return 0, {"result": "OK", "diag": out_diag, "logged_in": True,
               "report": "豆包登录成功，登录态已保存，之后可自动签到"}


# ------------------------------------------------------------------ 签到

def _page_text(page):
    try:
        return page.inner_text("body")
    except Exception:
        return ""


def _card_text(page):
    """定位「每日签到」卡片；返回卡片文本，定位不到时返回 None。"""
    for sel in ('[data-reward-code="daily_signin"]',):
        try:
            el = page.query_selector(sel)
            if el:
                return el.inner_text()
        except Exception:
            continue
    return None


def _shot(page):
    try:
        os.makedirs(os.path.dirname(ERROR_SHOT), exist_ok=True)
        page.screenshot(path=ERROR_SHOT)
        return ERROR_SHOT
    except Exception:
        return None


def _find_sign_button(page):
    sel = ('[data-reward-code="daily_signin"] button:has-text("立即签到"), '
           'button:has-text("立即签到")')
    try:
        loc = page.locator(sel)
        for i in range(loc.count()):
            item = loc.nth(i)
            if item.is_visible():
                return item
    except Exception:
        pass
    return None


def _amount(text):
    for pat in (r"累计获得额度[：:]\s*(\d+)", r"领取\s*(\d+)\s*额度", r"(\d+)\s*额度"):
        m = re.search(pat, text or "")
        if m:
            try:
                return int(m.group(1))
            except Exception:
                pass
    return None


def do_checkin(claim=True):
    sync_playwright = _playwright()
    if not sync_playwright:
        return 2, {"result": "NO_AUTH", "diag": diag(),
                   "report": "未安装 Playwright，先执行：pip install playwright；"
                             "首次使用还需 ./run.sh doubao-login 扫码登录"}
    if not _readable(STATE_FILE):
        return 2, {"result": "NO_AUTH", "diag": diag(),
                   "report": "还没有豆包登录态，先运行 ./run.sh doubao-login 扫码登录一次"}

    base = diag()
    with sync_playwright() as p:
        browser = _launch(p, headless=not os.environ.get("DOUBAO_HEADED"))
        ctx = browser.new_context(
            storage_state=STATE_FILE, user_agent=samantha_ua(),
            locale="zh-CN", viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        try:
            page.goto(CHECKIN_URL, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT * 1000)
            page.wait_for_timeout(SETTLE_MS)
            try:
                page.wait_for_selector(
                    '[data-reward-code="daily_signin"], button[data-testid="to_login_button"]',
                    timeout=15000)
            except Exception:
                pass

            url = page.url
            body = _page_text(page)
            base["url"] = url
            card = _card_text(page)

            if "loginSuccessBackUrl" in url or not _cookie_names(ctx):
                return 1, {"result": "NO_SESSION", "diag": base,
                           "report": "豆包登录态已失效，请重新运行 ./run.sh doubao-login 扫码登录"}
            if "该活动仅支持在豆包电脑版参与" in body or "版本过低" in body:
                shot = _shot(page)
                base["screenshot"] = shot
                return 1, {"result": "ERROR", "diag": base,
                           "report": "签到页要求电脑版客户端（UA 版本需不低于 %s），"
                                     "可设置 DOUBAO_APP_VERSION 后重试" % app_version()}
            if not card and "立即签到" not in body and "每日签到" not in body \
                    and "已签到" not in body:
                shot = _shot(page)
                base["screenshot"] = shot
                base["page_hint"] = re.sub(r"\s+", " ", body)[:160]
                return 1, {"result": "ERROR", "diag": base,
                           "report": "页面未出现签到模块（活动可能未开放/改版）；"
                                     "可用 DOUBAO_HEADED=1 打开窗口查看"}

            scope = card if card is not None else body
            pending = "立即签到" in scope
            already = (not pending) and ("已签到" in scope or "已签到" in body)

            if already:
                return 0, {"result": "ALREADY", "diag": base,
                           "report": "豆包今日已签到，无需重复领取"}
            if not pending:
                shot = _shot(page)
                base["screenshot"] = shot
                base["page_hint"] = re.sub(r"\s+", " ", scope)[:160]
                return 1, {"result": "ERROR", "diag": base,
                           "report": "没找到「立即签到」按钮，页面结构可能已改版"}
            if not claim:
                amt = _amount(scope)
                return 0, {"result": "PENDING", "diag": base, "credits": amt,
                           "report": "豆包今日未签到%s"
                                     % ("，可领 %s 额度" % amt if amt else "")}

            button = _find_sign_button(page)
            if button is None:
                shot = _shot(page)
                base["screenshot"] = shot
                return 1, {"result": "ERROR", "diag": base,
                           "report": "签到按钮不可点击（可能被弹窗遮挡），可用 DOUBAO_HEADED=1 排查"}

            button.click(timeout=15000)
            page.wait_for_timeout(6000)

            body2 = _page_text(page)
            card2 = _card_text(page)
            scope2 = card2 if card2 is not None else body2
            amt = _amount(body2)
            base["signed_in_after"] = "已签到" in scope2

            _save_state(ctx)

            if "签到成功" in body2 or "领取成功" in body2 or "已签到" in scope2 \
                    or "已签到" in body2:
                got = amt
                return 0, {"result": "CLAIMED", "diag": base, "credits": got,
                           "report": ("成功领取 %s 额度" % got if got else "签到成功")}
            shot = _shot(page)
            base["screenshot"] = shot
            base["page_hint"] = re.sub(r"\s+", " ", body2)[:160]
            return 1, {"result": "ERROR", "diag": base,
                       "report": "点击后未检测到签到成功反馈，已截图：%s" % shot}
        finally:
            try:
                ctx.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass


def main():
    action = sys.argv[1] if len(sys.argv) > 1 else "auto"
    try:
        if action == "login":
            code, out = do_login()
        elif action in ("auto", "claim"):
            code, out = do_checkin(claim=True)
        elif action == "status":
            code, out = do_checkin(claim=False)
        else:
            code, out = 2, {"result": "ERROR",
                            "report": "未知命令：%s（可用：login / auto / status / claim）" % action}
    except Exception as e:
        code, out = 2, {"result": "ERROR",
                        "report": "脚本运行异常（%s: %s）" % (type(e).__name__, e)}
    emit(out)
    return code


if __name__ == "__main__":
    sys.exit(main())
