#!/usr/bin/env python3
"""Trae CN 每日签到自动领取脚本（每日 200 积分）。

原理：读取 Trae 桌面端 storage.json 里的加密登录态（iCubeAuthInfo://icube.cloudide），
AES-128-CBC 解密（SHA-512 密钥派生）得到 accessToken，直调官方接口：
  POST https://api.trae.cn/trae/api/v2/ug/checkin_credits/status  查询签到状态
  POST https://api.trae.cn/trae/api/v2/ug/checkin_credits/claim   领取今日积分

解密布局（与 Trae 论坛公开 skill 交叉验证一致）：
  blob = base64(value)；key=blob[6:38] → sha512(key) 与两张硬编码表 XOR 结果拼接
  → 再 sha512 → 前 16B=AES key，16-32B=IV；密文 blob[38:]，明文跳过前 64B 即 JSON。

AES 解密通过 openssl 子进程完成，避免引入 cryptography 依赖（Ubuntu/macOS 自带）。

--- 本文件相对 agent-auto-signin/scripts/trae_signin.py 的关键修复（Linux 适配）---
1. x-device-id 用错 → 服务端风控直接返回 9074「当前参与用户太多」。
   真实客户端取 iCubeDeviceRegisterMainService.guaranteedDeviceId，
   即 storage.json 里 `iCubeAuthInfo://icube-dc:<数字ID>` 的后缀；
   旧脚本取 telemetry.devDeviceId（形如 42b61129-... 的 UUID），与客户端不一致。
2. x-device-type / x-os-version / x-device-brand / x-app-version 按 Linux 真实客户端对齐：
   - x-device-type = os_name        → "linux"（不是硬编码 "macOS"）
   - x-os-version  = /etc/os-release 的 PRETTY_NAME（不是内核版本号）
   - x-device-brand= /sys/class/dmi/id/product_name（不是 platform.machine()）
   - x-app-version = product.json 的 appVersion（Linux 路径 /usr/share/trae-cn/...，
     旧脚本只认 macOS 的 /Applications，读不到就退回过期的 3.4.3）
3. storage.json / product.json 增加 Linux、Windows 路径探测。

用法：
  python3 trae_signin.py auto    # 查状态 → 未签才领（幂等，日常用这个）
  python3 trae_signin.py status  # 仅查状态
  python3 trae_signin.py claim   # 仅领取（幂等）

输出一行 JSON，人话在 report 字段。任何情况下都不打印 token。
退出码：0 正常（含 ALREADY/INACTIVE）、1 硬失败、2 凭据/用法错误。
"""

import base64
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

# 密钥派生用的两张硬编码表（源自 Trae 客户端实现）
TABLE_A = bytes([82, 9, 106, 213, 48, 54, 165, 56, 191, 64, 163, 158, 129, 243, 215, 251,
                 124, 227, 57, 130, 155, 47, 255, 135, 52, 142, 67, 68, 196, 222, 233, 203,
                 84, 123, 148, 50, 166, 194, 35, 61, 238, 76, 149, 11, 66, 250, 195, 78,
                 8, 46, 161, 102, 40, 217, 36, 178, 118, 91, 162, 73, 109, 139, 209, 37])
TABLE_B = bytes([31, 221, 168, 51, 136, 7, 199, 49, 177, 18, 16, 89, 39, 128, 236, 95,
                 96, 81, 127, 169, 25, 181, 74, 13, 45, 229, 122, 159, 147, 201, 156, 239,
                 160, 224, 59, 77, 174, 42, 245, 176, 200, 235, 187, 60, 131, 83, 153, 97,
                 23, 43, 4, 126, 186, 119, 214, 38, 225, 105, 20, 99, 85, 33, 12, 125])

STORAGE_RELS = [
    os.path.join("Trae CN", "User", "globalStorage", "storage.json"),
    os.path.join("TRAE SOLO CN", "User", "globalStorage", "storage.json"),  # 2026-10-14 前的旧名
]

# 与真实客户端保持一致：ttnet 里 checkin 请求的 timeout 是 3e4 ms，
# 这里的 x-rust-request-timeout 头会带上同一个值。
REQUEST_TIMEOUT = 30

# product.json 位置（动态读取 appVersion / packageType）
APP_JSON_CANDIDATES = [
    # Linux（deb 包）
    "/usr/share/trae-cn/resources/app/product.json",
    "/opt/Trae-CN/resources/app/product.json",
    "/usr/lib/trae-cn/resources/app/product.json",
    # macOS
    "/Applications/Trae CN.app/Contents/Resources/app/product.json",
    "/Applications/TRAE SOLO CN.app/Contents/Resources/app/product.json",
]
if os.environ.get("LOCALAPPDATA"):  # Windows
    for _p in ("Trae CN", "TRAE SOLO CN"):
        APP_JSON_CANDIDATES.append(
            os.path.join(os.environ["LOCALAPPDATA"], "Programs", _p, "resources", "app", "product.json"))

