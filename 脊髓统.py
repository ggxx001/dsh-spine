# 脊髓统.py —— 唯一的通用脊髓入口：大脑只调这几个动词，细节全在本地
# 动词: status | see | click | type | key | scroll | wait | observe | learn | icons | gate
# 用法举例:
#   python 脊髓统.py status
#   python 脊髓统.py see --kind input_box          # 认容器（检测器+几何）
#   python 脊髓统.py see --tpl X.png --click       # 认已知元素并点（过闸+验）
#   python 脊髓统.py click --x 100 --y 200
#   python 脊髓统.py type --text 你好 | key --keys ENTER | scroll --dir down --amount 3
#   python 脊髓统.py wait --stable | wait --seconds 2
#   python 脊髓统.py observe --seconds 5            # 变化 + 类型，不打扰
#   python 脊髓统.py learn --tag DSH --index 3 --name 名字  # 探针→挑→点→验→入库
#   python 脊髓统.py icons --want 20               # 只列可点图标
import os, sys, json, time, subprocess, re
# 2026-11-19：子进程 stdout 默认 GBK，宿主编解码是 UTF-8 → 中文会乱码；强制 UTF-8
for _s in (sys.stdout, sys.stderr):
    try: _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass
HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.environ.get("SPINE_BASE") or os.path.dirname(os.path.abspath(__file__))
PY = os.environ.get("SPINE_PY") or sys.executable
ENV = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
NO_WIN = 0x08000000

# ==== 脊髓控制台 v2（2026-11-22）：结构化轨迹 / 失败归类 / 模型开关 / 动作编排 ====
# 轨迹是**给界面看的**：每次动作落一条 JSONL，带每个阶段的耗时与结果；
# 动作进行中另写一份 trace-live.json，界面靠它实时点亮拓扑。失败时存一张全屏快照。
TRACE_DIR = os.path.join(BASE, "训练", "ui-detect")
TRACEF = os.path.join(TRACE_DIR, "trace.jsonl")
TRACELIVE = os.path.join(TRACE_DIR, "trace-live.json")
FAIL_DIR = os.path.join(TRACE_DIR, "fails")
PLAN_DIR = os.path.join(HERE, "计划")
MODELFLAGS = os.path.join(HERE, "模型开关.json")
MODELS = {"omni_icon": r"E:\models\OmniParser-v2\icon_detect\model.pt",
          "ui_detect": os.path.join(BASE, "训练", "ui-detect", "det", "runs", "ui3", "weights", "best.pt")}
_T = None


def _trace_id():
    return "t" + time.strftime("%Y%m%d-%H%M%S") + "-" + str(int((time.time() % 1) * 1000)).zfill(3)


def model_flags():
    try:
        d = json.load(open(MODELFLAGS, encoding="utf-8"))
    except Exception:
        d = {}
    return {k: bool(d.get(k, True)) for k in MODELS}


def trace_begin(verb):
    global _T
    _T = {"id": _trace_id(), "ts": int(time.time() * 1000), "t0": time.time(),
          "verb": verb, "mode": MODE, "stages": [],
          "parent": arg("--trace-parent", None),
          "window": arg("--window", "") or ""}
    _live()


def _live():
    if _T is None:
        return
    try:
        os.makedirs(TRACE_DIR, exist_ok=True)
        d = dict(_T)
        d.pop("t0", None)
        d.pop("_last_t", None)
        d["done"] = False
        json.dump(d, open(TRACELIVE, "w", encoding="utf-8"), ensure_ascii=False)
    except Exception:
        pass


def mark(name, ok=True, **kw):
    """一个阶段的边界。界面拿 stages[].ms 画时间线，拿最后一段点亮拓扑节点。"""
    if _T is None:
        return
    now = time.time()
    st = {"name": name, "ok": bool(ok), "ms": int((now - _T.get("_last_t", _T["t0"])) * 1000)}
    _T["_last_t"] = now
    for k, v in kw.items():
        if v is not None:
            st[k] = v
    _T["stages"].append(st)
    _live()


def _parse_json_blob(blob):
    s = (blob or "").strip()
    i, j = s.find("{"), s.rfind("}")
    if 0 <= i < j:
        try:
            return json.loads(s[i:j + 1])
        except Exception:
            pass
    return _last_json(blob)


FAIL_RULES = [
    ("stopped", "急停", ("STOP 文件", "stopped")),
    ("gate_lock", "让位闸/锁被占", ("lock_blocked", "另一个进程正在驱动")),
    ("window_not_found", "窗口没找到", ("找不到窗口",)),
    ("not_found", "没定位到", ("没定位到", "没检测到", "OCR 没找到", "都没有命中", "没命中", "没匹配")),
    ("foreground", "前台不是目标窗口", ("前台不是目标窗口",)),
    ("focus_lost", "焦点没落到", ("焦点不在可输入控件", "焦点变了", "按键没到", "没落到目标")),
    ("no_change", "画面与控件都没变", ("画面没变", "控件也没变")),
    ("desktop", "落在桌面/任务栏", ("落在桌面",)),
    ("bad_coords", "坐标非法", ("坐标越界", "缺坐标", "坐标非法")),
    ("verify_failed", "回读判据没过", ("目标状态没等到",)),
    ("no_response", "区域没响应", ("区域变化",)),
    ("suppressed", "历史失败被降权", ("敏化拒绝",)),
    ("rule_blocked", "规则拦下", ("blocked", "规则")),
    ("timeout", "超时", ("timeout", "超时")),
]


def classify_fail(verb, data, blob):
    txt = (blob or "")[:3000] + " " + json.dumps(data if isinstance(data, dict) else {}, ensure_ascii=False)[:2000]
    for code, name, keys in FAIL_RULES:
        if any(k in txt for k in keys):
            return code, name
    return "other", "其它（看回执原文）"


def fail_snap():
    if has("--no-snap") or _T is None:
        return None
    try:
        from PIL import ImageGrab
        os.makedirs(FAIL_DIR, exist_ok=True)
        p = os.path.join(FAIL_DIR, time.strftime("%Y%m%d-%H%M%S") + "-" + str(_T.get("verb") or "x") + ".png")
        ImageGrab.grab(all_screens=True).save(p)
        return os.path.relpath(p, BASE).replace("\\", "/")
    except Exception:
        return None


