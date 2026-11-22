# 手眼.py —— 看→找→点→验 的闭环（不用语言）：截图捕获、模板定位、过闸点击、变化校验
# 用法:
#   python 手眼.py selftest                 # 捕获当前屏，裁一块再找回来（不点）
#   python 手眼.py see <模板>                # 只定位
#   python 手眼.py click <模板> [--dry]      # 定位后点中心；--dry 只报坐标
import sys, os, json, time, subprocess
from PIL import ImageGrab
import numpy as np

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

# 2026-11-19：两档模式（careful 时点后加一次变化校验）
MODE = os.environ.get("SHRE_MODE", "fast")

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.environ.get("SPINE_BASE") or os.path.dirname(os.path.abspath(__file__))
SHOTS = os.path.join(BASE, "训练", "ui-detect", "shots", "_手眼")
PY = os.environ.get("SPINE_PY") or sys.executable
sys.path.insert(0, HERE)

def log(m): print(m, flush=True)

def diff_ratio(a, b, pixel_thresh=12):
    """两张图的变化像素占比（0..1）。任一张读不到返回 1.0（保守：算变了，让人再看一眼）"""
    if a is None or b is None:
        return 1.0
    if getattr(a, "shape", None) != getattr(b, "shape", None):
        return 1.0
    d = np.abs(a.astype(np.int16) - b.astype(np.int16)).max(axis=2)
    return float((d > pixel_thresh).mean())


def changed_coarse_paths(a_path, b_path, pixel_thresh=12, ratio_thresh=0.01):
    """路径版「有没有变」：桌面上一直有桌宠/别的窗口在动，实测静止两帧也有 ~0.28% 像素差，
       所以阈值取 1%（低于这个的差别分不出来——真要判「点对了」必须用 点到位.py --expect）。
       2026-11-20 红队第二轮：原来把 capture() 返回的**路径字符串**当图像传进来，直接 AttributeError。"""
    import numpy as _np
    import cv2 as _cv
    if not a_path or not b_path:
        return True
    try:
        a = _cv.imdecode(_np.fromfile(a_path, dtype=_np.uint8), _cv.IMREAD_COLOR)
        b = _cv.imdecode(_np.fromfile(b_path, dtype=_np.uint8), _cv.IMREAD_COLOR)
    except Exception:
        return True
    if a is None or b is None or a.shape != b.shape:
        return True
    d = _np.abs(a.astype(_np.int16) - b.astype(_np.int16)).max(axis=2)
    return bool((d > pixel_thresh).mean() > ratio_thresh)


def changed_coarse(a, b, pixel_thresh=12, ratio_thresh=0.002, min_pixels=24):
    """粗判「有没有变」：占比过阈 或 变化的像素数≥24（小目标也能被抓到）。
       注意这是**粗信号**，要判定「点对了」请用 点到位.py --expect。"""
    if a is None or b is None or getattr(a, "shape", None) != getattr(b, "shape", None):
        return True
    d = np.abs(a.astype(np.int16) - b.astype(np.int16)).max(axis=2)
    mask = (d > pixel_thresh)
    return bool(mask.mean() > ratio_thresh or int(mask.sum()) >= min_pixels)


def capture(path=None):
    os.makedirs(SHOTS, exist_ok=True)
    p = path or os.path.join(SHOTS, time.strftime("%Y%m%d"), time.strftime("%H%M%S") + "-cap.png")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    ImageGrab.grab(all_screens=True).save(p)
    return p

def ahash(path):
    out = subprocess.run([PY, os.path.join(HERE, "脊髓.py"), "hash", path], capture_output=True, creationflags=0x08000000, text=True, encoding="utf-8", errors="replace")
    try: return json.loads(out.stdout).get("hash")
    except Exception: return None

def _j(args):
    out = subprocess.run([PY, os.path.join(HERE, "脊髓.py")] + args, capture_output=True, creationflags=0x08000000, text=True, encoding="utf-8", errors="replace")
    try: return json.loads(out.stdout)
    except Exception: return {"ok": False, "error": (out.stdout or out.stderr)[-300:]}

def locate(shot, tpl, thresh=0.8, mode="color"):
    return _j(["find", shot, tpl, str(thresh), "--mode", mode])

KINDS = {"input_box": "输入框", "overlay": "弹窗", "close_btn": "关闭钮"}

def locate_kind(kind):
    """不靠模板：用几何启发找一类元素，返回 box（找不到 None）。"""
    cap = capture()
    out = subprocess.run([PY, os.path.join(HERE, "启发.py"), cap], capture_output=True, text=True,
                         encoding="utf-8", errors="replace", creationflags=0x08000000)
    try:
        info = json.loads((out.stdout or "").strip().splitlines()[-1])
    except Exception:
        return None
    if kind == "input_box":
        cand = info.get("input_box") or []
        return cand[0] if cand else None
    if kind == "overlay":
        cand = info.get("overlay") or []
        return cand[0][:4] if cand else None
    if kind == "close_btn":
        cand = info.get("close_btn") or []
        return cand[0] if cand else None
    return None

