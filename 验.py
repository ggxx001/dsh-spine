# -*- coding: utf-8 -*-
"""验.py —— 效应器回读：动作之后，验「目标状态成没成」，不是验「画面变没变」
用法:
  python 验.py "fg-title:记事本" --timeout 3000
  python 验.py "title:另存为"
  python 验.py "bgone:计算器"
  python 验.py "pixel:100,200,#ffffff,20"
  python 验.py "uia:保存"
退出码: 0=成立 1=超时不成立 2=参数/环境错误
"""
import sys, time, json, os, ctypes, ctypes.wintypes as wt

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

u = ctypes.windll.user32
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try: u.SetProcessDPIAware()
    except Exception: pass


def titles():
    out = []
    EP = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def cb(h, l):
        if u.IsWindowVisible(h):
            n = u.GetWindowTextLengthW(h)
            if n:
                b = ctypes.create_unicode_buffer(n + 1)
                u.GetWindowTextW(h, b, n + 1)
                r = wt.RECT()
                u.GetWindowRect(h, ctypes.byref(r))
                # 排除最小化哨兵位置
                if not (r.left <= -30000 and r.top <= -30000):
                    out.append((b.value, (r.left, r.top, r.right, r.bottom)))
        return True

    u.EnumWindows(EP(cb), 0)
    return out


def fg_title():
    h = u.GetForegroundWindow()
    n = u.GetWindowTextLengthW(h)
    b = ctypes.create_unicode_buffer(n + 1)
    u.GetWindowTextW(h, b, n + 1)
    return b.value


def pixel_at(x, y):
    hdc = u.GetDC(0)
    v = ctypes.windll.gdi32.GetPixel(hdc, int(x), int(y))
    u.ReleaseDC(0, hdc)
    if v == -1 or v == 0xFFFFFFFF:
        return None
    return ((v & 0xFF), ((v >> 8) & 0xFF), ((v >> 16) & 0xFF))


class BadPredicate(Exception):
    """谓词本身有问题（拼错/未知类型）——这不是「没验到」，是「验不了」，必须返回 rc=2"""
    pass


