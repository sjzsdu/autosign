#!/usr/bin/env python3
"""Qoder CN 每日签到 / 领取 credits。

原理：
  1. Qoder 桌面端把登录态写在 ~/.config/com.qodercn.app.stable/auth.v1.dat；
  2. 该文件是 Chromium/Electron safeStorage（OSCrypt）的 "v11" 封装，
     AES-128-CBC，IV 为 16 个空格；密钥由系统钥匙串里的
     "Chromium Safe Storage"（application=Qoder CN App）经
     PBKDF2-HMAC-SHA1(password, "saltysalt", 1, 16) 派生；
  3. 用 urllib 直调开放平台：
       GET  /sash/api/v1/me/campaigns                     查询活动列表
       POST /sash/api/v1/me/campaigns/<id>/claim          领取活动 benefit
       GET  /sash/api/v2/me/usage                         读取当前 credits 余额

  关键：必须带 `Cosy-ClientType: 10`。缺这个头时服务端会返回 200 + 空
  campaigns（表现为“今日暂无可领取的签到活动”），加上后才会下发
  “每天领 100 Credits”等活动。

  钥匙串通过 libsecret 的 C API（ctypes，纯标准库）读取；没有 libsecret 时
  回退 PyGObject / secret-tool，最后回退 Electron 无钥匙串时的 "peanuts"。
  解密只用 openssl 子进程，不引入第三方 Python 包。

与 Trae 的差异：
  - Authorization 是 `Bearer <token>`（Trae 是 Cloud-IDE-JWT）；
  - Qoder 的领取接口是活动 claim，而不是把通知标记已读；
  - `hasClaimableCampaign` 可能被订阅推广通知置为 true，判断是否可领必须以
    `campaigns[].actionType == "CLAIM_BENEFIT"` + `claimStatus == "CLAIMABLE"` 为准；
  - 按验收约定，Qoder 客户端未运行时直接返回 NO_AUTH（可用
    QODER_ALLOW_OFFLINE=1 跳过该检查，仅建议在确认登录态有效时使用）。

用法：
  python3 qoder_signin.py auto    # 查状态 → 可领则领（幂等，日常用这个）
  python3 qoder_signin.py status  # 仅查状态
  python3 qoder_signin.py claim   # 仅领取（幂等）

输出一行 JSON，人话在 report 字段。任何情况下都不打印 token / refreshToken。
退出码：0 正常（含 CLAIMED/ALREADY/INACTIVE/PENDING）、1 硬失败、2 凭据/用法错误。
"""

import ctypes
import ctypes.util
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone

API_BASE = os.environ.get("QODER_API_BASE", "https://openapi.qoder.com.cn").rstrip("/")
REQUEST_TIMEOUT = float(os.environ.get("QODER_TIMEOUT", "10"))
APP_VERSION = os.environ.get("QODER_APP_VERSION", "0.5.2")
# 桌面客户端的业务身份；缺省 10 对应 Qoder CN 桌面端
COSY_CLIENT_TYPE = os.environ.get("QODER_CLIENT_TYPE", "10")
KEYRING_APP = os.environ.get("QODER_KEYRING_APP", "Qoder CN App")
KEYRING_SCHEMA = "chrome_libsecret_os_crypt_password_v2"
KEYRING_SALT = b"saltysalt"
SAFE_STORAGE_IV = b" " * 16
# Chromium 在没有系统钥匙串（basic_text 后端）时使用的固定口令
BASIC_TEXT_PASSWORD = b"peanuts"

AUTH_RELS = [
    os.path.join(".config", "com.qodercn.app.stable", "auth.v1.dat"),
    os.path.join("Library", "Application Support", "com.qodercn.app.stable", "auth.v1.dat"),
]


