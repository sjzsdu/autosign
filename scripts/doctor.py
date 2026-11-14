#!/usr/bin/env python3
"""autosign 环境体检：告诉你缺什么、去哪装、下一步做什么。

检查三类内容：
  1. 基础依赖：Python / openssl / Playwright / 浏览器 / 定时任务；
  2. 六个平台的安装与登录态（直接复用各签到脚本自己的探测逻辑，不重复实现）；
  3. 按当前实际情况生成一份按顺序执行的启动清单。

用法：
  python3 doctor.py            # 本地体检（不发任何签到请求）
  python3 doctor.py --verify   # 额外用 status 模式真实请求各平台接口（只查不领）

退出码：至少一个平台可直接签到且基础依赖齐全 → 0，否则 → 1。
"""

import glob
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import codebuddy_signin  # noqa: E402
import doubao_signin  # noqa: E402
import minimax_signin  # noqa: E402
import qoder_signin  # noqa: E402
import qwenwork_signin  # noqa: E402
import trae_signin  # noqa: E402

SIGNERS = [
    ("Trae CN", "trae_signin.py"),
    ("Qoder CN", "qoder_signin.py"),
    ("CodeBuddy", "codebuddy_signin.py"),
    ("豆包", "doubao_signin.py"),
    ("MiniMax Code", "minimax_signin.py"),
    ("千问办公", "qwenwork_signin.py"),
]

URLS = {
    "trae": "https://www.trae.cn/ide/download",
    "qoder": "https://qoder.com.cn/download",
    "codebuddy": "https://www.codebuddy.cn/ide/",
    "doubao": "https://www.doubao.com/download/desktop",
    "minimax": "https://agent.minimax.cn/download",
    "qwenwork": "https://qwenwork.cn/download",
}

# 输出符号与颜色：非 tty 时退化为纯文本，方便写进日志
GLYPH = {"ok": "✓", "warn": "!", "missing": "✗", "skip": "-"}
COLOR = {"ok": "32", "warn": "33", "missing": "31", "skip": "36"}


def _tty():
    return sys.stdout.isatty()


def _c(state, text):
    code = COLOR.get(state)
    if not code or not _tty():
        return text
    return "\033[1;%sm%s\033[0m" % (code, text)


def _bold(text):
    return "\033[1m%s\033[0m" % text if _tty() else text


def _readable(path):
    try:
        with open(path, "rb") as f:
            f.read(1)
        return True
    except Exception:
        return False


def _which(*names):
    for name in names:
        if shutil.which(name):
            return True
    return False


def _app_installed(*keywords):
    """在 /Applications、~/Applications（Windows/Linux 常见目录）里按名字找客户端。"""
    dirs = []
    if sys.platform == "darwin":
        dirs = ["/Applications", os.path.expanduser("~/Applications")]
    elif os.name == "nt":
        for env in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            base = os.environ.get(env)
            if base:
                dirs.append(os.path.join(base, "Programs") if env == "LOCALAPPDATA" else base)
    for d in dirs:
        try:
            entries = os.listdir(d)
        except Exception:
            continue
        for entry in entries:
            low = entry.lower()
            if any(k.lower() in low for k in keywords):
                return True
    return False


def _bundled_chromium():
    for d in ("~/Library/Caches/ms-playwright", "~/.cache/ms-playwright"):
        try:
            if any(n.startswith("chromium-") for n in os.listdir(os.path.expanduser(d))):
                return True
        except Exception:
            continue
    return False


def _fmt_ms(ms):
    try:
        return time.strftime("%Y-%m-%d", time.localtime(float(ms) / 1000))
    except Exception:
        return ""


def _fmt_ts(sec):
    try:
        return time.strftime("%Y-%m-%d", time.localtime(float(sec)))
    except Exception:
        return ""


def _platform(name, state, summary, detail=None, actions=None, *,
              installed=True, logged_in=False, login_in_app=False,
              login_cmd=None, install_url=None):
    return {
        "name": name, "state": state, "summary": summary, "detail": detail,
        "actions": actions or [], "installed": installed, "logged_in": logged_in,
        "login_in_app": login_in_app, "login_cmd": login_cmd,
        "install_url": install_url,
    }


