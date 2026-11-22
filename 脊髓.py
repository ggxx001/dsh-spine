"""脊髓.py —— 一个入口。用一句话下目标，它自己判断走 UIA 还是 CDP。

用法：
  python 脊髓.py --info                 看当前前台窗口该走哪条路、为什么
  python 脊髓.py "看 文件"              找控件（默认只找不点）
  python 脊髓.py "点 文件" --go         找到就点，并给出控件级确认

设计（按 Kimi 建议的三层路由）：
  ⓪ 端口归属：CDP 端口必须属于前台窗口自己的进程（hwnd→pid 核对）；对不上就不连，
     明说「这个窗口没开调试端口」——不许拿别家浏览器的页面冒充当前窗口。
  ① 静态规则：窗口类名 Chrome_WidgetWin* / 进程 chrome/msedge → CDP 优先
  ② 能力探测：UIA 树深度 < 3 且只剩 Document/Pane → 判为「UIA 看不到里面」，降级 CDP
  ③ 兜底：两条路都不通就明说，不乱点
"""
import argparse, ctypes, json, os, socket, sys, threading, time

import uiautomation as auto

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

CLICKABLE = {
    "ButtonControl", "MenuItemControl", "ListItemControl", "TreeItemControl",
    "TabItemControl", "CheckBoxControl", "RadioButtonControl", "HyperlinkControl",
    "ComboBoxControl", "EditControl",
}
CONTAINER = {"PaneControl", "GroupControl", "WindowControl", "DocumentControl"}
CDP_PORTS = [9222, 9333, 9230, 9229]


def probe_cdp():
    """看本机有没有可用的 CDP 端口（只探测，不连）。"""
    import socket
    alive = []
    for p in CDP_PORTS:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.08)
        try:
            if s.connect_ex(("127.0.0.1", p)) == 0:
                alive.append(p)
        finally:
            s.close()
    return alive


# ==== 2026-11-22（剪辑线报的真问题）：CDP 端口要核对归属，别串到别的浏览器 ====
# 现象：前台是豆包（Electron，类名同样是 Chrome_WidgetWin_1），脊髓却连着 9222（Edge）的页面，
# 把 member.bilibili.com 当成「当前窗口」。根因：只看类名就判 CDP，然后拿到什么端口连什么。
# 规则：端口必须属于前台窗口自己的进程；对不上就不用，并说清「这个窗口没开调试端口」。
# 例外：前台是「发起调用的窗口」（DSH/终端，本进程的祖先进程）——它不是目标，
# 这时用不属于发起窗口的外部端口；一个外部端口都没有，就老实说没有可操作的目标页面。

def hwnd_pid(hwnd):
    """hwnd 属于哪个进程。拿不到返回 0。"""
    try:
        if not hwnd:
            return 0
        u = ctypes.windll.user32
        u.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        pid = ctypes.c_ulong()
        u.GetWindowThreadProcessId(ctypes.c_void_p(int(hwnd)), ctypes.byref(pid))
        return int(pid.value)
    except Exception:
        return 0


def pid_exe(pid):
    """进程可执行文件名（如 msedge.exe）。拿不到返回空串。"""
    try:
        if not pid:
            return ""
        k = ctypes.windll.kernel32
        k.OpenProcess.restype = ctypes.c_void_p
        k.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        h = k.OpenProcess(0x1000, False, int(pid))   # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        try:
            k.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                                                     ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_ulong)]
            buf = ctypes.create_unicode_buffer(1024)
            size = ctypes.c_ulong(1024)
            ok = k.QueryFullProcessImageNameW(ctypes.c_void_p(h), 0, buf, ctypes.byref(size))
            return os.path.basename(buf.value) if ok else ""
        finally:
            k.CloseHandle(ctypes.c_void_p(h))
    except Exception:
        return ""


_ANCESTOR_PIDS = None


def ancestor_pids(limit=12):
    """本进程自己 + 所有祖先进程的 pid。
    前台窗口若是其中之一，说明那只是「发起调用的窗口」（DSH / 终端），不是要操作的目标。"""
    global _ANCESTOR_PIDS
    if _ANCESTOR_PIDS is not None:
        return _ANCESTOR_PIDS
    parents = {}
    snap = None
    try:
        class _PE32(ctypes.Structure):
            _fields_ = [("dwSize", ctypes.c_ulong), ("cntUsage", ctypes.c_ulong),
                        ("th32ProcessID", ctypes.c_ulong), ("th32DefaultHeapID", ctypes.c_void_p),
                        ("th32ModuleID", ctypes.c_ulong), ("cntThreads", ctypes.c_ulong),
                        ("th32ParentProcessID", ctypes.c_ulong), ("pcPriClassBase", ctypes.c_long),
                        ("dwFlags", ctypes.c_ulong), ("szExeFile", ctypes.c_wchar * 260)]
        k = ctypes.windll.kernel32
        k.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        k.CreateToolhelp32Snapshot.argtypes = [ctypes.c_ulong, ctypes.c_ulong]
        snap = k.CreateToolhelp32Snapshot(0x2, 0)   # TH32CS_SNAPPROCESS
        pe = _PE32()
        pe.dwSize = ctypes.sizeof(_PE32)
        ok = k.Process32FirstW(ctypes.c_void_p(snap), ctypes.byref(pe))
        while ok:
            parents[int(pe.th32ProcessID)] = (int(pe.th32ParentProcessID), pe.szExeFile)
            ok = k.Process32NextW(ctypes.c_void_p(snap), ctypes.byref(pe))
    except Exception:
        parents = {}
    finally:
        try:
            if snap:
                ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(snap))
        except Exception:
            pass
    pids = set()
    pid = os.getpid()
    for _ in range(limit):
        pids.add(pid)
        pid = parents.get(pid, (0, ""))[0]
        if not pid or pid in pids:
            break
    _ANCESTOR_PIDS = pids
    return pids


