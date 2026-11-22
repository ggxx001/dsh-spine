# 面板/服务.py —— 脊髓可视化操控台的本地服务（端口 8813）
# 2026-11-22 知鱼知乐写。契约见同目录 接口契约.md。
# 只读地看本机状态 + 代理脊髓统.py 的动词 + 截屏给前端画框。动手的事一律交给 脊髓统.py，
# 它里面有三层刹车（STOP / 让位闸 / 失败即停），这里不重复实现，也不绕过。
import base64, io, json, os, struct, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

HERE = os.path.dirname(os.path.abspath(__file__))
SPINE = os.path.dirname(HERE)              # 工具\界面反射
BASE = os.environ.get("SPINE_BASE") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # D:\嘎嘎大鱼
PY = sys.executable
LOG = os.path.join(SPINE, "反射日志.txt")
STOPF = os.path.join(SPINE, "STOP")
ARCF = os.path.join(SPINE, "弧.json")
ELEMF = os.path.join(SPINE, "学习到的元素.json")
DATA = os.path.join(BASE, "训练", "ui-detect", "实验数据.jsonl")
# 控制台 v2（2026-11-22）：轨迹 / 失败快照 / 计划 / 模型开关
TRACEF = os.path.join(BASE, "训练", "ui-detect", "trace.jsonl")
TRACELIVE = os.path.join(BASE, "训练", "ui-detect", "trace-live.json")
FAILDIR = os.path.join(BASE, "训练", "ui-detect", "fails")
PLANDIR = os.path.join(SPINE, "计划")
MODELF = os.path.join(SPINE, "模型开关.json")
MODELBIN = os.environ.get("SPINE_MODEL") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "model", "spine.bin")
MODELS = {"omni_icon": r"E:\models\OmniParser-v2\icon_detect\model.pt",
          "ui_detect": os.path.join(BASE, "训练", "ui-detect", "det", "runs", "ui3", "weights", "best.pt")}
PORT = int(os.environ.get("SPINE_PANEL_PORT", "8813"))
CHILD_ENV = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
CREATE_NO_WINDOW = 0x08000000
VERB_OK = {"status","see","icons","find","elements","click","rclick","dbl","drag","hover",
           "type","key","scroll","arc","seq","observe","wait","gate","focus","text","settext","sheet","go",
           "learn","models","region","preflight","vitals"}
# 会改机器状态的动词。引擎 脊髓统.py 的 STOP 列表漏了 text/settext/go（独立审计 #3 发现，
# text 带 --click 会真点鼠标），所以面板这一层自己再拦一次——两处都拦才叫兜住。
DANGEROUS = {"click","rclick","dbl","drag","hover","type","key","scroll","arc","seq",
             "learn","focus","text","settext","go"}

# ---- DPI：必须在任何窗口/坐标调用之前 ----
try:
    import ctypes
    from ctypes import wintypes
    try: ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception: ctypes.windll.user32.SetProcessDPIAware()
    USER32 = ctypes.windll.user32
    DWM = ctypes.windll.dwmapi
except Exception:
    ctypes = None; USER32 = None; DWM = None


def log(m):
    line = time.strftime("%m-%d %H:%M:%S") + "  [面板] " + m
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f: f.write(line + "\n")
    except Exception: pass


# ---------- 窗口 ----------
def _title(h):
    n = USER32.GetWindowTextLengthW(h)
    if not n: return ""
    b = ctypes.create_unicode_buffer(n + 1)
    USER32.GetWindowTextW(h, b, n + 1)
    return b.value.strip()


def hwnd_for(win):
    """win: '' → 整屏(None)；'@fg' → 前台；其他 → 标题片段（含则认）"""
    if not win: return None, None
    if win in ("@fg", "fg", "@foreground"):
        h = USER32.GetForegroundWindow()
        return (h or None), (_title(h) if h else "")
    lower = win.lower(); found = []
    EP = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(h, l):
        if USER32.IsWindowVisible(h):
            t = _title(h)
            if t and lower in t.lower(): found.append(h)
        return True
    USER32.EnumWindows(EP(cb), 0)
    if not found: return None, ""
    return found[0], _title(found[0])


