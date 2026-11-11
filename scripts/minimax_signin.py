#!/usr/bin/env python3
"""MiniMax Code 每日签到领积分。

原理：agent.minimax.cn 的签到面板走 minimax-cloud 网关：
  GET  /minimax-cloud/api/v1/signin/status   查询 7 天签到面板
  POST /minimax-cloud/api/v1/signin/claim    领取今日积分（body 为 {}）
  GET  /minimax-cloud/api/v1/signin/config   面板皮肤（本脚本不用）

鉴权要点（照抄网页端实现，缺一不可）：
  - 头 `token: <JWT>`，不是 Authorization；
  - 查询参数必须带 user_id（网页端取自 localStorage.user_detail_agent.realUserID）；
  - 头 `x-timestamp`（秒）与 `x-signature` = md5(timestamp + 固定盐 + 请求体原文)；
  - 网关不校验 yy 指纹头，可以不带。

登录态：网页端把 JWT 存在 localStorage._token。本脚本复用豆包那套做法——
一次性 `login` 打开有头浏览器人工登录，把 Playwright storage_state 存到
~/.cache/autosign/minimax.state.json；日常签到只读该文件发 HTTP 请求，不起浏览器。

签到面板状态码：1 未开始 / 2 可领取 / 3 已领取 / 4 已停用。
重复执行幂等：先查 status，今日已领取直接返回 ALREADY，不再发 claim。

用法：
  python3 minimax_signin.py login   # 首次登录（有窗口，扫码/手机号）
  python3 minimax_signin.py auto    # 查状态 → 未签才领（日常用这个）
  python3 minimax_signin.py status  # 仅查状态
  python3 minimax_signin.py claim   # 仅领取（幂等）

输出一行 JSON，人话在 report 字段；任何情况下都不打印 token。
退出码：0 正常（CLAIMED/ALREADY/PENDING）、1 硬失败、2 凭据/用法错误。
"""

import base64
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API_BASE = os.environ.get("MINIMAX_API_BASE", "https://agent.minimax.cn").rstrip("/")
STATE_FILE = os.path.expanduser("~/.cache/autosign/minimax.state.json")
REQUEST_TIMEOUT = float(os.environ.get("MINIMAX_TIMEOUT", "15"))

# 网页端签名盐，写死在 minimax-cloud 前端包里
SIGN_SALT = "I*7Cf%WZ#S&%1RlZJ&C2"

STATUS_PATH = "/minimax-cloud/api/v1/signin/status"
CLAIM_PATH = "/minimax-cloud/api/v1/signin/claim"

# 面板状态：2 可领取 / 3 已领取
ST_CLAIMABLE = 2
ST_CLAIMED = 3


def emit(out):
    print(json.dumps(out, ensure_ascii=False, default=str), flush=True)


def jwt_exp(token):
    """取 JWT 的 exp（秒）；解析失败返回 None。"""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode()))
        exp = data.get("exp")
        return int(exp) if isinstance(exp, (int, float)) else None
    except Exception:
        return None


def load_auth():
    """从 Playwright storage_state 里取 token / user_id / uuid。"""
    if not os.path.exists(STATE_FILE):
        return None, "NO_AUTH", (
            "未找到 MiniMax 登录态，先执行 ./run.sh minimax-login 完成一次登录"
            "（保存到 %s）" % STATE_FILE)
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
    except Exception as e:
        return None, "NO_AUTH", "读取登录态失败（%s: %s），请重新执行 ./run.sh minimax-login" % (
            type(e).__name__, e)

    local = {}
    for origin in state.get("origins") or []:
        if origin.get("origin", "").rstrip("/") == API_BASE:
            for kv in origin.get("localStorage") or []:
                local[kv.get("name")] = kv.get("value")

    token = local.get("_token") or ""
    if not token:
        return None, "NO_AUTH", "登录态里没有 _token，请重新执行 ./run.sh minimax-login"

    user_id = ""
    try:
        user_id = str(json.loads(local.get("user_detail_agent") or "{}").get("realUserID") or "")
    except Exception:
        pass
    if not user_id:
        return None, "NO_SESSION", "登录态缺少 user_id，请重新执行 ./run.sh minimax-login"

    exp = jwt_exp(token)
    if exp and exp < time.time():
        return None, "NO_SESSION", "MiniMax 登录态已于 %s 过期，请重新执行 ./run.sh minimax-login" % (
            time.strftime("%Y-%m-%d %H:%M", time.localtime(exp)))

    return {
        "token": token,
        "user_id": user_id,
        "uuid": local.get("UNIQUE_USER_ID") or "",
        "expires_at": exp,
    }, None, None


def call(auth, path, body=None):
    """签名后调用网关；body 为 None 表示 GET，否则 POST JSON。"""
    sec = int(time.time())
    params = {
        "device_platform": "web", "biz_id": "3", "app_id": "3001",
        "version_code": "22201", "unix": str(sec * 1000), "timezone_offset": "28800",
        "sys_language": "zh", "lang": "zh", "uuid": auth.get("uuid") or "",
        "os_name": "macOS", "browser_name": "Chrome", "device_memory": "16",
        "cpu_core_num": "8", "browser_language": "zh-CN", "browser_platform": "MacIntel",
        "user_id": auth["user_id"], "screen_width": "1512", "screen_height": "982",
        "client": "web",
    }
    body_text = "{}" if body is not None else ""
    headers = {
        "token": auth["token"],
        "x-timestamp": str(sec),
        "x-signature": hashlib.md5(
            ("%s%s%s" % (sec, SIGN_SALT, body_text)).encode()).hexdigest(),
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": API_BASE,
        "Referer": API_BASE + "/",
    }
    url = "%s%s?%s" % (API_BASE, path, urllib.parse.urlencode(params))
    data = body_text.encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw[:200]}
    except Exception as e:
        return -1, {"error": str(e)}