def listen_owners(ports):
    """{监听端口: 占用它的 pid}。只查 IPv4 回环——probe_cdp 连的就是 127.0.0.1。"""
    want = set(int(p) for p in ports)
    got = {}
    try:
        class _ROW(ctypes.Structure):
            _fields_ = [("dwState", ctypes.c_ulong), ("dwLocalAddr", ctypes.c_ulong),
                        ("dwLocalPort", ctypes.c_ulong), ("dwRemoteAddr", ctypes.c_ulong),
                        ("dwRemotePort", ctypes.c_ulong), ("dwOwningPid", ctypes.c_ulong)]
        ip = ctypes.windll.iphlpapi
        ip.GetExtendedTcpTable.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong),
                                           ctypes.c_int, ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        ip.GetExtendedTcpTable.restype = ctypes.c_ulong
        size = ctypes.c_ulong(0)
        rc = ip.GetExtendedTcpTable(None, ctypes.byref(size), False, 2, 3, 0)  # AF_INET, OWNER_PID_LISTENER
        if rc not in (0, 122):
            raise OSError("GetExtendedTcpTable rc=%d" % rc)
        buf = ctypes.create_string_buffer(size.value or 4)
        rc = ip.GetExtendedTcpTable(ctypes.cast(buf, ctypes.c_void_p), ctypes.byref(size), False, 2, 3, 0)
        if rc != 0:
            raise OSError("GetExtendedTcpTable rc=%d" % rc)
        n = int(ctypes.cast(buf, ctypes.POINTER(ctypes.c_ulong))[0])
        head = ctypes.sizeof(ctypes.c_ulong)
        step = ctypes.sizeof(_ROW)
        for i in range(n):
            off = head + i * step
            row = _ROW.from_buffer_copy(buf[off:off + step])
            lp = int(socket.ntohs(row.dwLocalPort & 0xFFFF))
            if lp in want:
                got[lp] = int(row.dwOwningPid)
    except Exception:
        got = _listen_owners_netstat(want)
    return got


def _listen_owners_netstat(want):
    """GetExtendedTcpTable 不可用时的兜底：解析 netstat -ano。"""
    import subprocess
    got = {}
    try:
        p = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=8,
                           creationflags=0x08000000)
        for line in (p.stdout or "").splitlines():
            f = line.split()
            if len(f) >= 5 and f[0].upper() == "TCP" and f[3].upper() == "LISTENING":
                try:
                    lp = int(f[1].rsplit(":", 1)[1])
                except Exception:
                    continue
                if lp in want:
                    try:
                        got[lp] = int(f[4])
                    except Exception:
                        pass
    except Exception:
        pass
    return got


def window_cdp_info(win):
    """这个窗口能用哪些 CDP 端口：只认属于它自己进程的端口；对不上就不用。
    返回 dict：fore(前台进程) / alive(本机活着) / owner(端口归属) / usable(能用) / conductor / note。"""
    try:
        hwnd = int(getattr(win, "NativeWindowHandle", 0) or 0)
    except Exception:
        hwnd = 0
    fore_pid = hwnd_pid(hwnd)
    fore_exe = pid_exe(fore_pid) if fore_pid else ""
    alive = probe_cdp()
    owners = listen_owners(alive) if alive else {}
    info = {"fore": {"pid": fore_pid, "exe": fore_exe}, "alive": alive, "owner": {},
            "usable": [], "conductor": False, "verified": False, "note": ""}
    for p in alive:
        opid = owners.get(p)
        info["owner"][str(p)] = {"pid": opid, "exe": (pid_exe(opid) if opid else "")}
    if not alive:
        info["note"] = "本机没有活着的 CDP 调试端口（9222/9333/9230/9229）"
        return info
    if not fore_pid:
        # 个别 UIA 窗口给不出 hwnd：没法核对，只能按老路走，但把「没核对」标出来
        info.update({"usable": alive, "note": "取不到前台窗口的 pid，没核对端口归属"})
        return info
    info["verified"] = True
    own = [p for p in alive if owners.get(p) == fore_pid]
    if own:
        info.update({"usable": own, "note": "端口 %s 属于这个窗口自己的进程（pid %d）" % (own, fore_pid)})
        return info
    anc = ancestor_pids()
    if fore_pid in anc:
        # 前台是「发起调用的窗口」（DSH/终端）：它不是目标，用它之外的外部端口
        ext = [p for p in alive if owners.get(p) not in anc]
        info.update({"usable": ext, "conductor": True})
        if ext:
            info["note"] = ("前台是发起调用的窗口（%s pid %d），按外部调试端口 %s 操作"
                            % (fore_exe or "?", fore_pid, ext))
        else:
            info["note"] = ("前台是发起调用的窗口（%s pid %d），本机只剩发起窗口自己的调试端口 %s；"
                            "要操作的外部程序没开调试端口" % (fore_exe or "?", fore_pid, alive))
        return info
    foreign = "、".join("%d（%s pid %s）" % (p, (info["owner"][str(p)]["exe"] or "?"),
                                            info["owner"][str(p)]["pid"]) for p in alive)
    info["note"] = ("这个窗口（%s pid %d）没有开调试端口；本机 CDP 端口 %s属于别的进程，不连它"
                    % (fore_exe or "?", fore_pid, foreign))
    return info



def uia_depth(win, limit=3):
    """UIA 树往下探几层，看是不是「只有个壳」。"""
    try:
        kids = win.GetChildren()
    except Exception:
        return 0, []
    kinds = [k.ControlTypeName for k in kids[:6]]
    deep = 0
    for k in kids[:4]:
        try:
            if k.GetChildren():
                deep += 1
        except Exception:
            pass
    return (1 + (1 if deep else 0)), kinds


class Win32Focus:
    """uiautomation 的 GetForegroundControl() 对全屏 DirectX / 游戏 / 部分 UWP 窗口
    返回 None（剪辑线 2026-11-22 报：系统明明有前台窗口，脊髓却报 no_foreground_window）。
    这里用 win32 兜底：只有 hwnd + 类名 + 标题，没有 UIA 子树。"""
    _raw = True

    def __init__(self, hwnd, cls, name):
        self.NativeWindowHandle = hwnd
        self.ControlTypeName = "WindowControl"
        self.ClassName = cls or ""
        self.Name = name or ""


def win32_foreground():
    """取当前前台窗口的 hwnd/类名/标题；取不到返回 None。"""
    try:
        u = ctypes.windll.user32
        u.GetForegroundWindow.restype = ctypes.c_void_p
        hwnd = u.GetForegroundWindow()
        if not hwnd:
            return None
        buf = ctypes.create_unicode_buffer(512)
        u.GetWindowTextW(ctypes.c_void_p(hwnd), buf, 512)
        cbuf = ctypes.create_unicode_buffer(256)
        u.GetClassNameW(ctypes.c_void_p(hwnd), cbuf, 256)
        return Win32Focus(hwnd, cbuf.value, buf.value)
    except Exception:
        return None


def resolve_foreground():
    """当前前台窗口：优先 UIA 控件；UIA 拿不到（DirectX/游戏/UWP）就用 win32 兜底。"""
    win = auto.GetForegroundControl()
    if win is None:
        win = win32_foreground()
    return win