def check(spec):
    kind, _, arg = spec.partition(":")
    kind = kind.strip().lower()
    # 组合门控：all:a&b（全过） / any:a&b（任一过）——对应「窗口+控件+数据」三层串联
    if kind in ("all", "any"):
        parts = [p for p in arg.split("&") if p.strip()]
        results = []
        for p in parts:
            try:
                ok, info = check(p)
            except Exception as e:
                ok, info = False, {"err": str(e)[:80]}
            results.append({"spec": p, "ok": ok, "info": info})
        oks = [r["ok"] for r in results]
        passed = all(oks) if kind == "all" else any(oks)
        return passed, {"mode": kind, "results": results}
    if kind == "fg-title":
        t = fg_title()
        return (arg.lower() in t.lower()), {"fg_title": t}
    if kind == "title":
        ts = [t for t, _ in titles()]
        hit = [t for t in ts if arg.lower() in t.lower()]
        return (len(hit) > 0), {"matched": hit[:3], "windows": len(ts)}
    if kind == "bgone":
        # 2026-11-20 红队 F3：最小化的窗口仍然 IsWindowVisible=True，原实现会把「最小化」当成「已关闭」。
        # bgone 现在要求：窗口不存在，或不可见，或已最小化 → 都算「不在眼前」，但会分别报出来。
        if not arg.strip():
            raise SystemExit("empty predicate arg: bgone")
        open_now, minimized, invisible = [], [], []
        # 精确查：按标题找到 hwnd，再判 IsIconic
        EP = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

        def cb(h, l):
            n = u.GetWindowTextLengthW(h)
            if n:
                b = ctypes.create_unicode_buffer(n + 1)
                u.GetWindowTextW(h, b, n + 1)
                if arg.lower() in b.value.lower():
                    if u.IsIconic(h):
                        minimized.append(b.value)
                    elif u.IsWindowVisible(h):
                        open_now.append(b.value)
                    else:
                        invisible.append(b.value)
            return True

        u.EnumWindows(EP(cb), 0)
        # 只有「窗口真的不存在」才算 ok：最小化/隐藏都还活着，都不算关闭（红队 F3）
        ok = (len(open_now) + len(minimized) + len(invisible)) == 0
        return ok, {"still_open": open_now[:3], "minimized": minimized[:3], "invisible": invisible[:3]}
    if kind == "pixel":
        parts = arg.split(",")
        x, y = int(parts[0]), int(parts[1])
        want = parts[2].strip()
        tol = int(parts[3]) if len(parts) > 3 else 12
        want_rgb = (int(want[1:3], 16), int(want[3:5], 16), int(want[5:7], 16))
        got = pixel_at(x, y)
        if got is None:
            return False, {"pixel": None}
        d = max(abs(got[i] - want_rgb[i]) for i in range(3))
        return (d <= tol), {"pixel": got, "want": want_rgb, "delta": d}
    # ---- 通用谓词（2026-11-20 加）：不再为每个场景单独写脚本 ----
    if kind == "file":
        # file:路径,最小字节数
        parts = arg.rsplit(",", 1)
        path = parts[0].strip()
        minb = int(parts[1]) if len(parts) > 1 and parts[1].strip().isdigit() else 1
        ok = os.path.isfile(path) and os.path.getsize(path) >= minb
        return ok, {"file": path, "exists": os.path.isfile(path),
                    "size": (os.path.getsize(path) if os.path.isfile(path) else 0), "min": minb}
    if kind == "proc":
        # proc:exe名（不带 .exe 也行）
        import subprocess
        name = arg.strip()
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq " + (name if name.lower().endswith(".exe") else name + ".exe")],
                             capture_output=True, text=True, encoding="utf-8", errors="replace").stdout or ""
        ok = "PID" in out or "pid" in out.lower()
        return ok, {"proc": name, "seen": ok}
    if kind == "http":
        # http:url,期望子串
        url, _, want = arg.rpartition(",")
        import urllib.request
        try:
            req = urllib.request.Request(url.strip(), headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"})
            with urllib.request.urlopen(req, timeout=10) as r:
                body = r.read().decode("utf-8", "replace")
        except Exception as e:
            return False, {"http": url.strip(), "err": str(e)[:100]}
        return (want in body if want else True), {"http": url.strip(), "want": want, "len": len(body)}
    if kind == "json":
        # json:url,key=value 或 json:url,key.sub=value
        url, _, expr = arg.rpartition(",")
        key, _, want = expr.partition("=")
        import urllib.request
        try:
            req = urllib.request.Request(url.strip(), headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"})
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read().decode("utf-8"))
        except Exception as e:
            return False, {"json": url.strip(), "err": str(e)[:100]}
        cur = data
        for k in key.strip().split("."):
            if isinstance(cur, dict) and k in cur:
                cur = cur[k]
            else:
                return False, {"json": key, "missing": k}
        ok = (str(cur).lower() == want.strip().lower())
        return ok, {"json": key.strip(), "got": cur, "want": want.strip()}
    if kind == "clip":
        # clip:子串 —— 剪贴板里有这段文字
        u.OpenClipboard(0)
        try:
            if not u.IsClipboardFormatAvailable(13):
                return False, {"clip": "非文本"}
            h = u.GetClipboardData(13)
            txt = ctypes.c_wchar_p(h).value or ""
        finally:
            u.CloseClipboard()
        return (arg in txt), {"clip_len": len(txt), "want": arg}
    if kind == "run":
        # run:脚本路径,期望子串 —— 任意脚本当检查器（自主生长的入口）
        import subprocess
        path, _, want = arg.rpartition(",")
        path = path.strip()
        if not os.path.isabs(path):
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
        if not os.path.isfile(path):
            return False, {"run": path, "err": "脚本不存在"}
        cmd = [sys.executable, path] if path.lower().endswith(".py") else ["pwsh", "-NoProfile", "-File", path]
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        blob = (p.stdout or "") + (p.stderr or "")
        ok = (p.returncode == 0) and ((want in blob) if want else True)
        return ok, {"run": os.path.basename(path), "rc": p.returncode, "want": want, "tail": blob.strip()[-90:]}
    if kind == "uia":
        # 2026-11-20 Kimi 建议：UIA 谓词默认必须有作用域（否则后台/别的应用同名控件也算「找到了」）。
        #   uia:名字            → 只在前台窗口里找（默认）
        #   uia:名字@all        → 全局找（老行为，会报 ambiguous）
        #   uia:名字@窗口标题片段 → 在那个窗口的子树里找
        import uiautomation as ua
        ua.SetGlobalSearchTimeout(0.8)
        name, _, scope = arg.partition("@")
        name = name.strip()
        scope = scope.strip()
        if not name:
            raise BadPredicate("uia: 缺控件名")
        try:
            if scope.lower() == "all":
                c = ua.Control(Name=name, searchDepth=10)
                ok = bool(c and c.Exists(0.3))
                return ok, {"uia": name, "scope": "all", "found": ok,
                            "note": "全局搜索，同名控件可能有多个，不能证明「就在我点的那个界面里」"}
            if scope:
                win = ua.WindowControl(searchDepth=3, SubName=scope)
                if not win.Exists(0.5):
                    return False, {"uia": name, "scope": scope, "found": False, "why": "找不到该窗口"}
            else:
                win = ua.ControlFromHandle(u.GetForegroundWindow())
                if win is None:
                    return False, {"uia": name, "scope": "fg", "found": False, "why": "拿不到前台窗口"}
            c = win.Control(Name=name, searchDepth=10)
            ok = bool(c and c.Exists(0.3))
            return ok, {"uia": name, "scope": (scope or "fg"), "found": ok}
        except SystemExit:
            raise
        except Exception as e:
            return False, {"uia": name, "scope": scope or "fg", "err": str(e)[:100]}
    raise BadPredicate("unknown expect: " + spec)


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"ok": False, "err": "usage: 验.py <spec> [--timeout ms]"}, ensure_ascii=False))
        return 2
    spec = sys.argv[1]
    # 2026-11-20 红队 F10：空谓词（title: / all:）会静默 ok:true，等于"没验"当"验过"
    _kind, _, _arg = spec.partition(":")
    if not spec.strip():
        print(json.dumps({"ok": False, "err": "空谓词"}, ensure_ascii=False)); return 2
    if _kind.strip().lower() in ("title", "fg-title", "bgone", "uia", "clip", "run", "file", "proc", "http", "json") and not _arg.strip():
        print(json.dumps({"ok": False, "err": "谓词缺参数：" + spec, "hint": "例如 title:记事本 / file:路径,字节"}, ensure_ascii=False)); return 2
    if _kind.strip().lower() in ("all", "any") and not [p for p in _arg.split("&") if p.strip()]:
        print(json.dumps({"ok": False, "err": "组合谓词里没有子判据：" + spec}, ensure_ascii=False)); return 2
    timeout = 3000
    if "--timeout" in sys.argv:
        timeout = int(sys.argv[sys.argv.index("--timeout") + 1])
    t0 = time.time()
    last = {}
    while True:
        try:
            ok, info = check(spec)
        except BadPredicate as e:
            print(json.dumps({"ok": False, "err": str(e), "expect": spec}, ensure_ascii=False))
            return 2
        except SystemExit:
            raise
        except Exception as e:
            ok, info = False, {"err": str(e)[:120]}
        last = info
        if ok:
            print(json.dumps({"ok": True, "expect": spec, "waited_ms": int((time.time() - t0) * 1000), **info}, ensure_ascii=False))
            return 0
        if (time.time() - t0) * 1000 >= timeout:
            print(json.dumps({"ok": False, "expect": spec, "waited_ms": int((time.time() - t0) * 1000), **last}, ensure_ascii=False))
            return 1
        time.sleep(0.15)


if __name__ == "__main__":
    sys.exit(main())
