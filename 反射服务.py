# 反射服务.py —— 本机常驻，执行器只读 界面.json（world），不再每套界面写死坐标。
# 用法：pythonw 反射服务.py   默认 127.0.0.1:8811
# 路由：GET /status  GET /interfaces  POST /run {"interface":"钉钉","action":"发文件","params":{"path":"..."},"dry":true}
import json, os, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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

BASE = os.environ.get("SPINE_BASE") or os.path.dirname(os.path.abspath(__file__))
HERE = os.path.join(BASE, "工具", "界面反射")
TABLE = os.path.join(HERE, "界面.json")
LOG = os.path.join(HERE, "反射日志.txt")
DATA = os.path.join(BASE, "训练", "ui-detect", "实验数据.jsonl")
# 子进程是 python 时，强制它用 UTF-8 输出（否则管道下默认 GBK，中文会乱码）
CHILD_ENV = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
PORT = int(os.environ.get("REFLEX_PORT", "8811"))
PWSH = r"C:\Program Files\PowerShell\7\pwsh.exe"

def log(m):
    line = time.strftime("%m-%d %H:%M:%S") + "  " + m
    try:
        with open(LOG, "a", encoding="utf-8") as f: f.write(line + "\n")
    except Exception: pass
    print(line, flush=True)

def record(kind, name, ok, ms, extra=None):
    """实验数据：每跑一次记一行 JSONL（论文用）。字段固定，别乱加。"""
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"), "kind": kind, "name": name,
           "ok": bool(ok), "ms": int(ms), "extra": extra or {}}
    try:
        os.makedirs(os.path.dirname(DATA), exist_ok=True)
        with open(DATA, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass

def table():
    with open(TABLE, encoding="utf-8") as f: return json.load(f)

def run(iface, action, params, dry):
    t = table()["interfaces"].get(iface)
    if not t: return {"ok": False, "error": "没有这个界面：" + iface}
    script = os.path.join(HERE, t.get("script", ""))
    if not os.path.exists(script): return {"ok": False, "error": "脚本还没写：" + script}
    if iface == "钉钉" and not (params.get("path")):
        return {"ok": False, "error": "钉钉需要 params.path（要发的文件），否则脚本参数绑定会失败"}
    cmd = [PWSH, "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-File", script]
    if params.get("path"): cmd += ["-Path", params["path"]]
    if dry: cmd += ["-DryRun"]
    log("RUN " + iface + "/" + action + (" [dry]" if dry else "") + " " + json.dumps(params, ensure_ascii=False))
    t0 = time.time()
    p = subprocess.run(cmd, capture_output=True, creationflags=0x08000000, text=True, encoding="utf-8", errors="replace", timeout=180)
    record("run", iface + "/" + action, p.returncode == 0, (time.time() - t0) * 1000, {"dry": dry})
    out = ((p.stdout or "") + (p.stderr or "")).strip()
    ok = (p.returncode == 0)
    log(("OK  " if ok else "FAIL ") + iface + "/" + action + " rc=" + str(p.returncode))
    return {"ok": ok, "rc": p.returncode, "out": out[-4000:]}

PANEL_HTML = '''<!doctype html><meta charset=utf-8><title>反射台</title>
<style>body{font-family:system-ui;background:#0f1420;color:#e6eefc;margin:24px}
button{background:#1d2a44;color:#e6eefc;border:1px solid #35507d;border-radius:8px;padding:8px 14px;margin:4px;cursor:pointer}
button:hover{background:#274067}pre{background:#0b1018;padding:12px;border-radius:8px;overflow:auto;max-height:60vh}</style>
<h2>反射台（本机 8811）</h2>
<div id=btns></div><h3>输出</h3><pre id=out>点上面按钮</pre>
<script>
async function j(u,b){const r=await fetch(u,b?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)}:{});return await r.text()}
async function load(){const s=JSON.parse(await j('/status'));const d=document.getElementById('btns');
 d.innerHTML=Object.keys(s.interfaces).map(k=>`<button onclick=\"run('${k}')\">${k} · dry</button>`).join('');
 document.getElementById('out').textContent=JSON.stringify(s,null,2)}
async function run(k){document.getElementById('out').textContent='跑 '+k+' ...';
 const t=await j('/run',{interface:k,action:'run',params:{},dry:true});document.getElementById('out').textContent=t;load()}
load()
</script>'''

_SY2 = None
def _sy2():
    """懒加载 脊髓2（常驻进程只加载一次，避免每次重开 Python 再加载模型）"""
    global _SY2
    if _SY2 is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("sy2", os.path.join(HERE, "脊髓2.py"))
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        _SY2 = m
    return _SY2
TOKEN_FILE = os.path.join(HERE, "服务token.txt")
def _token():
    try:
        return open(TOKEN_FILE, encoding="utf-8").read().strip()
    except Exception:
        return ""
def _need_token(self):
    """写接口要 token（读接口不需要），2026-11-19"""
    want = _token()
    if not want:
        return True                      # 没建 token 文件时保持旧行为，避免把人锁死
    got = self.headers.get("X-Spine-Token") or ""
    return got == want

class H(BaseHTTPRequestHandler):
    def _send(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)
    def do_GET(self):
        if self.path.startswith("/panel"):
            b = PANEL_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
            return
        if self.path.startswith("/status"):
            try:
                t = table()
                ifs = {k: {"kind": v.get("kind"), "script": v.get("script"), "status": v.get("status", "待写")} for k, v in t["interfaces"].items()}
                self._send({"ok": True, "port": PORT, "count": len(ifs), "interfaces": ifs})
            except Exception as e:
                self._send({"ok": False, "error": str(e)}, 500)
        elif self.path.startswith("/interfaces"):
            try: self._send(table())
            except Exception as e: self._send({"ok": False, "error": str(e)}, 500)
        else:
            self._send({"ok": False, "error": "未知路由，试 /status 或 /interfaces"}, 404)

    def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            if self.path.startswith("/perceive"):
                try:
                    sy2 = _sy2()
                    r = sy2.perceive(float(body.get("seconds", 3)), float(body.get("every", 0.25)), body.get("kind"))
                    self._send(r)
                except Exception as e:
                    self._send({"ok": False, "error": str(e)})
                return
            if self.path.startswith("/icons"):
                try:
                    sy2 = _sy2()
                    t0 = time.time()
                    boxes = sy2.from_icons(conf=float(body.get("conf", 0.25)), want=int(body.get("want", 40)))
                    self._send({"ok": True, "count": len(boxes), "icons": boxes, "ms": int((time.time()-t0)*1000)})
                except Exception as e:
                    self._send({"ok": False, "error": str(e)})
                return
            if self.path.startswith("/locate2"):
                try:
                    kind = body.get("kind"); tpl = body.get("template"); win = body.get("window")
                    sy2 = _sy2()
                    t0 = time.time()
                    r = sy2.locate(kind=kind, tpl=tpl, window=win, models=body.get("models"))
                    r["ms"] = int((time.time() - t0) * 1000)
                    self._send(r)
                except Exception as e:
                    self._send({"ok": False, "error": str(e)})
                return
            if self.path.startswith("/locate"):
                shot = body.get("shot"); tpl = body.get("template")
                if not shot or not tpl:
                    self._send({"ok": False, "error": "要 shot 和 template 两个路径"})
                    return
                try:
                    cmd = [sys.executable, os.path.join(HERE, "脊髓.py"), "find", shot, tpl, str(body.get("thresh", 0.8))]
                    p = subprocess.run(cmd, capture_output=True, creationflags=0x08000000, text=True, encoding="utf-8", errors="replace", timeout=60)
                    # 2026-11-20 红队 F8：脚本 rc=0 只说明"跑完了"，不代表"命中了"。把内层 ok 透出来。
                    _out = (p.stdout or "").strip()
                    _inner = None
                    for _line in _out.splitlines():
                        _line = _line.strip()
                        if _line.startswith("{"):
                            try:
                                _inner = json.loads(_line)
                            except Exception:
                                _inner = None
                    _ok = (p.returncode == 0) if _inner is None else (bool(_inner.get("ok")) and p.returncode == 0)
                    self._send({"ok": _ok, "inner_ok": (None if _inner is None else _inner.get("ok")),
                                "run_ok": p.returncode == 0, "out": _out, "err": (p.stderr or "").strip()[-500:]})
                except Exception as e:
                    self._send({"ok": False, "error": str(e)})
                return
            if self.path.startswith("/input") and not _need_token(self):
                self._send({"ok": False, "error": "缺 X-Spine-Token 头（写接口要 token）"}, 401); return
            if self.path.startswith("/input"):
                act = [PWSH, "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-File", os.path.join(HERE, "动作.ps1"), "-Action", str(body.get("action", "probe"))]
                for k, flag in (("x", "-X"), ("y", "-Y"), ("count", "-Count"), ("amount", "-Amount")):
                    if body.get(k) is not None: act += [flag, str(body[k])]
                for k, flag in (("button", "-Button"), ("keys", "-Keys"), ("text", "-Text"), ("direction", "-Direction")):
                    if body.get(k): act += [flag, str(body[k])]
                log("INPUT " + json.dumps(body, ensure_ascii=False))
                t0 = time.time()
                try:
                    p = subprocess.run(act, capture_output=True, creationflags=0x08000000, text=True, encoding="utf-8", errors="replace", timeout=60)
                    _el = body.get("element")
                    if _el:
                        try:
                            import importlib.util as _il4
                            _spe = _il4.spec_from_file_location("elem_fb3", os.path.join(HERE, "元素反馈.py"))
                            _fbe = _il4.module_from_spec(_spe); _spe.loader.exec_module(_fbe)
                            _fbe.record(_el, p.returncode == 0, "反射服务/input")
                        except Exception:
                            pass
                    record("input", str(body.get("action", "probe")), p.returncode == 0, (time.time() - t0) * 1000,
                           {"why": ((p.stdout or "") + (p.stderr or "")).strip()[-120:] if p.returncode else ""})
                    self._send({"ok": p.returncode == 0, "rc": p.returncode, "out": ((p.stdout or "") + (p.stderr or "")).strip()[-2000:]})
                except subprocess.TimeoutExpired:
                    self._send({"ok": False, "error": "动作超时"})
                return
            try:
                # 2026-11-19：把两档模式传下去（careful 只对会出事的流程有意义）
                mode = str(body.get("mode") or "").lower()
                if mode in ("fast", "careful"):
                    os.environ["SHRE_MODE"] = mode
                # 2026-11-19 真分支：careful 先跑一次 dry（检查），dry 通过才真做
                if os.environ.get("SHRE_MODE") == "careful" and not body.get("dry"):
                    pre = run(body.get("interface", ""), body.get("action", ""), body.get("params") or {}, True)
                    if not pre.get("ok"):
                        self._send({"ok": False, "why": "careful：dry 检查没过，没真做", "dry": pre}); return
                    r = run(body.get("interface", ""), body.get("action", ""), body.get("params") or {}, False)
                    r["careful_dry_ok"] = True
                else:
                    r = run(body.get("interface", ""), body.get("action", ""), body.get("params") or {}, bool(body.get("dry")))
                if r is not None: r["mode"] = os.environ.get("SHRE_MODE", "fast")
            except subprocess.TimeoutExpired:
                r = {"ok": False, "error": "脚本超过 180 秒没结束"}
            except Exception as e:
                r = {"ok": False, "error": str(e)}
            self._send(r)
    def log_message(self, *a): pass

def warmup():
    """预热：启动就把模型加载 + 跑一张空图，避免第一次 /locate2 等 12 秒"""
    try:
        t0 = time.time()
        sy = _sy2()
        import numpy as np
        sy._m = None
        sy.from_det("input_box", None, img_arr=np.zeros((720, 1280, 3), dtype="uint8"))
        log("预热完成 %.1fs" % (time.time() - t0))
    except Exception as e:
        log("预热失败：" + str(e))

if __name__ == "__main__":
    import threading
    threading.Thread(target=warmup, daemon=True).start()
    log("反射服务启动 :" + str(PORT))
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()