def route(win):
    """返回 (path, reason, detail)。path 取 uia / cdp / unknown。
    2026-11-22：CDP 端口先过「归属」这一关（见 window_cdp_info），对不上就不给端口。"""
    cls = win.ClassName or ""
    cdp = window_cdp_info(win)
    if getattr(win, "_raw", False):
        # 没有 UIA 子树，只能按类名判：Chromium 系照旧走 CDP，其余老实说「看不见」
        if "Chrome_WidgetWin" in cls:
            return "cdp", "窗口是 Chromium 系（%s）；%s" % (cls, cdp["note"]), dict(cdp, via="win32")
        return "unknown", ("这个窗口 UIA 拿不到子树（多半是 DirectX 全屏 / 游戏 / 部分 UWP），"
                           "类名 %s；要操作里面只能走图像/OCR，别指望 UIA 语义" % (cls or "空")), dict(cdp, via="win32", uia_unavailable=True)
    if "Chrome_WidgetWin" in cls:
        return "cdp", "窗口是 Chromium 系（%s）；%s" % (cls, cdp["note"]), cdp
    d, kinds = uia_depth(win)
    if d < 2 and all(k in CONTAINER for k in kinds if k):
        return "unknown", ("UIA 树只有个壳（%s），内部不可见；类名 %s 不是 Chromium 系，"
                           "CDP 也连不上，只能截图/OCR" % (kinds, cls or "空")), dict(cdp, uia_invisible=True)
    return "uia", "UIA 能看到结构（子控件类型：%s）" % (kinds,), {"depth": d}


GENERIC_SUFFIX = ("输入框", "输入", "文本框", "按钮", "链接", "下拉框", "下拉", "选项", "区域", "页面", "页")


def expand_words(words):
    """2026-11-22（剪辑线 #115513-70f599 缺口 4）：她把元素叫「标题输入框」，但页面上
    placeholder 是「请输入标题」——整串匹配不上。这里把泛称后缀剥掉再补几个词。"""
    out = []
    for w in words:
        w = str(w or "").strip()
        if not w:
            continue
        out.append(w)
        for g in GENERIC_SUFFIX:
            if w.endswith(g) and len(w) > len(g):
                out.append(w[: -len(g)])
            if w.startswith(g) and len(w) > len(g):
                out.append(w[len(g):])
    seen, res = set(), []
    for w in out:
        if w not in seen:
            seen.add(w)
            res.append(w)
    return res