# ------------------------------------------------------------------ 基础依赖

def check_env():
    items = []  # (state, 名称, 详情, 修复命令/提示)

    py_ok = sys.version_info >= (3, 8)
    py_ver = "%d.%d.%d" % sys.version_info[:3]
    items.append(("ok" if py_ok else "warn", "Python %s" % py_ver, sys.executable,
                  "" if py_ok else "需要 Python 3.8+，请升级后重跑"))

    openssl = shutil.which("openssl")
    if openssl:
        ver = "openssl"
        try:
            out = subprocess.run([openssl, "version"], capture_output=True, text=True,
                                 timeout=5).stdout.split()
            ver = " ".join(out[:2]) or ver
        except Exception:
            pass
        items.append(("ok", ver, openssl, ""))
    else:
        hint = ("macOS 自带 openssl，若缺失请先安装 Xcode Command Line Tools"
                if sys.platform == "darwin" else
                "sudo apt-get install -y openssl")
        items.append(("missing", "openssl 未安装", "", hint))

    spec = importlib.util.find_spec("playwright")
    if spec:
        ver = ""
        try:
            from importlib.metadata import version
            ver = " " + version("playwright")
        except Exception:
            pass
        items.append(("ok", "Playwright%s" % ver, os.path.dirname(spec.origin or ""), ""))
    else:
        items.append(("warn", "Playwright 未安装", "豆包签到、豆包/MiniMax/千问办公登录需要",
                      "%s -m pip install playwright" % sys.executable))

    channel = doubao_signin.browser_launch_options().get("channel")
    if channel:
        items.append(("ok", "浏览器：%s" % {"chrome": "Chrome", "msedge": "Edge"}.get(channel, channel),
                      "", ""))
    elif _bundled_chromium():
        items.append(("ok", "浏览器：Playwright 内置 Chromium", "", ""))
    else:
        items.append(("warn", "未找到 Chrome / Edge", "豆包签到需要本机浏览器",
                      "安装 Google Chrome：https://www.google.com/chrome/ "
                      "或执行 %s -m playwright install chromium" % sys.executable))

    state, text = schedule_status()
    items.append((state, "定时任务：%s" % text, "", ""))
    return items


def schedule_status():
    if sys.platform == "darwin":
        files = glob.glob(os.path.expanduser("~/Library/LaunchAgents/*autosign*.plist"))
        if files:
            return "ok", "LaunchAgent 已安装（%s）" % os.path.basename(files[0])
        return "warn", "未安装（macOS 推荐 launchd，配置见 README「定时执行（macOS）」）"
    if os.name == "nt":
        return "warn", "未检查（Windows 请用任务计划程序每天执行 run.sh）"
    try:
        out = subprocess.run(["crontab", "-l"], capture_output=True, text=True,
                             timeout=5).stdout
    except Exception:
        return "warn", "系统没有 crontab，无法配置定时任务"
    if "autosign" in out:
        return "ok", "crontab 已安装"
    return "warn", "未安装（运行 ./run.sh install-cron）"


# ------------------------------------------------------------------ 平台探测