def rect_of(h):
    """窗口可见矩形（屏幕物理像素）。优先 DWM 扩展边框——GetWindowRect 含不可见阴影边，
    直接用它画框会整体偏 7px 左右（2026-11-22 实测）。"""
    r = wintypes.RECT()
    if DWM is not None:
        try:
            if DWM.DwmGetWindowAttribute(h, 9, ctypes.byref(r), ctypes.sizeof(r)) == 0 and r.right > r.left:
                return [int(r.left), int(r.top), int(r.right - r.left), int(r.bottom - r.top)]
        except Exception:
            pass
    if not USER32.GetWindowRect(h, ctypes.byref(r)):
        return None
    return [int(r.left), int(r.top), int(r.right - r.left), int(r.bottom - r.top)]


def virtual_screen():
    try:
        vx = USER32.GetSystemMetrics(76); vy = USER32.GetSystemMetrics(77)
        vw = USER32.GetSystemMetrics(78); vh = USER32.GetSystemMetrics(79)
        return int(vx), int(vy), int(vw), int(vh)
    except Exception:
        return 0, 0, 0, 0


def grab():
    from PIL import ImageGrab
    return ImageGrab.grab(all_screens=True)


_WC = {"m": None, "tried": False}


def printwindow(h):
    """PrintWindow(PW_RENDERFULLCONTENT) 抓窗口自身内容——被别的窗口压住也能拿到。
    抓不到/全黑（部分 GPU 合成窗口）就返回 None，交给截屏裁剪那条路。"""
    if not _WC["tried"]:
        _WC["tried"] = True
        try:
            import importlib.util
            p = os.path.join(BASE, "工具", "_归档", "win_cap.py")
            spec = importlib.util.spec_from_file_location("win_cap", p)
            m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
            _WC["m"] = m
        except Exception as e:
            log("PrintWindow 模块加载失败：%s" % str(e)[:80])
    m = _WC["m"]
    if m is None: return None
    try:
        got = m.grab(h)
        if not got: return None
        img, ok = got
        if not ok or img is None: return None
        from PIL import ImageStat
        if ImageStat.Stat(img.convert("L")).mean[0] < 3:   # 全黑 = 没抓到内容
            return None
        return img
    except Exception:
        return None


def shot(win):
    from PIL import Image
    t0 = time.time()
    h, title = hwnd_for(win)
    if win and not h:
        return {"ok": False, "why": "找不到窗口：%r（可以填标题片段）" % win}
    vx, vy, vw, vh = virtual_screen()
    capture = "screencopy"; covered = False; img = None
    if h:
        # 先试 PrintWindow：窗口被压住时截屏裁剪会把压在上面的窗口一起截进来（2026-11-22 实测踩到）
        img = printwindow(h)
        if img is not None:
            capture = "printwindow"
            r0 = wintypes.RECT()
            if not USER32.GetWindowRect(h, ctypes.byref(r0)):
                return {"ok": False, "why": "拿不到窗口矩形（窗口可能已关闭）"}
            gw = [int(r0.left), int(r0.top), int(r0.right - r0.left), int(r0.bottom - r0.top)]
            rect = rect_of(h) or gw
            # PrintWindow 画的是整块矩形（含不可见边框），DWM 给的是可见区：
            # 把图按差值裁一下，两条路径的坐标系就统一成"可见区 + DWM rect"，画框不会偏。
            dx, dy = rect[0] - gw[0], rect[1] - gw[1]
            if dx or dy or (rect[2], rect[3]) != (gw[2], gw[3]):
                cx0, cy0 = max(0, dx), max(0, dy)
                cx1, cy1 = min(img.width, dx + rect[2]), min(img.height, dy + rect[3])
                if cx1 - cx0 > 8 and cy1 - cy0 > 8:
                    img = img.crop((cx0, cy0, cx1, cy1))
                else:
                    rect = gw
            fh = USER32.GetForegroundWindow()
            covered = (fh != h)
            buf = io.BytesIO(); img.save(buf, format="PNG", compress_level=3)
            return {"ok": True, "png": base64.b64encode(buf.getvalue()).decode(),
                    "w": img.width, "h": img.height, "rect": rect, "scale": 1.0,
                    "shot_kind": "window", "capture": capture, "covered": covered,
                    "window": title, "sx": rect[2] / max(1, img.width), "sy": rect[3] / max(1, img.height),
                    "ms": int((time.time() - t0) * 1000)}
    if img is None:
        img = grab()
    if h:
        rc = rect_of(h)
        covered = (USER32.GetForegroundWindow() != h)
        if not rc: return {"ok": False, "why": "拿不到窗口矩形（窗口可能已关闭）"}
        # 裁剪：虚拟屏坐标 → 图内坐标
        box = (rc[0] - vx, rc[1] - vy, rc[0] - vx + rc[2], rc[1] - vy + rc[3])
        box = (max(0, box[0]), max(0, box[1]), min(img.width, box[2]), min(img.height, box[3]))
        if box[2] - box[0] < 8 or box[3] - box[1] < 8:
            return {"ok": False, "why": "窗口在屏幕外或太小（%r）" % (rc,)}
        img = img.crop(box)
        rect = rc; kind = "window"
    else:
        rect = [vx, vy, img.width, img.height]; kind = "screen"
    buf = io.BytesIO(); img.save(buf, format="PNG", compress_level=3)
    png = base64.b64encode(buf.getvalue()).decode()
    return {"ok": True, "png": png, "w": img.width, "h": img.height, "rect": rect,
            "scale": 1.0, "shot_kind": kind, "capture": capture, "covered": covered, "window": title,
            "sx": rect[2] / max(1, img.width), "sy": rect[3] / max(1, img.height),
            "ms": int((time.time() - t0) * 1000)}