# 输出兜底脱敏：即使异常消息意外带出登录态，也不会打印到 stdout/日志
_TOKEN_PATTERNS = [
    re.compile(r"(?i)\b(?:dt|drt)-[A-Za-z0-9._~+/=\-]{12,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    re.compile(r"(?i)(\"?(?:access_?token|refresh_?token|token)\"?\s*[:=]\s*\"?)[^\"\s,}]{8,}"),
]


def _redact_text(value):
    for pat in _TOKEN_PATTERNS:
        value = pat.sub("<redacted>", value)
    return value


def redact(obj):
    if isinstance(obj, str):
        return _redact_text(obj)
    if isinstance(obj, list):
        return [redact(x) for x in obj]
    if isinstance(obj, dict):
        return {k: redact(v) for k, v in obj.items()}
    return obj


def emit(out):
    print(json.dumps(redact(out), ensure_ascii=False, default=str), flush=True)


def _readable(path):
    try:
        with open(path, "rb") as f:
            f.read(1)
        return True
    except Exception:
        return False


def diag_base():
    return {
        "app_version": APP_VERSION,
        "api_base": API_BASE,
        "cosy_client_type": COSY_CLIENT_TYPE,
        "keyring_app": KEYRING_APP,
    }


# ------------------------------------------------------------------ 进程检查

def qoder_running():
    """检查 Qoder 主进程是否在运行。

    只认主进程：排除 Electron 的 --type= 子进程、SDK worker、MCP daemon 等，
    避免残留辅助进程被误判成客户端已打开。QODER_PROCESS_MATCH 便于测试/定制。
    """
    if os.environ.get("QODER_ALLOW_OFFLINE", "").lower() in ("1", "true", "yes", "on"):
        return True
    pattern = os.environ.get("QODER_PROCESS_MATCH", "qoder-cn")
    skip_flags = ("--type=", "--eval", "--sdk", "--utility-sub-type=",
                  "mcp-bridge", "daemon-server", "qoder-worker-runtime")
    try:
        pids = os.listdir("/proc")
    except Exception:
        return False
    for pid in pids:
        if not pid.isdigit():
            continue
        try:
            with open("/proc/%s/cmdline" % pid, "rb") as f:
                raw = f.read()
        except Exception:
            continue
        argv = [a for a in raw.split(b"\0") if a]
        if not argv:
            continue
        # 只认可执行文件名匹配，避免把 shell/命令行里恰好出现关键字的进程当成客户端
        exe = os.path.basename(argv[0].decode("utf-8", "replace"))
        if pattern not in exe:
            continue
        cmd = raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()
        if any(flag in cmd for flag in skip_flags):
            continue
        return True
    return False


# ------------------------------------------------------------------ 登录态

def find_auth_file():
    override = os.environ.get("QODER_AUTH_FILE")
    if override:
        return override if _readable(override) else None
    home = os.path.expanduser("~")
    candidates = []
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        candidates.append(os.path.join(xdg, "com.qodercn.app.stable", "auth.v1.dat"))
    for rel in AUTH_RELS:
        candidates.append(os.path.join(home, rel))
    appdata = os.environ.get("APPDATA")  # Windows
    if appdata:
        candidates.append(os.path.join(appdata, "com.qodercn.app.stable", "auth.v1.dat"))
    seen = set()
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            if _readable(c):
                return c
    return None


def _libsecret_lookup(app_name):
    """通过 libsecret 的 C API 读取 Chromium Safe Storage 口令（纯标准库 ctypes）。"""
    try:
        libname = ctypes.util.find_library("secret-1") or "libsecret-1.so.0"
        lib = ctypes.CDLL(libname)
    except Exception:
        return None

    class _Attr(ctypes.Structure):
        _fields_ = [("name", ctypes.c_char_p), ("type", ctypes.c_int)]

    class _Schema(ctypes.Structure):
        _fields_ = [("name", ctypes.c_char_p), ("flags", ctypes.c_int),
                    ("attributes", _Attr * 32)]

    schema = _Schema()
    schema.name = KEYRING_SCHEMA.encode()
    schema.flags = 0
    schema.attributes[0].name = b"application"
    schema.attributes[0].type = 0  # SECRET_SCHEMA_ATTRIBUTE_STRING

    fn = lib.secret_password_lookup_sync
    fn.restype = ctypes.c_void_p
    fn.argtypes = [ctypes.POINTER(_Schema), ctypes.c_void_p, ctypes.c_void_p]
    try:
        res = fn(ctypes.byref(schema), None, None,
                 b"application", app_name.encode("utf-8"), None)
    except Exception:
        return None
    if not res:
        return None
    try:
        pw = ctypes.string_at(res)
    finally:
        try:
            lib.secret_password_free.argtypes = [ctypes.c_void_p]
            lib.secret_password_free(res)
        except Exception:
            pass
    return pw or None


def _gi_lookup(app_name):
    """PyGObject 回退（很多 GNOME 桌面自带 python3-gi，但不是标准库）。"""
    try:
        import gi
        gi.require_version("Secret", "1")
        from gi.repository import Secret
        schema = Secret.Schema.new(
            KEYRING_SCHEMA, Secret.SchemaFlags.NONE,
            {"application": Secret.SchemaAttributeType.STRING})
        value = Secret.password_lookup_sync(schema, {"application": app_name}, None)
        return value.encode("utf-8") if value else None
    except Exception:
        return None


def _secret_tool_lookup(app_name):
    """secret-tool 回退（libsecret 的命令行封装）。"""
    try:
        proc = subprocess.run(
            ["secret-tool", "lookup", "application", app_name],
            capture_output=True, timeout=5)
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except Exception:
        pass
    return None


def get_keyring_password():
    """返回 (password_bytes, source) 或 (None, None)。"""
    for source, fn in (("libsecret", _libsecret_lookup),
                       ("gi", _gi_lookup),
                       ("secret-tool", _secret_tool_lookup)):
        try:
            pw = fn(KEYRING_APP)
        except Exception:
            pw = None
        if pw:
            return pw, source
    return None, None


def _openssl_decrypt_aes_cbc(data, key, iv):
    proc = subprocess.run(
        ["openssl", "enc", "-d", "-aes-128-cbc", "-nopad",
         "-K", key.hex(), "-iv", iv.hex()],
        input=data, capture_output=True, timeout=10)
    if proc.returncode != 0:
        raise RuntimeError("openssl 解密失败: %s" %
                           proc.stderr.decode("utf-8", "replace")[:120])
    return proc.stdout


def decrypt_auth(path, password):
    """解密 auth.v1.dat，返回 JSON dict；失败抛异常。"""
    with open(path, "rb") as f:
        data = f.read()
    if data[:8] == b"QASSET1\0":
        raise RuntimeError("检测到 HarmonyOS QASSET1 信封，Linux 端不支持（无需使用）")
    if data[:3] not in (b"v10", b"v11"):
        raise RuntimeError("未知的登录态格式（magic=%r）" % data[:3])

    # Chromium OSCrypt（Linux）：AES-128-CBC，key=PBKDF2-SHA1(pw,"saltysalt",1,16)，
    # IV 固定为 16 个空格，密文前 3 字节是版本号。
    key = hashlib.pbkdf2_hmac("sha1", password, KEYRING_SALT, 1, 16)
    pt = _openssl_decrypt_aes_cbc(data[3:], key, SAFE_STORAGE_IV)
    if pt:
        pad = pt[-1]
        if 1 <= pad <= 16 and pt.endswith(bytes([pad]) * pad):
            pt = pt[:-pad]
    if not pt.lstrip().startswith(b"{"):
        raise RuntimeError("解密结果不是 JSON（口令/格式不匹配）")
    try:
        return json.loads(pt.decode("utf-8", "replace"))
    except Exception:
        # 不要让 JSONDecodeError 把包含 token 的明文片段带进 report
        raise RuntimeError("解密结果不是有效 JSON（口令/格式不匹配）")


def load_auth():
    """返回 (auth, source, path, None) 或 (None, None, path, (code, msg))。"""
    path = find_auth_file()
    if not path:
        return None, None, None, (
            "NO_AUTH",
            "未找到 Qoder 登录态 auth.v1.dat，请打开 Qoder CN 客户端并登录后重试")

    password, source = get_keyring_password()
    tried = []
    candidates = []
    if password:
        candidates.append((password, "keyring:%s" % source))
    # 没有钥匙串时 Electron 会退回 basic_text（固定口令 peanuts）
    candidates.append((BASIC_TEXT_PASSWORD, "basic_text"))

    last_err = None
    for pw, src in candidates:
        if any(pw == old for old, _ in tried):
            continue
        tried.append((pw, src))
        try:
            auth = decrypt_auth(path, pw)
            return auth, src, path, None
        except Exception as e:
            last_err = e
    msg = "无法读取 Qoder 登录态（%s）" % (last_err or "钥匙串不可用")
    if password is None:
        msg += "；系统钥匙串里没有找到 Chromium Safe Storage，请确认已登录桌面会话且 Qoder 保持登录"
    else:
        msg += "；请重新登录 Qoder CN 客户端"
    return None, None, path, ("NO_AUTH", msg)


def _expired(expires_at):
    if not expires_at:
        return False
    try:
        exp = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return exp < datetime.now(timezone.utc)
    except Exception:
        return False


# ------------------------------------------------------------------ HTTP

def _headers(token):
    return {
        "Authorization": "Bearer %s" % token,
        "X-Request-ID": str(uuid.uuid4()),
        "Accept": "application/json",
        "User-Agent": "Qoder/%s" % APP_VERSION,
        # 缺这个头服务端会隐藏活动列表（200 + campaigns:[]）
        "Cosy-ClientType": COSY_CLIENT_TYPE,
        "Cosy-Version": APP_VERSION,
    }


def request_json(method, path, token, body=None, timeout=None):
    """返回 (status, dict)；status=-1 表示网络错误。绝不返回/打印 token。"""
    url = path if path.startswith("http") else API_BASE + path
    headers = _headers(token)
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout or REQUEST_TIMEOUT) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw.decode("utf-8", "replace")) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            return e.code, {}
    except Exception as e:
        return -1, {"error": "%s: %s" % (type(e).__name__, e)}


