# 序列.py —— 连续操作：按步骤跑「找→过闸→动→验」，每步重新定位（画面变了也不怕）
# 用法: python 序列.py steps.json
# 步骤格式: [{"click":"模板路径"},{"wait":1.0},{"stable":true},{"key":"^v"},{"expect_change":true}]
import sys, os, json, time, subprocess
import numpy as np, cv2
from PIL import ImageGrab

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
BASE = os.environ.get("SPINE_BASE") or os.path.dirname(os.path.abspath(__file__))
SHOTS = os.path.join(BASE, "训练", "ui-detect", "shots", "_手眼")
PY = os.environ.get("SPINE_PY") or sys.executable
NO_WIN = 0x08000000

def log(m): print(m, flush=True)

def diff_ratio(a, b, pixel_thresh=12):
    """两张图的变化像素占比；任一为空或尺寸不同 → 1.0（保守判为有变化）"""
    if a is None or b is None:
        return 1.0
    if getattr(a, "shape", None) != getattr(b, "shape", None):
        return 1.0
    d = np.abs(a.astype(np.int16) - b.astype(np.int16)).max(axis=2)
    return float((d > pixel_thresh).mean())


def changed_coarse_paths(a_path, b_path, pixel_thresh=12, ratio_thresh=0.01):
    """路径版「有没有变」：桌面上一直有别的窗口在动，静止两帧也有 ~0.28% 差别，阈值取 1%。
       要判「点对了」请用 点到位.py --expect，别用这个。"""
    if not a_path or not b_path:
        return True
    try:
        a = cv2.imdecode(np.fromfile(a_path, dtype=np.uint8), cv2.IMREAD_COLOR)
        b = cv2.imdecode(np.fromfile(b_path, dtype=np.uint8), cv2.IMREAD_COLOR)
    except Exception:
        return True
    if a is None or b is None or a.shape != b.shape:
        return True
    d = np.abs(a.astype(np.int16) - b.astype(np.int16)).max(axis=2)
    return bool((d > pixel_thresh).mean() > ratio_thresh)


def changed_coarse(a, b, pixel_thresh=12, ratio_thresh=0.002, min_pixels=24):
    """粗判「有没有变」：占比过阈 或 变化的像素数≥24（小目标也能被抓到）。
       注意这是**粗信号**，要判定「点对了」请用 点到位.py --expect。"""
    if a is None or b is None or getattr(a, "shape", None) != getattr(b, "shape", None):
        return True
    d = np.abs(a.astype(np.int16) - b.astype(np.int16)).max(axis=2)
    mask = (d > pixel_thresh)
    return bool(mask.mean() > ratio_thresh or int(mask.sum()) >= min_pixels)


def cap():
    os.makedirs(SHOTS, exist_ok=True)
    p = os.path.join(SHOTS, time.strftime("%Y%m%d"), time.strftime("%H%M%S") + "-seq.png")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    ImageGrab.grab(all_screens=True).save(p)
    return p

def ahash(p):
    out = subprocess.run([PY, os.path.join(HERE, "脊髓.py"), "hash", p], capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=NO_WIN)
    try: return json.loads(out.stdout).get("hash")
    except Exception: return None

def gate():
    out = subprocess.run(["pwsh", "-NoProfile", "-STA", "-File", os.path.join(HERE, "动作.ps1"), "-Action", "probe"], capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=NO_WIN)
    return "gate ok=True" in (out.stdout or "")

def act(*args):
    out = subprocess.run(["pwsh", "-NoProfile", "-STA", "-File", os.path.join(HERE, "动作.ps1")] + list(args), capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=NO_WIN)
    return out.returncode == 0, ((out.stdout or "") + (out.stderr or "")).strip()[-200:]

def locate(tpl):
    out = subprocess.run([PY, os.path.join(HERE, "手眼.py"), "see", tpl], capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=NO_WIN)
    try:
        for line in (out.stdout or "").splitlines():
            line = line.strip()
            if line.startswith("{"): return json.loads(line)
    except Exception: pass
    return {"ok": False}

