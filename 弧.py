# 弧.py —— 反射弧注册与执行：一条弧 = 触发条件 + 步骤链 + 成功判据（数据在 弧.json）
# 用法: python 弧.py --list | python 弧.py <弧名> [--dry]
import sys, os, json, time, subprocess

# ==== DPI 感知（2026-11-20 知鱼知乐补）：保证坐标与截图同坐标系 ====
try:
    import ctypes as _dpi_ct
    try:
        _dpi_ct.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        _dpi_ct.windll.user32.SetProcessDPIAware()
except Exception:
    pass
# ==== DPI 结束 ====
HERE = os.path.dirname(os.path.abspath(__file__))
PY = os.environ.get("SPINE_PY") or sys.executable
BASE = os.environ.get("SPINE_BASE") or os.path.dirname(os.path.abspath(__file__))
SHOTS = os.path.join(BASE, "训练", "ui-detect", "shots", "_手眼")
ARCS = os.path.join(HERE, "弧.json")
ENV = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
NO_WIN = 0x08000000

def log(m): print(m, flush=True)

def ps1(cmd, timeout=120):
    return subprocess.run(["pwsh", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-Command", cmd],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          env=ENV, creationflags=NO_WIN, timeout=timeout)

def win_ps(code):
    return ps1(code)

ENUM = r"""Add-Type -Namespace W -Name U -MemberDefinition '[DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc f, IntPtr l); public delegate bool EnumWindowsProc(IntPtr h, IntPtr l); [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr h, System.Text.StringBuilder s, int n); [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);'; $script:hits=@(); $cb=[W.U+EnumWindowsProc]{ param($h,$l); $sb=New-Object System.Text.StringBuilder 256; [void][W.U]::GetWindowText($h,$sb,256); if($sb.ToString() -like $env:ARC_PAT -and [W.U]::IsWindowVisible($h)){ $script:hits += $sb.ToString() }; return $true }; [void][W.U]::EnumWindows($cb,[IntPtr]::Zero); $script:hits | ConvertTo-Json -Compress"""

def find_window(pattern):
    env = dict(ENV, ARC_PAT=("*" + pattern + "*"))
    out = subprocess.run(["pwsh", "-NoProfile", "-Command", ENUM], capture_output=True, text=True,
                         encoding="utf-8", errors="replace", env=env, creationflags=NO_WIN, timeout=60)
    txt = (out.stdout or "").strip()
    return txt if txt and txt != "[]" else None

def snapshot(tag="arc"):
    os.makedirs(SHOTS, exist_ok=True)
    p = os.path.join(SHOTS, "arc_%s_%s.png" % (tag, time.strftime("%H%M%S")))
    code = "from PIL import ImageGrab; ImageGrab.grab(all_screens=True).save(r'%s')" % p
    subprocess.run([PY, "-c", code], capture_output=True, env=ENV, creationflags=NO_WIN, timeout=60)
    return p

STOP_PATH = os.path.join(HERE, "STOP")


def stop_requested():
    """急停：只要这个文件存在就停。她或我随时 touch 一下就刹车（2026-11-22）。"""
    return os.path.exists(STOP_PATH)


def gate_allows(wait_seconds=12):
    """通用让位闸：她在动键鼠就等一会儿，仍不让就返回 False（整条弧停）。
    2026-11-22：原来只有 click 走 点到位 才带闸，key/type/scroll 那些没统一问闸门。"""
    for i in range(max(1, int(wait_seconds))):
        r = subprocess.run([PY, os.path.join(HERE, "闸门.py"), "check"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           env=ENV, creationflags=NO_WIN, timeout=20)
        if r.returncode == 0:
            return True
        if i == 0:
            log("  让位：她正在用键鼠，等一等（最多 %ds）" % wait_seconds)
        time.sleep(1)
    return False


def run_arc(name, dry=False):
    with open(ARCS, encoding="utf-8") as f: data = json.load(f)
    arc = next((a for a in data.get("arcs", []) if a.get("name") == name), None)
    if not arc: log("没有这条弧：" + name); return 2
    log("弧：%s（%s）%s" % (name, arc.get("trigger", ""), "  [careful]" if arc.get("careful") else ""))
    steps = arc.get("steps", [])
    launched_pid = 0
    # 2026-11-19 真分支：careful 每步先等界面稳定（防在动画中间点）
    MODE = os.environ.get("SHRE_MODE", "fast")
    arc_careful = MODE == "careful" or bool(arc.get("careful"))
    for i, st in enumerate(steps, 1):
        # 2026-11-22：每步先看急停标志，再过让位闸——控制自如的前提是「随时能停、不抢她的键鼠」
        if stop_requested():
            log("  见到 STOP 文件，整条弧停下（删掉 %s 才能继续）" % STOP_PATH)
            return 3
        kind0 = st.get("do")
        if kind0 not in ("wait", "verify", "wait_window", "wait_process", "launch") and not dry:
            if not gate_allows():
                log("  让位闸一直不放行，整条弧停下（她还在用键鼠）")
                return 3
        if arc_careful and st.get("do") in ("type", "key"):   # 弧只有 type/key 这两种会动界面
            log("  [careful] 等界面稳定…")
            subprocess.run([PY, os.path.join(HERE, "手眼.py"), "stable"], capture_output=True, creationflags=0x08000000, timeout=60)
        kind = st.get("do")
        t0 = time.time()
        ok = True; note = ""
        # ==== 2026-11-20 新增：会动界面的步骤，动手前先查白名单外的弹窗 ====
        if kind in ("key", "type", "scroll", "click", "window"):
            chk = [PY, os.path.join(HERE, "查弹窗.py")]
            if st.get("x") is not None and st.get("y") is not None:
                chk += ["--point", "%d,%d" % (int(st["x"]), int(st["y"]))]
            rc2 = subprocess.run(chk, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                 env=ENV, creationflags=NO_WIN, timeout=30)
            if rc2.returncode == 3:
                log("  弹窗拦截，整段停下：" + ((rc2.stdout or "").strip()[:160]))
                return 1
        if kind == "launch":
            cmd = "'" + st["path"] + "'"
            if st.get("args"): cmd += " -ArgumentList '" + st["args"] + "'"
            r = ps1("Start-Process -FilePath " + cmd + " -PassThru | ForEach-Object { $_.Id }")
            ok = r.returncode == 0; note = "launch " + st["path"]
            try:
                launched_pid = int(((r.stdout or "").strip() or "0").splitlines()[0])
            except Exception:
                launched_pid = 0
        elif kind == "wait_window":
            hits = None
            titles = st["title"] if isinstance(st.get("title"), list) else [st.get("title")]
            for _ in range(int(st.get("tries", 20))):
                for ttl in titles:
                    hits = find_window(ttl)
                    if hits: break
                if hits: break
                time.sleep(0.7)
            ok = bool(hits); note = "window " + str(titles)[:60] + " -> " + str(hits)[:60]
        elif kind == "wait_process":
            # 按 exe 名等「有可见窗口」的进程，并把真实标题学回来（写回 弧.json）
            exe = st["exe"]
            proc_ps = (
                "$p = Get-Process -Name '" + exe + "' -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 }; "
                "if ($p) { $p | ForEach-Object { $_.MainWindowTitle } | Select-Object -First 1 } else { 'NONE' }"
            )
            title = None
            for _ in range(int(st.get("tries", 40))):
                r = ps1(proc_ps)
                out0 = ((r.stdout or "") + "").strip()
                if out0 and out0 != "NONE":
                    title = out0.splitlines()[0].strip()
                    break
                time.sleep(0.8)
            ok = bool(title)
            note = "process " + exe + " -> " + str(title)[:50]
            if ok and st.get("learn", True) and arc:
                try:
                    with open(ARCS, encoding="utf-8") as f: d = json.load(f)
                    for a in d.get("arcs", []):
                        if a.get("name") == name:
                            for s2 in a.get("steps", []):
                                if s2.get("do") == "wait_window" and not s2.get("_learned"):
                                    s2["title"] = title; s2["_learned"] = True
                    with open(ARCS, "w", encoding="utf-8") as f: json.dump(d, f, ensure_ascii=False, indent=1)
                except Exception:
                    pass
        elif kind == "window":
            # 窗口级动作走语义（按名字点），不靠坐标：maximize / restore / close / focus
            win = st.get("window"); act0 = st.get("action", "maximize")
            name_map = {"maximize": "最大化", "restore": "还原", "close": "关闭", "minimize": "最小化"}
            target = name_map.get(act0, act0)
            pid_args = ["--pid", str(launched_pid)] if launched_pid else []
            r2 = subprocess.run([PY, os.path.join(HERE, "语义.py"), "click"] + pid_args + ["--window", win, "--name", target],
                                capture_output=True, text=True, encoding="utf-8", errors="replace",
                                env=ENV, creationflags=NO_WIN, timeout=60)
            blob2 = ((r2.stdout or "") + (r2.stderr or "")).strip()
            ok = '"clicked": true' in blob2
            note = "window %s -> %s %s" % (win, target, blob2[-80:])
        elif kind == "gate":
            plat = st.get("platform", "bili")
            r = subprocess.run([PY, os.path.join(HERE, "发布门禁.py"), plat], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", env=ENV, creationflags=NO_WIN, timeout=300)
            blob = ((r.stdout or "") + (r.stderr or "")).strip()
            # 不只是「能不能发」，还要求门禁真的读到了页面（有 missing 字段）；
            # 读不到页面 ≠ 缺材料，不能算通过
            info = None
            for line in blob.splitlines():
                line = line.strip()
                if line.startswith("{"):
                    try: info = json.loads(line)
                    except Exception: pass
            readable = bool(info) and ("missing" in info)
            passed = ("可以发" in blob)
            expect = bool(st.get("expect_pass", True))
            ok = readable and (passed == expect)
            note = "gate %s readable=%s -> %s" % (plat, readable, (blob.splitlines()[-1][:50] if blob else "no output"))
        elif kind == "snapshot":
            note = "shot " + snapshot(name)
        elif kind == "close_window":
            # 2026-11-19 修：title 可能是列表（wait_window 支持列表，这里也必须支持），否则 subprocess 收到 list 报 TypeError
            t0 = time.time()
            titles0 = st.get("title")
            if not isinstance(titles0, list): titles0 = [titles0]
            closed = False
            for ttl in titles0:
                rr = subprocess.run(["pwsh", "-NoProfile", "-File", os.path.join(HERE, "_close_by_title.ps1"), "-Title", str(ttl)],
                                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                                    env=ENV, creationflags=NO_WIN, timeout=90)
                if "CLOSED" in ((rr.stdout or "") + (rr.stderr or "")):
                    closed = True; break
            ok = closed; note = "close " + "|".join(str(x) for x in titles0)
        elif kind == "run":
            script = os.path.join(HERE, st.get("script", ""))
            args = st.get("args", [])
            # .py 直接用 Python 跑（2026-11-20 加，方便把校验器接进弧）
            runner = [PY, script] if script.lower().endswith(".py") else ["pwsh", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-File", script]
            r = subprocess.run(runner + args,
                               capture_output=True, text=True, encoding="utf-8", errors="replace",
                               env=ENV, creationflags=NO_WIN, timeout=300)
            blob_run = (r.stdout or "") + (r.stderr or "")
            exp = st.get("expect")
            ok = (r.returncode == 0) and ((exp in blob_run) if exp else True)
            note = "run " + os.path.basename(script) + " rc=" + str(r.returncode) + ((" 缺「%s」" % exp) if (exp and exp not in blob_run) else (" | " + blob_run.strip()[-60:].replace("\n", " ")))
        elif kind == "key":
            # 2026-11-20 起走 动.py（唯一执行层，带规则闸/脊髓锁/让位闸）
            r = subprocess.run([PY, os.path.join(HERE, "动.py"), "key", st["keys"]],
                               capture_output=True, text=True, encoding="utf-8", errors="replace",
                               env=ENV, creationflags=NO_WIN, timeout=60)
            ok = r.returncode == 0
            note = "key " + st["keys"] + ("" if ok else " rc=%d %s" % (r.returncode, (r.stdout or "").strip()[:80]))
        elif kind == "type":
            r = subprocess.run([PY, os.path.join(HERE, "动.py"), "type", st["text"]],
                               capture_output=True, text=True, encoding="utf-8", errors="replace",
                               env=ENV, creationflags=NO_WIN, timeout=60)
            ok = r.returncode == 0
            note = "type %d chars" % len(st["text"]) + ("" if ok else " rc=%d %s" % (r.returncode, (r.stdout or "").strip()[:80]))
        elif kind == "scroll":
            amt = int(st.get("amount", 3))
            if st.get("dir", "down") == "up":
                amt = -amt
            r = subprocess.run([PY, os.path.join(HERE, "动.py"), "scroll", str(amt)],
                               capture_output=True, text=True, encoding="utf-8", errors="replace",
                               env=ENV, creationflags=NO_WIN, timeout=60)
            r2 = type("X", (), {"returncode": r.returncode})()
            r = r2; ok = r.returncode == 0; note = "scroll" + ("" if ok else " rc=%d" % r.returncode)
        elif kind == "verify":
            # 只验不点：目标状态回读（窗口/控件/数据三层可用 all:& 串起来）
            exp = st.get("expect", "")
            wait_ms = int(st.get("wait", 2000))
            rv = subprocess.run([PY, os.path.join(HERE, "验.py"), exp, "--timeout", str(wait_ms)],
                                capture_output=True, text=True, encoding="utf-8", errors="replace",
                                env=ENV, creationflags=NO_WIN, timeout=(wait_ms / 1000.0) + 20)
            ok = rv.returncode == 0
            note = "verify " + exp[:60] + (" OK" if ok else " FAIL " + ((rv.stdout or "").strip()[:90]))
        elif kind == "click":
            # 坐标点击 + 目标状态回读 + 抑制（走 点到位.py）
            cmd2 = [PY, os.path.join(HERE, "点到位.py"), "--x", str(int(st["x"])), "--y", str(int(st["y"]))]
            if st.get("expect"):
                cmd2 += ["--expect", st["expect"], "--wait", str(int(st.get("wait", 1500)))]
            if st.get("path"):
                cmd2 += ["--path", st["path"]]
            elif arc:
                cmd2 += ["--path", name + "#" + str(i)]
            rc3 = subprocess.run(cmd2, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                 env=ENV, creationflags=NO_WIN, timeout=int(st.get("timeout", 120)))
            ok = rc3.returncode == 0
            note = "click (%d,%d) %s" % (int(st["x"]), int(st["y"]), ((rc3.stdout or "").strip()[:110]))
        elif kind == "wait":
            time.sleep(float(st.get("seconds", 1))); note = "waited"
        else:
            ok = False; note = "未知步骤 " + str(kind)
        # 被让位闸挡下时等一下再试（她刚好在动键鼠）
        gate_retry = 0
        while (not ok) and ("闸门" in note or "gate" in note.lower() or "rc=3" in note) and gate_retry < 3:
            gate_retry += 1
            time.sleep(3)
            r = ps1("& '" + os.path.join(HERE, "动作.ps1") + "' -Action probe")
            if "gate ok=True" in ((r.stdout or "") + (r.stderr or "")):
                if kind == "scroll":
                    r2 = ps1("& '" + os.path.join(HERE, "动作.ps1") + "' -Action scroll -Direction " + st.get("dir", "down") + " -Amount " + str(st.get("amount", 3)))
                    ok = r2.returncode == 0
        log("  %d %s %s %5dms" % (i, "OK" if ok else "FAIL", note, (time.time() - t0) * 1000))
        if not ok: return 1
    return 0

def record_arc(name, ok, ms, steps):
    """任务级日志：一条弧 = 一个 episode（含步骤数、成败、耗时）"""
    try:
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"), "kind": "arc", "name": name,
               "task": name, "ok": bool(ok), "ms": int(ms),
               "extra": {"steps": steps}}
        p = os.path.join(BASE, "训练", "ui-detect", "实验数据.jsonl")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a", encoding="utf-8") as f: f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception: pass

def main():
    if len(sys.argv) < 2 or sys.argv[1] == "--list":
        with open(ARCS, encoding="utf-8") as f: d = json.load(f)
        for a in d.get("arcs", []): log("- %s ｜ %s ｜ %d 步" % (a.get("name"), a.get("trigger", ""), len(a.get("steps", []))))
        return 0
    t0 = time.time()
    rc = run_arc(sys.argv[1], "--dry" in sys.argv)
    try:
        cnt = 0
        with open(ARCS, encoding="utf-8") as fh:
            for x in json.load(fh).get("arcs", []):
                if x.get("name") == sys.argv[1]: cnt = len(x.get("steps", []))
    except Exception:
        cnt = 0
    record_arc(sys.argv[1], rc == 0, (time.time() - t0) * 1000, cnt)
    return rc

if __name__ == "__main__": sys.exit(main())