def locate_any(shot, tpl):
    """回退顺序：color → edge → 都没有就返回 None（交给通用腿）。
    消融开关：REFLEX_NO_EDGE=1 关掉 edge 回退；REFLEX_NO_GEO=1 关掉几何启发。"""
    r = locate(shot, tpl, 0.8, "color")
    if r.get("ok"): r["used"] = "color"; return r
    if os.environ.get("REFLEX_NO_EDGE") == "1":
        return None
    r = locate(shot, tpl, 0.85, "edge")
    if r.get("ok"): r["used"] = "edge"; return r
    return None

def locate_text(shot, text):
    """OCR 找文字——定位降级链里最通用的一层。
    2026-11-22 加：模板会过期（界面改版就废），但「按钮上有字」这件事几乎不变。
    这是「能应对没见过的界面」真正靠得住的那一层；代价是慢（整窗 OCR 约半秒）。"""
    try:
        import importlib.util as _il
        from PIL import Image
        _p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "win_ocr.py")
        _sp = _il.spec_from_file_location("ocr_mod", _p)
        oc = _il.module_from_spec(_sp)
        _sp.loader.exec_module(oc)
    except Exception as e:
        return {"ok": False, "why": "win_ocr 加载失败: %s" % str(e)[:80]}
    try:
        # 2026-11-22：手眼.py 里没有 cv2，只有 PIL + numpy；而且 PIL 读中文路径本来就比 cv2 稳。
        from PIL import Image as _Im
        img = _Im.open(shot).convert("RGB")
        lines = oc.ocr_pil(img)
    except Exception as e:
        return {"ok": False, "why": "OCR 失败: %s" % str(e)[:80]}
    tgt = (text or "").replace(" ", "")
    if not tgt:
        return {"ok": False, "why": "没给 text"}
    for l in lines:
        if tgt in (l.get("text") or "").replace(" ", ""):
            return {"ok": True, "box": [l["x"], l["y"], l["w"], l["h"]],
                    "text": (l.get("text") or "")[:40], "lines": len(lines)}
    return {"ok": False, "why": "OCR 没找到 %r（识别到 %d 行）" % (text, len(lines))}


FAIL_DIR = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "训练", "ui-detect", "失败样本"))


def _failure_origin(tpl, kind, text):
    """2026-11-22（3.26 待做）：给失败样本标来源，实验样本不许混进覆盖率统计。
    判据：负片模板 / 人为关层环境变量 / 假目标文字 → test；否则 real。
    （「没给 kind」不再算 test——按模板定位的真实调用本来就没有 kind。）"""
    reasons = []
    base = os.path.basename(tpl or "")
    if base and ("_tpl_neg" in base or base.lower().startswith("neg")):
        reasons.append("neg-template")
    flags = {}
    for k in ("REFLEX_NO_EDGE", "REFLEX_NO_OCR", "REFLEX_NO_GEO"):
        v = os.environ.get(k)
        if v:
            flags[k] = v
    if flags:
        reasons.append("env:" + ",".join(sorted(flags)))
    if text and ("绝对不会存在" in str(text) or "not-exist" in str(text) or "不存在的目标" in str(text)):
        reasons.append("fake-text")
    return ("test" if reasons else "real"), flags, reasons


def _save_failure(shot, tpl, kind, text, trail):
    """2026-11-22：四层全不中时把现场存下来。
    存在的理由：光知道「失败了」没用，要知道「四层分别差在哪」。
    攒够样本才能回答「每一个细节到底缺哪几块」，而不是靠猜。
    只存目标窗口的截图 + 定位参数，不含任何额外信息。"""
    try:
        ts = time.strftime("%Y%m%d-%H%M%S")
        tag = str(abs(hash((tpl or "", kind or "", text or ""))) % 10000)
        d = os.path.join(FAIL_DIR, ts + "-" + tag)
        os.makedirs(d, exist_ok=True)
        if shot and os.path.exists(shot):
            import shutil as _sh
            _sh.copy2(shot, os.path.join(d, "shot.png"))
        origin, flags, reasons = _failure_origin(tpl, kind, text)
        meta = {"at": ts, "tpl": os.path.basename(tpl) if tpl else None,
                "kind": kind, "text": text, "trail": trail,
                # 2026-11-22：来源与当时的开关一起存，事后才分得清「层被跳过」还是「层找不到」
                "origin": origin, "flags": flags, "reasons": reasons}
        with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False, indent=1)
        return d
    except Exception:
        return None