def fetch_credits(token):
    """返回 (credits, source) 或 (None, None)。"""
    code, body = request_json("GET", "/sash/api/v2/me/usage", token)
    if code == 200 and isinstance(body, dict):
        usage = body.get("qoderUsage") if isinstance(body.get("qoderUsage"), dict) else body
        total = 0.0
        found = False
        for key in ("userQuota", "addOnQuota"):
            quota = usage.get(key)
            if isinstance(quota, dict):
                val = quota.get("remaining")
                if isinstance(val, (int, float)):
                    total += float(val)
                    found = True
        if found:
            return (int(total) if float(total).is_integer() else round(total, 2)), "usage"
    # 兜底：近一年累计 credits（不是余额，仅用于 report 参考）
    code, body = request_json("GET", "/sash/api/v1/ai-conversations/credits-summary", token)
    if code == 200 and isinstance(body, dict):
        val = body.get("totalCredits")
        if isinstance(val, (int, float)):
            return (int(val) if float(val).is_integer() else round(val, 2)), "summary"
    return None, None


# ------------------------------------------------------------------ 活动/签到

def _campaign_list(body):
    items = body.get("campaigns") if isinstance(body, dict) else None
    if not isinstance(items, list):
        return []
    return [c for c in items if isinstance(c, dict)]