def check_trae():
    storage, _ = trae_signin.find_storage()
    product = next((p for p in trae_signin.APP_JSON_CANDIDATES if _readable(p)), None)
    installed = bool(storage or product) or _app_installed("Trae CN", "TRAE", "Trae") \
        or _which("trae-cn", "trae")

    if not installed:
        return _platform(
            "Trae CN", "missing", "未检测到 Trae CN 客户端",
            "安装后打开并登录一次，脚本会自动读取本机登录态",
            ["安装 Trae CN：%s" % URLS["trae"], "打开 Trae CN 并登录", "重跑 ./run.sh doctor"],
            installed=False, login_in_app=True, install_url=URLS["trae"])

    if not storage:
        return _platform(
            "Trae CN", "warn", "已安装，但还没有登录态",
            "未找到 storage.json", ["打开 Trae CN 并完成登录", "重跑 ./run.sh doctor"],
            logged_in=False, login_in_app=True, install_url=URLS["trae"])

    loaded, code, msg = trae_signin.load_auth()
    if code:
        return _platform(
            "Trae CN", "warn", "已安装，但登录态不可用", msg,
            ["打开 Trae CN 重新登录（token 约 7 天过期，打开客户端会自动续期）",
             "重跑 ./run.sh doctor"],
            logged_in=False, login_in_app=True, install_url=URLS["trae"])

    _, _, _, exp = loaded
    tail = "（token 有效期至 %s）" % str(exp)[:10] if exp else "（token 约 7 天过期）"
    return _platform(
        "Trae CN", "ok", "已登录，可直接签到" + tail,
        "登录态：%s" % storage,
        logged_in=True, install_url=URLS["trae"])


def check_qoder():
    path = qoder_signin.find_auth_file()

    # 本脚本的 Qoder 登录态解密是按 Linux 的 Chromium/libsecret 实现的；
    # macOS 用 Keychain、Windows 用 DPAPI，都不是同一条路径，这里如实说明并跳过。
    if sys.platform != "linux":
        detail = "登录态文件：%s" % path if path else "未找到 auth.v1.dat"
        return _platform(
            "Qoder CN", "skip", "当前系统不支持（脚本按 Linux 钥匙串实现），会自动跳过",
            detail,
            ["把项目部署到 Linux 即可启用 Qoder 签到；本机跳过不影响其他平台"],
            logged_in=False, install_url=URLS["qoder"])

    installed = bool(path) or _app_installed("Qoder CN", "Qoder") \
        or _which("qoder-cn", "qoder")

    if not installed:
        return _platform(
            "Qoder CN", "missing", "未检测到 Qoder CN 客户端",
            "安装后打开并登录一次",
            ["安装 Qoder CN：%s" % URLS["qoder"], "打开 Qoder CN 并登录",
             "重跑 ./run.sh doctor"],
            installed=False, login_in_app=True, install_url=URLS["qoder"])

    if not path:
        return _platform(
            "Qoder CN", "warn", "已安装，但还没有登录态",
            "未找到 auth.v1.dat", ["打开 Qoder CN 并登录", "重跑 ./run.sh doctor"],
            logged_in=False, login_in_app=True, install_url=URLS["qoder"])

    auth, source, auth_path, err = qoder_signin.load_auth()
    if err:
        return _platform(
            "Qoder CN", "warn", "已安装，但登录态读取失败", err[1],
            ["打开 Qoder CN 重新登录",
             "Linux 还需确认钥匙串可用：secret-tool lookup application \"Qoder CN App\"",
             "重跑 ./run.sh doctor"],
            logged_in=False, login_in_app=True, install_url=URLS["qoder"])

    detail = "登录态：%s（来源 %s）" % (auth_path, source or "-")
    if sys.platform == "darwin":
        detail += "；macOS 无法检测客户端进程，手动签到请加 QODER_ALLOW_OFFLINE=1"
    exp = str(auth.get("expiresAt") or "")[:10]
    tail = "（登录态有效期至 %s）" % exp if exp else ""
    return _platform(
        "Qoder CN", "ok", "已登录，可直接签到" + tail, detail,
        logged_in=True, install_url=URLS["qoder"])