# ---------- 让位闸（快查，不跑 12 秒等待） ----------
IDLE_FILE = os.path.join(BASE, "eye-test", "user_idle.json")
MIN_USER_IDLE = 4.0


def idle_now():
    """优先读低级钩子写的 user_idle.json（已滤掉我自己注入的输入），否则 GetLastInputInfo。"""
    try:
        st = os.stat(IDLE_FILE)
        if time.time() - st.st_mtime < 5:
            d = json.load(open(IDLE_FILE, encoding="utf-8"))
            # input_watch.py 写的字段名是 idleSec（真人空闲秒数，已滤掉注入的输入）
            idle = float(d.get("idleSec", d.get("idle", 0))); return idle, "hook"
    except Exception:
        pass
    if ctypes is None: return 999.0, "none"
    class LII(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]
    li = LII(); li.cbSize = ctypes.sizeof(LII)
    if not USER32.GetLastInputInfo(ctypes.byref(li)): return 999.0, "api-fail"
    now = ctypes.windll.kernel32.GetTickCount()
    return max(0.0, (now - li.dwTime) / 1000.0), "api"


# ---------- 引擎回执 ----------
def _last_json(text):
    best = {}
    for line in (text or "").splitlines():
        s = line.strip()
        if s.startswith("{"):
            try: best = json.loads(s)
            except Exception: pass
    return best


def _parse_blob(text):
    """引擎的 see 回执是多行 pretty JSON；_last_json 只看单行会拿到空对象。"""
    s = (text or "").strip()
    i, j = s.find("{"), s.rfind("}")
    if 0 <= i < j:
        try:
            return json.loads(s[i:j + 1])
        except Exception:
            pass
    return _last_json(text)


def run_spine(verb, args):
    cmd = [PY, os.path.join(SPINE, "脊髓统.py"), verb]
    for k, v in (args or {}).items():
        flag = k if str(k).startswith("--") else "--" + str(k)
        if v is True: cmd.append(flag); continue
        if v is False or v is None: continue
        if isinstance(v, (list, tuple)): cmd += [flag] + [str(x) for x in v]
        else: cmd += [flag, str(v)]
    t0 = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=CHILD_ENV, cwd=SPINE,
                           timeout=180, creationflags=CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return {"ok": False, "rc": -1, "ms": int((time.time()-t0)*1000), "data": {},
                "why": "超过 180 秒没回来（动作可能卡在等界面）"}
    out = (p.stdout or "").strip()
    data = _parse_blob(out)
    ok = p.returncode == 0 and data.get("ok") is not False and not data.get("stopped")
    why = ""
    if not ok:
        why = data.get("why") or data.get("error") or (p.stderr or "").strip()[-300:] or ("rc=%d" % p.returncode)
    return {"ok": ok, "rc": p.returncode, "ms": int((time.time()-t0)*1000), "data": data,
            "raw": out[-2000:], "why": why}


def _is4(x):
    return isinstance(x, (list, tuple)) and len(x) == 4 and all(isinstance(n, (int, float)) for n in x)