def trace_end(rc, blob):
    global _T
    if _T is None:
        return
    data = _parse_json_blob(blob)
    if not isinstance(data, dict):
        data = {"raw": str(blob or "")[:600]}
    if ("{" in (blob or "")) and ("ok" in data):
        ok = bool(data.get("ok"))
    else:
        ok = (rc == 0)
    why = str(data.get("why") or data.get("error") or "")[:300]
    src = ""
    try:
        cands = data.get("candidates") or []
        if cands and isinstance(cands[0], dict):
            src = str(cands[0].get("src") or "")
        if not src:
            src = ",".join(str(x) for x in (data.get("used") or []))[:80]
    except Exception:
        src = ""
    line = {"id": _T["id"], "ts": int(time.time() * 1000), "verb": _T.get("verb"),
            "mode": _T.get("mode"), "ok": ok, "rc": rc,
            "ms": int((time.time() - _T["t0"]) * 1000),
            "why": why, "src": src,
            "at": data.get("at") or data.get("clicked"),
            "window": (data.get("window") or _T.get("window") or "")[:80],
            "fg": fg_title()[:80],
            "stages": _T.get("stages") or [],
            "parent": _T.get("parent"),
            "category": None}
    if not ok:
        code, name = classify_fail(_T.get("verb"), data, blob)
        line["category"] = code
        line["category_name"] = name
        line["snap"] = fail_snap()
    try:
        os.makedirs(TRACE_DIR, exist_ok=True)
        if os.path.exists(TRACEF) and os.path.getsize(TRACEF) > 8 * 1024 * 1024:
            os.replace(TRACEF, TRACEF.replace(".jsonl", ".1.jsonl"))
        with open(TRACEF, "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    except Exception:
        pass
    try:
        live = dict(line)
        live["done"] = True
        json.dump(live, open(TRACELIVE, "w", encoding="utf-8"), ensure_ascii=False)
    except Exception:
        pass
    _T = None


def models_cmd():
    flags = model_flags()
    setarg = arg("--set", "")
    if setarg:
        for part in str(setarg).split(","):
            if "=" in part:
                k, v = part.split("=", 1)
                k = k.strip()
                if k in MODELS:
                    flags[k] = v.strip().lower() in ("1", "on", "true", "yes", "开")
        try:
            json.dump(flags, open(MODELFLAGS, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        except Exception as e:
            return 1, json.dumps({"ok": False, "why": "写模型开关失败：%s" % str(e)[:100]}, ensure_ascii=False)
        mark("模型开关", True, flags=flags)
    out = {"ok": True, "flags": flags, "models": {}}
    for k, p in MODELS.items():
        ex = os.path.exists(p)
        out["models"][k] = {"path": p, "exists": ex, "enabled": flags[k],
                            "size": os.path.getsize(p) if ex else 0,
                            "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(p))) if ex else None}
    return 0, json.dumps(out, ensure_ascii=False)


def _args_to_cli(args):
    out = []
    for k, v in (args or {}).items():
        k = str(k)
        if not k.startswith("--"):
            k = "--" + k
        if v is True:
            out.append(k)
        elif v is False or v is None:
            continue
        elif isinstance(v, (list, tuple)):
            for x in v:
                out += [k, str(x)]
        else:
            out += [k, str(v)]
    return out


def plan_cmd():
    sub = sys.argv[2] if len(sys.argv) > 2 else "list"
    os.makedirs(PLAN_DIR, exist_ok=True)
    if sub in ("list", "ls"):
        items = []
        for fn in sorted(os.listdir(PLAN_DIR)):
            if not fn.endswith(".json"):
                continue
            try:
                d = json.load(open(os.path.join(PLAN_DIR, fn), encoding="utf-8"))
            except Exception as e:
                items.append({"name": fn[:-5], "file": fn, "ok": False, "why": str(e)[:80]})
                continue
            items.append({"name": d.get("name") or fn[:-5], "file": fn,
                          "desc": d.get("desc") or "", "steps": len(d.get("steps") or []),
                          "saved": time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(os.path.join(PLAN_DIR, fn))))})
        return 0, json.dumps({"ok": True, "count": len(items), "plans": items}, ensure_ascii=False)
    name = str(arg("--name", "") or "").strip()
    if not name and sub not in ("list", "ls"):
        return 2, json.dumps({"ok": False, "why": "plan 需要 --name"}, ensure_ascii=False)
    path = os.path.join(PLAN_DIR, name + ".json")
    if sub in ("show", "get"):
        try:
            d = json.load(open(path, encoding="utf-8"))
        except Exception as e:
            return 1, json.dumps({"ok": False, "why": "没有这个计划或读不出：%s" % str(e)[:80]}, ensure_ascii=False)
        return 0, json.dumps({"ok": True, "plan": d}, ensure_ascii=False)
    if sub in ("save", "put"):
        src = str(arg("--file", "") or "")
        try:
            d = json.load(open(src, encoding="utf-8"))
        except Exception as e:
            return 1, json.dumps({"ok": False, "why": "读不到来源 JSON：%s" % str(e)[:80]}, ensure_ascii=False)
        steps = d.get("steps")
        if not isinstance(steps, list) or not steps:
            return 2, json.dumps({"ok": False, "why": "计划必须有非空 steps 数组"}, ensure_ascii=False)
        for i, st in enumerate(steps):
            if not isinstance(st, dict) or not (st.get("verb") or st.get("do")):
                return 2, json.dumps({"ok": False, "why": "第 %d 步缺 verb" % (i + 1)}, ensure_ascii=False)
        d["name"] = d.get("name") or name
        try:
            json.dump(d, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        except Exception as e:
            return 1, json.dumps({"ok": False, "why": "写计划失败：%s" % str(e)[:100]}, ensure_ascii=False)
        return 0, json.dumps({"ok": True, "saved": path, "steps": len(steps)}, ensure_ascii=False)
    if sub in ("run", "step"):
        if os.path.exists(os.path.join(HERE, "STOP")):
            return 3, json.dumps({"ok": False, "stopped": True,
                                  "why": "STOP 文件存在：计划执行已停用（删掉 %s 恢复）" % os.path.join(HERE, "STOP")},
                                 ensure_ascii=False)
        try:
            d = json.load(open(path, encoding="utf-8"))
        except Exception as e:
            return 1, json.dumps({"ok": False, "why": "没有这个计划：%s" % str(e)[:80]}, ensure_ascii=False)
        steps = d.get("steps") or []
        only = str(arg("--step", "") or "").strip()
        dry = has("--dry")
        keep = has("--keep-going")
        parent = (_T or {}).get("id")
        idxs = [int(only) - 1] if only.isdigit() else list(range(len(steps)))
        results = []
        for i in idxs:
            if i < 0 or i >= len(steps):
                return 2, json.dumps({"ok": False, "why": "步号越界：%s（共 %d 步）" % (only, len(steps))}, ensure_ascii=False)
            st = steps[i]
            verb = str(st.get("verb") or st.get("do"))
            args = st.get("args") or {}
            cmd = [PY, os.path.join(HERE, "脊髓统.py"), verb] + _args_to_cli(args)
            if dry:
                cmd += ["--dry"]
            if parent:
                cmd += ["--trace-parent", parent]
            t0 = time.time()
            try:
                p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                   env=ENV, creationflags=NO_WIN, timeout=int(st.get("timeout") or 180))
                out = ((p.stdout or "") + (p.stderr or "")).strip()
                rc = p.returncode
            except Exception as e:
                out, rc = "子进程失败：%s" % str(e)[:200], 1
            data = _parse_json_blob(out)
            ok = bool(data.get("ok")) if isinstance(data, dict) and "ok" in data else (rc == 0)
            ms = int((time.time() - t0) * 1000)
            why = str((data or {}).get("why") or "")[:160] if isinstance(data, dict) else ""
            results.append({"step": i + 1, "verb": verb, "ok": ok, "ms": ms, "why": why,
                            "out": out[-800:]})
            mark("步骤%d·%s" % (i + 1, verb), ok, why=why or None)
            if not ok and not keep:
                break
        allok = all(r["ok"] for r in results)
        return (0 if allok else 1), json.dumps({"ok": allok, "name": name, "steps": len(steps),
                                               "ran": len(results), "dry": dry,
                                               "results": results}, ensure_ascii=False)
    return 2, json.dumps({"ok": False, "why": "未知 plan 子命令：%s（list/show/save/run/step）" % sub}, ensure_ascii=False)


def run_py(script, *args, timeout=300):
    r = subprocess.run([PY, os.path.join(HERE, script)] + list(args), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=ENV, creationflags=NO_WIN, timeout=timeout)
    return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()

def run_ps(*args, timeout=120):
    r = subprocess.run(["pwsh", "-NoProfile", "-STA", "-File", os.path.join(HERE, "动作.ps1")] + list(args),
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       env=ENV, creationflags=NO_WIN, timeout=timeout)
    return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()

# 两种模式：fast=无脑快路（可逆、低风险）；careful=认真慢路（不可逆、对外）
# 规矩：对外发布/删东西/动账号/写文件 → 必须 careful；其余默认 fast
MODE = "careful" if "--careful" in sys.argv else ("fast" if "--fast" in sys.argv else "fast")

def arg(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default

def has(flag): return flag in sys.argv

def status():
    import urllib.request
    out = {"gate": False, "service": False, "elements": 0, "models": {}}
    rc, blob = run_ps("-Action", "probe")
    out["gate"] = "gate ok=True" in blob
    try:
        st = json.loads(urllib.request.urlopen("http://127.0.0.1:8811/status", timeout=5).read().decode())
        out["service"] = bool(st.get("ok")); out["interfaces"] = list((st.get("interfaces") or {}).keys())
    except Exception as e:
        out["service_error"] = str(e)[:80]
    try:
        le = json.load(open(os.path.join(HERE, "学习到的元素.json"), encoding="utf-8"))
        items = le.get("items", [])
        out["elements"] = len(items)
        out["mode"] = MODE
        out["disabled"] = sum(1 for i in items if i.get("disabled"))
    except Exception: pass
    for w, name in ((r"E:\models\OmniParser-v2\icon_detect\model.pt", "omni_icon"),
                    (os.path.join(BASE, "训练", "ui-detect", "det", "runs", "ui3", "weights", "best.pt"), "ui_detect")):
        out["models"][name] = os.path.exists(w)
    return 0, json.dumps(out, ensure_ascii=False)

def verify_target(box, seconds=0.4):
    """点击前确认：目标此刻还在（用 /perceive 的实时跟踪），不在就不点"""
    import urllib.request
    try:
        body = json.dumps({"seconds": seconds, "every": 0.2}).encode()
        req = urllib.request.Request("http://127.0.0.1:8811/perceive", data=body,
                                     headers={"Content-Type": "application/json"})
        d = json.loads(urllib.request.urlopen(req, timeout=60).read().decode())
    except Exception as e:
        return False, "perceive 不可用：" + str(e)[:60]
    x, y, w, h = box
    for tr in d.get("tracks", []):
        b = tr.get("last_box") or []
        if len(b) != 4: continue
        ix = max(0, min(x + w, b[0] + b[2]) - max(x, b[0]))
        iy = max(0, min(y + h, b[1] + b[3]) - max(y, b[1]))
        inter = ix * iy
        if inter > 0.4 * min(w * h, b[2] * b[3]):
            return True, "跟踪命中 %s(%s)" % (tr.get("id"), tr.get("kind"))
    return False, "跟踪里没有这个目标（可能被盖住/已消失）"

def fg_title():
    """前台窗口标题（用来只截那一块，8.7s → 1s 的关键）"""
    try:
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        h = u.GetForegroundWindow()
        n = u.GetWindowTextLengthW(h)
        if not n: return ""
        buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(h, buf, n + 1)
        return buf.value.strip()
    except Exception:
        return ""


def focus_window(title):
    """2026-11-19：SetForegroundWindow 常失败，用最小化→还原把窗口真拉到前台"""
    try:
        import ctypes, time as _t
        u = ctypes.windll.user32
        u.SetProcessDPIAware()
        found = []
        EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def cb(h, l):
            if u.IsWindowVisible(h):
                n = u.GetWindowTextLengthW(h)
                if n:
                    b = ctypes.create_unicode_buffer(n + 1)
                    u.GetWindowTextW(h, b, n + 1)
                    if title.lower() in b.value.lower():
                        found.append(h)
            return True
        u.EnumWindows(EnumProc(cb), 0)
        if not found:
            return False, "找不到窗口"
        h = found[0]
        if u.GetForegroundWindow() == h:
            return True, "已在前台"
        # 2026-11-20 剪辑师实测：DSH 自己会抢前台，把脊髓的点击全截走（focus_after 一直是 DSH）。
        # 对策：目标不是前台时，若前台是 DSH，先把它最小化让开，再激活目标。
        _fg = u.GetForegroundWindow()
        if _fg and _fg != h:
            _n = u.GetWindowTextLengthW(_fg)
            _b = ctypes.create_unicode_buffer(_n + 1)
            u.GetWindowTextW(_fg, _b, _n + 1)
            if "deepseek harness" in _b.value.lower():
                u.ShowWindow(_fg, 6)   # SW_MINIMIZE：让开，别让它截点击
                _t.sleep(0.35)
        u.ShowWindow(h, 6); _t.sleep(0.5)
        u.ShowWindow(h, 9); _t.sleep(0.5)
        if u.GetForegroundWindow() == h:
            return True, "最小化→还原置前成功"
        u.SetForegroundWindow(h); _t.sleep(0.4)
        return (u.GetForegroundWindow() == h), "尝试 SetForegroundWindow"
    except Exception as e:
        return False, "置前异常：%s" % str(e)[:60]


# ==== 键盘路径（2026-11-20 修：资源管理器里 Ctrl+L 不生效）====
# 诊断：key → 动作.ps1 -Action key → 输入序列.ps1 Human-Key → SendKeys.SendWait。
# SendKeys **没有窗口参数**，它把键打进「当前前台窗口的焦点控件」——谁在前台、谁有焦点，它一概不管，
# 而 Human-Key 只写一行 "KEY ^L" 就算完，于是日志能证明「我发了 ^L」，证明不了「它到了地址栏」。
# 这次实测两件事：
#   1) 本文件 126-137 行的注释已经写明 DSH 会抢前台 —— 抢走之后 SendKeys 打的是 DSH，不是资源管理器；
#   2) 就算打中了资源管理器，焦点还在列表（ListItemControl）上，^L 是框架级加速键，
#      没落在编辑框上时经常被子控件吞掉；同一轮里可靠拿到 EditControl:地址栏 的办法是鼠标点地址栏。
# 所以可靠做法不是「再发一遍 ^L」，而是：先把焦点落到地址栏编辑框（UIA SetFocus，不行就点它），
# 回读焦点确认是 Edit 再发键；全程把焦点前后写进返回，不报假成功。
def _dpi():
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            import ctypes
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _uia():
    import uiautomation as _ua
    _ua.SetGlobalSearchTimeout(0.8)
    return _ua


def focused_rect():
    """当前焦点控件的矩形与描述，形如 ((l,t,r,b), 'EditControl:发消息或创建任务')。
    2026-11-22（#48d0ac 四）：Chromium/Electron 的网页画布在 UIA 里是 GroupControl，
    真正的输入框是焦点控件 EditControl——所以「点没点中输入框」要靠落点是否在焦点控件矩形里，
    不能只看 control_at 的类型。"""
    try:
        c = _uia().GetFocusedControl()
        if not c:
            return None, None
        info = ((c.ControlTypeName or "") + ":" + (c.Name or ""))[:60]
        try:
            r = c.BoundingRectangle
            rect = (int(r.left), int(r.top), int(r.right), int(r.bottom))
        except Exception:
            rect = None
        return rect, info
    except Exception:
        return None, None


def point_in_rect(rect, x, y, pad=2):
    if not rect:
        return False
    l, t, r, b = rect
    return (l - pad) <= x <= (r + pad) and (t - pad) <= y <= (b + pad)


def focused_info():
    """当前焦点控件，形如 'EditControl:地址栏'。用它证明「键发给了谁」。"""
    try:
        c = _uia().GetFocusedControl()
        if not c:
            return None
        return ((c.ControlTypeName or "") + ":" + (c.Name or ""))[:60]
    except Exception:
        return None


DESKTOP_CLASSES = ("Progman", "WorkerW", "SHELLDLL_DefView", "SysListView32", "Shell_TrayWnd")


def window_at(x, y):
    """坐标落在哪个顶层窗口上。返回 (hwnd, class, title)。
    2026-11-21：队友实测「几何件在壁纸上给假候选，线上 9/10 落在记事本窗口外，
    不加断言点击会送到桌面」——点击前必须知道目标是不是一个真实窗口，而不是桌面。"""
    import ctypes
    from ctypes import wintypes

    class PT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    u = ctypes.windll.user32
    h = u.WindowFromPoint(PT(int(x), int(y)))
    if not h:
        return 0, "", ""
    root = u.GetAncestor(h, 2) or h  # GA_ROOT = 2
    cls = ctypes.create_unicode_buffer(256)
    u.GetClassNameW(root, cls, 256)
    n = u.GetWindowTextLengthW(root)
    buf = ctypes.create_unicode_buffer(n + 1)
    u.GetWindowTextW(root, buf, n + 1)
    return root, cls.value, buf.value


def is_desktop(win_class):
    c = win_class or ""
    return any(d in c for d in DESKTOP_CLASSES)


# 2026-11-22（吸收 Kimi 建议里真正有用的那半句）：候选中心点的控件类型校验。
# 我们原来只做 WindowFromPoint（查「是不是窗口」），没查「中心点上是什么控件」。
# 集合刻意宽松：真实界面上大量可点区域不是标准控件，硬拒会误伤正常点击。
# 2026-11-22（独立验收 #50477c 的批评，它说得对）：
# 上一版为了消除 P4 的「不一致」，把容器类（Pane/Window/Group/List/Image）也塞进了可点集合，
# 结果 type_clickable 恒为 true、--strict-type 在空白 Pane 上照样放行 → 这个闸等于没开。
# 现在改成**闭合的叶子集合**：只有「本体可点」的控件类型才算 true；
# 容器类一律 false。想点容器（网页空白、窗格）就别加 --strict-type。
CLICKABLE_TYPES = {
    "ButtonControl", "MenuItemControl", "ListItemControl", "TreeItemControl",
    "TabItemControl", "CheckBoxControl", "RadioButtonControl", "HyperlinkControl",
    "ComboBoxControl", "EditControl", "DocumentControl",
    "SplitButtonControl", "MenuItemBase", "ListItemBase",
}

# 2026-11-22（独立验收会话指出 type_clickable 区分度太低）：
# 容器类（Group/Pane/Window）被判 type_clickable=true 是合理的——点它内部也算点它，
# 但这也意味着「几乎任何位置都 true」，于是它**只能当粗筛，不能当安全判据**。
# 所以再给一个更有区分度的字段：is_leaf —— 这个坐标上是不是**叶子控件**（按钮/菜单项这类本体）。
LEAF_TYPES = {
    "ButtonControl", "MenuItemControl", "ListItemControl", "TreeItemControl",
    "TabItemControl", "CheckBoxControl", "RadioButtonControl", "HyperlinkControl",
    "ComboBoxControl", "EditControl", "TextControl", "ImageControl",
    "SplitButtonControl", "ListItemBase",
}


def _ctrl_rect(c):
    try:
        r = c.BoundingRectangle
        return (int(r.left), int(r.top), int(r.right), int(r.bottom))
    except Exception:
        return None


def _ctrl_at_deepest(x, y, depth=6):
    """从 ControlFromPoint 的控件往下钻到「包含该点、面积最小」的那一层。
    2026-11-22（#cddedc 剪辑线实测）：ControlFromPoint 在资源管理器详细列表里返回的是
    列表容器/首项，不是 (x,y) 真正压着的那一项——导致 control_before == control_after 的假失败。"""
    u = _uia()
    c = u.ControlFromPoint(int(x), int(y))
    if not c:
        return None
    best = c
    for _ in range(depth):
        try:
            kids = best.GetChildren() or []
        except Exception:
            break
        pick = None
        pick_area = None
        for k in kids:
            r = _ctrl_rect(k)
            if not r:
                continue
            l, t, rr, b = r
            if not (l <= x <= rr and t <= y <= b):
                continue
            area = max(1, (rr - l) * (b - t))
            if pick_area is None or area < pick_area:
                pick, pick_area = k, area
        if pick is None:
            break
        best = pick
    return best


def control_chain_at(x, y, depth=6):
    """从「落点上的控件」一路往下到最深的子控件，返回 ["类型:名字", ...]。
    2026-11-22（#cddedc）：单看最深一层会被按钮里的 Text 标签骗（点按钮最深是 TextControl:7，
    而焦点落在 ButtonControl:七）；单看最浅一层会把容器里换项当成「没变」。
    判定要用整条链——焦点只要落在这条链上就算点中，链变了就算身份变了。"""
    try:
        u = _uia()
        c = u.ControlFromPoint(int(x), int(y))
        if not c:
            return []
        chain = []
        best = c
        for _ in range(depth):
            chain.append("%s:%s" % (best.ControlTypeName, (best.Name or "")[:24]))
            try:
                kids = best.GetChildren() or []
            except Exception:
                break
            pick = None
            pick_area = None
            for k in kids:
                r = _ctrl_rect(k)
                if not r:
                    continue
                l, t, rr, b = r
                if not (l <= x <= rr and t <= y <= b):
                    continue
                area = max(1, (rr - l) * (b - t))
                if pick_area is None or area < pick_area:
                    pick, pick_area = k, area
            if pick is None:
                break
            best = pick
        return chain
    except Exception:
        return []


def control_at(x, y):
    """这个坐标上现在是哪个控件（"类型:名字"）。点完用它判断「点到的东西变了没有」。
    2026-11-22：改为钻到最深一层（容器 → 具体项/按钮），否则同一容器里换项会被当成「没变」。"""
    try:
        c = _ctrl_at_deepest(int(x), int(y))
        if not c:
            return None
        return "%s:%s" % (c.ControlTypeName, (c.Name or "")[:24])
    except Exception:
        return None


def _last_json(blob):
    """从 ps1 的一堆输出里挑最后一行 JSON。"""
    for line in reversed((blob or "").splitlines()):
        s = line.strip()
        if s.startswith("{") and s.endswith("}"):
            try:
                return json.loads(s)
            except Exception:
                continue
    return {"ok": False, "raw": (blob or "")[-160:]}


def is_edit(info):
    return bool(info) and ("Edit" in info or "Document" in info)


def find_address_bar(win_title):
    """在窗口里找地址栏编辑框（Win11 资源管理器：EditControl, Name='地址栏', AutomationId='1001'）。"""
    if not win_title:
        return None
    try:
        ua = _uia()
        win = ua.WindowControl(searchDepth=1, SubName=win_title)
        if not win.Exists(1, 1):
            return None
        for kw in ({"Name": "地址栏"}, {"AutomationId": "1001"}):
            try:
                c = win.EditControl(**kw)
                if c.Exists(1, 1):
                    return c
            except Exception:
                pass
        try:
            c = win.EditControl(searchDepth=12, RegexName=r"地址|Address")
            if c.Exists(1, 1):
                return c
        except Exception:
            pass
        return None
    except Exception:
        return None


def focus_control(ctl):
    """UIA SetFocus，并回读焦点确认真落上了。"""
    try:
        ctl.SetFocus()
        time.sleep(0.25)
        return is_edit(focused_info())
    except Exception:
        return False


def click_in_control(ctl, fx=0.55, fy=0.5):
    """点控件内部某相对位置。地址栏点靠右一点（0.55），避开左边图标和已选中的文字 ——
    实测这条路径能稳定拿到 EditControl:地址栏。"""
    try:
        r = ctl.BoundingRectangle
        if not r:
            return False, 0, 0
        w, h = r.right - r.left, r.bottom - r.top
        if w < 4 or h < 4:
            return False, 0, 0
        x, y = int(r.left + w * fx), int(r.top + h * fy)
        rc, _blob = run_ps("-Action", "click", "-X", str(x), "-Y", str(y))
        return rc == 0, x, y
    except Exception:
        return False, 0, 0


def key_cmd():
    """key 动词：保证「谁在前台、谁有焦点」，再发键；地址栏类目标不靠 ^L 加速键。"""
    keys = arg("--keys", "")
    want_window = arg("--window", "")
    _dpi()
    win = want_window or fg_title()
    # 1) 前台守卫：SendKeys 只打前台。前台不是目标窗口就拒绝 —— 键打进别人窗口比按错更糟。
    if want_window:
        fg = fg_title() or ""
        if want_window.lower() not in fg.lower():
            focus_window(want_window)
            time.sleep(0.2)
            fg = fg_title() or ""
        if want_window.lower() not in fg.lower():
            return 1, json.dumps({"ok": False, "why": "前台不是目标窗口，拒绝发键（键会打进别人窗口）",
                                  "want": want_window, "foreground": fg,
                                  "hint": "先 spine focus --window，或核对窗口标题"}, ensure_ascii=False)
    f0 = focused_info()
    mark("前台闸", True, window=win)
    norm = (keys or "").strip().lower().replace(" ", "")
    want_addr = norm in ("^l", "ctrl+l", "l+ctrl", "{ctrl}l")
    want_edit = want_addr or has("--ensure-edit")
    method, addr = "sendkeys", None
    if want_edit:
        addr = find_address_bar(win)
        if addr is not None:
            if focus_control(addr):
                method = "uia-setfocus"
            else:
                _ok, _x, _y = click_in_control(addr, 0.55, 0.5)
                if _ok:
                    method = "click-addressbar"
            time.sleep(0.2)
    f1 = focused_info()
    mark("焦点", is_edit(f1), focus=f1)
    # 2) 只有「^L 且焦点已经被我们落到地址栏」才算完成：目标状态（地址栏进入输入态）已经达成，
    #    再发一遍 ^L 反而会把它切回去（Win11 的 ^L 是切换语义）。
    #    注意不覆盖 --ensure-edit 的普通按键：那种情况只是要求「发键前先有个编辑框有焦点」，键还是要发。
    if want_addr and is_edit(f1) and method != "sendkeys":
        _edit_ready = True
        try:
            _edit_ready = bool(addr is not None and addr.GetValuePattern() is not None)
        except Exception:
            _edit_ready = True
        mark("回读焦点", True, method=method)
        return 0, json.dumps({"ok": True, "sent": "", "method": method, "edit_ready": _edit_ready,
                              "focus_before": f0, "focus_after": f1, "window": win,
                              "why": "焦点已在地址栏编辑框，不再发 ^L（避免把地址栏切回非编辑态）；要输入请接着 spine type"},
                             ensure_ascii=False)
    # 3) 其余键照发，但要报「发给了谁」
    rc2, b2 = run_ps("-Action", "key", "-Keys", keys)
    mark("发键", rc2 == 0, keys=keys)
    if rc2 == 6:
        return 6, json.dumps({"ok": False, "why": "lock_blocked：另一个进程正在驱动（脊髓锁 15s）"}, ensure_ascii=False)
    time.sleep(0.25)
    f2 = focused_info()
    # 2026-11-21 修（D4）：键发出去 != 键落到了。被全局热键截走时焦点会跳到别的窗口/控件，
    # 那时原来也报 ok:true（实测 Ctrl+A 被钉钉截图热键截走、输入框一个字没删）。
    # 判据：发键前后焦点控件类型必须一致；换了类型就判「没落到」，并把前后焦点写进回执。
    def _ctype(s):
        return (s or "").split(":", 1)[0].strip()
    focus_kept = (not f0) or (_ctype(f0) == _ctype(f2))
    # 2026-11-21 修（D4 残留，我手测发现）：焦点在同一类控件上时，键"发出去了"仍被报 ok:true。
    #  实测：焦点在工具栏 ButtonControl:列表 上按 {END}，回执 ok:true/focus_kept:true，
    #  但一个字都没落进文本。焦点前后同类型 => 原来的判据看不到"键根本没进编辑区"。
    #  新判据：文本编辑类键（单字符 / ^a^c^v^x^z / 方向 / Home/End / Backspace/Delete）
    #  要求发键前焦点就在可输入控件里；不在就判没落到，并报当前焦点。
    #  不含 ENTER/TAB/ESC/F1-12/Space：那些本来就该打在按钮和菜单上。
    _edit_key = bool(re.fullmatch(r"(\^[acvxz]|[a-z0-9]|\{(home|end|left|right|up|down|backspace|delete)\})", norm))
    _focus_editable = is_edit(f0) if f0 else None
    out = {"ok": rc2 == 0 and focus_kept, "sent": keys, "method": method,
           "focus_before": f0, "focus_after": f2, "focus_kept": focus_kept,
           "focus_editable": _focus_editable, "text_key": _edit_key,
           "window": win, "note": b2[-120:]}
    if _edit_key and _focus_editable is False:
        out["ok"] = False
        out["why"] = "焦点不在可输入控件（当前 " + str(f0) + "）：这是文本编辑类键，打不进任何输入框，不算落到目标上"
    if rc2 == 0 and not focus_kept:
        out["why"] = "键发出去了但焦点变了（" + _ctype(f0) + " → " + _ctype(f2) + "）：可能被全局热键截走或目标窗口失焦，不算落到目标上"
    if want_addr:
        out["ok"] = (rc2 == 0) and is_edit(f2)
        if not out["ok"]:
            out["why"] = "发了 ^L 但焦点没进编辑框（键没到地址栏）：先 spine click 地址栏再 key，或直接 spine type"
    mark("回读焦点", bool(out.get("ok")), focus_after=f2)
    return rc2, json.dumps(out, ensure_ascii=False)


def snap_input_box(blob, kind):
    """2026-11-22（#48d0ac 四）：检测器给的 input_box 框可能整片偏下（实测 DSH 偏 58px，
    框中心落在输入框下方 → 点不进去）。当窗口里有可输入的焦点控件、且与候选框水平高度重叠时，
    用焦点控件矩形作为权威框插到第一位（src=focus_rect, score=1.0）。
    只在 kind 为空或 input_box 时做；焦点不在可输入控件上就原样返回。"""
    if kind and kind != "input_box":
        return blob
    try:
        d = json.loads(blob)
    except Exception:
        return blob
    if not isinstance(d, dict):
        return blob
    cands = d.get("candidates") or []
    rect, fi = focused_rect()
    if not rect or not is_edit(fi):
        return blob
    wl, wt, wr, wb = rect
    wwin = str(d.get("window") or "").replace("(printwindow)", "").strip()
    if wwin:
        cur = fg_title() or ""
        if wwin not in cur and cur not in wwin:
            return blob
    top = (cands[0].get("box") if cands else None) or [wl, wt, wr - wl, wb - wt]
    tl, tt, tw, th = top
    ov = max(0, min(wl + (wr - wl), tl + tw) - max(wl, tl))
    if tw <= 0 or ov / float(tw) < 0.6:
        return blob
    snapped = {"box": [wl, wt, wr - wl, wb - wt], "score": 1.0, "src": "focus_rect",
               "role": (fi or "").split(":", 1)[0], "text": (fi or "").split(":", 1)[1] if ":" in (fi or "") else ""}
    d["candidates"] = [snapped] + [c for c in cands if c.get("box") != snapped["box"]]
    d["snapped"] = {"why": "检测框与焦点输入控件水平重叠 %.0f%%，用焦点矩形校正" % (100.0 * ov / tw),
                    "from": top, "to": snapped["box"]}
    return json.dumps(d, ensure_ascii=False)


def see():
    kind = arg("--kind"); tpl = arg("--tpl")
    if tpl:
        rc, blob = run_py("手眼.py", "see", tpl)
    else:
        # 2026-11-19：默认只截前台窗口（整屏 12.5s → 窗口内 1.1s）
        win = arg("--window", "")
        if not win and not has("--full"):
            win = fg_title() or "@fg"
        _args = ["--kind", kind or "input_box"]
        if win:
            _args += ["--window", win]
        # 2026-11-19：优先打常驻服务（模型已预热，~220ms）；每次新起 Python 光加载权重就 2-3 秒
        rc, blob = 1, ""
        try:
            import urllib.request as _ur
            _body = json.dumps({"kind": kind or "input_box", "window": win,
                                "models": ({"ui_detect": False, "omni_icon": False} if has("--no-model") else model_flags())}).encode()
            _req = _ur.Request("http://127.0.0.1:8811/locate2", data=_body,
                               headers={"Content-Type": "application/json"})
            _resp = _ur.urlopen(_req, timeout=30).read().decode("utf-8", "replace")
            rc, blob = 0, _resp
        except Exception:
            rc, blob = run_py("脊髓2.py", "locate", *_args)
    if not tpl:
        # 2026-11-22（重启后自测发现）：指定窗口没找到时，snap 仍把「当前焦点输入框」（DSH 自己的）
        # 当成候选塞了进来——等于用别的窗口的控件冒充目标窗口。窗口没找到就不要再补。
        try:
            _probe = json.loads(blob)
        except Exception:
            _probe = None
        _no_win = isinstance(_probe, dict) and str(_probe.get("why", "")).startswith("找不到窗口")
        if not _no_win:
            blob = snap_input_box(blob, kind)
    _parsed = _parse_json_blob(blob) if isinstance(blob, str) else {}
    _psrc = ""
    try:
        _pc = (_parsed or {}).get("candidates") or []
        if _pc and isinstance(_pc[0], dict):
            _psrc = str(_pc[0].get("src") or "")
        if not _psrc:
            _psrc = ",".join(str(x) for x in ((_parsed or {}).get("used") or []))
    except Exception:
        pass
    mark("定位", ok=(bool((_parsed or {}).get("ok")) if isinstance(_parsed, dict) and "ok" in (_parsed or {}) else rc == 0),
         src=_psrc or None, n=len((_parsed or {}).get("candidates") or []) if isinstance(_parsed, dict) else None)
    if has("--click"):
        if MODE == "careful":
            # 慢路：先存底图，点完再验一次变化
            run_py("手眼.py", "stable", timeout=120)
        box = None
        # 2026-11-19 修：服务用 indent=2 输出，多行 JSON——先整体解析，失败再按行
        _whole = None
        try:
            _whole = json.loads(blob)
        except Exception:
            _whole = None
        if isinstance(_whole, dict):
            box = _whole.get("box") or ((_whole.get("candidates") or [{}])[0].get("box"))
        if not box:
            for line in blob.splitlines():
                s = line.strip()
                if not s.startswith("{"):
                    continue
                try:
                    d = json.loads(s)
                except Exception:
                    continue
                box = d.get("box") or ((d.get("candidates") or [{}])[0].get("box")) or box
        _unverified = ""
        if has("--dry"):
            mark("点前校验", bool(box), dry=True)
            return 0, json.dumps({"ok": bool(box), "dry": True, "box": box, "would_click": bool(box)}, ensure_ascii=False)
        # 2026-11-20 敏化（这条才是真路径）：模板命中后、点击前，用该模板的历史成功率降权
        if box and tpl:
            try:
                import importlib.util as _il5
                _sp5 = _il5.spec_from_file_location("elem_fb4", os.path.join(HERE, "元素反馈.py"))
                _fb5 = _il5.module_from_spec(_sp5); _sp5.loader.exec_module(_fb5)
                _score5 = 0.0
                if isinstance(_whole, dict):
                    _score5 = float(_whole.get("score") or 0)
                _pen5 = _fb5.penalty(tpl)
                # 2026-11-20 独立会话：1.0×0.5 正好 =0.5，用 < 会把「精确命中但历史全败」放过去 → 改 <=
                if _pen5 < 1.0 and _score5 > 0 and _score5 * _pen5 <= 0.5:
                    mark("敏化", False, why="历史失败降权")
                    return 1, json.dumps({"ok": False,
                                          "why": "敏化拒绝：这条模板历史失败多（权重 %.2f，加权分 %.2f ≤ 0.5）" % (_pen5, _score5 * _pen5),
                                          "penalty": round(_pen5, 3), "raw_score": _score5, "tpl": tpl},
                                         ensure_ascii=False)
            except Exception as _e5:
                try:
                    with open(os.path.join(HERE, "脊髓统-异常.log"), "a", encoding="utf-8") as _lf5:
                        _lf5.write("%s 敏化(see路径) 失败: %s\n" % (time.strftime("%H:%M:%S"), str(_e5)[:120]))
                except Exception:
                    pass
        if box:
            okv, whyv = verify_target(box)
            if not okv:
                # 2026-11-19：几何路径的单帧框常常不在跟踪里，但不是错框。
                # 判据改成「叶子尺寸 + 落在前台窗口内」就放行，但把未验事实写进结果，不假装验过。
                import ctypes
                from ctypes import wintypes
                u = ctypes.windll.user32
                # 2026-11-19：按「目标窗口」的矩形判断，不按当前前台（前台可能是别的窗口）
                _win = win or fg_title()
                rc0 = wintypes.RECT()
                _got = False
                if _win:
                    try:
                        _p = run_py("探针.py", "--help")  # 仅确保脚本在
                    except Exception:
                        pass
                    try:
                        import importlib.util as _il
                        _spec = _il.spec_from_file_location("probe_m", os.path.join(HERE, "探针.py"))
                        _pm = _il.module_from_spec(_spec); _spec.loader.exec_module(_pm)
                        _wr = _pm.win_rect(_win)
                        if _wr:
                            rc0.left, rc0.top, rc0.right, rc0.bottom = _wr[0], _wr[1], _wr[0] + _wr[2], _wr[1] + _wr[3]
                            _got = True
                    except Exception:
                        _got = False
                if not _got:
                    h = u.GetForegroundWindow()
                    u.GetWindowRect(h, ctypes.byref(rc0))
                inside = (box[0] >= rc0.left - 4 and box[1] >= rc0.top - 4 and
                          box[0] + box[2] <= rc0.right + 4 and box[1] + box[3] <= rc0.bottom + 4)
                _winw = rc0.right - rc0.left
                leaf = box[2] <= _winw * 0.85 if (rc0 and _winw > 100) else box[2] <= 900
                if not (inside and leaf):
                    mark("点前校验", False, why=whyv)
                    return 1, json.dumps({"ok": False, "why": "点前校验不过：" + whyv,
                                          "inside_fg": inside, "leaf": leaf}, ensure_ascii=False)
                _unverified = "跟踪校验未过（%s），按叶子尺寸+前台窗口内放行，效果需另行确认" % whyv
            x, y, w, h = box
            mark("点前校验", True, box=box)
            # 2026-11-19（测试员乙）：锁被占用（动作.ps1 rc=6）时不能报 clicked
            rc2, b2 = run_ps("-Action", "click", "-X", str(x + w // 2), "-Y", str(y + h // 2))
            if rc2 == 6:
                mark("点击", False, why="lock_blocked")
                return 6, json.dumps({"ok": False, "why": "lock_blocked：另一个进程正在驱动（脊髓锁 15s）",
                                      "blocked_at": [x + w // 2, y + h // 2]}, ensure_ascii=False)
            mark("点击", rc2 == 0, at=[x + w // 2, y + h // 2])
            note = b2[-100:]
            if rc2 == 0 and "未验" in _unverified:
                note = note + " | " + _unverified[:60]
            # 2026-11-20：把「用上了没有」回写进元素库 —— 没有这条，遗忘和敏化都没有数据
            try:
                import importlib.util as _il2
                _sp2 = _il2.spec_from_file_location("elem_fb", os.path.join(HERE, "元素反馈.py"))
                _fb = _il2.module_from_spec(_sp2); _sp2.loader.exec_module(_fb)
                _key = tpl or kind or ""
                _ok = (rc2 == 0) and ("未验" not in _unverified)
                _r = _fb.record(_key, _ok, "see+click" + ("" if _ok else "(unverified)"))
                note = note + " | 回写:%s" % (_r.get("hit") if isinstance(_r, dict) else "?")
                mark("归档", True, element=_key[:40])
            except Exception:
                pass
            mark("回读判据", rc2 == 0, note=(note or "")[:80])
            return rc2, json.dumps({"ok": rc2 == 0, "clicked": [x + w // 2, y + h // 2], "note": note}, ensure_ascii=False)
        mark("点前校验", False, why="没定位到")
        return 1, json.dumps({"ok": False, "why": "没定位到"}, ensure_ascii=False)
    return rc, blob

def check_rules(action, interface=""):
    """动手前先查「条件反射规则」——教训不再靠我想起来"""
    args = [PY, os.path.join(HERE, "查规则.py"), "--action", action]
    if interface: args += ["--interface", interface]
    if action == "click" and not (has("--tpl") or has("--kind")): args.append("--no-target")
    r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       env=ENV, creationflags=NO_WIN, timeout=60)
    try: return json.loads((r.stdout or "").strip().splitlines()[-1])
    except Exception: return {"count": 0, "rules": [], "blocked": False}

def region_cmd():
    """纯图形区的像素判据（2026-11-22）：先 --save 存基线，之后 --diff 基线 比变化比例。
    截图是屏幕物理像素，坐标和 click 同一套。只比画面，不改任何东西。"""
    try:
        x, y, w, h = int(arg("--x")), int(arg("--y")), int(arg("--w")), int(arg("--h"))
    except Exception:
        return 2, json.dumps({"ok": False, "why": "region 需要 --x --y --w --h（屏幕物理像素）"}, ensure_ascii=False)
    if w < 2 or h < 2:
        return 2, json.dumps({"ok": False, "why": "区域太小：%dx%d" % (w, h)}, ensure_ascii=False)
    import numpy as np
    from PIL import Image, ImageGrab
    try:
        im = ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True).convert("RGB")
    except Exception as e:
        return 1, json.dumps({"ok": False, "why": "截区域失败：%s" % str(e)[:100]}, ensure_ascii=False)
    arr = np.asarray(im, dtype=np.int16)
    save = str(arg("--save", "") or "")
    diff = str(arg("--diff", "") or "")
    if save:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(save)), exist_ok=True)
            im.save(save)
        except Exception as e:
            return 1, json.dumps({"ok": False, "why": "存基线失败：%s" % str(e)[:100]}, ensure_ascii=False)
        mark("像素区", True, save=save, box=[x, y, w, h])
        return 0, json.dumps({"ok": True, "saved": save, "box": [x, y, w, h],
                              "mean": [round(float(arr[:, :, i].mean()), 1) for i in range(3)]}, ensure_ascii=False)
    if diff:
        try:
            base = Image.open(diff).convert("RGB").resize((w, h))
        except Exception as e:
            return 2, json.dumps({"ok": False, "why": "读不到基线图：%s" % str(e)[:100]}, ensure_ascii=False)
        tol = int(arg("--tol", "12") or 12)
        minr = float(arg("--min", "0.02") or 0.02)
        b = np.asarray(base, dtype=np.int16)
        d = np.abs(arr - b).max(axis=2)
        ratio = float((d > tol).mean())
        ok = ratio >= minr
        mark("像素区", ok, ratio=round(ratio, 4), tol=tol, diff=True)
        return (0 if ok else 1), json.dumps({"ok": ok, "ratio": round(ratio, 4), "mean": round(float(d.mean()), 2),
                                            "tol": tol, "min": minr, "box": [x, y, w, h],
                                            "why": "" if ok else "区域变化 %.2f%% < 阈值 %.2f%%（画面可能没响应）" % (ratio * 100, minr * 100)},
                                           ensure_ascii=False)
    return 0, json.dumps({"ok": True, "box": [x, y, w, h],
                          "mean": [round(float(arr[:, :, i].mean()), 1) for i in range(3)],
                          "hint": "只采样。--save 存基线；--diff 基线 比变化"}, ensure_ascii=False)


def _win_list(title):
    import ctypes
    from ctypes import wintypes
    u = ctypes.windll.user32
    out = []
    EP = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(h, l):
        try:
            if u.IsWindowVisible(h):
                n = u.GetWindowTextLengthW(h)
                if n:
                    b = ctypes.create_unicode_buffer(n + 1)
                    u.GetWindowTextW(h, b, n + 1)
                    if title.lower() in b.value.lower():
                        r = wintypes.RECT()
                        u.GetWindowRect(h, ctypes.byref(r))
                        out.append({"hwnd": int(h), "title": b.value,
                                    "rect": [int(r.left), int(r.top), int(r.right - r.left), int(r.bottom - r.top)]})
        except Exception:
            pass
        return True
    u.EnumWindows(EP(cb), 0)
    return out


def _idle_sec():
    try:
        import ctypes
        from ctypes import wintypes
        class LII(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]
        li = LII(); li.cbSize = ctypes.sizeof(LII)
        u = ctypes.windll.user32
        if not u.GetLastInputInfo(ctypes.byref(li)):
            return None
        return max(0.0, (ctypes.windll.kernel32.GetTickCount() - li.dwTime) / 1000.0)
    except Exception:
        return None


def _trace_tail(n=300):
    out = []
    try:
        with open(TRACEF, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except Exception:
        return out
    for line in lines[-n:]:
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out


def preflight_cmd():
    """术前核对：只读。任何 critical 未过 → rc=1，界面据此禁用「开始手术」。"""
    checks = []
    def add(cid, name, ok, why="", critical=False, detail=None):
        c = {"id": cid, "name": name, "ok": bool(ok), "why": why or ("通过" if ok else "未通过"), "critical": bool(critical)}
        if detail:
            c["detail"] = detail
        checks.append(c)

    stop = os.path.exists(os.path.join(HERE, "STOP"))
    add("stop", "中止开关", not stop, "STOP 文件存在：动手动词全被拒" if stop else "未中止", True)

    ok8811, lat, err = False, None, ""
    t0 = time.time()
    try:
        import urllib.request
        json.loads(urllib.request.urlopen("http://127.0.0.1:8811/status", timeout=2).read().decode())
        ok8811 = True; lat = int((time.time() - t0) * 1000)
    except Exception as e:
        err = str(e)[:80]
    add("service", "反射服务 8811", ok8811, ("在线 %dms" % lat) if ok8811 else "不可用：" + err, True, {"latency_ms": lat})

    flags = model_flags()
    missing = [k for k, p in MODELS.items() if not os.path.exists(p)]
    add("models", "受体权重", not missing, ("缺：" + ",".join(missing)) if missing else "omni_icon / ui_detect 就绪", False, {"flags": flags})

    try:
        items = json.load(open(os.path.join(HERE, "学习到的元素.json"), encoding="utf-8")).get("items", [])
        act = sum(1 for i in items if not i.get("disabled"))
        add("elements", "突触库", len(items) > 0, "共 %d 条，可用 %d" % (len(items), act), False)
    except Exception as e:
        add("elements", "突触库", False, "读不到：" + str(e)[:60], False)

    win = str(arg("--window", "") or "")
    if win and win != "@fg":
        hits = _win_list(win.replace("(printwindow)", ""))
        ok = len(hits) == 1
        why = "找不到窗口" if not hits else ("命中 %d 个，需更精确" % len(hits) if len(hits) > 1 else "已定位")
        add("target", "手术靶窗", ok, why, True,
            {"matches": [{"title": h["title"], "rect": h["rect"]} for h in hits[:4]]})
    else:
        fg = fg_title()
        add("target", "手术靶窗", bool(fg), fg or "没有前台窗口", True)

    idle = _idle_sec()
    gate = idle is not None and idle >= 4.0
    add("gate", "让位闸", gate, ("空闲 %.1fs" % idle) if idle is not None else "读不到空闲时间", False)

    try:
        import ctypes
        u = ctypes.windll.user32
        sw, sh = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
        add("screen", "屏幕坐标", sw >= 800 and sh >= 600, "%dx%d" % (sw, sh), False)
    except Exception:
        add("screen", "屏幕坐标", False, "读不到屏幕尺寸", False)

    lock = os.path.join(HERE, "脊髓锁.json")
    if os.path.exists(lock):
        age = 0; pid = "?"
        try:
            d = json.load(open(lock, encoding="utf-8"))
            pid = d.get("pid"); age = max(0, int(time.time() - int(d.get("at") or 0)))
        except Exception:
            pass
        add("lock", "动作锁", age > 15, "锁被占用 %ds（pid=%s）" % (age, pid), False)
    else:
        add("lock", "动作锁", True, "空闲")

    blocking = [c["id"] for c in checks if c["critical"] and not c["ok"]]
    warnings = [c["id"] for c in checks if not c["critical"] and not c["ok"]]
    return 0, json.dumps({
        "ok": True, "ready": not blocking, "blocking": blocking, "warnings": warnings, "checks": checks,
        "target": win or "@fg", "at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
    }, ensure_ascii=False)


def vitals_cmd():
    """生命体征：只读。给手术监护页用，全部是真的本机状态。"""
    import urllib.request
    t0 = time.time(); svc = False
    try:
        json.loads(urllib.request.urlopen("http://127.0.0.1:8811/status", timeout=1.5).read().decode())
        svc = True
    except Exception:
        pass
    lat = int((time.time() - t0) * 1000)
    idle = _idle_sec()
    tr = _trace_tail(300)
    okc = sum(1 for t in tr if t.get("ok"))
    total = len(tr)
    now = int(time.time() * 1000)
    recent = [t for t in tr if now - int(t.get("ts") or 0) <= 5 * 60 * 1000]
    last = tr[-1] if tr else None
    fails_dir = os.path.join(BASE, "训练", "ui-detect", "fails")
    try:
        nfails = len([x for x in os.listdir(fails_dir) if x.endswith(".png")])
    except Exception:
        nfails = 0
    try:
        plans = len([x for x in os.listdir(PLAN_DIR) if x.endswith(".json")])
    except Exception:
        plans = 0
    try:
        arcs = len(json.load(open(os.path.join(HERE, "弧.json"), encoding="utf-8")).get("arcs") or [])
    except Exception:
        arcs = 0
    try:
        elements = len(json.load(open(os.path.join(HERE, "学习到的元素.json"), encoding="utf-8")).get("items", []))
    except Exception:
        elements = 0
    panel = False
    try:
        json.loads(urllib.request.urlopen("http://127.0.0.1:8813/api/health", timeout=1).read().decode())
        panel = True
    except Exception:
        panel = False
    stop = os.path.exists(os.path.join(HERE, "STOP"))
    lock_age = None
    lk = os.path.join(HERE, "脊髓锁.json")
    if os.path.exists(lk):
        try:
            lock_age = max(0, int(time.time() - int(json.load(open(lk, encoding="utf-8")).get("at") or 0)))
        except Exception:
            lock_age = 0
    models = {}
    for k, p in MODELS.items():
        ex = os.path.exists(p)
        models[k] = {"exists": ex, "enabled": model_flags().get(k, True),
                     "mtime": time.strftime("%m-%d %H:%M", time.localtime(os.path.getmtime(p))) if ex else None}
    return 0, json.dumps({
        "ok": True, "service8811": svc, "latency8811_ms": lat, "panel8813": panel,
        "idle_s": round(idle, 1) if idle is not None else None,
        "gate": bool(idle is not None and idle >= 4.0), "stop": stop,
        "lock_age_s": lock_age, "models": models, "elements": elements,
        "plans": plans, "arcs": arcs, "fails_png": nfails,
        "trace": {"total": total, "ok": okc, "rate": round(okc / total, 3) if total else 0,
                  "fails": total - okc, "per_min": round(len(recent) / 5.0, 1),
                  "recent5m": len(recent), "last": last},
        "at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
    }, ensure_ascii=False)


def main():
    global MODE
    # 2026-11-22（独立验收 #222403 实测发现）：这里以前**没**设 DPI awareness。
    # 动作.ps1/输入序列.ps1 都是 DPI-aware 物理坐标，而本进程不是 —— 于是：
    #   GetSystemMetrics 报 1707x960（真实 2560x1440），右下 1/3 屏幕被误判「越界」；
    #   WindowFromPoint 查的是逻辑坐标下的点，窗口守卫看错了位置（可能放过桌面、误拒真窗口）。
    # 必须在任何窗口/坐标计算之前调用，所以放在 main 第一行。
    _dpi()
    if has("--careful"):
        MODE = "careful"   # 2026-11-19：环境变量在 import 后改不了，命令行 --careful 也得真生效
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    # 2026-11-22：急停开关——STOP 文件存在时，所有会动手的动词一律拒绝（只读动词照常）。
    # 她和我在任何时刻都能 touch 一个文件刹车，不用 kill 进程。
    _stop = os.path.join(HERE, "STOP")
    if os.path.exists(_stop) and cmd in ("click", "rclick", "dbl", "drag", "hover", "type", "key",
                                         "scroll", "arc", "seq", "learn", "focus", "maximize_active",
                                         "scroll_bottom", "focus_input", "cleanup_edge"):
        return 3, json.dumps({"ok": False, "stopped": True,
                              "why": "STOP 文件存在：所有动手动作已停用（删掉 %s 恢复；只读动词如 status/see/icons/find 照常）" % _stop},
                             ensure_ascii=False)
    act_map = {"click": "click", "type": "type", "key": "type", "scroll": "click",
               "see": "click", "learn": "click"}
    if cmd in act_map:
        rr = check_rules(act_map[cmd], arg("--interface", ""))
        if rr.get("blocked"):
            print(json.dumps({"ok": False, "blocked": True, "rules": rr.get("rules")}, ensure_ascii=False))
            return 1
        for rule in rr.get("rules", []):
            print("[规则 %s] %s  <- %s" % (rule["id"], "; ".join(rule["require"]), rule["why"]), file=sys.stderr)
    if cmd == "status": return status()
    if cmd == "preflight": return preflight_cmd()
    if cmd == "vitals": return vitals_cmd()
    if cmd == "models": return models_cmd()
    if cmd == "region": return region_cmd()
    if cmd == "plan": return plan_cmd()
    if cmd == "sheet":
        # 筛一张候选小表（本地筛完只送小图，省 token）
        _w = arg("--window", "")
        _a = ["--window", _w] if _w else []
        return run_py("筛图.py", *_a)
    if cmd == "settext":
        # 2026-11-19：UIA ValuePattern 直接写值——不点、不抢焦点、可读回
        _w = arg("--window", ""); _n = arg("--name", ""); _t = arg("--text", "")
        if not (_w and _n):
            return 2, json.dumps({"ok": False, "why": "settext 需要 --window 与 --name"}, ensure_ascii=False)
        return run_py("设值.py", "--window", _w, "--name", _n, "--text", _t)
    if cmd == "text":
        # 2026-11-19：按文字找元素（Windows 自带 OCR，返回屏幕坐标），可选 --click
        _w = arg("--window", "") or fg_title() or "@fg"
        rc_t, blob_t = run_py("脊髓2.py", "text", "--text", arg("--text", ""), "--window", _w)
        _d = {}
        for _line in reversed(blob_t.splitlines()):
            _s = _line.strip()
            if _s.startswith("{"):
                try:
                    _d = json.loads(_s); break
                except Exception:
                    continue
        if not has("--click"):
            return (0 if _d.get("ok") else 1), json.dumps(_d, ensure_ascii=False)
        _cands = _d.get("candidates") or []
        if not _cands:
            return 1, json.dumps({"ok": False, "why": "OCR 没找到该文字"}, ensure_ascii=False)
        # 2026-11-20 敏化（修）：这里在 main() 的 OCR 分支，没有 kind；键要用 OCR 出来的文字
        try:
            import importlib.util as _il3
            _sp3 = _il3.spec_from_file_location("elem_fb2", os.path.join(HERE, "元素反馈.py"))
            _fb2 = _il3.module_from_spec(_sp3); _sp3.loader.exec_module(_fb2)
            _tkey = str(_cands[0].get("text") or "")
            _pen = _fb2.penalty(_tkey) if _tkey else 1.0
            _raw = float(_cands[0].get("score") or 0)
            if _pen < 1.0 and _raw * _pen <= 0.5:
                return 1, json.dumps({"ok": False,
                                      "why": "敏化拒绝：这条元素历史失败多（权重 %.2f，加权分 %.2f ≤ 0.5）" % (_pen, _raw * _pen),
                                      "penalty": round(_pen, 3), "raw_score": _raw}, ensure_ascii=False)
        except Exception as _e:
            try:
                with open(os.path.join(HERE, "脊髓统-异常.log"), "a", encoding="utf-8") as _lf:
                    _lf.write("%s 敏化(OCR分支) 失败: %s\n" % (time.strftime("%H:%M:%S"), str(_e)[:120]))
            except Exception:
                pass
        _b = _cands[0]["box"]
        _fok, _fwhy = focus_window(_w.replace("(printwindow)", ""))
        if not _fok:
            return 1, json.dumps({"ok": False, "why": "置前失败：" + _fwhy}, ensure_ascii=False)
        _rc, _b2 = run_ps("-Action", "click", "-X", str(_b[0] + _b[2] // 2), "-Y", str(_b[1] + _b[3] // 2))
        if _rc == 6:
            return 6, json.dumps({"ok": False, "why": "lock_blocked"}, ensure_ascii=False)
        return _rc, json.dumps({"ok": _rc == 0, "clicked": [_b[0] + _b[2] // 2, _b[1] + _b[3] // 2],
                                "text": _cands[0].get("text"), "score": _cands[0].get("score"),
                                "note": _b2[-80:]}, ensure_ascii=False)
    if cmd == "focus":
        ok_f, why_f = focus_window(arg("--window", ""))
        mark("置前", ok_f, why=why_f)
        return (0 if ok_f else 1), json.dumps({"ok": ok_f, "why": why_f, "window": arg("--window", "")}, ensure_ascii=False)
    if cmd == "elements":
        # 2026-11-19：元素清单——不只给 box，还给 role + 文本（走 UIA；Chromium 需 --force-renderer-accessibility）
        win = arg("--window", "")
        if not win: return 2, "elements 需要 --window <窗口名>"
        _rc_el, _b_el = run_py("语义.py", "tree", "--window", win, "--depth", arg("--depth", "10"))
        mark("元素树", _rc_el == 0, src="uia", suspect=True)
        return _rc_el, _b_el
    if cmd == "find":
        nm = arg("--name", sys.argv[2] if len(sys.argv) > 2 else "")
        if not nm: return 2, "find 需要 --name 或第二个参数（名字）"
        _w = arg("--window", "")
        _rc_f, _b_f = run_py("手眼.py", "find", nm, *(["--window", _w] if _w else []))
        mark("查找", _rc_f == 0, target=nm)
        return _rc_f, _b_f
    if cmd == "see": return see()
    if cmd == "icons":
        _iw = arg("--window", "")
        _rc_i, _b_i = run_py("探针.py", "--tag", arg("--tag", "_icons"), "--want", arg("--want", "20"),
                             *(["--window", _iw] if _iw else []))
        mark("图标", _rc_i == 0, src="omni_icon")
        return _rc_i, _b_i
    if cmd == "learn":
        return run_py("学习弧.py", "--tag", arg("--tag"), "--index", arg("--index"), "--name", arg("--name", ""),
                      *(["--dry"] if has("--dry") else []))
    if cmd == "arc":
        # 一次调用干完整件事：把已固化的弧按名字跑掉
        nm = arg("--name", "")
        if not nm: return 2, "arc 需要 --name 弧名（先 弧.py --list 看）"
        _rc_a, _b_a = run_py("弧.py", nm)
        mark("固化弧", _rc_a == 0, target=nm)
        return _rc_a, _b_a
    if cmd == "go":
        # 网页按名字去（导航.py；没记录的用 --learn 学）
        pg = arg("--page", "")
        if not pg: return 2, "go 需要 --page 页面名"
        extra = ["--learn"] if has("--learn") else []
        return run_py("导航.py", pg, *extra)
    if cmd == "observe":
        _rc_o, _b_o = run_py("手眼.py", "watch-events", arg("--seconds", "5"))
        mark("观察", _rc_o == 0)
        return _rc_o, _b_o
    if cmd == "wait":
        if has("--stable"):
            # 2026-11-21（剪辑线指出）：原来直接把 手眼.py 的判定吐出来，只有 {"stable": false}，
            # 不说等了几秒、差多少——DSH 这类一直有动画的界面永远不稳定，等于没给信息。
            _tw0 = time.time()
            _rc_w, _b_w = run_py("手眼.py", "stable", timeout=120)
            _d_w = _last_json(_b_w) if _b_w else {}
            if not isinstance(_d_w, dict):
                _d_w = {"raw": str(_b_w)[:200]}
            _d_w["waited_ms"] = int((time.time() - _tw0) * 1000)
            _d_w.setdefault("hint", "stable=false 只说明这段时间画面一直在变（动画界面常态），不等于等待失败；看 waited_ms 与实际差值")
            mark("等待", bool(_d_w.get("stable")), waited_ms=_d_w["waited_ms"])
            return _rc_w, json.dumps(_d_w, ensure_ascii=False)
        time.sleep(float(arg("--seconds", "1"))); return 0, "waited"
    # 2026-11-20 独立验收 F4：不传坐标时 PowerShell 把空串当 0，会真的点屏幕原点 (0,0)。
    # 缺参数或坐标非法 → 直接拒绝，不许下发。
    if cmd in ("hover", "rclick", "dbl", "click"):
        for k in ("--x", "--y"):
            v = str(arg(k, "")).strip()
            if not v.lstrip("-").isdigit():
                return 2, json.dumps({"ok": False, "error": "缺坐标或坐标非法：" + k + "=" + repr(v),
                                      "hint": "click 必须给 --x 和 --y"}, ensure_ascii=False)
        # 2026-11-21 修（D3 的 CLI 侧）：插件层拦了越界，但直接跑 CLI 时没拦——
        # SetCursorPos 会把 99999 夹到屏幕边，点在别处还报成功。这里先按屏幕尺寸拒掉。
        import ctypes as _ct
        _u2 = _ct.windll.user32
        _SW, _SH = _u2.GetSystemMetrics(0), _u2.GetSystemMetrics(1)
        _px, _py = int(arg("--x")), int(arg("--y"))
        if not (0 <= _px < _SW and 0 <= _py < _SH):
            return 2, json.dumps({"ok": False,
                                  "error": "坐标越界：(%d,%d) 不在屏幕范围 0..%d / 0..%d，已拒绝，未移动鼠标" % (_px, _py, _SW - 1, _SH - 1)},
                                  ensure_ascii=False)
    if cmd == "drag":
        for k in ("--x", "--y", "--x2", "--y2"):
            v = str(arg(k, "")).strip()
            if not v.lstrip("-").isdigit():
                return 2, json.dumps({"ok": False, "error": "drag 缺坐标：" + k + "=" + repr(v)}, ensure_ascii=False)
    if cmd == "hover":
        # 2026-11-21 修（D10）：hover 原来恒 ok:true 且 out 为空，调用方什么也拿不到
        _hx, _hy = int(arg("--x")), int(arg("--y"))
        rc5, b5 = run_ps("-Action", "hover", "-X", str(_hx), "-Y", str(_hy))
        d5 = _last_json(b5)
        d5["ok"] = bool(rc5 == 0) or bool(d5.get("control_at"))
        d5.pop("raw", None)
        d5["at"] = [_hx, _hy]
        d5["control_at"] = control_at(_hx, _hy)
        d5["focus"] = focused_info()
        return rc5, json.dumps(d5, ensure_ascii=False)
    if cmd == "rclick":
        return run_ps("-Action", "click", "-X", arg("--x"), "-Y", arg("--y"), "-Button", "right")
    if cmd == "dbl":
        return run_ps("-Action", "click", "-X", arg("--x"), "-Y", arg("--y"), "-Count", "2")
    if cmd == "drag":
        return run_ps("-Action", "drag", "-X", arg("--x"), "-Y", arg("--y"), "-X2", arg("--x2"), "-Y2", arg("--y2"))
    if cmd == "click":
        # 2026-11-21 修（D7）：原来「画面变 或 焦点变」都算成功，点窗口空白处也 ok:true。
        # 新判据：像素变 或 该坐标上的控件身份变了 才算成功；只有焦点变不算。
        _cx, _cy = int(arg("--x")), int(arg("--y"))
        # 2026-11-21（队友实测后加）：点前必须确认落点在真实窗口里。
        # 原来几何件在壁纸上给出假候选，9/10 落在目标窗外，点击直接被送到桌面。
        _hwnd, _wcls, _wtitle = window_at(_cx, _cy)
        _dry = has("--dry")
        # 中心点是什么控件（Kimi 建议的硬门槛）。默认只记录不拒绝——
        # 要严格模式加 --strict-type（消融/排障时用）。
        _before0 = control_at(_cx, _cy)
        _ctype0 = (_before0 or "").split(":", 1)[0] if _before0 else ""
        _type_ok = _ctype0 in CLICKABLE_TYPES
        _is_leaf = _ctype0 in LEAF_TYPES
        mark("落点校验", True, ctrl=_before0, window=_wtitle[:40] or None)
        if has("--strict-type") and not _type_ok:
            mark("落点校验", False, why="strict-type")
            return 2, json.dumps({
                "ok": False, "at": [_cx, _cy], "window_class": _wcls,
                "control_type": _ctype0, "type_clickable": False,
                "why": "strict-type：该坐标上的控件类型 %r 不在可点集合里，已拒绝、未移动鼠标" % (_ctype0 or "(无控件)"),
                "dry": _dry,
            }, ensure_ascii=False)
        if is_desktop(_wcls):
            mark("落点校验", False, why="落在桌面")
            return 2, json.dumps({
                "ok": False, "at": [_cx, _cy], "window_class": _wcls, "window_title": _wtitle[:40],
                "why": "该坐标落在桌面/任务栏上（不是任何应用窗口），已拒绝、未移动鼠标：大概率是候选错位",
                "dry": _dry,
            }, ensure_ascii=False)
        if _dry:
            mark("点击", True, dry=True)
            return 0, json.dumps({
                "ok": True, "dry": True, "at": [_cx, _cy],
                "window_class": _wcls, "window_title": _wtitle[:40], "would_click": True,
                "control_type": _ctype0, "type_clickable": _type_ok, "is_leaf": _is_leaf,
            }, ensure_ascii=False)
        _chain_before = control_chain_at(_cx, _cy)
        _before = _chain_before[-1] if _chain_before else control_at(_cx, _cy)
        _fi0 = focused_info() or ""
        rc6, b6 = run_ps("-Action", "click", "-X", str(_cx), "-Y", str(_cy))
        mark("点击", rc6 == 0, at=[_cx, _cy])
        time.sleep(0.2)
        _chain_after = control_chain_at(_cx, _cy)
        _after = _chain_after[-1] if _chain_after else control_at(_cx, _cy)
        mark("回读", True, after=_after)
        d6 = _last_json(b6)
        pixel_changed = bool(d6.get("changed"))
        _chain_moved = bool(_chain_before) and bool(_chain_after) and (_chain_before != _chain_after)
        ident_changed = bool(_before) and bool(_after) and (_before != _after) or _chain_moved
        focus_only = bool(d6.get("focus_changed")) and not pixel_changed and not ident_changed
        # 2026-11-21 修（D7 二次，她实测发现）：点「本来就聚焦的输入框」不该判失败——
        # 输入框里早等着光标，点它只是把插入点挪进去，画面天然不变、控件身份也不变。
        # 上一版把这类点法一律判 ok:false，等于把「点中了但没变化」误判成没点中。
        # 判据：坐标上本来就是可输入控件（Edit/Document）且点完焦点确实落在输入控件里 → 算点中。
        # 2026-11-22（#48d0ac 四，实测「点中输入框却报 ok:false」）：
        # 原来要求 control_at 本身就是 Edit——但 Electron/Chromium 的网页画布在 UIA 里是
        # GroupControl，输入框只出现在焦点控件里。判据改成：点后焦点落在可输入控件，
        # 且（落点在该控件矩形内 或 点前落点上就是可输入控件）。
        _rect_f, _fi = focused_rect()
        _fi = _fi or ""
        _typed_before = ("Edit" in (_before or "")) or ("Document" in (_before or ""))
        _focus_is_typed = ("Edit" in _fi) or ("Document" in _fi)
        _point_in_focus = point_in_rect(_rect_f, _cx, _cy)
        _into_typed = bool(rc6 == 0 and _focus_is_typed and (_typed_before or _point_in_focus))
        # 2026-11-22（#cddedc 剪辑线报的边界）：点列表项时焦点从搜索框移到该项、画面与控件身份都不变。
        # 放宽，但只认「焦点落到的正是落点上的那个控件」——类型相同、名字互为前缀。
        # 不认「只要焦点变了」（那会把点空白也算成功）。
        def _same_control(a, b):
            if not a or not b:
                return False
            ta, na = (a.split(":", 1) + [""])[:2]
            tb, nb = (b.split(":", 1) + [""])[:2]
            if ta != tb:
                return False
            na, nb = (na or "").strip(), (nb or "").strip()
            if not na or not nb:
                return False
            return na.startswith(nb) or nb.startswith(na)

        # 焦点落在「落点控件链」上的任意一层都算命中（按钮里的文字标签/列表项都覆盖）
        _focus_is_at_point = bool(rc6 == 0 and _fi and any(_same_control(_fi, c) for c in _chain_after))
        d6["focused_before"] = _fi0
        d6["focused_after"] = _fi
        d6["focus_rect"] = list(_rect_f) if _rect_f else None
        d6["point_in_focus"] = _point_in_focus
        d6["focus_is_at_point"] = _focus_is_at_point
        d6["into_input"] = _into_typed
        d6["at"] = [_cx, _cy]
        d6["control_before"] = _before
        d6["control_after"] = _after
        d6["control_chain_before"] = _chain_before
        d6["control_chain_after"] = _chain_after
        d6["control_type"] = _ctype0
        d6["type_clickable"] = _type_ok
        d6["is_leaf"] = _is_leaf
        d6["ok"] = bool(rc6 == 0 and (_into_typed or _focus_is_at_point
                                      or ((pixel_changed or ident_changed) and not focus_only)))
        if rc6 == 0 and not d6["ok"]:
            if _focus_is_typed and not _point_in_focus:
                d6["why"] = "焦点在可输入控件上，但落点不在它的矩形内（点在边框/空白上）：不算点中输入框"
            else:
                d6["why"] = ("画面没变、该坐标上的控件也没变" + ("，只有焦点变了（不能算点中了）" if focus_only else "") +
                             "：这个坐标可能落在空白处，或目标没响应")
        elif rc6 == 0 and _into_typed:
            d6["why"] = ("点中了输入框（输入框不随点击改变画面），焦点已落在可输入控件内"
                         + ("，落点也在它的矩形里" if _point_in_focus else ""))
        elif rc6 == 0 and _focus_is_at_point:
            d6["why"] = "点中了：焦点落到的正是落点上的那个控件（画面与控件身份不变也算，如列表项/按钮）"
        mark("回读判据", d6["ok"], why=(d6.get("why") or "")[:80])
        # 2026-11-21（剪辑线 b0dc182c 指出「两套实现打架」）：把 点并验.py 的 --expect 能力搬进来，
        # 这样 spine click 只走这一条实现。原来插件走 点并验.py、CLI 走这里，
        # 于是昨晚修的 D7 判据（control_before/into_input/像素）在插件路径上是死代码。
        _exp = str(arg("--expect", "") or "").strip()
        if _exp:
            _rc_e, _b_e = run_py("验.py", _exp, timeout=90)
            try:
                d6["expect"] = _last_json(_b_e)
            except Exception:
                d6["expect"] = {"ok": False, "raw": str(_b_e)[:160]}
            if not bool((d6.get("expect") or {}).get("ok")):
                d6["ok"] = False
                d6["why"] = "目标状态没等到：" + _exp
            mark("目标状态", bool((d6.get("expect") or {}).get("ok")), expect=_exp)
        return rc6, json.dumps(d6, ensure_ascii=False)
    if cmd == "type":
        # 2026-11-21 修（D4）：type 原来只证明「发了」，不证明「落到了」。
        # 判据：输入前后焦点控件类型必须一致；变了就判没落到（回执带前后焦点）
        f0 = focused_info()
        rc3, b3 = run_ps("-Action", "type", "-Text", arg("--text", ""))
        mark("输入", rc3 == 0, n=len(str(arg("--text", ""))))
        time.sleep(0.2)
        f2 = focused_info()
        _t0 = (f0 or "").split(":", 1)[0].strip()
        _t2 = (f2 or "").split(":", 1)[0].strip()
        kept = (not f0) or (_t0 == _t2)
        mark("回读焦点", kept, before=f0, after=f2)
        try:
            d = json.loads([x for x in (b3 or "").splitlines() if x.strip().startswith("{")][-1])
        except Exception:
            d = {"ok": rc3 == 0, "raw": (b3 or "")[-200:]}
        d["focus_before"] = f0
        d["focus_after"] = f2
        d["focus_kept"] = kept
        if d.get("ok") and not kept:
            d["ok"] = False
            d["why"] = "文字发出去了但焦点变了（" + _t0 + " → " + _t2 + "）：可能没落到目标输入框"
        return (0 if d.get("ok") else 9), json.dumps(d, ensure_ascii=False)
    if cmd == "key":
        # 2026-11-20：原来直接 run_ps 发键（SendKeys 无窗口参数、不验焦点）→ ^L 打空。
        # 现在走 key_cmd：前台守卫 + 地址栏先取焦点 + 回读焦点，见文件上方函数注释。
        return key_cmd()
    if cmd == "scroll":
        # 2026-11-21 修（D8）：方向原来不校验，dir=sideways 也照样 ok:true 并回执 SCROLL sideways
        # 2026-11-21（剪辑线指出）：缺 --dir 时原来默认 down 并真滚屏，属于「静默动作」。
        # 缺参数就报错，跟 click 缺坐标一个口径。插件层仍会给默认值（那是显式的）。
        _raw_dir = str(arg("--dir", "") or "").strip()
        if not _raw_dir:
            return 2, json.dumps({"ok": False, "why": "scroll 缺 --dir：必须显式给 up/down/left/right（不再默认 down 静默滚屏）"}, ensure_ascii=False)
        _dir = _raw_dir.lower()
        if _dir not in ("up", "down", "left", "right"):
            return 2, json.dumps({"ok": False, "why": "scroll 方向非法：%r（只允许 up/down/left/right）" % _dir}, ensure_ascii=False)
        return run_ps("-Action", "scroll", "-Direction", _dir, "-Amount", arg("--amount", "3"))
    if cmd == "gate":
        return run_ps("-Action", "probe")
    return 2, "未知动词: " + cmd

if __name__ == "__main__":
    trace_begin(sys.argv[1] if len(sys.argv) > 1 else "status")
    try:
        rc, blob = main()
    except Exception as _e:
        rc, blob = 1, json.dumps({"ok": False, "why": "未捕获异常：%s" % str(_e)[:300]}, ensure_ascii=False)
        try:
            with open(os.path.join(HERE, "脊髓统-异常.log"), "a", encoding="utf-8") as _lf:
                _lf.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), str(_e)[:300]))
        except Exception:
            pass
    try:
        trace_end(rc, blob)
    except Exception:
        pass
    print(blob)
    sys.exit(rc)