def locate_best(shot, tpl=None, kind=None, text=None):
    """统一优先级：color 模板 → edge 模板 → OCR 文字 → 几何启发 → 失败。
    返回 {"ok":True,"box":[...],"src":"template_color|template_edge|ocr_text|heuristic"}
    全失败时把现场存进 训练/ui-detect/失败样本/ 再返回 None。"""
    trail = []
    if tpl:
        r = locate(shot, tpl, 0.8, "color") or {}
        trail.append({"layer": "color", "ok": bool(r.get("ok")), "score": r.get("score")})
        if r.get("ok"):
            r["src"] = "template_color"; return r
        # 2026-11-21 修：REFLEX_NO_EDGE 原来只接在 locate_any，locate_best 不认——
        # 用它做的「关掉 edge 回退」消融组是假消融。这里补上，两函数口径一致。
        if os.environ.get("REFLEX_NO_EDGE") != "1":
            r = locate(shot, tpl, 0.85, "edge") or {}
            trail.append({"layer": "edge", "ok": bool(r.get("ok")), "score": r.get("score")})
            if r.get("ok"):
                r["src"] = "template_edge"; return r
        else:
            trail.append({"layer": "edge", "skipped": "REFLEX_NO_EDGE=1"})
    # 2026-11-22：模板都不中用 OCR 找字。放在几何之前——几何会给假候选（实测 9/10 落窗外）。
    if text and os.environ.get("REFLEX_NO_OCR") != "1":
        r = locate_text(shot, text) or {}
        trail.append({"layer": "ocr", "ok": bool(r.get("ok")), "why": str(r.get("why"))[:80]})
        if r.get("ok"):
            r["src"] = "ocr_text"; return r
    elif text:
        trail.append({"layer": "ocr", "skipped": "REFLEX_NO_OCR=1"})
    if kind:
        if os.environ.get("REFLEX_NO_GEO") == "1":
            trail.append({"layer": "heuristic", "skipped": "REFLEX_NO_GEO=1"})
        else:
            box = locate_kind(kind)
            trail.append({"layer": "heuristic", "ok": bool(box)})
            if box:
                return {"ok": True, "box": box, "src": "heuristic", "kind": kind, "thresh": None}
    else:
        trail.append({"layer": "heuristic", "skipped": "没给 kind"})
    saved = _save_failure(shot, tpl, kind, text, trail)
    if saved:
        try:
            log(json.dumps({"failure_saved": saved, "trail": trail}, ensure_ascii=False))
        except Exception:
            pass
    return None

def best_near(shot, tpl):
    """多命中时用「分数 + 离上次位置最近」取舍；没有历史点就直接取最高分。"""
    hits = _j(["find-multi", shot, tpl, "0.6", "--topk", "5", "--mode", "color"])
    lst = hits.get("hits") or []
    if not lst: return None
    last = None
    try:
        with open(os.path.join(SHOTS, "_last_point.json"), encoding="utf-8") as fh: last = json.load(fh)
    except Exception: pass
    if len(lst) == 1: return lst[0]
    def dist(h):
        x, y = h["box"][0] + h["box"][2] // 2, h["box"][1] + h["box"][3] // 2
        if not last: return 0
        return ((x - last["x"]) ** 2 + (y - last["y"]) ** 2) ** 0.5
    lst2 = sorted(lst, key=lambda h: (dist(h), -h["score"]))
    return lst2[0]

LEARNED = os.path.join(HERE, "学习到的元素.json")