def check_codebuddy():
    path, _ = codebuddy_signin.find_auth_file()
    installed = bool(path) or _app_installed("CodeBuddy", "WorkBuddy") \
        or _which("codebuddy", "workbuddy", "buddy")

    if not installed:
        return _platform(
            "CodeBuddy", "missing", "未检测到 CodeBuddy IDE / 插件",
            "安装 IDE 或给 VS Code 装 CodeBuddy 插件，登录一次即可",
            ["安装 CodeBuddy：%s" % URLS["codebuddy"],
             "打开 IDE / 插件并登录", "重跑 ./run.sh doctor"],
            installed=False, login_in_app=True, install_url=URLS["codebuddy"])

    if not path:
        return _platform(
            "CodeBuddy", "warn", "已安装，但还没有登录态",
            "未找到 Tencent-Cloud.coding-copilot.info",
            ["打开 CodeBuddy IDE / 插件并登录", "重跑 ./run.sh doctor"],
            logged_in=False, login_in_app=True, install_url=URLS["codebuddy"])

    auth, code, msg = codebuddy_signin.load_auth()
    if code:
        action = ("打开一次 CodeBuddy IDE / 插件自动续期（token 约 28 天过期）"
                  if code == "NO_SESSION" else "重新登录 CodeBuddy IDE / 插件")
        return _platform(
            "CodeBuddy", "warn",
            "已安装，但登录态已过期" if code == "NO_SESSION" else "已安装，但登录态不可用",
            msg, [action, "重跑 ./run.sh doctor"],
            logged_in=False, login_in_app=True, install_url=URLS["codebuddy"])

    exp = _fmt_ms(auth.get("expires_at")) if auth.get("expires_at") else ""
    tail = "（token 有效期至 %s）" % exp if exp else ""
    return _platform(
        "CodeBuddy", "ok", "已登录，可直接签到" + tail,
        "登录态：%s" % path,
        logged_in=True, install_url=URLS["codebuddy"])


def check_doubao():
    state = doubao_signin.STATE_FILE
    has_state = _readable(state)
    app = _app_installed("Doubao", "豆包") or _which("doubao")
    has_pw = importlib.util.find_spec("playwright") is not None
    has_browser = bool(doubao_signin.browser_launch_options().get("channel")) \
        or _bundled_chromium()
    installed = bool(app or has_state)

    if has_state and has_pw and has_browser:
        return _platform(
            "豆包", "ok", "已扫码登录，可直接签到",
            "登录态：%s" % state,
            logged_in=True, install_url=URLS["doubao"])

    if has_state and not has_pw:
        return _platform(
            "豆包", "warn", "已登录，但缺少 Playwright",
            "豆包签到要用 Playwright 驱动本机浏览器",
            ["%s -m pip install playwright" % sys.executable, "重跑 ./run.sh doctor"],
            logged_in=True, install_url=URLS["doubao"])

    if has_state and not has_browser:
        return _platform(
            "豆包", "warn", "已登录，但未找到可用浏览器",
            "豆包签到需要本机 Chrome / Edge",
            ["安装 Google Chrome：https://www.google.com/chrome/",
             "或执行 %s -m playwright install chromium" % sys.executable],
            logged_in=True, install_url=URLS["doubao"])

    if app:
        return _platform(
            "豆包", "warn", "已安装，但还没有扫码登录",
            "首次需要扫码一次，之后无头自动签到",
            ["./run.sh doubao-login   # 扫码一次", "重跑 ./run.sh doctor"],
            logged_in=False, login_cmd="./run.sh doubao-login",
            install_url=URLS["doubao"])

    return _platform(
        "豆包", "missing", "未检测到豆包客户端，也没有登录态",
        "豆包签到依赖一次扫码登录，建议先装电脑版",
        ["下载豆包电脑版：%s" % URLS["doubao"],
         "./run.sh doubao-login   # 扫码一次", "重跑 ./run.sh doctor"],
        installed=False, login_cmd="./run.sh doubao-login",
        install_url=URLS["doubao"])