def norm_cands(d, kind):
    """把引擎各种候选格式统一成 [{box,score,kind,label}]（屏幕物理像素）"""
    out = []
    raw = None
    for key in ("candidates", "boxes", "icons", "results", "items"):
        v = d.get(key)
        if isinstance(v, list) and v: raw = v; break
    if raw is None:
        for key in ("box", "bbox", "rect"):
            if _is4(d.get(key)): raw = [d.get(key)]; break
    raw = raw or []
    for c in raw:
        if isinstance(c, dict):
            b = None
            for key in ("box", "bbox", "rect", "last_box"):
                if _is4(c.get(key)): b = list(c.get(key)); break
            if b is None:
                for key in ("boxes", "candidates"):
                    if isinstance(c.get(key), list):
                        for bb in c[key]:
                            if _is4(bb): out.append({"box": list(bb), "score": 0, "kind": kind, "label": ""})
                        break
                continue
            out.append({"box": b, "score": float(c.get("score") or c.get("conf") or 0),
                        "kind": c.get("kind") or kind, "label": c.get("label") or c.get("name") or c.get("text") or "",
                        "src": str(c.get("src") or ""), "role": str(c.get("role") or ""),
                        "text": str(c.get("text") or ""),
                        "suspect": bool(c.get("suspect")) or str(c.get("src") or "").startswith("uia")})
        elif _is4(c):
            out.append({"box": list(c), "score": 0, "kind": kind, "label": ""})
    return out


def locate(kind, win, want):
    import urllib.request
    t0 = time.time()
    h, title = hwnd_for(win or "@fg")
    body = json.dumps({"kind": kind, "window": title or "@fg", "want": want,
                       "models": model_flags()}).encode()
    try:
        req = urllib.request.Request("http://127.0.0.1:8811/locate2", data=body,
                                     headers={"Content-Type": "application/json"})
        d = json.loads(urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "replace"))
        cands = norm_cands(d, kind)
        return {"ok": bool(cands) or bool(d.get("ok")), "src": "8811", "candidates": cands,
                "window": title, "ms": int((time.time()-t0)*1000),
                "why": "" if cands else (d.get("why") or "8811 没给候选")}
    except Exception as e:
        r = run_spine("see", {"--kind": kind, "--window": title or "@fg"})
        cands = norm_cands(r.get("data") or {}, kind)
        return {"ok": bool(cands), "src": "see", "candidates": cands, "window": title,
                "ms": int((time.time()-t0)*1000), "why": "" if cands else (r.get("why") or str(e)[:120])}


# ---------- 缓存 ----------
_C = {"elem": (0, 0), "health8811": (0, None)}


def elements_count():
    try:
        st = os.stat(ELEMF)
        if _C["elem"][0] == st.st_mtime and _C["elem"][1]:
            return _C["elem"][1]
        items = json.load(open(ELEMF, encoding="utf-8")).get("items", [])
        v = len(items)
        _C["elem"] = (st.st_mtime, v)
        return v
    except Exception:
        return 0


def service8811():
    now = time.time()
    ts, val = _C["health8811"]
    if now - ts < 3: return bool(val)
    ok = False
    try:
        import urllib.request
        r = json.loads(urllib.request.urlopen("http://127.0.0.1:8811/status", timeout=0.8).read().decode())
        ok = bool(r.get("ok"))
    except Exception:
        ok = False
    _C["health8811"] = (now, ok)
    return ok


MODELS = {"omni_icon": r"E:\models\OmniParser-v2\icon_detect\model.pt",
          "ui_detect": os.path.join(BASE, "训练", "ui-detect", "det", "runs", "ui3", "weights", "best.pt")}
_MODEL_CACHE = {"t": 0, "v": {}}


def models():
    now = time.time()
    if now - _MODEL_CACHE["t"] < 10: return _MODEL_CACHE["v"]
    v = {k: os.path.exists(p) for k, p in MODELS.items()}
    _MODEL_CACHE["t"] = now; _MODEL_CACHE["v"] = v
    return v


def health():
    t0 = time.time()
    idle, src = idle_now()
    return {"ok": True, "port": PORT, "pid": os.getpid(), "engine": "脊髓统.py",
            "stop": os.path.exists(STOPF), "gate": idle >= MIN_USER_IDLE, "idle_ms": int(idle*1000),
            "idle_src": src, "service8811": service8811(), "elements": elements_count(),
            "models": models(), "version": "2", "console": True, "ms": int((time.time()-t0)*1000)}