def _campaign_id(c):
    for key in ("campaignId", "id"):
        val = c.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _campaign_key(c):
    val = c.get("campaignKey")
    return val.strip() if isinstance(val, str) and val.strip() else None


def _claim_status(c):
    return str(c.get("claimStatus") or "").strip().upper()


def _campaign_active(c):
    """endAt 是秒级时间戳；已结束的活动不参与今日判断。"""
    end = c.get("endAt")
    if isinstance(end, (int, float)) and not isinstance(end, bool):
        return end > time.time()
    return True


def _is_credit_campaign(c):
    """只把 CLAIM_BENEFIT + CREDITS 的活动当作签到，忽略订阅推广。"""
    if str(c.get("actionType") or "").strip().upper() != "CLAIM_BENEFIT":
        return False
    benefit = c.get("benefit")
    if isinstance(benefit, dict):
        kind = str(benefit.get("kind") or "").strip().upper()
        if kind and kind != "CREDITS":
            return False
    return True


def _campaign_amount(c):
    benefit = c.get("benefit")
    if not isinstance(benefit, dict):
        return None
    amount = benefit.get("amount")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        return None
    return amount


def _campaign_label(c):
    placements = c.get("placements")
    if isinstance(placements, list):
        for p in placements:
            content = p.get("content") if isinstance(p, dict) else None
            if not isinstance(content, dict):
                continue
            for lang in ("zh", "en"):
                block = content.get(lang)
                if isinstance(block, dict) and isinstance(block.get("title"), str):
                    title = block["title"].strip()
                    if title:
                        return title
    return _campaign_key(c) or "签到活动"