def check_minimax():
    state = minimax_signin.STATE_FILE
    if not _readable(state):
        return _platform(
            "MiniMax Code", "warn", "未登录（签到不依赖客户端，网页登录即可）",
            "登录态文件：%s" % state,
            ["./run.sh minimax-login   # 打开浏览器登录一次",
             "可选：安装桌面端 %s" % URLS["minimax"],
             "重跑 ./run.sh doctor"],
            logged_in=False, login_cmd="./run.sh minimax-login",
            install_url=URLS["minimax"])

    auth, code, msg = minimax_signin.load_auth()
    if code:
        return _platform(
            "MiniMax Code", "warn", "登录态已过期或不可用",
            msg, ["./run.sh minimax-login   # 重新登录一次", "重跑 ./run.sh doctor"],
            logged_in=False, login_cmd="./run.sh minimax-login",
            install_url=URLS["minimax"])

    exp = _fmt_ts(auth.get("expires_at")) if auth.get("expires_at") else ""
    tail = "（登录态有效期至 %s）" % exp if exp else ""
    return _platform(
        "MiniMax Code", "ok", "已登录，可直接签到" + tail,
        "登录态：%s（日常签到不启动浏览器）" % state,
        logged_in=True, install_url=URLS["minimax"])


def check_qwenwork():
    state = qwenwork_signin.STATE_FILE
    if not _readable(state):
        return _platform(
            "千问办公", "warn", "未登录（无手动签到，脚本只做每日自动登录 + 积分核对）",
            "登录态文件：%s" % state,
            ["./run.sh qwenwork-login   # 打开浏览器登录一次",
             "可选：安装客户端 %s" % URLS["qwenwork"],
             "重跑 ./run.sh doctor"],
            logged_in=False, login_cmd="./run.sh qwenwork-login",
            install_url=URLS["qwenwork"])

    _auth, code, msg = qwenwork_signin.load_auth()
    if code:
        return _platform(
            "千问办公", "warn", "登录态已过期或不可用",
            msg, ["./run.sh qwenwork-login   # 重新登录一次", "重跑 ./run.sh doctor"],
            logged_in=False, login_cmd="./run.sh qwenwork-login",
            install_url=URLS["qwenwork"])

    return _platform(
        "千问办公", "ok", "已登录（每日积分 00:00 自动发放，脚本只做登录保活）",
        "登录态：%s（日常不启动浏览器）" % state,
        logged_in=True, install_url=URLS["qwenwork"])


# ------------------------------------------------------------------ 在线验证

def verify_online(results):
    skipped = {r["name"] for r in results if r["state"] == "skip"}
    print("\n%s" % _bold("[在线验证] status 模式，只查状态不领取"))
    for name, script in SIGNERS:
        if name in skipped:
            print("  %s %-14s 已跳过（当前系统不支持）" % (_c("skip", GLYPH["skip"]), name))
            continue
        try:
            proc = subprocess.run(
                [sys.executable, os.path.join(HERE, script), "status"],
                capture_output=True, text=True, timeout=300)
        except subprocess.TimeoutExpired:
            print("  %s %-14s 超时（>300s）" % (_c("warn", "!"), name))
            continue
        except Exception as e:
            print("  %s %-14s 无法执行：%s" % (_c("warn", "!"), name, e))
            continue

        data = {}
        for line in reversed((proc.stdout or "").strip().splitlines()):
            if line.startswith("{"):
                try:
                    data = json.loads(line)
                except Exception:
                    data = {}
                break
        result = str(data.get("result") or "")
        report = str(data.get("report") or "").strip()
        if result in ("CLAIMED", "ALREADY", "PENDING", "INACTIVE", "OK"):
            state, glyph = "ok", GLYPH["ok"]
        elif result in ("NO_AUTH", "NO_SESSION"):
            state, glyph = "warn", GLYPH["warn"]
        else:
            state, glyph = "missing", GLYPH["missing"]
        print("  %s %-14s %s%s" % (
            _c(state, glyph), name, (result + "：") if result else "",
            report[:160] or "无输出"))


# ------------------------------------------------------------------ 输出

def print_env(items):
    print("\n%s" % _bold("[基础环境]"))
    for state, label, detail, hint in items:
        print("  %s %s" % (_c(state, GLYPH[state]), label))
        extra = "      " + detail if detail else ""
        if state != "ok" and hint:
            extra += ("；" if extra else "      ") + hint
        if extra:
            print(extra)