def panel_summary(data, day=None):
    """从面板数据里挑出今天/指定天，返回 (状态, 分数) 与整体描述。"""
    days = (data or {}).get("days") or []
    if not days:
        return None, None, ""
    target = day or next((d for d in days if d.get("is_today")), days[0])
    pts = target.get("points")
    if target.get("bonus_points"):
        pts = "%s(+%s)" % (pts, target["bonus_points"])
    streak = sum(1 for d in days if d.get("status") == ST_CLAIMED)
    return target.get("status"), pts, "连续 %s 天" % streak if streak else ""


def checkin(claim=False):
    auth, err_code, err_msg = load_auth()
    if err_code:
        return (2 if err_code == "NO_AUTH" else 1), {
            "result": err_code, "report": err_msg,
            "diag": {"state_file": STATE_FILE}}

    diag = {"state_file": STATE_FILE, "user_id_set": bool(auth["user_id"])}
    scode, sbody = call(auth, STATUS_PATH)
    if scode == -1:
        return 1, {"result": "NETWORK", "diag": diag,
                   "report": "网络不可达，签到跳过（%s）" % (sbody.get("error") or "")}
    if scode in (401, 403):
        return 1, {"result": "NO_SESSION", "diag": diag,
                   "report": "MiniMax 登录态已失效（HTTP %s），请重新执行 ./run.sh minimax-login"
                             % scode}
    if scode != 200 or (sbody.get("base_resp") or {}).get("status_code") != 0:
        msg = (sbody.get("base_resp") or {}).get("status_msg") or json.dumps(
            sbody, ensure_ascii=False)[:160]
        return 1, {"result": "ERROR", "diag": diag,
                   "report": "签到状态接口异常：%s（HTTP %s）" % (msg, scode)}

    data = sbody.get("data") or {}
    st, pts, streak = panel_summary(data)
    diag["today_status"] = st

    if st == ST_CLAIMED:
        return 0, {"result": "ALREADY", "diag": diag,
                   "report": "今日已签到%s" % (("（%s）" % streak) if streak else "")}
    if st is None:
        return 0, {"result": "INACTIVE", "diag": diag, "report": "MiniMax 签到面板不可用"}
    if st != ST_CLAIMABLE:
        return 0, {"result": "PENDING", "diag": diag,
                   "report": "今日暂不可领取（面板状态 %s）" % st}
    if not claim:
        return 0, {"result": "PENDING", "diag": diag,
                   "report": "今日未签到，可领 %s 积分%s" % (pts, ("（%s）" % streak) if streak else "")}

    ccode, cbody = call(auth, CLAIM_PATH, body={})
    if ccode == -1:
        return 1, {"result": "NETWORK", "diag": diag,
                   "report": "领取请求未能送达，下次自动重试"}
    if ccode in (401, 403):
        return 1, {"result": "NO_SESSION", "diag": diag,
                   "report": "MiniMax 登录态已失效（HTTP %s），请重新执行 ./run.sh minimax-login"
                             % ccode}
    if ccode == 200 and (cbody.get("base_resp") or {}).get("status_code") == 0:
        cdata = cbody.get("data") or {}
        got = cdata.get("points") or cdata.get("claim_points") or pts
        return 0, {"result": "CLAIMED", "diag": diag,
                   "report": "签到成功，领取 %s 积分" % got}

    msg = (cbody.get("base_resp") or {}).get("status_msg") or json.dumps(
        cbody, ensure_ascii=False)[:160]
    if "已签到" in msg or "已领取" in msg or "already" in msg.lower():
        return 0, {"result": "ALREADY", "diag": diag, "report": "今日已签到"}
    return 1, {"result": "ERROR", "diag": diag,
               "report": "领取失败：%s（HTTP %s）" % (msg, ccode)}


def do_login(timeout=480):
    """打开有头浏览器，人工登录后把 storage_state 存盘。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return 2, {"result": "ERROR",
                   "report": "缺少 Playwright，先执行：.venv/bin/pip install playwright"}
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, channel="chrome")
        ctx = browser.new_context(locale="zh-CN", viewport={"width": 1400, "height": 900})
        page = ctx.new_page()
        page.goto(API_BASE + "/", wait_until="domcontentloaded", timeout=60000)
        page.bring_to_front()
        print("请在浏览器窗口内登录 MiniMax（右上角「登录」），最长等待 %d 秒…" % timeout,
              file=sys.stderr, flush=True)
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if page.evaluate("() => localStorage.getItem('_token') || ''"):
                    ctx.storage_state(path=STATE_FILE)
                    os.chmod(STATE_FILE, 0o600)
                    browser.close()
                    return 0, {"result": "LOGIN_OK",
                               "report": "登录成功，登录态已保存到 %s" % STATE_FILE}
            except Exception:
                pass
            page.wait_for_timeout(3000)
        browser.close()
    return 1, {"result": "NO_AUTH", "report": "等待登录超时，请重试 ./run.sh minimax-login"}


def main():
    action = sys.argv[1] if len(sys.argv) > 1 else "auto"
    try:
        if action == "login":
            code, out = do_login()
        elif action in ("auto", "claim"):
            code, out = checkin(claim=True)
        elif action == "status":
            code, out = checkin(claim=False)
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