def cdp_execute(port, words, go, out):
    """在 CDP 端口对应的页面上找元素；--go 时点击并用 DOM 变化确认。"""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        from _cdp import CDP, pick_page
    except Exception as e:
        out.update({"ok": False, "why": "cdp_client_missing", "detail": str(e)[:80]})
        return out, 6
    t = pick_page(port)
    if not t:
        out.update({"ok": False, "why": "no_page_on_port", "port": port,
                    "note": "这个调试端口上没有可操作的页面（Electron 应用常只暴露内部 target）"})
        return out, 7
    out["page"] = {"title": (t.get("title") or "")[:50], "url": (t.get("url") or "")[:80]}
    try:
        cdp = CDP(t["webSocketDebuggerUrl"])
    except Exception as e:
        out.update({"ok": False, "why": "cdp_connect_failed", "detail": str(e)[:80]})
        return out, 6
    js_words = json.dumps(words, ensure_ascii=False)
    t0 = time.perf_counter()
    find_js = """(() => {
      const words = %s.map(w => String(w).toLowerCase());
      const all = [...document.querySelectorAll('a,button,div,span,li,select,input,textarea,label,[contenteditable=true]')];
      const hits = [];
      // 2026-11-22（缺口 4）：input/textarea 没有 innerText，得同时看 value/placeholder/aria-label/name/id/title
      const txtOf = (e) => [
        (e.innerText || ''), (e.value || ''),
        (e.getAttribute('aria-label') || ''), (e.getAttribute('placeholder') || ''),
        (e.getAttribute('name') || ''), (e.getAttribute('title') || ''), (e.id || '')
      ].join(' ').replace(/\s+/g, ' ').trim();
      for (const e of all) {
        const txt = txtOf(e);
        if (!txt) continue;
        const low = txt.toLowerCase();
        if (!words.some(w => low.includes(w))) continue;
        const r = e.getBoundingClientRect();
        const isFile = e.tagName === 'INPUT' && (e.type || '').toLowerCase() === 'file';
        // 文件选择框常常是 display:none，但它正是「选视频」要的那一个，不能在尺寸过滤里被丢掉
        if (!isFile && (r.width < 2 || r.height < 2)) continue;
        hits.push({el: e, tag: e.tagName.toLowerCase(), text: txt.slice(0, 40),
                   x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2),
                   visible: r.top >= 0 && r.left >= 0, is_file: isFile,
                   area: Math.max(1, Math.round(r.width * r.height))});
      }
      // 2026-11-22（缺口 3b）：容器 div 会先命中（整页文本都包含关键词）。
      // 去掉「包含另一个命中元素」的外层，再按面积从小到大取——这样点的是那个按钮，不是整页。
      // 2026-11-22（剪辑线复查）：文件框 / contenteditable / 无文本下拉，**没有可匹配的文本**，
      // 按文字找永远找不到。这几类按「元素类型」无条件收进来，label 用能识别的属性拼。
      const nearText = (e) => {
        let p = e;
        for (let i = 0; i < 4 && p; i++, p = p.parentElement) {
          const t = (p.innerText || '').replace(/\s+/g, ' ').trim();
          if (t) return t.slice(0, 24);
        }
        return '';
      };
      const auto = [];
      const pick = (e, label, kind) => {
        if (hits.some(h => h.el === e) || auto.some(h => h.el === e)) return;
        const r = e.getBoundingClientRect();
        auto.push({el: e, tag: e.tagName.toLowerCase(), text: label, kind: kind,
                   x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2),
                   visible: r.top >= 0 && r.left >= 0 && r.width > 2 && r.height > 2,
                   is_file: kind === 'file', area: Math.max(1, Math.round(r.width * r.height))});
      };
      // 只在关键词指向这类元素时才补，不然会变成一堆噪音（实测「看 简介」后面挂了 3 个文件框）
      const wantFile = words.some(w => /文件|上传|视频|素材|选择文件/.test(w));
      const wantEdit = words.some(w => /简介|描述|正文|内容|说明|编辑/.test(w));
      const wantSel = words.some(w => /分区|分类|下拉|选择|类型/.test(w));
      if (!wantFile && !wantEdit && !wantSel) {
        const only = hits.filter(h => !hits.some(o => o !== h && h.el.contains(o.el)));
        only.sort((a, b) => a.area - b.area);
        return JSON.stringify(only.slice(0, 8).map(h => ({tag: h.tag, text: h.text, x: h.x, y: h.y, visible: h.visible, is_file: h.is_file, kind: h.kind})));
      }
      for (const e of (wantFile ? document.querySelectorAll('input[type=file]') : [])) {
        pick(e, '[file] accept=' + (e.getAttribute('accept') || '').slice(0, 40), 'file');
      }
      for (const e of (wantEdit ? document.querySelectorAll('[contenteditable=true],[contenteditable=""],.ql-editor,[role=textbox]') : [])) {
        pick(e, '[editable] ' + (e.getAttribute('aria-label') || e.getAttribute('data-placeholder') || nearText(e) || 'contenteditable'), 'editable');
      }
      for (const e of (wantSel ? document.querySelectorAll('[class*=select-controller],[class*=bcc-select],[class*=ant-select],[class*=semi-select],[class*=el-select]') : [])) {
        if (e.querySelector('input,select')) continue;
        pick(e, '[select] ' + (nearText(e) || (e.className || '').toString().slice(0, 24)), 'select');
      }
      const inner = hits.filter(h => !hits.some(o => o !== h && h.el.contains(o.el)));
      inner.sort((a, b) => a.area - b.area);
      const merged = inner.concat(auto);
      return JSON.stringify(merged.slice(0, 10).map(h => ({tag: h.tag, text: h.text, x: h.x, y: h.y, visible: h.visible, is_file: h.is_file, kind: h.kind})));
    })()""" % js_words
    try:
        raw = cdp.eval(find_js)
        hits = json.loads(raw) if raw else []
    except Exception as e:
        out.update({"ok": False, "why": "cdp_eval_failed", "detail": str(e)[:100]})
        return out, 6
    out["ms"] = round((time.perf_counter() - t0) * 1000, 1)
    if not hits:
        out.update({"ok": False, "why": "not_found", "note": "页面上没有名字含这些词的元素"})
        avail_js = """(() => {
          const all = [...document.querySelectorAll('a,button,[role=button],select,input,textarea,label')];
          const o = [];
          for (const e of all) {
            const t = ((e.innerText || e.value || e.getAttribute('aria-label') || '') + '').trim();
            if (!t) continue;
            const r = e.getBoundingClientRect();
            if (r.width < 2 || r.height < 2) continue;
            o.push(t.slice(0, 24));
            if (o.length >= 40) break;
          }
          return JSON.stringify([...new Set(o)].slice(0, 15));
        })()"""
        try:
            raw_avail = cdp.eval(avail_js)
            out["available"] = json.loads(raw_avail) if raw_avail else []
        except Exception:
            out["available"] = []
        out["hint"] = "页面上有这些可点元素；换个词再试"
        return out, 4
    out["candidates"] = [{"i": i, "label": h["text"], "kind": h["tag"], "clickable": True,
                           "at": [h["x"], h["y"]], "visible": h["visible"],
                           # 2026-11-22：文件选择框没有坐标也可用（CDP setInputFiles 走的是元素本身）
                           "file_input": bool(h.get("is_file")),
                           "element_kind": h.get("kind") or h["tag"],
                           "extra": {"tag": h["tag"], "words": words,
                                     "note": ("文件框不可点坐标，用 CDP setInputFiles" if h.get("is_file")
                                              else ("contenteditable：先 focus 再输入" if h.get("kind") == "editable" else None))}} for i, h in enumerate(hits)]
    if not go:
        out["note"] = "只找没点（加 --go 才点）"
        return out, 0
    h = hits[0]
    click_js = """(() => {
      const els = [...document.querySelectorAll('a,button,div,span,li,select,input,textarea,label')];
      const hit = els.find(e => {
        const t = ((e.innerText || e.value || e.getAttribute('aria-label') || '') + '').trim();
        const r = e.getBoundingClientRect();
        return t.includes(%s) && r.width > 2 && r.height > 2;
      });
      if (!hit) return 'no_element';
      const before = document.body.innerHTML.length;
      hit.click();
      return JSON.stringify({before: before, tag: hit.tagName.toLowerCase()});
    })()""" % json.dumps(words[0], ensure_ascii=False)
    try:
        r1 = cdp.eval(click_js)
        time.sleep(0.35)
        after_len = cdp.eval("document.body.innerHTML.length")
        out["clicked"] = {"i": 0, "label": h["text"], "at": [h["x"], h["y"]]}
        try:
            before_len = json.loads(r1).get("before") if r1 and r1.startswith("{") else None
        except Exception:
            before_len = None
        changed = (before_len is not None and after_len is not None and after_len != before_len)
        out["verify"] = {"changed": changed, "how": "cdp:dom_len",
                         "evidence": "dom_len %s -> %s" % (before_len, after_len)}
        out["ok"] = True
        return out, 0
    except Exception as e:
        out.update({"ok": False, "why": "cdp_click_failed", "detail": str(e)[:100]})
        return out, 6


def list_available(root, limit=15):
    """找不到目标时，告诉人「这里现在有什么可点的」，比只说 not_found 有用。"""
    out = []

    def walk(c, d):
        if d > 10 or len(out) >= limit:
            return
        try:
            nm = (c.Name or "").strip()
            ct = c.ControlTypeName
            if nm and ct in CLICKABLE and 0 < len(nm) <= 24:
                if nm not in [x["label"] for x in out]:
                    out.append({"label": nm, "kind": ct})
            for ch in c.GetChildren():
                walk(ch, d + 1)
        except Exception:
            return

    walk(root, 0)
    return out[:limit]


def gate_ok(timeout=20):
    """过闸门：调 脊髓统.py gate（她 4 秒内动过键鼠就不许动）。新入口原来直接 c.Click()，把让位闸和脊髓锁全绕过去了——合并的第一步就是补这个。"""
    import subprocess
    try:
        p = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), '脊髓统.py'), 'gate'],
                           capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout)
        out = (p.stdout or '') + (p.stderr or '')
        return ('ok=True' in out), out.strip()[:160]
    except Exception as e:
        return False, 'gate_error: ' + str(e)[:80]