def print_platforms(results):
    print("\n%s" % _bold("[签到平台]"))
    width = max(len(r["name"]) for r in results)
    for r in results:
        print("  %s %s  %s" % (_c(r["state"], GLYPH[r["state"]]),
                               r["name"].ljust(width), r["summary"]))
        if r["detail"]:
            print("      %s" % r["detail"])
        for action in r["actions"]:
            print("      → %s" % action)


def build_guide(results, env, schedule_ok):
    steps = []

    dep_cmds = [hint for state, _label, _detail, hint in env
                if state != "ok" and hint]
    if dep_cmds:
        steps.append(("补齐基础依赖", dep_cmds))

    missing = [(r["name"], r["install_url"]) for r in results
               if not r["installed"] and r["install_url"]]
    if missing:
        steps.append(("安装缺失的客户端",
                      ["%s：%s" % (name, url) for name, url in missing]))

    app_logins = [r["name"] for r in results if r["login_in_app"] and not r["logged_in"]]
    if app_logins:
        steps.append(("打开客户端并登录",
                      ["%s：打开客户端 → 登录" % name for name in app_logins]))

    web_logins = [r["login_cmd"] for r in results
                  if r["login_cmd"] and not r["logged_in"]]
    if web_logins:
        steps.append(("一次性网页扫码 / 登录", web_logins))

    steps.append(("验证环境（只查状态，不领取）", ["./run.sh status"]))
    steps.append(("正式签到", ["./run.sh"]))

    if not schedule_ok:
        if sys.platform == "darwin":
            steps.append(("配置每天自动签到",
                          ["按 README「定时执行（macOS）」安装 LaunchAgent"
                           "（macOS 不要用 cron）"]))
        elif os.name == "nt":
            steps.append(("配置每天自动签到",
                          ["用任务计划程序每天执行 run.sh"]))
        else:
            steps.append(("配置每天自动签到", ["./run.sh install-cron"]))

    return steps


def print_guide(steps):
    print("\n%s" % _bold("[保姆级启动步骤]"))
    if not steps:
        print("  环境已就绪，直接运行 ./run.sh 即可。")
        return
    for idx, (title, cmds) in enumerate(steps, 1):
        print("  %d) %s" % (idx, title))
        for cmd in cmds:
            print("       %s" % cmd)


def main():
    want_verify = "--verify" in sys.argv[1:]
    if any(arg in ("-h", "--help") for arg in sys.argv[1:]):
        print(__doc__.strip())
        return 0

    env = check_env()
    results = [check_trae(), check_qoder(), check_codebuddy(),
               check_doubao(), check_minimax(), check_qwenwork()]
    schedule_ok = any(label.startswith("定时任务") and state == "ok"
                      for state, label, _d, _h in env)

    print(_bold("== autosign doctor =="))
    print("系统：%s %s | 时间：%s" % (
        platform.system(), platform.machine(),
        time.strftime("%Y-%m-%d %H:%M:%S")))

    print_env(env)
    print_platforms(results)

    ready = [r["name"] for r in results if r["state"] == "ok"]
    todo = [r["name"] for r in results if r["state"] in ("warn", "missing")]
    skipped = [r["name"] for r in results if r["state"] == "skip"]

    print("\n%s" % _bold("[结论]"))
    if ready:
        print("  可直接签到：%d 项（%s）" % (len(ready), "、".join(ready)))
    else:
        print("  %s" % _c("warn", "暂时没有可直接签到的平台，请按下面的步骤处理"))
    if todo:
        print("  需要处理：%d 项（%s）" % (len(todo), "、".join(todo)))
    if skipped:
        print("  已跳过：%d 项（%s，当前系统不支持，不影响其他签到）"
              % (len(skipped), "、".join(skipped)))

    print_guide(build_guide(results, env, schedule_ok))

    if want_verify:
        verify_online(results)

    base_ok = not any(state == "missing" and label.startswith("openssl")
                      for state, label, _d, _h in env)
    return 0 if (base_ok and ready) else 1


if __name__ == "__main__":
    sys.exit(main())
