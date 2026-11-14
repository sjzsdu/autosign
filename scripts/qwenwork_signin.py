#!/usr/bin/env python3
"""千问办公（QwenWork）每日自动登录 + 积分核对。

先说清楚事实：QwenWork **没有手动签到按钮**，也没有 claim 类接口。
官方公告「每日登录送积分」活动已在 2026-08-09 结束，规则本身就是
「每日 00:00 系统统一发放，无需用户操作」；现在积分页（/app/credits）的
「每日刷新积分」同样是每天 00:00 自动刷新。

所以本脚本做的事只有两件：
  1. 用保存的登录态访问一次工作台接口（等效于「每日首次登录」），
     让服务端记住这次登录；顺带验证登录态是否还有效。
  2. 读 /user/v2/quota，报告今日「每日刷新积分」余额，方便确认权益到账。

它不会伪造 CLAIMED——没有可领取的东西就不假装领到了。
登录态失效时报 NO_SESSION，提示重新执行 ./run.sh qwenwork-login。

接口（与网页端完全一致，同源、Cookie 鉴权）：
  GET /biz/user/v1/auth/me   我是谁（校验登录态）
  GET /user/v2/quota         每日 / 月度 / 长期积分余额
响应信封：{"code": "ok" | "SUCCESS", "data": {...}}，其余视为错误。
请求头只需要 `X-Client-Source: web`，鉴权靠 Cookie（网页端 withCredentials）。

登录态：一次性 `login` 打开有头 Chrome 人工登录（阿里云账号扫码/手机号），
把 Playwright storage_state 存到 ~/.cache/autosign/qwenwork.state.json，
之后日常只读该文件发 HTTP 请求，不起浏览器。

用法：
  python3 qwenwork_signin.py login   # 首次登录（有窗口）
  python3 qwenwork_signin.py auto    # 每日自动登录 + 积分核对（日常用）
  python3 qwenwork_signin.py status  # 同上，语义等价（没有「领取」动作）

输出一行 JSON，人话在 report 字段；任何情况下都不打印 Cookie。
退出码：0 正常（ALREADY / INACTIVE）、1 硬失败、2 凭据/用法错误。
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

API_BASE = os.environ.get("QWENWORK_API_BASE", "https://qwenwork.cn").rstrip("/")
STATE_FILE = os.path.expanduser("~/.cache/autosign/qwenwork.state.json")
REQUEST_TIMEOUT = float(os.environ.get("QWENWORK_TIMEOUT", "15"))
APP_URL = API_BASE + "/app/"

ME_PATH = "/biz/user/v1/auth/me"
QUOTA_PATH = "/user/v2/quota"

USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36")


def emit(out):
    print(json.dumps(out, ensure_ascii=False, default=str), flush=True)


def _balance(obj):
    """从 {total_balance|balance|total|amount: n} 里取余额；取不到返回 None。"""
    if not isinstance(obj, dict):
        return None
    for key in ("total_balance", "balance", "total", "amount"):
        v = obj.get(key)
        if isinstance(v, (int, float)):
            return v
    return None


def load_auth():
    """从 Playwright storage_state 里取出 qwenwork.cn 的 Cookie。"""
    if not os.path.exists(STATE_FILE):
        return None, "NO_AUTH", (
            "未找到千问办公登录态，先执行 ./run.sh qwenwork-login 完成一次登录"
            "（保存到 %s）" % STATE_FILE)
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
    except Exception as e:
        return None, "NO_AUTH", "读取登录态失败（%s: %s），请重新执行 ./run.sh qwenwork-login" % (
            type(e).__name__, e)

    pairs = []
    for c in state.get("cookies") or []:
        domain = (c.get("domain") or "").lstrip(".")
        if domain == "qwenwork.cn" or domain.endswith(".qwenwork.cn"):
            name, value = c.get("name"), c.get("value")
            if name and value is not None:
                pairs.append("%s=%s" % (name, value))

    if not pairs:
        return None, "NO_AUTH", "登录态里没有千问办公 Cookie，请重新执行 ./run.sh qwenwork-login"

    return {
        "cookie": "; ".join(pairs),
        "cookie_names": [p.split("=", 1)[0] for p in pairs],
    }, None, None


def api_get(auth, path):
    """带 Cookie 调用同源接口，返回 (http状态, url, json或raw文本)。"""
    headers = {
        "Cookie": auth["cookie"],
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "X-Client-Source": "web",
        "Origin": API_BASE,
        "Referer": APP_URL,
        "User-Agent": USER_AGENT,
    }
    req = urllib.request.Request(API_BASE + path, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, resp.geturl(), json.loads(raw)
            except Exception:
                return resp.status, resp.geturl(), raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, e.geturl(), json.loads(raw)
        except Exception:
            return e.code, e.geturl(), raw
    except Exception as e:
        return -1, "", {"error": str(e)}


def _envelope_ok(body):
    if not isinstance(body, dict):
        return False
    return body.get("code") in ("ok", "SUCCESS")


def _envelope_msg(body):
    if not isinstance(body, dict):
        return str(body)[:160]
    for key in ("msg", "message"):
        v = body.get(key)
        if isinstance(v, str) and v:
            return v
    return json.dumps(body, ensure_ascii=False)[:160]


def _looks_unauthenticated(scode, final_url, body):
    if scode in (401, 403):
        return True
    if final_url and "/signin" in final_url:
        return True
    if not isinstance(body, dict):  # 登录页返回 HTML
        return True
    code = body.get("code")
    return code in ("UNAUTHORIZED", "NOT_LOGIN", "NO_LOGIN", "LOGIN_REQUIRED")


def checkin():
    auth, err_code, err_msg = load_auth()
    if err_code:
        return (2 if err_code == "NO_AUTH" else 1), {
            "result": err_code, "report": err_msg,
            "diag": {"state_file": STATE_FILE}}

    diag = {"state_file": STATE_FILE, "cookie_count": len(auth["cookie_names"])}

    # 1) 打一次「我是谁」，这就是网页端每次进入工作台都会发的请求
    scode, surl, sbody = api_get(auth, ME_PATH)
    if scode == -1:
        return 1, {"result": "NETWORK", "diag": diag,
                   "report": "网络不可达，千问办公登录跳过（%s）" % (sbody.get("error") or "")}
    if _looks_unauthenticated(scode, surl, sbody):
        return 1, {"result": "NO_SESSION", "diag": diag,
                   "report": "千问办公登录态已失效（HTTP %s），请重新执行 ./run.sh qwenwork-login"
                             % scode}
    if scode != 200 or not _envelope_ok(sbody):
        return 1, {"result": "ERROR", "diag": diag,
                   "report": "用户信息接口异常：%s（HTTP %s）" % (_envelope_msg(sbody), scode)}

    me = sbody.get("data") or {}
    diag["user_id_set"] = bool(me.get("id") or me.get("userId") or me.get("uid"))

    # 2) 读积分，核对今日「每日刷新积分」是否已到账
    qcode, qurl, qbody = api_get(auth, QUOTA_PATH)
    if qcode == -1:
        return 1, {"result": "NETWORK", "diag": diag,
                   "report": "积分接口不可达，下次自动重试"}
    if _looks_unauthenticated(qcode, qurl, qbody):
        return 1, {"result": "NO_SESSION", "diag": diag,
                   "report": "千问办公登录态已失效（HTTP %s），请重新执行 ./run.sh qwenwork-login"
                             % qcode}
    if qcode != 200 or not _envelope_ok(qbody):
        return 1, {"result": "ERROR", "diag": diag,
                   "report": "积分接口异常：%s（HTTP %s）" % (_envelope_msg(qbody), qcode)}

    quota = qbody.get("data") or {}
    daily = _balance(quota.get("daily_credits"))
    monthly = _balance(quota.get("monthly_credits"))
    longterm = _balance(quota.get("longterm_credits"))
    diag.update({
        "daily_credits": daily,
        "monthly_credits": monthly,
        "longterm_credits": longterm,
        "has_daily_field": isinstance(quota.get("daily_credits"), dict),
    })

    if daily is None:
        return 0, {"result": "INACTIVE", "diag": diag,
                   "report": "登录态有效；当前账号无每日积分权益（月度 %s / 长期 %s）"
                             % (monthly if monthly is not None else "-",
                                longterm if longterm is not None else "-")}

    return 0, {"result": "ALREADY", "diag": diag,
               "report": "登录成功；今日每日刷新积分 %s（每日 00:00 自动发放，无需手动领取）"
                         % daily}


def do_login(timeout=480):
    """打开有头浏览器，人工登录后把 storage_state 存盘。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return 2, {"result": "ERROR",
                   "report": "缺少 Playwright，先执行：.venv/bin/pip install playwright"}
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)

    probe = """async () => {
        const r = await fetch('/biz/user/v1/auth/me', {
            credentials: 'include',
            headers: {'X-Client-Source': 'web', 'Accept': 'application/json'}
        });
        if (!r.ok) return '';
        const j = await r.json().catch(() => ({}));
        return (j && (j.code === 'ok' || j.code === 'SUCCESS')) ? 'ok' : '';
    }"""

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, channel="chrome")
        ctx = browser.new_context(locale="zh-CN", viewport={"width": 1400, "height": 900})
        page = ctx.new_page()
        page.goto(APP_URL, wait_until="domcontentloaded", timeout=60000)
        page.bring_to_front()
        print("请在浏览器窗口内登录千问办公（阿里云账号扫码 / 手机号），最长等待 %d 秒…" % timeout,
              file=sys.stderr, flush=True)
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if page.evaluate(probe):
                    ctx.storage_state(path=STATE_FILE)
                    os.chmod(STATE_FILE, 0o600)
                    browser.close()
                    return 0, {"result": "LOGIN_OK",
                               "report": "登录成功，登录态已保存到 %s" % STATE_FILE}
            except Exception:
                pass
            page.wait_for_timeout(3000)
        browser.close()
    return 1, {"result": "NO_AUTH", "report": "等待登录超时，请重试 ./run.sh qwenwork-login"}


def main():
    action = sys.argv[1] if len(sys.argv) > 1 else "auto"
    try:
        if action == "login":
            code, out = do_login()
        elif action in ("auto", "status", "claim"):
            # claim 保留为别名：QwenWork 没有领取动作，行为与 auto 相同
            code, out = checkin()
        else:
            code, out = 2, {"result": "ERROR",
                            "report": "未知命令：%s（可用：login / auto / status）" % action}
    except Exception as e:
        code, out = 2, {"result": "ERROR",
                        "report": "脚本运行异常（%s: %s）" % (type(e).__name__, e)}
    emit(out)
    return code


if __name__ == "__main__":
    sys.exit(main())
