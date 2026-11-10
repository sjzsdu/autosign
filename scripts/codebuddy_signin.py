#!/usr/bin/env python3
"""CodeBuddy（腾讯）每日签到领取 credits。

原理：CodeBuddy 的 IDE 插件登录后，把登录态写在
  .../CodeBuddyExtension/Data/Public/auth/Tencent-Cloud.coding-copilot.info
（macOS: ~/Library/Application Support/...，Windows: %LOCALAPPDATA%\\...，
 Linux: ~/.config/...）。文件是明文 JSON，直接取 auth.accessToken / account.uid，
用标准库直调官方计费接口：

  POST /v2/billing/meter/checkin-activity-status   查询活动与今日签到状态
  POST /v2/billing/meter/daily-checkin             领取今日 credits

要点：
  - 鉴权头必须是 `Authorization: Bearer <accessToken>`（漏掉 Bearer 会 401）；
  - 域名用 www.codebuddy.cn（copilot.tencent.com 同样可用；旧教程里少了 /v2/
    的路径才会 404）；
  - 客户端会把活动主题、连续天数、当前总 credits 一起返回，脚本直接复用，
    不再额外查积分接口；
  - 新版 WorkBuddy 桌面端（workbuddy-desktop.info）把 token 改成 at-rest
    信封加密（$wbEncrypted），本脚本不读写它，只用 IDE 插件的明文登录态；
  - 重复执行是幂等的：先查状态，今天已签直接返回 ALREADY，不会再发领取请求。

用法：
  python3 codebuddy_signin.py auto    # 查状态 → 未签才领（幂等，日常用这个）
  python3 codebuddy_signin.py status  # 仅查状态
  python3 codebuddy_signin.py claim   # 仅领取（幂等）

输出一行 JSON，人话在 report 字段。任何情况下都不打印 token。
退出码：0 正常（含 CLAIMED/ALREADY/INACTIVE/PENDING）、1 硬失败、2 凭据/用法错误。
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

API_BASE = os.environ.get("CODEBUDDY_API_BASE", "https://www.codebuddy.cn").rstrip("/")
REQUEST_TIMEOUT = float(os.environ.get("CODEBUDDY_TIMEOUT", "15"))

STATUS_PATH = "/v2/billing/meter/checkin-activity-status"
CLAIM_PATH = "/v2/billing/meter/daily-checkin"

# 登录态文件：IDE 插件的明文文件优先。桌面端的 workbuddy-desktop.info 是
# at-rest 加密信封，这里不尝试解密（读到了也只会报 NO_AUTH）。
AUTH_FILE_NAMES = ["Tencent-Cloud.coding-copilot.info"]

# 服务端会话失效错误码（客户端插件里同样是这两个触发重新登录）
SESSION_INVALID_CODES = {12153, 401, 403}

_TOKEN_PATTERNS = [
    re.compile(r"(?i)\b(?:dt|drt)-[A-Za-z0-9._~+/=\-]{12,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    re.compile(r"(?i)(\"?(?:access_?token|refresh_?token|token)\"?\s*[:=]\s*\"?)[^\"\s,}]{8,}"),
]


def _redact_text(value):
    for pat in _TOKEN_PATTERNS:
        value = pat.sub("<redacted>", value)
    return value


def emit(out):
    print(json.dumps(out, ensure_ascii=False, default=str), flush=True)


def _readable(path):
    """试读 1 字节代替 exists() —— 兼容沙箱对受保护路径的假阴性。"""
    try:
        with open(path, "rb") as f:
            f.read(1)
        return True
    except Exception:
        return False


def auth_candidates():
    """返回登录态候选路径（CODEBUDDY_AUTH_FILE 一旦设置即为唯一候选）。"""
    override = os.environ.get("CODEBUDDY_AUTH_FILE")
    if override:
        return [override]

    rel = os.path.join("CodeBuddyExtension", "Data", "Public", "auth")
    bases = []
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        bases.append(os.path.join(home, "Library", "Application Support"))
    elif os.name == "nt":
        if os.environ.get("APPDATA"):
            bases.append(os.environ["APPDATA"])
        if os.environ.get("LOCALAPPDATA"):
            bases.append(os.environ["LOCALAPPDATA"])
    else:
        bases.append(os.path.join(home, ".config"))

    return [os.path.join(b, rel, name) for b in bases for name in AUTH_FILE_NAMES]


def find_auth_file():
    candidates = auth_candidates()
    for c in candidates:
        if c and _readable(c):
            return c, candidates
    return None, candidates


def request(url, headers, body=None, timeout=REQUEST_TIMEOUT):
    if body is None:
        body = b"{}"
    elif isinstance(body, (dict, list)):
        body = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            return e.code, {}
    except Exception as e:
        return -1, {"error": str(e)}


def load_auth():
    """返回 (auth dict, None, None) 或 (None, 错误码, 中文说明)。"""
    path, candidates = find_auth_file()
    if not path:
        return None, "NO_AUTH", (
            "未找到 CodeBuddy 登录态，请先登录 CodeBuddy IDE / 插件"
            "（候选：%s）" % " | ".join(c[:120] for c in candidates if c))

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return None, "NO_AUTH", "读取登录态失败（%s: %s），请重新登录 CodeBuddy" % (
            type(e).__name__, e)

    auth = data.get("auth") or {}
    account = data.get("account") or {}
    token = auth.get("accessToken")
    if isinstance(token, dict):
        return None, "NO_AUTH", (
            "%s 中的 token 是加密信封（桌面端新版格式），请用 CodeBuddy IDE 登录一次，"
            "或设置 CODEBUDDY_AUTH_FILE 指向明文登录态" % os.path.basename(path))
    if not token:
        return None, "NO_AUTH", "登录态里没有 accessToken，请先登录 CodeBuddy"

    expires_at = auth.get("expiresAt")
    if isinstance(expires_at, (int, float)) and expires_at > 0:
        if expires_at / 1000 < datetime.now(timezone.utc).timestamp():
            return None, "NO_SESSION", (
                "accessToken 已于 %s 过期，打开 CodeBuddy IDE / 插件会自动续期"
                % datetime.fromtimestamp(expires_at / 1000, timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M"))

    return {
        "token": token,
        "uid": account.get("uid") or "",
        "domain": (auth.get("domain") or "").strip() or "www.codebuddy.cn",
        "expires_at": expires_at,
        "auth_file": os.path.basename(path),
    }, None, None


def build_headers(auth):
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer %s" % auth["token"],
        "X-Domain": auth["domain"],
        # 客户端会带；实测可省，但带上更接近真实请求
        "X-User-Id": auth["uid"],
    }
    return headers


def diag_base(auth=None):
    out = {"api_base": API_BASE, "auth_file": None, "domain": None,
           "uid_set": False, "token_expires_at": None}
    if auth:
        exp = auth.get("expires_at")
        out.update({
            "auth_file": auth.get("auth_file"),
            "domain": auth.get("domain"),
            "uid_set": bool(auth.get("uid")),
            "token_expires_at": (
                datetime.fromtimestamp(exp / 1000, timezone.utc).isoformat()
                if isinstance(exp, (int, float)) and exp > 0 else None),
        })
    return out


def api_error(sbody):
    """从响应体提取人话。"""
    if not isinstance(sbody, dict):
        return str(sbody)[:160]
    msg = sbody.get("msg") or sbody.get("message")
    if msg:
        return msg
    err = sbody.get("error")
    if isinstance(err, dict) and err.get("message"):
        return err["message"]
    return json.dumps(sbody, ensure_ascii=False)[:160]


def is_session_invalid(scode, sbody):
    if scode in SESSION_INVALID_CODES:
        return True
    if isinstance(sbody, dict) and sbody.get("code") in SESSION_INVALID_CODES:
        return True
    return False


def checkin(claim=False):
    auth, err_code, err_msg = load_auth()
    if err_code:
        return (2 if err_code == "NO_AUTH" else 1), {
            "result": err_code, "report": err_msg, "diag": diag_base()}

    base = diag_base(auth)
    headers = build_headers(auth)
    api = API_BASE

    scode, sbody = request(api + STATUS_PATH, headers)
    if scode == -1:
        return 1, {"result": "NETWORK", "diag": base,
                   "report": "网络不可达，签到跳过（%s）" % (sbody.get("error") or "")}
    if is_session_invalid(scode, sbody):
        return 1, {"result": "NO_SESSION", "diag": base,
                   "report": "登录态已失效（%s），打开 CodeBuddy IDE / 插件重新登录即可续期"
                             % api_error(sbody)}
    if scode != 200 or not isinstance(sbody, dict) or sbody.get("code") != 0:
        return 1, {"result": "ERROR", "diag": base, "body": sbody,
                   "report": "状态接口异常：%s（HTTP %s）" % (api_error(sbody), scode)}

    data = sbody.get("data") or {}
    base["streak_days"] = data.get("streak_days")
    base["theme"] = data.get("theme_name") or data.get("activity_name")
    credits = data.get("total_credits")
    streak = data.get("streak_days")
    daily = data.get("daily_credit")

    def suffix(credits_now):
        parts = []
        if streak:
            parts.append("连续 %s 天" % streak)
        if credits_now is not None:
            parts.append("当前 %s credits" % credits_now)
        return "（%s）" % "，".join(parts) if parts else ""

    if not data.get("active"):
        return 0, {"result": "INACTIVE", "diag": base, "credits": credits,
                   "report": "CodeBuddy 签到活动未开启%s" % suffix(credits)}
    if data.get("today_checked_in"):
        return 0, {"result": "ALREADY", "diag": base, "credits": credits,
                   "report": "今日已签到%s" % suffix(credits)}
    if not claim:
        return 0, {"result": "PENDING", "diag": base, "credits": credits,
                   "report": "今日未签到，可领 %s credits%s"
                             % (daily if daily is not None else "?", suffix(credits))}

    ccode, cbody = request(api + CLAIM_PATH, headers)
    if ccode == -1:
        return 1, {"result": "NETWORK", "diag": base,
                   "report": "领取请求未能送达，下次自动重试"}
    if is_session_invalid(ccode, cbody):
        return 1, {"result": "NO_SESSION", "diag": base,
                   "report": "登录态已失效（%s），打开 CodeBuddy IDE / 插件重新登录即可续期"
                             % api_error(cbody)}

    cmsg = api_error(cbody)
    if ccode == 200 and isinstance(cbody, dict) and cbody.get("code") == 0:
        cdata = cbody.get("data") or {}
        got = cdata.get("credit")
        streak = cdata.get("streak_days") or streak
        # 领取响应不含总余额，补一次状态查询；失败不影响本次结论
        ncode, nbody = request(api + STATUS_PATH, headers)
        if ncode == 200 and isinstance(nbody, dict) and nbody.get("code") == 0:
            ndata = nbody.get("data") or {}
            credits = ndata.get("total_credits", credits)
            streak = ndata.get("streak_days") or streak
        parts = []
        if streak:
            parts.append("连续 %s 天" % streak)
        if credits is not None:
            parts.append("当前 %s credits" % credits)
        tail = "（%s）" % "，".join(parts) if parts else ""
        return 0, {"result": "CLAIMED", "diag": base, "credits": credits,
                   "report": ("成功领取 %s credits" % got if got else "签到成功") + tail}

    # 并发/重复请求时服务端可能回“今日已签到”，按幂等结果处理
    if "已签到" in cmsg or "已领取" in cmsg or "already" in cmsg.lower():
        return 0, {"result": "ALREADY", "diag": base, "credits": credits,
                   "report": "今日已签到%s" % suffix(credits)}
    return 1, {"result": "ERROR", "diag": base, "body": cbody,
               "report": "领取失败：%s（HTTP %s）" % (cmsg, ccode)}


def main():
    action = sys.argv[1] if len(sys.argv) > 1 else "auto"
    try:
        if action in ("auto", "claim"):
            code, out = checkin(claim=True)
        elif action == "status":
            code, out = checkin(claim=False)
        else:
            code, out = 2, {"result": "ERROR",
                            "report": "未知命令：%s（可用：auto / status / claim）" % action}
    except Exception as e:
        code, out = 2, {"result": "ERROR",
                        "report": "脚本运行异常（%s: %s）" % (type(e).__name__, e)}
    for k in ("report",):
        if isinstance(out.get(k), str):
            out[k] = _redact_text(out[k])
    emit(out)
    return code


if __name__ == "__main__":
    sys.exit(main())