def learn_element(tpl_path, box, cx, cy):
    """点成功后自动记一条「元素」：模板 + 中心点 + 时间。下次不用再裁模板。"""
    try:
        with open(LEARNED, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {"items": []}
    name = os.path.splitext(os.path.basename(tpl_path))[0]
    tpl_copy = os.path.join(SHOTS, name + ".png")
    try:
        if os.path.abspath(tpl_path) != os.path.abspath(tpl_copy):
            with open(tpl_path, "rb") as a, open(tpl_copy, "wb") as b:
                b.write(a.read())
    except Exception:
        tpl_copy = tpl_path
    data.setdefault("items", [])
    existed = False
    for it in data["items"]:
        if os.path.basename(it.get("template", "")) == os.path.basename(tpl_copy):
            it["used_count"] = int(it.get("used_count", 0)) + 1
            it["last_used"] = time.strftime("%Y-%m-%dT%H:%M:%S+08:00")
            existed = True
    if existed:
        try:
            with open(LEARNED, "w", encoding="utf-8") as f: json.dump(data, f, ensure_ascii=False, indent=1)
        except Exception: pass
        return False
    if not any(i.get("template") == tpl_copy for i in data["items"]):
        data["items"].append({"template": tpl_copy, "box": box, "center": [cx, cy],
                              "learned_at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
                              "used_count": 1, "last_used": time.strftime("%Y-%m-%dT%H:%M:%S+08:00")})
        os.makedirs(os.path.dirname(LEARNED), exist_ok=True)
        with open(LEARNED, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        return True
    return False

def bump_score(tpl_path, ok):
    """脊髓自评：成功 +1、失败 -1；score<=-3 自动停用（不需要我判断）"""
    try:
        with open(LEARNED, encoding="utf-8") as f: data = json.load(f)
    except Exception:
        return None
    name = os.path.basename(tpl_path)
    hit = None
    for it in data.get("items", []):
        if os.path.basename(it.get("template", "")) == name:
            it["attempts"] = int(it.get("attempts", 0)) + 1
            it["success"] = int(it.get("success", 0)) + (1 if ok else 0)
            it["score"] = int(it.get("score", 0)) + (1 if ok else -1)
            it["last_try"] = time.strftime("%Y-%m-%dT%H:%M:%S+08:00")
            if it["score"] <= -3:
                it["disabled"] = True
            hit = it
    if hit is not None:
        try:
            with open(LEARNED, "w", encoding="utf-8") as f: json.dump(data, f, ensure_ascii=False, indent=1)
        except Exception: pass
    return hit

def is_disabled(tpl_path):
    try:
        with open(LEARNED, encoding="utf-8") as f: data = json.load(f)
    except Exception:
        return False
    name = os.path.basename(tpl_path)
    for it in data.get("items", []):
        if os.path.basename(it.get("template", "")) == name and it.get("disabled"):
            return True
    return False

def touch_learned(tpl_path):
    """用过一次就记账：used_count+1、刷新 last_used；顺带把 30 天没用的沉底。"""
    try:
        with open(LEARNED, encoding="utf-8") as f: data = json.load(f)
    except Exception:
        return
    now = time.time()
    name = os.path.basename(tpl_path)
    for it in data.get("items", []):
        if os.path.basename(it.get("template", "")) == name:
            it["used_count"] = int(it.get("used_count", 0)) + 1
            it["last_used"] = time.strftime("%Y-%m-%dT%H:%M:%S+08:00")
    try:
        with open(LEARNED, "w", encoding="utf-8") as f: json.dump(data, f, ensure_ascii=False, indent=1)
    except Exception: pass
    prune_learned()

def prune_learned(stale_days=30, hard_cap=200):
    """长期没命中的沉底（不删，移到 stale），条目超上限也按 last_used 最旧的沉。"""
    try:
        with open(LEARNED, encoding="utf-8") as f: data = json.load(f)
    except Exception:
        return 0
    items = data.get("items", [])
    stale = data.setdefault("stale", [])
    now = time.time(); moved = 0
    keep = []
    for it in items:
        try:
            ts = time.mktime(time.strptime(it.get("last_used", it.get("learned_at"))[:19], "%Y-%m-%dT%H:%M:%S"))
        except Exception:
            ts = now
        if (now - ts) > stale_days * 86400:
            it["stale_why"] = "%d 天没命中" % stale_days; stale.append(it); moved += 1
        else:
            keep.append(it)
    if len(keep) > hard_cap:
        keep.sort(key=lambda i: i.get("last_used", ""), reverse=True)
        stale.extend(keep[hard_cap:]); moved += len(keep) - hard_cap; keep = keep[:hard_cap]
    data["items"] = keep
    if moved:
        try:
            with open(LEARNED, "w", encoding="utf-8") as f: json.dump(data, f, ensure_ascii=False, indent=1)
        except Exception: pass
    return moved

def remember_point(x, y):
    try:
        os.makedirs(SHOTS, exist_ok=True)
        with open(os.path.join(SHOTS, "_last_point.json"), "w", encoding="utf-8") as fh: json.dump({"x": x, "y": y}, fh)
    except Exception: pass

def gate():
    out = subprocess.run(["pwsh", "-NoProfile", "-STA", "-File", os.path.join(HERE, "动作.ps1"), "-Action", "probe"], capture_output=True, creationflags=0x08000000, text=True, encoding="utf-8", errors="replace")
    return "gate ok=True" in (out.stdout or "")

def click_at(x, y):
    out = subprocess.run(["pwsh", "-NoProfile", "-STA", "-File", os.path.join(HERE, "动作.ps1"), "-Action", "click", "-X", str(int(x)), "-Y", str(int(y))], capture_output=True, creationflags=0x08000000, text=True, encoding="utf-8", errors="replace")
    return out.returncode == 0, ((out.stdout or "") + (out.stderr or "")).strip()[-300:]

def click_checked(x, y):
    """2026-11-19：两档模式的真分支。fast = 点了就算；careful = 点后必须验到画面变化，
    没变就返回失败（容器类点击由调用方用 --focus-ok 声明，改用焦点判断）。"""
    before = ahash(capture())
    ok, msg = click_at(x, y)
    if not ok:
        return False, msg + "（点击没执行）"
    if MODE != "careful":
        return True, msg + "（fast：未验变化）"
    time.sleep(0.6)
    after = ahash(capture())
    if before is None or after is None:
        return True, msg + "（careful：拿不到哈希，未验）"
    d = sum(1 for a, b in zip(before, after) if a != b)
    if d >= 2:
        return True, msg + "（careful：已验变化 hamming=%d）" % d
    return False, msg + "（careful：点后画面没变 hamming=%d，判失败）" % d


def accept(step, ok, evidence):
    try: subprocess.run([PY, os.path.join(BASE, "工具", "验收.py"), "step", "手眼", step, "ok" if ok else "fail", evidence], capture_output=True, creationflags=0x08000000, text=True, encoding="utf-8", errors="replace", timeout=20)
    except Exception: pass

def watch(seconds=6.0, interval=0.4, thresh=6):
    """连续变化观测：按 interval 采样，用 aHash 汉明距离判变化；返回事件列表。"""
    import numpy as np
    events = []
    prev = None
    t0 = time.time()
    while time.time() - t0 < seconds:
        cap = capture()
        h = ahash(cap)
        if prev is not None and h is not None:
            d = sum(1 for a, b in zip(prev, h) if a != b)
            if d > thresh:
                events.append({"t": round(time.time() - t0, 2), "hamming": d})
        prev = h
        time.sleep(interval)
    return events

def wait_stable(need=3, interval=0.35, timeout=8.0):
    """等界面静止：连续 need 帧哈希一致才算稳（防止在动画中间点）"""
    import numpy as np
    last = None; same = 0; t0 = time.time()
    while time.time() - t0 < timeout:
        h = ahash(capture())
        if h is not None and h == last: same += 1
        else: same = 0; last = h
        if same >= need: return True
        time.sleep(interval)
    return False

def classify_pair(a, b):
    """调 识别.py 判断「变了什么」；失败返回 None。"""
    out = subprocess.run([PY, os.path.join(HERE, "识别.py"), a, b], capture_output=True, text=True,
                         encoding="utf-8", errors="replace", creationflags=0x08000000)
    try: return json.loads((out.stdout or "").strip().splitlines()[-1])
    except Exception: return None

def watch_events(seconds=12, interval=1.0, motion_thresh=8.0, cut_thresh=0.35):
    """阈值触发式观测：运动量或场景切换过线才留帧+记事件，否则什么都不做（0 token）。"""
    import numpy as np
    from PIL import ImageGrab
    import cv2
    os.makedirs(SHOTS, exist_ok=True)
    events = []; prev = None; t0 = time.time(); k = 0
    while time.time() - t0 < seconds:
        img = ImageGrab.grab(all_screens=True)
        cap_path = os.path.join(SHOTS, "_last_frame.png")
        os.makedirs(SHOTS, exist_ok=True)
        img.save(cap_path)
        frame = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        small = cv2.resize(gray, (160, 90))
        hist = cv2.calcHist([frame], [0, 1, 2], None, [8, 8, 8], [0, 256, 0, 256, 0, 256]); cv2.normalize(hist, hist)
        motion = float(np.abs(small.astype(np.int16) - prev[0].astype(np.int16)).mean()) if prev else 0.0
        cut = float(cv2.compareHist(prev[1], hist, cv2.HISTCMP_BHATTACHARYYA)) if prev else 0.0
        if motion > motion_thresh or cut > cut_thresh:
            p = os.path.join(SHOTS, time.strftime("%Y%m%d"), time.strftime("%H%M%S") + "-ev.png")
            os.makedirs(os.path.dirname(p), exist_ok=True)
            img.save(p)
            cls = classify_pair(prev[2], p) if prev and len(prev) > 2 else None
            events.append({"t": round(time.time() - t0, 2), "motion": round(motion, 2), "cut": round(cut, 3),
                           "frame": p, "label": (cls or {}).get("label"), "conf": (cls or {}).get("conf")})
        prev = (small, hist, cap_path); k += 1
        time.sleep(interval)
    return {"seconds": seconds, "samples": k, "events": len(events), "items": events}

def calibrate(seconds=3.0):
    """自适应基线：先看几秒算运动量的均值/标准差，阈值 = 均值 + 4σ（下限 1.5），免得每个场景手调数字。"""
    import cv2, numpy as np
    from PIL import ImageGrab
    vals = []; prev = None; t0 = time.time()
    while time.time() - t0 < seconds:
        img = ImageGrab.grab(all_screens=True)
        g = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2GRAY)
        small = cv2.resize(g, (160, 90))
        if prev is not None: vals.append(float(np.abs(small.astype(np.int16) - prev.astype(np.int16)).mean()))
        prev = small
        time.sleep(0.4)
    if not vals: return 1.5, 0.0
    mu = float(np.mean(vals)); sd = float(np.std(vals))
    return max(1.5, mu + 4 * sd), round(mu, 3)

POLICY = os.path.join(SHOTS, "_policy.json")

def should_surface(events, cooldown=300, daily_cap=30):
    """信息筛选 v2：先看「变了什么」（labels），再决定要不要打扰。
    - 高优先（弹窗出现/页面切换）：60 秒内最多一张，其余情况立刻放行
    - 中优先（内容滚动/视频动画）：走 300 秒冷却
    - 低优先（无变化/打字/没标签）：直接压掉，不上报
    """
    now = time.time()
    try:
        with open(POLICY, encoding="utf-8") as f: st = json.load(f)
    except Exception:
        st = {}
    today = time.strftime("%Y-%m-%d")
    if st.get("day") != today: st = {"day": today, "count": 0, "last": 0, "last_high": 0, "made": 0, "surfaced": 0}
    n = len(events)
    st["made"] = st.get("made", 0) + 1     # 每次评估都算「产生了一次候选」
    labels = [e.get("label") for e in events]
    high = [l for l in labels if l in ("弹窗出现", "页面切换")]
    mid = [l for l in labels if l in ("内容滚动", "视频/动画")]
    big_motion = max([e.get("motion", 0) for e in events] or [0])
    if st.get("count", 0) >= daily_cap:
        st["made"] = st.get("made", 0) + 1; _save_policy(st)
        return False, "今日已达上限 %d 张（labels=%s）" % (daily_cap, ",".join(str(x) for x in labels[:4]))
    if high:
        if now - st.get("last_high", 0) < 60:
            return False, "高优先但 60 秒内刚报过（%s）" % ",".join(high[:2])
        st.update(count=st.get("count", 0) + 1, last=now, last_high=now, made=st.get("made", 0) + 1, surfaced=st.get("surfaced", 0) + 1)
        _save_policy(st); return True, "高优先：%s" % ",".join(high[:2])
    if big_motion >= 25:
        st.update(count=st.get("count", 0) + 1, last=now, made=st.get("made", 0) + 1, surfaced=st.get("surfaced", 0) + 1)
        _save_policy(st); return True, "剧烈变化：运动 %.1f（>=25）" % big_motion
    if now - st.get("last", 0) < cooldown:
        return False, "冷却期内（%ds）labels=%s" % (int(now - st.get("last", 0)), ",".join(str(x) for x in labels[:3]))
    if mid:
        st.update(count=st.get("count", 0) + 1, last=now)
        _save_policy(st); return True, "中优先：%s" % ",".join(mid[:2])
    return False, "低优先，不上报（labels=%s）" % ",".join(str(x) for x in labels[:3])

def _save_policy(st):
    try:
        os.makedirs(os.path.dirname(POLICY), exist_ok=True)
        with open(POLICY, "w", encoding="utf-8") as f: json.dump(st, f, ensure_ascii=False)
    except Exception: pass

def watch_sheet(seconds=20, motion_thresh=8.0, max_frames=9, cols=3):
    """阈值观测 + 自动拼接触表：有事件才出图，一张图交给我。"""
    import cv2, numpy as np, importlib.util
    ev = watch_events(seconds, 1.0, motion_thresh)
    items = ev["items"][:max_frames]
    if len(items) < 2:
        return {"seconds": seconds, "events": len(items), "sheet": None, "reason": "事件不足 2 个，不拼表（等同什么都没发生）"}
    ok, why = should_surface(ev["items"])
    if not ok:
        return {"seconds": seconds, "events": len(items), "sheet": None, "reason": "筛选掉：" + why}
    spec = importlib.util.spec_from_file_location("shaixuan", os.path.join(HERE, "筛选.py"))
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    frames = []
    for it in items:
        img = cv2.imdecode(np.fromfile(it["frame"], dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is not None: frames.append({"t": it["t"], "bgr": img})
    out = os.path.join(SHOTS, time.strftime("%Y%m%d"), time.strftime("%H%M%S") + "-事件接触表.jpg")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    mod.imwrite_u(out, mod.sheet(frames, cols))
    return {"seconds": seconds, "events": len(items), "sheet": out, "times": [x["t"] for x in frames]}

def record_watch(seconds, events, thresh, sheet, ms):
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"), "kind": "vision-watch",
           "name": "%.0fs@%.2f" % (seconds, thresh), "ok": True, "ms": int(ms),
           "extra": {"events": events, "sheet": bool(sheet)}}
    try:
        p = os.path.join(BASE, "训练", "ui-detect", "实验数据.jsonl")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a", encoding="utf-8") as f: f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception: pass

def _fg_title():
    """当前前台窗口标题（2026-11-22 #48d0ac：find 的跨界面判据）"""
    try:
        import ctypes
        u = ctypes.windll.user32
        h = u.GetForegroundWindow()
        n = u.GetWindowTextLengthW(h)
        buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(h, buf, n + 1)
        return buf.value.strip()
    except Exception:
        return ""


# 2026-11-22（#48d0ac）：短名 tag 与窗口标题的别名表——DSH 是「DeepSeek Harness」的简称，
# 不认这层会把同一个界面误判成"跨界面"。
TAG_ALIAS = {
    "dsh": ["deepseek harness", "deepseek"],
    "b站": ["bilibili", "哔哩哔哩"],
    "剪映": ["capcut", "剪映专业版"],
}


def _tag_matches(tag, title):
    """元素所属界面 tag 与当前前台标题是否对得上（互相包含 / 别名 / 去 auto_ 前缀后包含）"""
    if not tag:
        return True
    t = str(tag).lower().strip()
    w = (title or "").lower()
    if not w:
        return False
    if t in w or w in t:
        return True
    for alias in TAG_ALIAS.get(t, []):
        if alias in w:
            return True
    core = t[5:] if t.startswith("auto_") else t
    core = core.split("(")[0].strip()
    if not core:
        return False
    if core in w or w in core:
        return True
    for alias in TAG_ALIAS.get(core, []):
        if alias in w:
            return True
    return False


def find_by_name(nm):
    """按名字找一个 active 元素（名字精确或包含），返回 (模板路径, 元素记录)。2026-11-19 加；
    2026-11-22 改成连元素一起返回——上层要看它属于哪个界面。"""
    try:
        items = json.load(open(LEARNED, encoding="utf-8")).get("items", [])
    except Exception:
        return None, None
    cands = [e for e in items if not e.get("disabled")
             and ((e.get("name") and (e["name"] == nm or nm in e["name"]))
                  or (e.get("ui_text") and (e["ui_text"] == nm or nm in e["ui_text"])))]
    cands.sort(key=lambda e: 0 if e.get("name") == nm or e.get("ui_text") == nm else 1)
    for e in cands:
        tpl = e.get("template") or ""
        if tpl and os.path.exists(tpl):
            return tpl, e
    return None, None


def main():
    if len(sys.argv) < 2: print(__doc__ or "见文件头"); return 2
    cmd = sys.argv[1]
    if cmd == "watch-sheet":
        secs = float(sys.argv[2]) if len(sys.argv) > 2 else 20
        th = float(sys.argv[sys.argv.index("--motion") + 1]) if "--motion" in sys.argv else 8.0
        t0 = time.time()
        if "--auto" in sys.argv:
            th, base = calibrate()
            log("自适应阈值：%.2f（基线均值 %.3f）" % (th, base))
        r = watch_sheet(secs, th)
        record_watch(secs, r.get("events", 0), th, r.get("sheet"), (time.time() - t0) * 1000)
        log(json.dumps(r, ensure_ascii=False))
        accept("观测接触表", bool(r.get("sheet")), ("出表 " + r["sheet"]) if r.get("sheet") else r.get("reason", ""))
        return 0
    if cmd == "watch-events":
        secs = float(sys.argv[2]) if len(sys.argv) > 2 else 12
        th = float(sys.argv[sys.argv.index("--motion") + 1]) if "--motion" in sys.argv else 8.0
        r = watch_events(secs, 1.0, th)
        log(json.dumps({k: v for k, v in r.items() if k != "items"}, ensure_ascii=False))
        for it in r["items"]: log("  ev " + json.dumps(it, ensure_ascii=False))
        accept("阈值观测", True, "%.0fs 内触发 %d 次" % (secs, r["events"]))
        return 0
    if cmd == "watch":
        secs = float(sys.argv[2]) if len(sys.argv) > 2 else 6.0
        ev = watch(secs)
        log(json.dumps({"seconds": secs, "changes": len(ev), "events": ev}, ensure_ascii=False))
        accept("连续观测", True, "%.1fs 内变化 %d 次" % (secs, len(ev)))
        return 0
    if cmd == "stable":
        ok = wait_stable()
        log(json.dumps({"stable": ok}, ensure_ascii=False))
        accept("等静止", ok, "连续 3 帧哈希一致" if ok else "8 秒内没稳定")
        return 0 if ok else 1
    if cmd == "selftest":
        cap = capture()
        img = np.array(ImageGrab.grab(all_screens=True))
        h, w = img.shape[:2]
        th, tw = 80, 200
        best = None
        for yy in range(0, h - th, 100):
            for xx in range(0, w - tw, 160):
                v = float(img[yy:yy+th, xx:xx+tw].std())
                if best is None or v > best[0]: best = (v, xx, yy)
        _, x, y = best
        tpl = os.path.join(SHOTS, "_selftest_tpl.png")
        os.makedirs(SHOTS, exist_ok=True)
        ImageGrab.grab(all_screens=True).crop((x, y, x + tw, y + th)).save(tpl)
        r = locate(cap, tpl, 0.9)
        ok = r.get("ok") and abs(r["box"][0] - x) <= 3 and abs(r["box"][1] - y) <= 3
        log(json.dumps({"selftest_ok": bool(ok), "expect": [x, y, tw, th], "found": r}, ensure_ascii=False))
        accept("自检-定位", bool(ok), "expect=%s found=%s" % ((x, y), r.get("box")))
        return 0 if ok else 1
    if cmd == "see-kind":
        kind = sys.argv[2] if len(sys.argv) > 2 else "input_box"
        box = locate_kind(kind)
        r = {"ok": bool(box), "kind": kind, "box": box}
        log(json.dumps(r, ensure_ascii=False)); accept("几何定位", bool(box), kind + " -> " + str(box)); return 0 if box else 1
    if cmd == "click-kind":
        kind = sys.argv[2] if len(sys.argv) > 2 else "input_box"
        box = locate_kind(kind)
        if not box:
            log(json.dumps({"ok": False, "kind": kind, "why": "几何启发没找到"}, ensure_ascii=False)); return 1
        bx, by, bw, bh = box
        cx, cy = bx + bw // 2, by + bh // 2
        if not gate(): log("闸门不过，不动手"); return 3
        ok, msg = click_at(cx, cy); log("几何点 %s (%d,%d) %s" % (kind, cx, cy, msg))
        time.sleep(1.0)
        cap = capture(); tpl = os.path.join(SHOTS, "auto_%s_%s.png" % (kind, time.strftime("%H%M%S")))
        try:
            from PIL import Image
            ImageGrab.grab(all_screens=True).crop((bx, by, bx + bw, by + bh)).save(tpl)
            learn_element(tpl, [bx, by, bw, bh], cx, cy)
        except Exception as e:
            log("存模板失败：" + str(e))
        accept("几何点击", bool(ok), "%s (%d,%d)" % (kind, cx, cy))
        return 0 if ok else 1
    if cmd == "locate-best":
        tpl = None; kind = None
        if "--tpl" in sys.argv: tpl = sys.argv[sys.argv.index("--tpl") + 1]
        if "--kind" in sys.argv: kind = sys.argv[sys.argv.index("--kind") + 1]
        if "--text" in sys.argv: text = sys.argv[sys.argv.index("--text") + 1]
        else: text = None
        r = locate_best(capture(), tpl, kind, text)
        log(json.dumps(r or {"ok": False}, ensure_ascii=False))
        accept("统一定位", bool(r), str((r or {}).get("src")))
        return 0 if r else 1
    if cmd == "find":
        nm = sys.argv[2] if len(sys.argv) > 2 else ""
        tpl, elem = find_by_name(nm)
        if not tpl:
            log(json.dumps({"ok": False, "name": nm, "why": "没有同名元素"}, ensure_ascii=False)); return 1
        # 2026-11-22（#48d0ac）：跨界面模板不许静默比对——先看这个元素属于哪个界面
        _tag = str((elem or {}).get("tag") or "")
        # 2026-11-22（剪辑线缺口 2）：可以显式指定「应该在哪个窗口上找」——
        # 从 DSH 里调时前台常是 DSH，拿它跟模板界面比就会给出假阳性
        _want = ""
        if "--window" in sys.argv:
            try:
                _want = sys.argv[sys.argv.index("--window") + 1]
            except Exception:
                _want = ""
        _title = _want or _fg_title()
        if _tag and not _tag_matches(_tag, _title):
            log(json.dumps({"ok": False, "name": nm, "tpl": tpl, "tag": _tag, "window": _title,
                            "why": "元素属于界面「%s」，当前前台是「%s」——跨界面模板不比，已拒绝。先切到那个界面再 find，或对新界面跑 spine learn 重新学。" % (_tag, _title)},
                           ensure_ascii=False)); return 1
        cap = capture(); r = locate(cap, tpl)
        r["name"] = nm; r["tpl"] = tpl; r["tag"] = _tag; r["window"] = _title
        # 模板是从哪个界面学的（目录名），跨窗口/跨子界面时一眼看得出，别静默
        try:
            r["learned_from"] = os.path.basename(os.path.dirname(tpl))
        except Exception:
            pass
        # 2026-11-19：命中框大到容器的程度时加警告（大框多半是容器，点了不一定是那按钮）
        b = r.get("box")
        if b:
            import ctypes
            u = ctypes.windll.user32
            sw, sh = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
            if (b[2] * b[3] > (sw * sh) * 0.03) or b[2] > 400 or b[3] > 200:
                r["warn"] = "框太大（%dx%d）可能是容器，点前先看框" % (b[2], b[3])
        if not r.get("ok"):
            # 2026-11-19（测试员甲）：分数不够时 box 会漂 100px+，不能留给上层当坐标
            r["unverified"] = True
            r["best_score"] = r.get("score")
            r["box"] = None
        log(json.dumps(r, ensure_ascii=False)); return 0 if r.get("ok") else 1
    if cmd == "see":
        cap = capture(); r = locate(cap, sys.argv[2])
        log(json.dumps(r, ensure_ascii=False)); accept("定位", bool(r.get("ok")), json.dumps(r.get("box"), ensure_ascii=False)); return 0 if r.get("ok") else 1
    if cmd == "click":
        dry = "--dry" in sys.argv
        tpl = sys.argv[2]
        before = capture()
        r = locate_any(before, tpl)
        if not r:
            bump_score(tpl, False)
            log(json.dumps({"ok": False, "error": "color/edge 都没匹配上"}, ensure_ascii=False))
            accept("点击-定位", False, "color+edge 都失败，需要通用腿")
            return 4
        hit = best_near(before, tpl)
        if hit and len(hit) > 0: r["box"] = hit["box"]; r["pick"] = {"score": hit["score"], "box": hit["box"]}
        log(json.dumps(r, ensure_ascii=False))
        bx, by, bw, bh = r["box"]
        cx, cy = bx + bw // 2, by + bh // 2
        if dry: log("dry：应点在 (%d,%d)" % (cx, cy)); accept("点击-dry", True, "坐标 (%d,%d)" % (cx, cy)); return 0
        if not gate(): log("闸门不过，不动手"); accept("点击-闸门", False, "网关拒绝"); return 3
        ok, msg = click_checked(cx, cy); log(msg)
        if ok:
            bump_score(tpl, True)
            remember_point(cx, cy)
            touch_learned(tpl)
            if learn_element(tpl, r["box"], cx, cy):
                log("已记为新元素：" + os.path.basename(tpl))
        time.sleep(1.2)
        after = capture()
        # 2026-11-20 红队 F6：整屏 aHash 会被桌宠/别的窗口污染，改按变化像素占比判（>0.2% 才算变）
        changed = changed_coarse_paths(before, after)
        log("屏幕变化: " + str(changed))
        accept("点击-闭环", bool(ok and changed), "click=%s changed=%s" % (ok, changed))
        return 0 if (ok and changed) else 1
    print("未知命令"); return 2

if __name__ == "__main__": sys.exit(main())