def _amount_text(amount):
    return "%s credits" % amount if amount else "credits"


def _state_path():
    return os.environ.get("QODER_STATE_FILE") or os.path.join(
        os.path.expanduser("~"), ".cache", "autosign", "qoder.json")


def _today():
    return datetime.now().astimezone().date().isoformat()


def load_state():
    try:
        with open(_state_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_state(amount, credits, campaign_keys):
    path = _state_path()
    try:
        os.makedirs(os.path.dirname(path) or ".", mode=0o700, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"date": _today(),
                       "claimedAt": datetime.now().astimezone().isoformat(),
                       "amount": amount,
                       "credits": credits,
                       "campaignKeys": campaign_keys}, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        pass


def _report(tail, credits, source=None):
    if credits is None:
        return tail
    if source == "summary":
        return "%s（近一年累计 credits %s）" % (tail, credits)
    return "%s（当前 credits %s）" % (tail, credits)


def checkin(claim):
    base = diag_base()
    auth, source, path, err = load_auth()
    if err:
        code, msg = err
        base["auth_file"] = path
        return 2, {"result": code, "report": msg, "diag": base}

    token = auth.get("token")
    if not token:
        base["auth_file"] = path
        return 2, {"result": "NO_AUTH",
                   "report": "Qoder 登录态里没有 token，请打开客户端重新登录",
                   "diag": base}
    if _expired(auth.get("expiresAt")):
        base["auth_file"] = path
        base["expires"] = str(auth.get("expiresAt"))[:10]
        return 2, {"result": "NO_SESSION",
                   "report": "Qoder 登录态已过期，请打开 Qoder CN 客户端重新登录",
                   "diag": base}

    base.update({
        "auth_file": path,
        "auth_source": source,
        "expires": str(auth.get("expiresAt") or "")[:10],
    })

    code, body = request_json("GET", "/sash/api/v1/me/campaigns", token)
    if code == -1:
        return 1, {"result": "NETWORK",
                   "report": "网络不可达，Qoder 签到跳过（%s），请检查网络后重试" %
                             body.get("error", ""), "diag": base}
    if code in (401, 403):
        return 2, {"result": "NO_SESSION",
                   "report": "Qoder 登录态已失效（HTTP %s），请打开客户端重新登录" % code,
                   "diag": base}
    if code != 200 or not isinstance(body, dict):
        return 1, {"result": "ERROR",
                   "report": "Qoder 活动接口异常（HTTP %s），请稍后重试；"
                             "若持续失败请打开客户端确认登录状态" % code,
                   "diag": base}

    campaigns = _campaign_list(body)
    credit_campaigns = [c for c in campaigns if _is_credit_campaign(c) and _campaign_active(c)]
    claimable = [c for c in credit_campaigns if _claim_status(c) == "CLAIMABLE"]
    claimed = [c for c in credit_campaigns
               if _claim_status(c) in ("CLAIMED", "DONE", "FINISHED", "COMPLETED")]
    base.update({
        "campaigns": len(campaigns),
        "credit_campaigns": len(credit_campaigns),
        "claimable": len(claimable),
        "claimed": len(claimed),
        "show_campaign": body.get("showCampaign") is True,
    })

    credits, credits_source = fetch_credits(token)
    if credits_source:
        base["credits_source"] = credits_source

    state = load_state()
    claimed_amount = sum((_campaign_amount(c) or 0) for c in claimed)
    claimable_amount = sum((_campaign_amount(c) or 0) for c in claimable)

    if not claimable:
        # 活动已领（campaigns 里有 CLAIMED 的签到活动）或本地记录今天领过
        if claimed or state.get("date") == _today():
            amount = claimed_amount or state.get("amount")
            tail = "今日已签到"
            if amount:
                tail += "，已领取 %s" % _amount_text(amount)
            if credits is None and isinstance(state.get("credits"), (int, float)):
                credits = state["credits"]
            return 0, {"result": "ALREADY",
                       "report": _report(tail, credits, credits_source),
                       "credits": credits, "diag": base}
        return 0, {"result": "INACTIVE",
                   "report": _report("今日暂无可领取的签到活动", credits, credits_source),
                   "credits": credits, "diag": base}

    if not claim:
        tail = "今日可签到但尚未领取"
        if claimable_amount:
            tail += "，可领 %s" % _amount_text(claimable_amount)
        return 0, {"result": "PENDING",
                   "report": _report(tail, credits, credits_source),
                   "credits": credits, "diag": base}

    errors = []
    replayed_any = False
    newly_claimed = 0
    claimed_keys = []
    for c in claimable:
        cid = _campaign_id(c)
        label = _campaign_label(c)
        if not cid:
            errors.append("%s 缺少 campaignId" % label)
            continue
        url = "/sash/api/v1/me/campaigns/%s/claim" % urllib.parse.quote(cid, safe="")
        code, cbody = request_json("POST", url, token, body={})
        if code == -1:
            errors.append("%s 请求未送达（%s）" %
                          (label, cbody.get("error", "") if isinstance(cbody, dict) else ""))
            continue
        if code in (401, 403):
            return 2, {"result": "NO_SESSION",
                       "report": "Qoder 登录态已失效（HTTP %s），请打开客户端重新登录" % code,
                       "diag": base}
        if code in (404, 409):
            replayed_any = True
            continue
        if code not in (200, 201, 202, 204):
            detail = ""
            if isinstance(cbody, dict):
                detail = str(cbody.get("errorMessage") or cbody.get("message") or "")[:120]
            errors.append("%s HTTP %s%s" % (label, code, ("：" + detail) if detail else ""))
            continue
        status = str(cbody.get("status") or "").upper() if isinstance(cbody, dict) else ""
        if status and status not in ("CLAIMED", "SUCCESS", "OK"):
            errors.append("%s 返回 %s" % (label, status))
            continue
        if isinstance(cbody, dict) and cbody.get("replayed") is True:
            replayed_any = True
            continue
        amount = None
        if isinstance(cbody, dict) and isinstance(cbody.get("benefit"), dict):
            amount = cbody["benefit"].get("amount")
        if not isinstance(amount, (int, float)) or isinstance(amount, bool):
            amount = _campaign_amount(c) or 0
        newly_claimed += amount
        key = _campaign_key(c)
        if key:
            claimed_keys.append(key)

    if newly_claimed or claimed_keys:
        credits2, source2 = fetch_credits(token)
        if credits2 is not None:
            credits, credits_source = credits2, source2
            base["credits_source"] = credits_source
        save_state(newly_claimed, credits, claimed_keys)
        base["claimable"] = 0
        base["claimed"] = len(claimed) + len(claimable)
        tail = "成功领取 %s" % _amount_text(newly_claimed) if newly_claimed else "签到成功"
        return 0, {"result": "CLAIMED",
                   "report": _report(tail, credits, credits_source),
                   "credits": credits, "diag": base}

    if errors:
        return 1, {"result": "ERROR",
                   "report": "Qoder 领取失败：%s；请打开客户端活动页确认，或稍后重试" %
                             "；".join(errors[:2]),
                   "diag": base}

    if replayed_any:
        return 0, {"result": "ALREADY",
                   "report": _report("今日已签到", credits, credits_source),
                   "credits": credits, "diag": base}

    return 0, {"result": "ALREADY",
               "report": _report("今日已签到", credits, credits_source),
               "credits": credits, "diag": base}


def main():
    action = sys.argv[1] if len(sys.argv) > 1 else "auto"
    try:
        if action not in ("auto", "status", "claim"):
            code, out = 2, {"result": "ERROR",
                            "report": "未知命令：%s（可用：auto / status / claim）" % action}
        elif not qoder_running():
            code, out = 2, {
                "result": "NO_AUTH",
                "report": "未检测到 Qoder CN 客户端在运行，请打开 Qoder 客户端并登录后重试"
                          "（如确认登录态有效，可设置 QODER_ALLOW_OFFLINE=1 跳过客户端检查）",
                "diag": diag_base(),
            }
        else:
            code, out = checkin(claim=(action in ("auto", "claim")))
    except Exception as e:
        code, out = 1, {"result": "ERROR",
                        "report": "Qoder 脚本运行异常（%s: %s），请重试；若持续失败请打开 Qoder 客户端确认登录状态" % (type(e).__name__, e),
                        "diag": diag_base()}
    emit(out)
    return code


if __name__ == "__main__":
    sys.exit(main())