def ensure_foreground(win, tries=4, gap=0.4):
    """确保目标窗口在前台。返回 (ok, why)。真实鼠标点击没有前台就点空，必须显式确认。"""
    import ctypes
    u = ctypes.windll.user32
    try:
        hwnd = win.NativeWindowHandle
    except Exception:
        return True, "no-hwnd"
    for i in range(tries):
        if u.GetForegroundWindow() == hwnd:
            return True, "foreground"
        if u.IsIconic(hwnd):
            u.ShowWindow(hwnd, 9)  # SW_RESTORE
        u.SetForegroundWindow(hwnd)
        time.sleep(gap)
    if u.GetForegroundWindow() == hwnd:
        return True, "foreground-after-retry"
    return False, "not_foreground"


def tree_size(root, maxdepth=6, cap=3000):
    """数一棵 UIA 子树的节点数——点击前后对比，比「该点控件有没有换」更能反映真实变化。"""
    n = 0

    def walk(c, d):
        nonlocal n
        if d > maxdepth or n >= cap:
            return
        n += 1
        try:
            for ch in c.GetChildren():
                walk(ch, d + 1)
        except Exception:
            return

    walk(root, 0)
    return n


def find_controls(root, words, limit=8):
    hits = []

    def walk(c, depth):
        if depth > 14 or len(hits) >= limit * 4:
            return
        try:
            nm = (c.Name or "").strip()
            ct = c.ControlTypeName
            if nm and any(w in nm for w in words):
                hits.append({"ctrl": c, "name": nm[:40], "type": ct, "clickable": ct in CLICKABLE})
            for ch in c.GetChildren():
                walk(ch, depth + 1)
        except Exception:
            return

    walk(root, 0)
    hits.sort(key=lambda h: (0 if h["clickable"] else 1, len(h["name"])))
    return hits[:limit]


def run_chain(goal, port=0, window=""):
    """一串目标按顺序做；任一步失败立刻停，并说清卡在第几步。"""
    import subprocess
    steps = [s.strip() for s in goal.replace("；", ";").replace(";;", ";").replace("->", ";").split(";") if s.strip()]
    if len(steps) < 2:
        return None
    # 支持每一步自己指定窗口："窗口=记事本 点 文件；窗口=计算器 点 七"
    # 做不到「多指针同时」，但可以按窗口轮流：每步先把目标窗口置前再操作。
    import re as _re
    _wre = _re.compile(r"^窗口[=:]\s*(\S+)\s+(.+)$")
    # 「等 X @N」= 等窗口/元素出现，最多 N 秒。这类步骤不占光标，可以并行挂着——
    # 实测两件各等 1 秒的事：串行 2.0s、并行 1.0s（2 倍）。
    _wait_re = _re.compile(r"^等(?:待)?\s*(.+?)(?:\s*@(\d+(?:\.\d+)?))?$")
    results = []
    _pending = []
    def _wait_job(idx, title, limit, box):
        """等一个窗口出现；等到就记 ok，超时记 timeout。不碰光标、不点任何东西。"""
        import uiautomation as _auto
        # 子线程里用 UIA 必须先初始化 COM，否则 WindowControl 永远查不到（主线程能查、这里不行）
        try:
            import ctypes as _ct2
            _ct2.windll.ole32.CoInitializeEx(None, 0x2)  # COINIT_APARTMENTTHREADED
        except Exception:
            pass
        try:
            _auto.SetGlobalSearchTimeout(0.5)
        except Exception:
            pass
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < limit:
            try:
                w = _auto.WindowControl(searchDepth=1, SubName=title)
                if w.Exists(0.2):
                    box.append({"step": idx, "goal": "等 " + title, "ok": True,
                                "detail": {"waited_ms": round((time.perf_counter() - t0) * 1000, 1)}})
                    return
            except Exception:
                pass
            time.sleep(0.15)
        box.append({"step": idx, "goal": "等 " + title, "ok": False, "why": "timeout",
                    "detail": {"waited_ms": round((time.perf_counter() - t0) * 1000, 1)}})

    for i, st in enumerate(steps, 1):
        _w = window
        _m0 = _wait_re.match(st)
        if _m0:
            _title = _m0.group(1).strip()
            _limit = float(_m0.group(2) or 8)
            _box = []
            _th = threading.Thread(target=_wait_job, args=(i, _title, _limit, _box))
            _th.start()
            _pending.append((i, _title, _th, _box))
            continue
        _m = _wre.match(st)
        if _m:
            _w = _m.group(1)
            st = _m.group(2).strip()
        cmd = [sys.executable, os.path.abspath(__file__), st]
        if port:
            cmd += ["--port", str(port)]
        if _w:
            cmd += ["--window", _w]
        if st.startswith("点") or st.startswith("按"):
            cmd.append("--go")
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        try:
            r = json.loads(p.stdout)
        except Exception:
            r = {"ok": False, "why": "bad_output", "raw": (p.stdout or "")[:200]}
        results.append({"step": i, "goal": st, "ok": bool(r.get("ok")), "detail": r})
        if not r.get("ok"):
            for _i, _t, _th, _b in _pending:
                _th.join()
                if _b:
                    results.extend(_b)
            results.sort(key=lambda x: x.get("step", 0))
            return {"ok": False, "steps_done": i - 1, "total": len(steps), "failed_at": i,
                    "failed_goal": st, "why": r.get("why"), "available": r.get("available"),
                    "hint": "卡在第 %d 步；前面 %d 步已完成" % (i, i - 1), "steps": results}
    # 所有等待在这里 join：并行挂着，最后统一收口
    for _i, _t, _th, _b in _pending:
        _th.join()
        results.extend(_b)
    results.sort(key=lambda x: x.get("step", 0))
    _bad = [r for r in results if not r.get("ok")]
    if _bad:
        return {"ok": False, "steps_done": len(steps) - len(_bad), "total": len(steps),
                "failed_at": _bad[0].get("step"), "failed_goal": _bad[0].get("goal"),
                "why": _bad[0].get("why") or "step_failed", "steps": results}
    return {"ok": True, "steps_done": len(steps), "total": len(steps), "steps": results}



# ==== 2026-11-21 修复：把旧版 find/find-multi/hash/diff/selftest 接回来 ====
# 20:00 那次统一路由把整段替换掉，导致 手眼.py 调 脊髓.py 的 hash 子命令失败、模板匹配链路全断。
import sys, json, os
import cv2, numpy as np

def imread_u(path, flags=cv2.IMREAD_COLOR):
    """OpenCV 在 Windows 上读不了中文路径，走 numpy 读字节再 imdecode。"""
    try:
        buf = np.fromfile(path, dtype=np.uint8)
        return cv2.imdecode(buf, flags)
    except Exception:
        return None