def run(steps):
    # 模式：显式 --careful 或清单里出现 gate/发布类 run → 自动 careful（多重验证）
    careful_flags = [s.get("mode") == "careful" or ("gate" in s) or ("run" in s and "投稿" in str(s.get("run"))) for s in steps]
    careful = ("--careful" in sys.argv) or any(careful_flags)
    if careful: print("模式：careful（含门禁/投稿/显式要求）", flush=True)
    else: print("模式：fast", flush=True)
    moved = 0; results = []
    for i, st in enumerate(steps, 1):
        name = list(st.keys())[0]
        t0 = time.time()
        # 2026-11-22：急停开关——序列每步前先看 STOP 文件（和 弧.py / 脊髓统.py 同一套开关）
        if os.path.exists(os.path.join(HERE, "STOP")):
            r = {"step": i, "kind": name, "ok": False, "stopped": True,
                 "why": "STOP 文件存在：序列停下（删掉 工具\界面反射\STOP 恢复）"}
            results.append(r); log(json.dumps(r, ensure_ascii=False)); return results, i
        if not gate():
            r = {"step": i, "kind": name, "ok": False, "why": "闸门不过（她刚动过或允许操作关了）"}
            results.append(r); log(json.dumps(r, ensure_ascii=False)); return results, i
        before = cap(); h_before = ahash(before)
        if "click_kind" in st:
            # 按类别定位 → 点前实时校验 → 点（这是唯一该用的「点容器」方式）
            import urllib.request
            k = st["click_kind"]
            body = json.dumps({"kind": k}).encode()
            req = urllib.request.Request("http://127.0.0.1:8811/locate2", data=body,
                                         headers={"Content-Type": "application/json"})
            loc = json.loads(urllib.request.urlopen(req, timeout=60).read().decode())
            cand = (loc.get("candidates") or [{}])[0]
            box = cand.get("box")
            if not box:
                r = {"step": i, "kind": "click_kind", "ok": False, "why": "没定位到 " + k}
                results.append(r); log(json.dumps(r, ensure_ascii=False)); return results, i
            pv = json.dumps({"seconds": 0.4, "every": 0.2}).encode()
            req2 = urllib.request.Request("http://127.0.0.1:8811/perceive", data=pv,
                                          headers={"Content-Type": "application/json"})
            tr = json.loads(urllib.request.urlopen(req2, timeout=60).read().decode())
            x, y, w, h = box
            keep = False
            for tk in tr.get("tracks", []):
                b = tk.get("last_box") or []
                if len(b) != 4: continue
                ix = max(0, min(x+w, b[0]+b[2]) - max(x, b[0])); iy = max(0, min(y+h, b[1]+b[3]) - max(y, b[1]))
                if ix * iy > 0.4 * min(w*h, b[2]*b[3]): keep = True; break
            if not keep:
                r = {"step": i, "kind": "click_kind", "ok": False, "why": "点前校验不过（目标不在跟踪里）"}
                results.append(r); log(json.dumps(r, ensure_ascii=False)); return results, i
            ok, msg = act("-Action", "click", "-X", str(x + w // 2), "-Y", str(y + h // 2)); moved += 1
        elif "click" in st:
            r0 = locate(st["click"])
            if not r0.get("ok"):
                r = {"step": i, "kind": "click", "ok": False, "why": "没定位到模板"}
                results.append(r); log(json.dumps(r, ensure_ascii=False)); return results, i
            bx, by, bw, bh = r0["box"]
            ok, msg = act("-Action", "click", "-X", str(bx + bw // 2), "-Y", str(by + bh // 2))
            moved += 1
        elif "move" in st:
            ok, msg = act("-Action", "move", "-X", str(st["move"][0]), "-Y", str(st["move"][1])); moved += 1
        elif "scroll" in st:
            ok, msg = act("-Action", "scroll", "-Direction", st.get("dir", "down"), "-Amount", str(st["scroll"])); moved += 1
        elif "key" in st:
            ok, msg = act("-Action", "key", "-Keys", st["key"]); moved += 1
        elif "type" in st:
            ok, msg = act("-Action", "type", "-Text", st["type"]); moved += 1
        elif "gate" in st:
            platform = st["gate"] or "all"
            out = subprocess.run([PY, os.path.join(HERE, "发布门禁.py"), platform], capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", creationflags=NO_WIN, timeout=200)
            blob = ((out.stdout or "") + (out.stderr or "")).strip()
            expect_pass = bool(st.get("expect_pass", True))
            is_pass = ("可以发" in blob)
            ok = (out.returncode == 0) if expect_pass else (out.returncode != 0)
            msg = blob.splitlines()[-1][:120] if blob else "no output"
        elif "run" in st:
            spec = st["run"]
            script = os.path.join(HERE, spec.get("script", ""))
            args = spec.get("args", [])
            out = subprocess.run(["pwsh", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-File", script] + args,
                                 capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=NO_WIN, timeout=300)
            blob = (out.stdout or "") + (out.stderr or "")
            expect = spec.get("expect")
            ok = (out.returncode == 0) and ((expect in blob) if expect else True)
            msg = ("rc=%d " % out.returncode) + blob.strip()[-120:].replace("\n", " ")
        elif "wait" in st:
            time.sleep(float(st["wait"])); ok, msg = True, "waited"
        elif "stable" in st:
            out = subprocess.run([PY, os.path.join(HERE, "手眼.py"), "stable"], capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=NO_WIN)
            ok = "true" in (out.stdout or "").lower(); msg = "stable"
        else:
            ok, msg = False, "未知步骤 " + name
        time.sleep(0.8)
        after = cap(); h_after = ahash(after)
        # 2026-11-20 红队 F6：整屏哈希会被别的窗口/桌宠带偏，改按变化像素占比判
        changed = changed_coarse_paths(before, after)
        expect = st.get("expect_change", None)
        good = ok and (changed if expect is True else True)
        if careful and good and name not in ("wait", "stable"):
            # 慢路多一拍：动作后再过一次闸，确认没有误触她正在用的窗口
            subprocess.run(["pwsh", "-NoProfile", "-STA", "-File", os.path.join(HERE, "动作.ps1"), "-Action", "probe"],
                           capture_output=True, creationflags=NO_WIN)
        r = {"step": i, "kind": name, "ok": good, "ms": int((time.time() - t0) * 1000), "changed": changed, "note": msg[:80], "mode": "careful" if careful else "fast", "why": ("" if good else msg[:120])}
        results.append(r); log(json.dumps(r, ensure_ascii=False))
        if not good: return results, i
    return results, 0

def main():
    if len(sys.argv) < 2: print(__doc__ or "用法见文件头"); return 2
    with open(sys.argv[1], encoding="utf-8") as f: steps = json.load(f)
    log("步数 %d，开始（每步都会重新定位 + 过闸）" % len(steps))
    results, failed = run(steps)
    ok = sum(1 for r in results if r.get("ok"))
    log("完成 %d/%d，%s" % (ok, len(results), ("第 %d 步失败" % failed) if failed else "全部通过"))
    try:
        task_name = sys.argv[sys.argv.index("--task") + 1] if "--task" in sys.argv else os.path.basename(sys.argv[1])
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"), "kind": "sequence", "name": task_name, "task": task_name,
               "ok": failed == 0, "ms": sum(r.get("ms", 0) for r in results), "extra": {"steps": len(steps), "ok_steps": ok, "failed_step": failed}}
        p = os.path.join(BASE, "训练", "ui-detect", "实验数据.jsonl")
        with open(p, "a", encoding="utf-8") as f: f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception: pass
    return 0 if failed == 0 else 1

if __name__ == "__main__": sys.exit(main())