DEFAULT_APP_VERSION = "3.4.4"
SOLO_PACKAGE_TYPES = {"SOLO_CN", "SOLO_I18N", "SOLO_CN_ENTERPRISE"}
# 9074 是服务端风控/容量护栏，参数对齐后仍可能偶发，用拉长的退避碰运气
BACKOFF_SECONDS = (0, 5, 20, 45)


def emit(out):
    print(json.dumps(out, ensure_ascii=False, default=str), flush=True)


def _readable(path):
    """试读 1 字节代替 exists() —— 兼容部分 Agent 沙箱对受保护路径的假阴性。"""
    try:
        with open(path, "rb") as f:
            f.read(1)
        return True
    except Exception:
        return False


def load_product_json():
    p = os.environ.get("TRAE_PRODUCT_JSON")
    for c in ([p] if p else []) + APP_JSON_CANDIDATES:
        if c and _readable(c):
            try:
                with open(c, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                continue
    return {}


_PRODUCT = load_product_json()
APP_VERSION = os.environ.get("TRAE_APP_VERSION") or _PRODUCT.get("appVersion") or DEFAULT_APP_VERSION
PACKAGE_TYPE = os.environ.get("TRAE_PACKAGE_TYPE") or _PRODUCT.get("packageType") or ""


def find_storage():
    """返回 (路径, 候选列表)。TRAE_STORAGE_FILE 一旦设置即为唯一候选（便于排查）。"""
    override = os.environ.get("TRAE_STORAGE_FILE")
    if override:
        candidates = [override]
        for c in candidates:
            if _readable(c):
                return c, candidates
        return None, candidates

    home = os.path.expanduser("~")
    candidates = []
    for rel in STORAGE_RELS:
        if os.name == "nt":  # Windows
            appdata = os.environ.get("APPDATA", "")
            if appdata:
                candidates.append(os.path.join(appdata, rel))
        else:
            # Linux（deb 包 / snap）
            candidates.append(os.path.join(home, ".config", rel))
            # macOS
            candidates.append(os.path.join(home, "Library", "Application Support", rel))
    for c in candidates:
        if c and _readable(c):
            return c, candidates
    return None, candidates


def decrypt_auth(b64_value):
    """解密 iCubeAuthInfo blob，返回 JSON dict；失败抛异常。"""
    blob = base64.b64decode(b64_value)
    sha = hashlib.sha512(blob[6:38]).digest()
    xor = bytes(a ^ b for a, b in zip(TABLE_A, TABLE_B))
    h = hashlib.sha512(sha + xor).digest()
    key_hex, iv_hex = h[:16].hex(), h[16:32].hex()
    proc = subprocess.run(
        ["openssl", "enc", "-d", "-aes-128-cbc", "-nopad", "-K", key_hex, "-iv", iv_hex],
        input=blob[38:], capture_output=True, timeout=10)
    if proc.returncode != 0:
        raise RuntimeError("openssl 解密失败: %s" % proc.stderr.decode(errors="replace")[:120])
    pt = proc.stdout
    pt = pt[:-pt[-1]]  # 去 PKCS7 padding
    return json.loads(pt[64:].decode("utf-8", errors="replace"))


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


# ---------------------------------------------------------------- device 信息

def _os_release_field(field):
    """读取 /etc/os-release 的字段（Linux 客户端 osVersion 即 PRETTY_NAME）。"""
    for path in ("/etc/os-release", "/usr/lib/os-release"):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if line.startswith(field + "="):
                        return line.split("=", 1)[1].strip().strip('"')
        except Exception:
            continue
    return ""


def os_name():
    """客户端 commonParams.os_name = process.platform 的映射（darwin→mac, win32→windows）。"""
    return {"darwin": "mac", "win32": "windows"}.get(sys.platform, sys.platform)


def os_version():
    """客户端 commonParams.os_version：
    - macOS: `macOS <release>`（sw_vers 拼接）
    - Windows: 系统版本号
    - Linux: /etc/os-release 的 PRETTY_NAME（如 "Ubuntu 24.04.5 LTS"）"""
    if sys.platform == "darwin":
        try:
            rel = subprocess.run(["sw_vers", "-productVersion"], capture_output=True,
                                 text=True, timeout=3).stdout.strip()
            if rel:
                return "macOS %s" % rel
        except Exception:
            pass
        return "macOS %s" % (platform.mac_ver()[0] or platform.release())
    if sys.platform.startswith("linux"):
        return _os_release_field("PRETTY_NAME") or "Linux"
    # Windows
    return " ".join(platform.win32_ver()[:2]) or platform.release()


def device_model():
    """客户端 commonParams.device_model（systeminformation 的 osInfo().model）。"""
    override = os.environ.get("TRAE_DEVICE_MODEL")
    if override:
        return override
    if sys.platform == "darwin":
        try:
            v = subprocess.run(["sysctl", "-n", "hw.model"], capture_output=True,
                               text=True, timeout=3).stdout.strip()
            if v:
                return v
        except Exception:
            pass
        return platform.machine() or "Mac"
    if sys.platform.startswith("linux"):
        for p in ("/sys/class/dmi/id/product_name",
                  "/sys/class/dmi/id/board_name",
                  "/sys/class/dmi/id/product_version"):
            try:
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    v = f.read().strip()
                if v and v.lower() not in ("none", "to be filled by o.e.m.", "default string"):
                    return v
            except Exception:
                continue
        # 无 DMI（容器/虚拟机）时退回硬件架构
        return platform.machine() or "Linux"
    return platform.machine() or "Windows"


def get_device_id(storage_path):
    """x-device-id 必须等于客户端的 guaranteedDeviceId。

    优先级：
      1. storage.json 的 `iCubeAuthInfo://icube-dc:<数字ID>` —— 真实客户端用的值
         （注意：不是 telemetry.devDeviceId！后者是 VSCode 的 UUID，发上去会被风控判 9074）
      2. macOS 的 @vscode/deviceid 缓存文件
      3. 回退 telemetry.devDeviceId（最后兜底）
    """
    if storage_path and _readable(storage_path):
        try:
            with open(storage_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for key in data:
                if key.startswith("iCubeAuthInfo://icube-dc:"):
                    did = key.split("icube-dc:", 1)[1]
                    if did:
                        return did
        except Exception:
            pass

    candidates = [
        os.path.join(os.path.expanduser("~"), "Library", "Application Support",
                     "Microsoft", "DeveloperTools", "deviceid"),
        os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "DeveloperTools", "deviceid"),
    ]
    for c in candidates:
        if c and _readable(c):
            try:
                v = open(c, "r", encoding="utf-8").read().strip()
                if v:
                    return v
            except Exception:
                pass

    if storage_path and _readable(storage_path):
        try:
            with open(storage_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            did = data.get("telemetry.devDeviceId")
            if did:
                return did
        except Exception:
            pass
    return None


def detect_req_source():
    """客户端 `req_source = packageType in (SOLO_CN, SOLO_I18N, SOLO_CN_ENTERPRISE) ? 2 : 1`。
    更名 Trae CN 后 packageType=TRAE_CN → 应发 1。"""
    override = os.environ.get("TRAE_REQ_SOURCE")
    if override:
        try:
            return int(override)
        except Exception:
            pass
    return 2 if PACKAGE_TYPE in SOLO_PACKAGE_TYPES else 1


def build_headers(token, region, storage_path):
    """按客户端 `_buildRawHeaders` + `mixAuthorization` + `_applyUgDeviceHeaders` 还原。"""
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Cloud-IDE-JWT %s" % token,
    }
    if region:
        headers["X-User-Region"] = region
    did = get_device_id(storage_path)
    if did:
        headers["x-device-id"] = did
    headers["x-device-brand"] = device_model()
    headers["x-device-type"] = os_name()
    headers["x-os-version"] = os_version()
    headers["x-app-version"] = APP_VERSION
    # ttnet 传输层会带的头，带上更接近真实客户端
    headers["x-rust-request-timeout"] = str(REQUEST_TIMEOUT * 1000)
    return headers


def diag():
    return {
        "app_version": APP_VERSION,
        "package_type": PACKAGE_TYPE,
        "req_source": detect_req_source(),
        "os_name": os_name(),
        "device_model": device_model(),
    }


# ------------------------------------------------------------------ 业务逻辑

def load_auth():
    """返回 ((token, region, host, expired_at), None, None) 或 (None, 错误码, 中文说明)。"""
    storage, candidates = find_storage()
    if not storage:
        return None, "NO_AUTH", ("未找到 Trae 的 storage.json，候选路径均不可读，"
                                 "请先登录 Trae 桌面端（候选：%s）" % " | ".join(c[:120] for c in candidates if c))
    try:
        with open(storage, "r", encoding="utf-8") as f:
            data = json.load(f)
        enc = data.get("iCubeAuthInfo://icube.cloudide")
        if not enc:
            return None, "NO_AUTH", "storage.json 中无登录态，请先登录 Trae 桌面端"
        auth = decrypt_auth(enc)
    except Exception as e:
        return None, "NO_AUTH", "解密登录态失败（%s: %s），请重新登录 Trae 桌面端" % (type(e).__name__, e)

    token = auth.get("token")
    if not token:
        return None, "NO_SESSION", "登录态中无 token，请重新登录 Trae 桌面端"
    exp = auth.get("expiredAt")
    if exp:
        try:
            exp_dt = datetime.fromisoformat(exp.replace("Z", "+00:00"))
            if exp_dt < datetime.now(timezone.utc):
                return None, "NO_SESSION", "token 已过期（%s），打开 Trae 客户端会自动刷新" % exp[:10]
        except Exception:
            pass
    region = "CN"
    if isinstance(auth.get("userRegion"), dict) and auth["userRegion"].get("region"):
        region = auth["userRegion"]["region"]
    host = (auth.get("host") or "https://api.trae.cn").rstrip("/")
    return (token, region, host, exp), None, None


def checkin(claim=False):
    loaded, err_code, err_msg = load_auth()
    if err_code:
        return (2 if err_code == "NO_AUTH" else 1), {"result": err_code, "report": err_msg}

    token, region, host, _exp = loaded
    storage_path, _ = find_storage()
    headers = build_headers(token, region, storage_path)
    body = {"req_source": detect_req_source()}
    base = diag()
    base["device_id_set"] = bool(headers.get("x-device-id"))

    if not base["device_id_set"]:
        return 2, {"result": "NO_AUTH",
                   "report": "未取到 x-device-id（storage.json 里没有 icube-dc 记录），"
                             "请先登录 Trae 桌面端", "diag": base}

    scode, sbody = request(host + "/trae/api/v2/ug/checkin_credits/status", headers, body)
    if scode == -1:
        return 1, {"result": "NETWORK", "report": "网络不可达，签到跳过（%s）" % sbody.get("error", ""), "diag": base}
    if scode in (401, 403):
        return 1, {"result": "NO_SESSION", "report": "登录态已失效（HTTP %s），请重新登录 Trae 桌面端" % scode, "diag": base}
    if scode != 200 or not isinstance(sbody, dict):
        return 1, {"result": "ERROR", "report": "状态接口异常（HTTP %s）" % scode,
                   "body": sbody, "diag": base}
    if not sbody.get("enable"):
        return 0, {"result": "INACTIVE", "report": "Trae 签到活动未开启", "diag": base}

    credits_now = sbody.get("credits")
    suffix = "（当前积分 %s）" % credits_now if credits_now is not None else ""

    if sbody.get("checked_in"):
        return 0, {"result": "ALREADY", "report": "今日已签过%s" % suffix,
                   "credits": credits_now, "diag": base}
    if not claim:
        return 0, {"result": "PENDING", "report": "今日未签到%s" % suffix,
                   "credits": credits_now, "diag": base}

    ccode, cbody = request(host + "/trae/api/v2/ug/checkin_credits/claim", headers, body)
    attempts = 1
    while (ccode == 200 and isinstance(cbody, dict) and cbody.get("code") == 9074
           and attempts < len(BACKOFF_SECONDS)):
        time.sleep(BACKOFF_SECONDS[attempts])
        ccode, cbody = request(host + "/trae/api/v2/ug/checkin_credits/claim", headers, body)
        attempts += 1

    if ccode == -1:
        return 1, {"result": "NETWORK", "report": "领取请求未能送达，下次自动重试", "diag": base}
    if ccode in (401, 403):
        return 1, {"result": "NO_SESSION", "report": "登录态已失效（HTTP %s），请重新登录 Trae 桌面端" % ccode, "diag": base}
    if isinstance(cbody, dict) and cbody.get("code") == 0:
        got = (cbody.get("data") or {}).get("credits") if isinstance(cbody.get("data"), dict) else None
        tail = "（当前积分 %s）" % (got if got is not None else credits_now) \
            if (got is not None or credits_now is not None) else ""
        return 0, {"result": "CLAIMED",
                   "report": ("成功领取 %s 积分" % got if got else "签到成功") + tail,
                   "credits": got if got is not None else credits_now, "diag": base}

    msg = (cbody.get("message") or json.dumps(cbody, ensure_ascii=False)[:160]) \
        if isinstance(cbody, dict) else str(cbody)
    if isinstance(cbody, dict) and cbody.get("code") == 9074:
        return 1, {"result": "ERROR",
                   "report": "领取失败：%s（已重试 %d 次，服务端限流/风控；本次参数 app_version=%s "
                             "package_type=%s req_source=%s）"
                             % (msg, attempts, APP_VERSION, PACKAGE_TYPE or "-",
                                detect_req_source()),
                   "diag": base}
    return 1, {"result": "ERROR", "report": "领取失败：%s（HTTP %s）" % (msg, ccode),
               "body": cbody, "diag": base}


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
        code, out = 2, {"result": "ERROR", "report": "脚本运行异常（%s: %s）" % (type(e).__name__, e)}
    emit(out)
    return code


if __name__ == "__main__":
    sys.exit(main())