def imwrite_u(path, img):
    ok, buf = cv2.imencode(os.path.splitext(path)[1] or '.png', img)
    if ok: buf.tofile(path)
    return ok

def _prep(img, mode):
    """匹配前的预处理：color 原样 / gray 灰度 / edge 边缘（对配色更稳）"""
    if img is None: return None
    if mode == "gray": return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    if mode == "edge":
        g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        return cv2.Canny(g, 60, 160)
    return img

def find(shot, tpl, thresh=0.8, scales=None, mode="color"):
    """模板匹配。scales 给了就做多尺度搜索（缩放是模板匹配的硬伤：2026-11-18 实测缩放 5% 分数掉到 0.63）。"""
    img = _prep(imread_u(shot), mode); t = _prep(imread_u(tpl), mode)
    if img is None or t is None: return {"ok": False, "error": "图读不到"}
    if scales:
        best = None
        s = scales[0]
        while s <= scales[1] + 1e-9:
            tt = cv2.resize(t, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
            if tt.shape[0] <= img.shape[0] and tt.shape[1] <= img.shape[1]:
                res = cv2.matchTemplate(img, tt, cv2.TM_CCOEFF_NORMED)
                _, mv, _, ml = cv2.minMaxLoc(res)
                if best is None or mv > best[0]: best = (mv, ml, tt.shape[1], tt.shape[0], s)
            s = round(s + scales[2], 4)
        if best is None: return {"ok": False, "error": "所有尺度都比截图大"}
        mv, ml, w, h, s = best
        return {"ok": bool(mv >= thresh), "score": round(float(mv), 4),
                "box": [int(ml[0]), int(ml[1]), int(w), int(h)], "scale": s, "thresh": thresh}
    if t.shape[0] > img.shape[0] or t.shape[1] > img.shape[1]: return {"ok": False, "error": "模板比截图大"}
    res = cv2.matchTemplate(img, t, cv2.TM_CCOEFF_NORMED)
    _, maxv, _, maxloc = cv2.minMaxLoc(res)
    h, w = t.shape[:2]
    return {"ok": bool(maxv >= thresh), "score": round(float(maxv), 4),
            "box": [int(maxloc[0]), int(maxloc[1]), int(w), int(h)], "thresh": thresh}

def find_multi(shot, tpl, thresh=0.7, scales=None, mode="color", topk=5):
    """多命中：把过阈值的候选都找出来，非极大抑制后按分数排序（用于「离上次位置最近」取舍）。"""
    img = _prep(imread_u(shot), mode); t0 = _prep(imread_u(tpl), mode)
    if img is None or t0 is None: return {"ok": False, "error": "图读不到"}
    cands = []
    ss = scales or (1.0, 1.0, 1.0)
    s = ss[0]
    while s <= ss[1] + 1e-9:
        t = cv2.resize(t0, None, fx=s, fy=s, interpolation=cv2.INTER_AREA) if scales else t0
        if t.shape[0] <= img.shape[0] and t.shape[1] <= img.shape[1]:
            res = cv2.matchTemplate(img, t, cv2.TM_CCOEFF_NORMED)
            ys, xs = np.where(res >= thresh)
            for y, x in zip(ys[:2000], xs[:2000]):
                cands.append((float(res[y, x]), int(x), int(y), int(t.shape[1]), int(t.shape[0]), round(s, 3)))
        s = round(s + (ss[2] if scales else 1.0), 4)
    cands.sort(key=lambda c: -c[0])
    kept = []
    for c in cands:
        if all(abs(c[1] - k[1]) > k[3] * 0.5 or abs(c[2] - k[2]) > k[4] * 0.5 for k in kept):
            kept.append(c)
        if len(kept) >= topk: break
    out = [{"score": round(c[0], 4), "box": [c[1], c[2], c[3], c[4]], "scale": c[5]} for c in kept]
    return {"ok": bool(out), "count": len(out), "hits": out, "thresh": thresh, "mode": mode}

def ahash(path, size=8):
    img = imread_u(path, cv2.IMREAD_GRAYSCALE)
    if img is None: return None
    small = cv2.resize(img, (size, size), interpolation=cv2.INTER_AREA)
    return small > small.mean()

def hamming(a, b):
    return int(np.count_nonzero(a != b))

def selftest(shot):
    img = imread_u(shot)
    if img is None: return {"ok": False, "error": "截图读不到"}
    h, w = img.shape[:2]
    # 找一块纹理最多的区域当模板（纯色区域会在别处重复命中，判不了）
    tw, th = 200, 80
    best = None
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    for yy in range(0, h - th, 80):
        for xx in range(0, w - tw, 120):
            v = float(gray[yy:yy + th, xx:xx + tw].std())
            if best is None or v > best[0]: best = (v, xx, yy)
    if best is None:
        # 2026-11-21：图太小（<320x160）或太均匀时 best 还是 None，原来这里直接崩。
        # 边界情况要给可读的结果，不许抛异常。
        return {"ok": False, "why": "图太小或太均匀，找不到可自测的块（需要 >= 320x160 且有纹理）",
                "size": [w, h]}
    _, x, y = best
    tpl_path = os.path.join(os.path.dirname(os.path.abspath(shot)), "_selftest_tpl.png")
    imwrite_u(tpl_path, img[y:y + th, x:x + tw])
    r = find(shot, tpl_path, 0.95)
    r["expect"] = [x, y, tw, th]
    r["selftest_ok"] = r.get("ok") and abs(r["box"][0] - x) <= 2 and abs(r["box"][1] - y) <= 2
    return r

def _legacy_main():
    if len(sys.argv) < 3: print(__doc__ or "用法见文件头"); return 2
    cmd = sys.argv[1]
    if cmd == "find":
        th = float(sys.argv[4]) if len(sys.argv) > 4 else 0.8
        mode = "color"
        if "--mode" in sys.argv:
            mode = sys.argv[sys.argv.index("--mode") + 1]
        scales = None
        if "--scales" in sys.argv:
            i = sys.argv.index("--scales")
            lo, hi, step = [float(x) for x in sys.argv[i + 1].split(",")]
            scales = (lo, hi, step)
        print(json.dumps(find(sys.argv[2], sys.argv[3], th, scales, mode), ensure_ascii=False))
    elif cmd == "find-multi":
        th = float(sys.argv[4]) if len(sys.argv) > 4 else 0.7
        mode = sys.argv[sys.argv.index("--mode") + 1] if "--mode" in sys.argv else "color"
        topk = int(sys.argv[sys.argv.index("--topk") + 1]) if "--topk" in sys.argv else 5
        scales = None
        if "--scales" in sys.argv:
            i = sys.argv.index("--scales"); lo, hi, st = [float(x) for x in sys.argv[i + 1].split(",")]
            scales = (lo, hi, st)
        print(json.dumps(find_multi(sys.argv[2], sys.argv[3], th, scales, mode, topk), ensure_ascii=False))
    elif cmd == "hash":
        h = ahash(sys.argv[2])
        print(json.dumps({"ok": h is not None, "hash": "".join("1" if x else "0" for x in h.flatten())}))
    elif cmd == "diff":
        # 2026-11-22 修（独立验收 #222403 实测）：
        #  ① 原来 int(sys.argv[4]) 会把 --rect 当阈值 → ValueError 直接崩；
        #  ② 全局 aHash 把图缩到 8x8，60x40 的真实变化被平均成 hamming=2，判"没变"（漏检）。
        # 现在：支持 --rect x,y,w,h 只比那一块，区域内用「变化像素占比」，比 aHash 敏感得多；
        # 不带 --rect 保留旧行为（全局 aHash），但参数解析不再靠位置猜。
        args2 = sys.argv[2:]
        rect = None
        if "--rect" in sys.argv:
            k = sys.argv.index("--rect")
            if k + 1 >= len(sys.argv):
                print(json.dumps({"ok": False, "why": "--rect 后面要跟 x,y,w,h"}, ensure_ascii=False)); return 2
            try:
                rect = [int(v) for v in sys.argv[k + 1].split(",")]
                if len(rect) != 4:
                    raise ValueError
            except Exception:
                print(json.dumps({"ok": False, "why": "--rect 需要 x,y,w,h 四个整数，收到 %r" % sys.argv[k + 1]}, ensure_ascii=False)); return 2
            args2 = [a for j, a in enumerate(sys.argv[2:], start=2) if j not in (k, k + 1)]
        if len(args2) < 2:
            print(json.dumps({"ok": False, "why": "diff 需要两张图：diff A B [阈值] [--rect x,y,w,h]"}, ensure_ascii=False)); return 2
        pa, pb = args2[0], args2[1]
        if rect:
            ia, ib = imread_u(pa), imread_u(pb)
            if ia is None or ib is None:
                print(json.dumps({"ok": False, "why": "有一张图读不到"}, ensure_ascii=False)); return 1
            x, y, w, h = rect
            ca = ia[y:y + h, x:x + w]
            cb = ib[y:y + h, x:x + w]
            hh = min(ca.shape[0], cb.shape[0]); ww = min(ca.shape[1], cb.shape[1])
            if hh <= 0 or ww <= 0:
                print(json.dumps({"ok": False, "why": "rect 超出图像范围"}, ensure_ascii=False)); return 2
            ca = ca[:hh, :ww]; cb = cb[:hh, :ww]
            try:
                th = float(args2[2]) if len(args2) > 2 else 0.002   # 变化像素占比阈值（0.2%）
            except Exception:
                print(json.dumps({"ok": False, "why": "阈值必须是数字"}, ensure_ascii=False)); return 2
            dpx = np.abs(ca.astype(int) - cb.astype(int)).sum(axis=2)
            ratio = float((dpx > 30).mean())
            print(json.dumps({"ok": True, "mode": "rect", "rect": rect, "changed_ratio": round(ratio, 5),
                              "changed": bool(ratio > th), "thresh": th}, ensure_ascii=False))
        else:
            try:
                th = int(args2[2]) if len(args2) > 2 else 6
            except Exception:
                print(json.dumps({"ok": False, "why": "全局模式的阈值是整数（汉明距离）"}, ensure_ascii=False)); return 2
            ha, hb = ahash(pa), ahash(pb)
            if ha is None or hb is None:
                print(json.dumps({"ok": False, "why": "有一张图读不到"}, ensure_ascii=False)); return 1
            d = hamming(ha, hb)
            print(json.dumps({"ok": True, "mode": "global_ahash", "hamming": d,
                              "changed": d > th, "thresh": th}, ensure_ascii=False))
    elif cmd == "selftest":
        print(json.dumps(selftest(sys.argv[2]), ensure_ascii=False))
    else:
        print("未知命令"); return 2
    return 0

def main():
    # 2026-11-21：旧子命令仍在被 手眼.py / 脊髓2.py 调用，必须先分发
    if len(sys.argv) >= 2 and sys.argv[1] in ('find', 'find-multi', 'hash', 'diff', 'selftest'):
        return _legacy_main()
    ap = argparse.ArgumentParser()
    ap.add_argument("goal", nargs="?", default="")
    ap.add_argument("--info", action="store_true")
    ap.add_argument("--window", default="")
    ap.add_argument("--go", action="store_true")
    ap.add_argument("--pick", type=int, default=0)
    ap.add_argument("--port", type=int, default=0, help="直接指定 CDP 端口，跳过窗口路由")
    ap.add_argument("--force", action="store_true", help="跳过让位闸（只用于没人在用电脑的时候）")
    a = ap.parse_args()
    auto.SetGlobalSearchTimeout(1.5)
    if a.goal and ((";" in a.goal) or ("；" in a.goal) or ("->" in a.goal)):
        res = run_chain(a.goal, a.port, a.window)
        if res is not None:
            print(json.dumps(res, ensure_ascii=False, indent=1))
            return 0 if res.get("ok") else 9
    if a.port:
        words0 = [w for w in a.goal.replace("点", " ").replace("看", " ").replace("按", " ").split() if w]
        base = {"ok": True, "route": "cdp", "reason": "手动指定端口 %d" % a.port}
        if not words0:
            print(json.dumps(base, ensure_ascii=False, indent=1)); return 0
        res, rc = cdp_execute(a.port, words0, a.go, base)
        print(json.dumps(res, ensure_ascii=False, indent=1)); return rc
    if a.window:
        win = auto.WindowControl(searchDepth=1, SubName=a.window)
        if not win.Exists(2):
            print(json.dumps({"ok": False, "why": "window_not_found"}, ensure_ascii=False)); return 3
    else:
        # 2026-11-22（剪辑线报的真问题）：全屏 DirectX/游戏窗口 UIA 返回 None，
        # 原来直接判 no_foreground_window，三层路由在第一层就死，CDP 两层没机会跑。
        win = resolve_foreground()
        guard = 0
        if win is not None and not getattr(win, "_raw", False):
            while win.ControlTypeName != "WindowControl" and guard < 8:
                win = win.GetParentControl(); guard += 1
        if win is None:
            print(json.dumps({"ok": False, "why": "no_foreground_window"}, ensure_ascii=False)); return 3
    path, reason, detail = route(win)
    out = {"ok": True, "window": (win.Name or "")[:50], "class": win.ClassName or "", "route": path, "reason": reason}
    if a.info or not a.goal:
        out["cdp"] = detail
        if path == "uia":
            out["hint"] = "UIA 可用"
        elif (detail or {}).get("usable"):
            out["hint"] = "该窗口要走 CDP，可用端口 %s（只报告，不动手）" % ((detail or {}).get("usable"),)
        else:
            out["hint"] = "该窗口没有可用调试端口，看不到里面；别拿别的窗口的页面顶替"
        print(json.dumps(out, ensure_ascii=False, indent=1)); return 0
    words = expand_words([w for w in a.goal.replace("点", " ").replace("看", " ").replace("按", " ").split() if w])
    if path != "uia":
        ports = (detail or {}).get("usable") or []
        if not a.goal:
            out["hint"] = "该窗口要走 CDP"
            print(json.dumps(out, ensure_ascii=False, indent=1)); return 0
        if not words:
            out.update({"ok": False, "why": "no_keywords"})
            print(json.dumps(out, ensure_ascii=False, indent=1)); return 2
        if not ports:
            _d = detail or {}
            out.update({"ok": False, "why": "route_is_cdp_no_port",
                        "note": "没有可用调试端口，不动手：" + (reason or "该窗口没开调试端口"),
                        "fore": _d.get("fore"), "cdp_owner": _d.get("owner"),
                        "hint": "Chromium 系窗口要带 --remote-debugging-port 启动；别的窗口走 UIA/截图"})
            print(json.dumps(out, ensure_ascii=False, indent=1)); return 5
        last = None
        for p in ports:
            res, rc = cdp_execute(p, words, a.go, out)
            last = (res, rc)
            # 2026-11-22：从 DSH 里调用时，9230 那个「dsh://host」页面会被优先选中，
            # 于是又变成「在自己的界面上找东西」。还有别的端口就先跳过 DSH 自己的页面。
            _pur = str(((res or {}).get("page") or {}).get("url") or "")
            if _pur.startswith("dsh://") and len(ports) > 1 and p != ports[-1]:
                continue
            if res.get("ok") or res.get("why") not in ("no_page_on_port", "cdp_connect_failed"):
                break
        out["route"] = "cdp"
        # 2026-11-22（剪辑线缺口 3）：DSH 自己也是 Chrome_WidgetWin_1，前台窗口会被当成「目标窗口」，
        # 于是 window 报 DSH、page 却是 Edge 的投稿页，看着像配错了对。这里把两者分开：
        # window=真正在操作的页面标题，host_window=发起调用的窗口。
        res = last[0]
        try:
            page = (res or {}).get("page") or {}
            host = (res or {}).get("window") or ""
            if page.get("title"):
                res["host_window"] = host
                res["window"] = page["title"].strip() + " (cdp)"
                if "DeepSeek Harness" in host:
                    res["note"] = ("发起调用的窗口本身就是 Chromium（DSH），已按 CDP 页面为准；"
                                   "host_window 是发起窗口，不是目标窗口。要给页面就用 --port/--window。")
        except Exception:
            pass
        print(json.dumps(res, ensure_ascii=False, indent=1)); return last[1]
    if not words:
        print(json.dumps({"ok": False, "why": "no_keywords"}, ensure_ascii=False)); return 2
    t0 = time.perf_counter()
    hits = find_controls(win, words)
    out["ms"] = round((time.perf_counter() - t0) * 1000, 1)
    if not hits:
        out["ok"] = False
        out["why"] = "not_found"
        out["goal"] = a.goal
        out["available"] = list_available(win)
        out["hint"] = "这里有这些可点控件；换个词再试，或确认窗口对不对"
        print(json.dumps(out, ensure_ascii=False, indent=1)); return 4
    cand = []
    for i, h in enumerate(hits):
        try:
            r = h["ctrl"].BoundingRectangle
            at = [(r.left + r.right) // 2, (r.top + r.bottom) // 2]
        except Exception:
            at = None
        cand.append({"i": i, "label": h["name"], "kind": h["type"], "clickable": h["clickable"],
                     "at": at, "extra": {"ctrl": h["type"], "class": h["ctrl"].ClassName or ""}})
    out["candidates"] = cand
    if not a.go:
        out["note"] = "只找没点（加 --go 才点）"
        print(json.dumps(out, ensure_ascii=False, indent=1)); return 0
    pick = a.pick if 0 <= a.pick < len(hits) else 0
    c = hits[pick]["ctrl"]
    r = c.BoundingRectangle
    cx, cy = (r.left + r.right) // 2, (r.top + r.bottom) // 2
    # 点之前必须让目标窗口在前台：uiautomation 的 Click 走真实鼠标坐标，窗口不在前台就会点空
    if not a.force:
        g_ok, g_msg = gate_ok()
        if not g_ok:
            out.update({"ok": False, "why": "gate_blocked",
                        "note": "让位闸没过，不动手（她可能正在操作）", "gate": g_msg})
            return out, 10
    fg_ok, fg_why = ensure_foreground(win)
    if not fg_ok:
        out.update({"ok": False, "why": "not_foreground",
                    "note": "目标窗口抢不到前台，点了也会落空，所以不点", "fg": fg_why})
        return out, 8
    before_n = tree_size(win)
    c.Click()
    time.sleep(0.25)
    after_n = tree_size(win)
    after = auto.ControlFromPoint(cx, cy)
    out["clicked"] = {"i": pick, "label": hits[pick]["name"], "at": [cx, cy]}
    a_type = after.ControlTypeName if after else None
    a_name = (after.Name or "")[:30] if after else None
    # changed 的语义两条路必须一致：点了之后「世界有没有变」。
    # UIA：点前后整个窗口的 UIA 节点数变了就算变（菜单弹出、面板展开都会增加节点）；
    # 「该点上的控件换没换」太迟钝——点菜单项时控件本身还在，换了也不一定变。
    out["verify"] = {"changed": (after_n != before_n), "how": "uia:tree-node-count",
                     "evidence": "窗口节点数 %d -> %d；该点上是 %s / %s" % (before_n, after_n, a_type, a_name)}
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())