def arcs():
    try:
        d = json.load(open(ARCF, encoding="utf-8"))
    except Exception as e:
        return {"ok": False, "why": "读不到 弧.json：%s" % str(e)[:80]}
    arr = d.get("arcs") if isinstance(d, dict) else d
    out = []
    for a in (arr or []):
        if not isinstance(a, dict): continue
        steps = a.get("steps") or []
        out.append({"name": a.get("name") or "?", "steps": len(steps), "desc": a.get("trigger") or ""})
    return {"ok": True, "count": len(out), "arcs": out}


def logs(n):
    n = max(1, min(500, int(n)))
    try:
        with open(LOG, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
        return {"ok": True, "lines": lines[-n:]}
    except FileNotFoundError:
        return {"ok": True, "lines": []}
    except Exception as e:
        return {"ok": False, "why": "读日志失败：%s" % str(e)[:80]}


# 常驻观察类：ms 记的是"盯了几秒"，不是一步动作花多久；混进去 p95 会是几十秒，没有意义。
# ms<=0 的是没记耗时的旧记录。两者都排除，并原样报出排除了多少条（不偷偷丢数据）。
LONG_KINDS = {"vision-watch", "observe"}
LONG_MS = 60000


def stats():
    tot = okc = 0; mss = []; bk = {}; longn = 0
    try:
        with open(DATA, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line: continue
                try: r = json.loads(line)
                except Exception: continue
                tot += 1
                good = bool(r.get("ok"))
                if good: okc += 1
                k = str(r.get("kind") or "?")
                try:
                    ms = int(r.get("ms") or 0)
                    if k in LONG_KINDS or ms <= 0 or ms >= LONG_MS: longn += 1
                    else: mss.append(ms)
                except Exception:
                    pass
                e = bk.setdefault(k, {"total": 0, "ok": 0})
                e["total"] += 1; e["ok"] += 1 if good else 0
    except FileNotFoundError:
        return {"ok": True, "total": 0, "ok_count": 0, "rate": 0, "ms_p50": 0, "ms_p95": 0, "by_kind": {}}
    except Exception as e:
        return {"ok": False, "why": "读实验数据失败：%s" % str(e)[:80]}
    mss.sort()
    def pct(p):
        if not mss: return 0
        i = min(len(mss) - 1, max(0, int(round(p * (len(mss) - 1)))))
        return mss[i]
    return {"ok": True, "total": tot, "ok_count": okc, "rate": (okc / tot) if tot else 0,
            "ms_p50": pct(0.5), "ms_p95": pct(0.95), "by_kind": bk, "ms_samples": len(mss),
            "long_excluded": longn}


# ---------- 控制台 v2：轨迹 / 失败快照 / 计划 / 元素 / 模型 ----------
def model_flags():
    try:
        d = json.load(open(MODELF, encoding="utf-8"))
    except Exception:
        d = {}
    return {k: bool(d.get(k, True)) for k in MODELS}


def trace_tail(n=50):
    n = max(1, min(400, int(n)))
    try:
        with open(TRACEF, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return {"ok": True, "count": 0, "items": []}
    except Exception as e:
        return {"ok": False, "why": "读轨迹失败：%s" % str(e)[:80]}
    items = []
    for line in lines[-n:]:
        try:
            items.append(json.loads(line))
        except Exception:
            continue
    items.reverse()
    return {"ok": True, "count": len(items), "items": items}


def trace_live():
    try:
        d = json.load(open(TRACELIVE, encoding="utf-8"))
        d["age_ms"] = int(time.time() * 1000) - int(d.get("ts") or 0)
        return {"ok": True, "live": d}
    except FileNotFoundError:
        return {"ok": True, "live": None}
    except Exception as e:
        return {"ok": False, "why": str(e)[:80]}


def snap_get(name):
    name = os.path.basename(str(name or ""))
    p = os.path.join(FAILDIR, name)
    if not name or not os.path.isfile(p):
        return None
    with open(p, "rb") as f:
        return f.read()


def plans_list():
    out = []
    try:
        names = sorted(os.listdir(PLANDIR))
    except FileNotFoundError:
        names = []
    for fn in names:
        if not fn.endswith(".json"):
            continue
        try:
            d = json.load(open(os.path.join(PLANDIR, fn), encoding="utf-8"))
        except Exception as e:
            out.append({"name": fn[:-5], "file": fn, "ok": False, "why": str(e)[:80]})
            continue
        out.append({"name": d.get("name") or fn[:-5], "file": fn, "desc": d.get("desc") or "",
                    "steps": len(d.get("steps") or []),
                    "saved": time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(os.path.join(PLANDIR, fn))))})
    return {"ok": True, "count": len(out), "plans": out}


def arc_detail(name):
    try:
        d = json.load(open(ARCF, encoding="utf-8"))
    except Exception as e:
        return {"ok": False, "why": "读不到 弧.json：%s" % str(e)[:80]}
    for a in (d.get("arcs") or []):
        if isinstance(a, dict) and a.get("name") == name:
            return {"ok": True, "arc": a}
    return {"ok": False, "why": "没有这条弧：%s" % name}


def elements_list(q="", limit=400):
    try:
        items = json.load(open(ELEMF, encoding="utf-8")).get("items", [])
    except Exception as e:
        return {"ok": False, "why": "读不到元素库：%s" % str(e)[:80]}
    q = str(q or "").strip().lower()
    lim = max(1, min(2000, int(limit or 400)))
    out = []
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            continue
        nm = str(it.get("name") or it.get("ui_text") or "")
        tag = str(it.get("tag") or "")
        if q and q not in nm.lower() and q not in tag.lower():
            continue
        out.append({"i": i, "name": nm, "tag": tag, "box": it.get("box"),
                    "status": it.get("status") or "", "kind": it.get("kind") or "",
                    "score": it.get("score"), "attempts": it.get("attempts") or 0,
                    "success": it.get("success") or 0, "disabled": bool(it.get("disabled")),
                    "template": os.path.basename(str(it.get("template") or ""))})
        if len(out) >= lim:
            break
    return {"ok": True, "total": len(items), "count": len(out), "items": out}


_ELEM_BAK_DONE = {"v": False}


def element_set(template, disabled):
    template = str(template or "")
    if not template:
        return {"ok": False, "why": "缺 template"}
    try:
        d = json.load(open(ELEMF, encoding="utf-8"))
    except Exception as e:
        return {"ok": False, "why": "读不到元素库：%s" % str(e)[:80]}
    hit = None
    for it in (d.get("items") or []):
        if isinstance(it, dict) and os.path.basename(str(it.get("template") or "")) == template:
            hit = it
            break
    if hit is None:
        return {"ok": False, "why": "元素库里没有这个模板：%s" % template}
    if not _ELEM_BAK_DONE["v"]:
        try:
            import shutil
            shutil.copy2(ELEMF, ELEMF + ".bak-before-console-" + time.strftime("%Y%m%d-%H%M%S"))
            _ELEM_BAK_DONE["v"] = True
        except Exception:
            pass
    hit["disabled"] = bool(disabled)
    hit["disabled_at"] = time.strftime("%Y-%m-%dT%H:%M:%S+08:00") if disabled else None
    try:
        json.dump(d, open(ELEMF, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    except Exception as e:
        return {"ok": False, "why": "写元素库失败：%s" % str(e)[:100]}
    log("元素 %s -> disabled=%s" % (template, bool(disabled)))
    return {"ok": True, "template": template, "disabled": bool(disabled)}


def models_status():
    flags = model_flags()
    out = {"ok": True, "flags": flags, "models": {}}
    for k, p in MODELS.items():
        ex = os.path.exists(p)
        out["models"][k] = {"exists": ex, "enabled": flags[k],
                            "size": os.path.getsize(p) if ex else 0,
                            "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(p))) if ex else None}
    return out


def models_set(body):
    setv = body.get("set") if isinstance(body, dict) else None
    flags = model_flags()
    if isinstance(setv, dict):
        for k, v in setv.items():
            if k in MODELS:
                flags[k] = bool(v)
    try:
        json.dump(flags, open(MODELF, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    except Exception as e:
        return {"ok": False, "why": "写模型开关失败：%s" % str(e)[:100]}
    log("模型开关 -> %s" % json.dumps(flags, ensure_ascii=False))
    return {"ok": True, "flags": flags, "note": "下一次 locate 生效；8811 若还在跑旧代码需重启一次 8811"}


def plan_save(body):
    name = str(body.get("name") or "").strip()
    steps = body.get("steps")
    if not name or any(ch in name for ch in '\\/:*?"<>|'):
        return {"ok": False, "why": "计划名不合法（不能为空或含路径字符）"}
    if not isinstance(steps, list) or not steps:
        return {"ok": False, "why": "steps 必须是非空数组"}
    keep = []
    for i, st in enumerate(steps):
        if not isinstance(st, dict) or not (st.get("verb") or st.get("do")):
            return {"ok": False, "why": "第 %d 步缺 verb" % (i + 1)}
        args = st.get("args") or {}
        if not isinstance(args, dict):
            return {"ok": False, "why": "第 %d 步 args 必须是对象" % (i + 1)}
        keep.append({"verb": str(st.get("verb") or st.get("do")), "args": args, "note": str(st.get("note") or "")})
    d = {"name": name, "desc": str(body.get("desc") or ""), "steps": keep,
         "updated": time.strftime("%Y-%m-%dT%H:%M:%S+08:00")}
    try:
        os.makedirs(PLANDIR, exist_ok=True)
        json.dump(d, open(os.path.join(PLANDIR, name + ".json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    except Exception as e:
        return {"ok": False, "why": "写计划失败：%s" % str(e)[:100]}
    log("计划保存 %s（%d 步）" % (name, len(keep)))
    return {"ok": True, "name": name, "steps": len(keep)}


def run_plan(body):
    sub = str(body.get("sub") or "list")
    if sub not in ("list", "show", "save", "run", "step"):
        return {"ok": False, "why": "plan 子命令不在白名单：%r" % sub[:20]}
    moving = sub in ("run", "step")
    if moving and os.path.exists(STOPF):
        return {"ok": False, "stopped": True, "why": "STOP 文件存在：计划执行已停用"}
    cmd = [PY, os.path.join(SPINE, "脊髓统.py"), "plan", sub]
    for k in ("name", "step", "file"):
        v = body.get(k)
        if v not in (None, ""):
            cmd += ["--" + k, str(v)]
    for k in ("dry", "keep-going"):
        if body.get(k):
            cmd.append("--" + k)
    t0 = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           env=CHILD_ENV, cwd=SPINE, timeout=900, creationflags=CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return {"ok": False, "rc": -1, "ms": int((time.time() - t0) * 1000),
                "why": "超过 900 秒没回来（计划可能卡在等界面）"}
    out = (p.stdout or "").strip()
    data = _parse_blob(out)
    ok = p.returncode == 0 and data.get("ok") is not False and not data.get("stopped")
    why = "" if ok else (data.get("why") or (p.stderr or "").strip()[-200:] or ("rc=%d" % p.returncode))
    return {"ok": ok, "rc": p.returncode, "ms": int((time.time() - t0) * 1000), "data": data,
            "raw": out[-3000:], "why": why}


# ---------- HTTP ----------
class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, fmt, *a):
        pass

    def _cors(self):
        # 2026-11-22 独立审计 #3 发现：原来这里是 Access-Control-Allow-Origin: *，
        # 等于任何网页都能 fetch 8813 —— 先 POST /api/stop {"on":false} 解刹车、再 click。
        # 面板和接口同源（都从 127.0.0.1:8813 发），根本不需要 CORS 头，所以一个都不发；
        # 浏览器就会自己拦掉跨源请求。别为了"方便调试"把它加回来。
        pass

    def _send(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self._cors(); self.end_headers(); self.wfile.write(b)

    def _file(self, path, ctype):
        try:
            with open(path, "rb") as f: b = f.read()
        except FileNotFoundError:
            self._send({"ok": False, "why": "没有这个文件：%s" % os.path.basename(path)}, 404); return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self._cors(); self.end_headers(); self.wfile.write(b)

    def do_OPTIONS(self):
        self.send_response(204); self._cors()
        self.send_header("Content-Length", "0"); self.end_headers()

    def do_GET(self):
        p = self.path.split("?")[0]
        q = {}
        if "?" in self.path:
            for kv in self.path.split("?", 1)[1].split("&"):
                if "=" in kv:
                    k, v = kv.split("=", 1); q[k] = unquote(v)
        try:
            if p in ("/", "/index.html"): return self._file(os.path.join(HERE, "index.html"), "text/html; charset=utf-8")
            if p.startswith("/static/"):
                name = os.path.basename(p[len("/static/"):])
                if not name or name.startswith("."): return self._send({"ok": False, "why": "非法文件名"}, 400)
                ext = os.path.splitext(name)[1].lower()
                ctype = {".png": "image/png", ".js": "text/javascript; charset=utf-8",
                         ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8"}.get(ext, "application/octet-stream")
                return self._file(os.path.join(HERE, name), ctype)
            if p == "/api/health": return self._send(health())
            if p == "/api/model":
                try:
                    with open(MODELBIN, "rb") as f:
                        b = f.read()
                except FileNotFoundError:
                    return self._send({"ok": False, "why": "模型文件不存在：%s" % MODELBIN}, 404)
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(b)))
                self.send_header("Cache-Control", "public, max-age=3600")
                self._cors(); self.end_headers(); self.wfile.write(b)
                return
            if p == "/api/arcs": return self._send(arcs())
            if p == "/api/logs": return self._send(logs(q.get("n", 120)))
            if p == "/api/stats": return self._send(stats())
            if p == "/api/trace": return self._send(trace_tail(q.get("n", 50)))
            if p == "/api/trace/live": return self._send(trace_live())
            if p == "/api/plans": return self._send(plans_list())
            if p == "/api/arc": return self._send(arc_detail(q.get("name", "")))
            if p == "/api/elements": return self._send(elements_list(q.get("q", ""), q.get("limit", 400)))
            if p == "/api/models": return self._send(models_status())
            if p == "/api/snap":
                b = snap_get(q.get("name"))
                if b is None: return self._send({"ok": False, "why": "没有这张快照"}, 404)
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(b)))
                self.send_header("Cache-Control", "no-store")
                self._cors(); self.end_headers(); self.wfile.write(b)
                return
            self._send({"ok": False, "why": "未知路由 %s（看 接口契约.md）" % p}, 404)
        except Exception as e:
            self._send({"ok": False, "why": "服务内部错误：%s" % str(e)[:200]}, 500)

    def do_POST(self):
        p = self.path.split("?")[0]
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads((self.rfile.read(n) if n else b"{}") or b"{}")
        except Exception as e:
            return self._send({"ok": False, "why": "请求体不是 JSON：%s" % str(e)[:80]}, 400)
        try:
            if p == "/api/shot": return self._send(shot(str(body.get("window") or "")))
            if p == "/api/locate":
                return self._send(locate(str(body.get("kind") or "input_box"),
                                         str(body.get("window") or ""), int(body.get("want") or 20)))
            if p == "/api/spine":
                verb = str(body.get("verb") or "")
                if verb not in VERB_OK:
                    return self._send({"ok": False, "why": "动词不在白名单：%r" % verb[:40]}, 400)
                args = body.get("args") or {}
                if not isinstance(args, dict):
                    return self._send({"ok": False, "why": "args 必须是对象"}, 400)
                # 光看 verb 不够：see/text/find 带 --click 也会真点鼠标（脊髓统.py:581 → :667）。
                # 审计用 {"verb":"see","args":{"--click":true}} 就能绕过只查 verb 的预检。
                _moves = verb in DANGEROUS or bool(args.get("--click")) or bool(args.get("--go"))
                if _moves and os.path.exists(STOPF):
                    return self._send({"ok": False, "stopped": True,
                                       "why": "STOP 文件存在：面板层已拒绝动手动词（%s）。删掉 %s 或点「恢复」再试"
                                              % (verb, STOPF)}, 400)
                return self._send(run_spine(verb, args))
            if p == "/api/plan": return self._send(run_plan(body))
            if p == "/api/plan/save": return self._send(plan_save(body))
            if p == "/api/models": return self._send(models_set(body))
            if p == "/api/element":
                return self._send(element_set(str(body.get("template") or ""), bool(body.get("disabled"))))
            if p == "/api/stop":
                on = bool(body.get("on"))
                if on:
                    open(STOPF, "w", encoding="utf-8").write("手动急停 %s\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
                else:
                    try: os.remove(STOPF)
                    except FileNotFoundError: pass
                log("STOP -> %s" % on)
                return self._send({"ok": True, "stop": os.path.exists(STOPF)})
            self._send({"ok": False, "why": "未知路由 %s" % p}, 404)
        except Exception as e:
            self._send({"ok": False, "why": "服务内部错误：%s" % str(e)[:200]}, 500)


def main():
    log("面板服务启动 端口=%d 引擎=%s" % (PORT, os.path.join(SPINE, "脊髓统.py")))
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    srv.daemon_threads = True
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("面板服务收到 Ctrl+C，退出")


if __name__ == "__main__":
    main()
