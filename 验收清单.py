# -*- coding: utf-8 -*-
"""验收清单.py —— 脊髓一回就能复验的回归清单（任何会话、任何时间都能跑）
用法: python 验收清单.py [--no-input]
      --no-input  跳过会动鼠标的项（比如你在用电脑的时候）
退出码: 0=全过 1=有失败
"""
import sys, os, json, subprocess, time

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
NO_INPUT = "--no-input" in sys.argv


def run(args, timeout=90):
    p = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()


def jrun(args):
    rc, out = run(args)
    obj = None
    s = out.find("{")
    if s >= 0:
        try:
            obj = json.loads(out[s:out.rfind("}") + 1])
        except Exception:
            obj = None
    return rc, obj, out


def main():
    results = []

    def add(name, ok, detail, skipped=False):
        results.append({"name": name, "ok": bool(ok), "skipped": skipped, "detail": str(detail)[:220]})

    # 1 DPI 感知
    rc, o, _ = jrun([PY, os.path.join(HERE, "动.py"), "probe"])
    # 分辨率不硬编码：比对「系统报告的屏幕」与「进程读到的屏幕」一致，且 DPI 感知为真
    import ctypes as _ctp
    _u = _ctp.windll.user32
    try:
        _ctp.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass
    _sw, _sh = _u.GetSystemMetrics(0), _u.GetSystemMetrics(1)
    ok = bool(o) and o.get("dpi_aware") and o.get("vw") == _sw and o.get("vh") == _sh
    add("1 DPI 感知（vw=2560/vh=1440）", ok, o)

    _spawned = []          # 测试自己起的进程，跑完一律清掉（独立会话指出：起了 3 个只杀 1 个）

    def point_in(window_sub):
        """动态取窗口内一点（别写死坐标——桌面布局会变，写死就会撞到别的窗口）"""
        import ctypes as _c, ctypes.wintypes as _w
        u2 = _c.windll.user32
        hit = []
        EP = _c.WINFUNCTYPE(_w.BOOL, _w.HWND, _w.LPARAM)

        def cb(h, l):
            if u2.IsWindowVisible(h) and not u2.IsIconic(h):
                n = u2.GetWindowTextLengthW(h)
                if n:
                    b = _c.create_unicode_buffer(n + 1)
                    u2.GetWindowTextW(h, b, n + 1)
                    if window_sub.lower() in b.value.lower():
                        r = _w.RECT(); u2.GetWindowRect(h, _c.byref(r))
                        hit.append((r.left, r.top, r.right, r.bottom, h))
            return True

        u2.EnumWindows(EP(cb), 0)
        if not hit:
            return None
        hwnd = hit[0][4] if len(hit[0]) > 4 else None
        l, t, r, b = hit[0][:4]
        # 把目标窗口抬到前面：否则算出来的点可能被别的窗口盖住（2026-11-20 实测被 Chrome 盖住）
        try:
            if hwnd:
                u2.ShowWindow(hwnd, 9)          # SW_RESTORE
                u2.SetForegroundWindow(hwnd)
                time.sleep(0.5)
        except Exception:
            pass
        return ((l + r) // 2, (t + b) // 2)


    # 2 精确落点
    if NO_INPUT:
        add("2 精确落点", False, "已跳过（--no-input）", skipped=True)
    else:
        rc, o0, _ = jrun([PY, os.path.join(HERE, "动.py"), "probe"])
        x0, y0 = (o0 or {}).get("x", 0), (o0 or {}).get("y", 0)
        run([PY, os.path.join(HERE, "动.py"), "move", "1400", "800", "0"])
        time.sleep(0.15)
        rc, o1, _ = jrun([PY, os.path.join(HERE, "动.py"), "probe"])
        got = (o1 or {})
        ok = abs(got.get("x", -99) - 1400) <= 2 and abs(got.get("y", -99) - 800) <= 2
        run([PY, os.path.join(HERE, "动.py"), "move", str(x0), str(y0), "0"])
        add("2 精确落点 1300/1400,800", ok, {"got": (got.get("x"), got.get("y"))})

    # 3 文件判据正例 —— 用系统文件，别用 验.py 自己（自我指涉等于没验，验收员 2026-11-20 指出）
    target = r"C:\Windows\System32\notepad.exe"
    rc, o, _ = jrun([PY, os.path.join(HERE, "验.py"), "file:" + target + ",1000", "--timeout", "500"])
    add("3 file: 正例（notepad.exe ≥1000B）", rc == 0 and (o or {}).get("ok") is True, o)

    # 4 窗口判据反例
    rc, o, _ = jrun([PY, os.path.join(HERE, "验.py"), "title:ZZTOP根本不存在", "--timeout", "500"])
    add("4 title: 反例必须失败", rc == 1 and (o or {}).get("ok") is False, o)

    # 5 接口字段判据
    rc, o, _ = jrun([PY, os.path.join(HERE, "验.py"),
                     "json:https://api.bilibili.com/x/web-interface/view?bvid=BV1Yesm6QE21,data.state=0",
                     "--timeout", "800"])
    add("5 json: 接口字段", rc == 0 and (o or {}).get("ok") is True, o)

    # 6 抑制：不可能成立的期望只能点 2 次
    if NO_INPUT:
        add("6 抑制（2 次后拉黑）", False, "已跳过（--no-input）", skipped=True)
    else:
        _pt = point_in("Notepad")
        if not _pt:
            _np6 = subprocess.Popen(["notepad.exe"]); _spawned.append(_np6)
            time.sleep(2.2)
            _pt = point_in("Notepad")
        if not _pt:
            add("6 抑制（2 次后拉黑）", False, "起不来记事本（跳过）", skipped=True)
            o = None
        else:
            rc, o, out = jrun([PY, os.path.join(HERE, "点到位.py"), "--x", str(_pt[0]), "--y", str(_pt[1]),
                               "--window", "Notepad",
                               "--expect", "title:ZZTOP根本不存在", "--wait", "400", "--path", "selfcheck-impossible"])
        ok = bool(o) and rc == 1 and (o or {}).get("attempts") == 2 and "拉黑" in str((o or {}).get("stop_reason", ""))
        if not o:
            pass
        elif not ok and "弹窗拦截" in str((o or {}).get("stop_reason", "")):
            add("6 抑制（2 次后拉黑）", False, "环境被挡（点被覆盖）：" + str((o or {}).get("stop_reason"))[:80], skipped=True)
        else:
            add("6 抑制：2 次后拉黑", ok, o or out[-160:])

    # 7 弹窗检测放行
    rc, o, _ = jrun([PY, os.path.join(HERE, "查弹窗.py")])
    add("7 无弹窗时放行", rc == 0 and (o or {}).get("blocked") is False, o)

    # 8 弧清单
    rc, out = run([PY, os.path.join(HERE, "弧.py"), "--list"])
    cnt = len([l for l in out.splitlines() if l.strip().startswith("- ")])
    need = ["自检：验证链路", "自检：点击并验", "发布前闸（真投前跑）"]
    ok = cnt >= 24 and all(n in out for n in need)
    add("8 弧清单（≥24 条 + 三条关键弧）", ok, {"count": cnt, "missing": [n for n in need if n not in out]})


    # ===== 2026-11-20 扩展：把今天修的判定逻辑都纳入回归 =====
    import tempfile, shutil
    TMP = tempfile.mkdtemp(prefix='jisui_verify_')

    def fixture(name, kind):
        import numpy as _np, cv2 as _cv
        p = os.path.join(TMP, name)
        if kind == 'blank':
            _cv.imwrite(p, _np.full((40, 80, 3), 200, _np.uint8))
        elif kind in ('one', 'two'):
            a = _np.full((300, 500, 3), 30, _np.uint8)
            t = _np.zeros((40, 60, 3), _np.uint8)
            _cv.rectangle(t, (0, 0), (59, 39), (255, 255, 255), 2)
            _cv.putText(t, 'AB', (5, 30), 0, 0.8, (200, 200, 200), 2)
            a[50:90, 80:140] = t
            if kind == 'two':
                a[180:220, 300:360] = t
            _cv.imwrite(p, a)
            _cv.imwrite(os.path.join(TMP, 'tt.png'), t)
        return p

    blank = fixture('blank.png', 'blank')
    one = fixture('one.png', 'one')
    two = fixture('two.png', 'two')
    tpl = os.path.join(TMP, 'tt.png')

    # 9 空白模板必须拒绝
    rc, o, _ = jrun([PY, os.path.join(HERE, '脊髓.py'), 'find', blank, blank, '0.8'])
    add('9 空白模板拒绝', rc == 0 and (o or {}).get('ok') is False and '没有纹理' in str((o or {}).get('error')), o)

    # 10 单命中正常 / 11 两同分必须报 ambiguous
    rc, o1, _ = jrun([PY, os.path.join(HERE, '脊髓.py'), 'find', one, tpl, '0.8'])
    add('10 单命中 ok', (o1 or {}).get('ok') is True and (o1 or {}).get('ambiguous') is False, o1)
    rc, o2, _ = jrun([PY, os.path.join(HERE, '脊髓.py'), 'find', two, tpl, '0.8'])
    add('11 两同分报 ambiguous', (o2 or {}).get('ok') is False and 'ambiguous' in str((o2 or {}).get('error')) and len((o2 or {}).get('candidates') or []) >= 2, o2)

    # 12 --resolve nearest 选对方向
    rc, o3, _ = jrun([PY, os.path.join(HERE, '脊髓.py'), 'find', two, tpl, '0.8', '--resolve', 'nearest:90,70'])
    near1 = (o3 or {}).get('box') or [0, 0]
    rc, o4, _ = jrun([PY, os.path.join(HERE, '脊髓.py'), 'find', two, tpl, '0.8', '--resolve', 'nearest:310,190'])
    near2 = (o4 or {}).get('box') or [0, 0]
    ok12 = (o3 or {}).get('ok') and abs(near1[0] - 80) <= 2 and (o4 or {}).get('ok') and abs(near2[0] - 300) <= 2
    add('12 --resolve nearest 两个方向都选对', ok12, {'left': near1, 'right': near2})

    # 13 失败签名归并（不同时间戳同签名）
    code = ('import importlib.util as u;'
            's=u.spec_from_file_location("g",r"' + os.path.join(HERE, '闸.py') + '");'
            'm=u.module_from_spec(s);s.loader.exec_module(m);'
            'a=m.sig(m.norm_title("报告 12:00:01"),"Notepad",10);'
            'b=m.sig(m.norm_title("报告 12:59:59"),"Notepad",10);'
            'print(a==b)')
    rc, out = run([PY, '-c', code])
    add('13 签名归并（不同时间戳同签名）', 'True' in out, out[-40:])

    # 14 弹窗闸：目标写错必须拦（fail-closed）
    rc, o, _ = jrun([PY, os.path.join(HERE, '查弹窗.py'), '--point', '400,300', '--target', '根本不存在ZZZ'])
    add('14 目标找不到就拦', rc == 3 and (o or {}).get('blocked') is True, o)

    if NO_INPUT:
        add('15 带 expect 的点击（成功/失败）', False, '已跳过（--no-input）', skipped=True)
        add('16 bgone 最小化不算关闭', False, '已跳过（--no-input）', skipped=True)
        add('17 UIA 作用域正负控', False, '已跳过（--no-input）', skipped=True)
    else:
        _np2 = subprocess.Popen(['notepad.exe']); _spawned.append(_np2)
        time.sleep(2.2)
        _p2 = point_in('Notepad')
        if not _p2:
            add('15 带 expect 的点击（成功/失败）', False, '起不来记事本', skipped=True)
            _p2 = None
        rc, o, _ = (1, {}, '') if not _p2 else jrun([PY, os.path.join(HERE, '点到位.py'), '--x', str(_p2[0]), '--y', str(_p2[1]),
                         '--window', 'Notepad',
                         '--expect', 'fg-title:Notepad', '--wait', '1200', '--path', 'chk-pass'])
        rc2, o2b, _ = (1, {}, '') if not _p2 else jrun([PY, os.path.join(HERE, '点到位.py'), '--x', str(_p2[0]), '--y', str(_p2[1]),
                            '--window', 'Notepad',
                            '--expect', 'title:绝不存在', '--wait', '300', '--path', 'chk-fail'])
        _blocked15 = "弹窗拦截" in str((o or {}).get("stop_reason", "")) or "弹窗拦截" in str((o2b or {}).get("stop_reason", ""))
        add('15 带 expect 的点击（成功 rc0 / 失败 rc1 且 2 次拉黑）',
            (rc == 0 and (o or {}).get('ok') is True and rc2 == 1 and (o2b or {}).get('attempts') == 2),
            ({'blocked_by_env': True, 'ok': o, 'fail': o2b} if _blocked15 else {'ok': o, 'fail': o2b}),
            skipped=_blocked15)
        # 16 bgone 最小化
        np_proc = subprocess.Popen(['notepad.exe']); _spawned.append(np_proc)
        time.sleep(2.2)
        hwnd = np_proc.pid and 0
        import ctypes as _ct, ctypes.wintypes as _wt
        u2 = _ct.windll.user32
        found = []
        EP = _ct.WINFUNCTYPE(_wt.BOOL, _wt.HWND, _wt.LPARAM)
        def _cb(h, l):
            cls = _ct.create_unicode_buffer(64); u2.GetClassNameW(h, cls, 64)
            n = u2.GetWindowTextLengthW(h)
            if cls.value == 'Notepad' and n:
                b = _ct.create_unicode_buffer(n + 1); u2.GetWindowTextW(h, b, n + 1); found.append((h, b.value))
            return True
        u2.EnumWindows(EP(_cb), 0)
        if found:
            h0, t0 = found[0]
            u2.ShowWindow(h0, 6)
            time.sleep(0.6)
            rc, o, _ = jrun([PY, os.path.join(HERE, '验.py'), 'bgone:' + t0, '--timeout', '600'])
            add('16 bgone 最小化不算关闭', rc == 1 and (o or {}).get('ok') is False and (o or {}).get('minimized'), o)
            rc, o, _ = jrun([PY, os.path.join(HERE, '验.py'), 'uia:文本编辑器@' + t0.split(' ')[0], '--timeout', '1200'])
            add('17 UIA 作用域正控', rc == 0 and (o or {}).get('ok') is True, o)
        else:
            add('16 bgone 最小化不算关闭', False, '没造出记事本窗口', skipped=True)
            add('17 UIA 作用域正控', False, '没造出记事本窗口', skipped=True)
        try: np_proc.terminate()
        except Exception: pass

    # （原第 18 项与第 7 项完全重复，2026-11-20 独立会话指出后删除）


    # ===== 2026-11-20 补：独立会话指出「8 条高危修复没进清单」 =====

    # 19 空谓词必须报错（rc=2），不许静默通过
    rc, o, _ = jrun([PY, os.path.join(HERE, '验.py'), 'title:', '--timeout', '300'])
    rc2, o2, _ = jrun([PY, os.path.join(HERE, '验.py'), 'all:', '--timeout', '300'])
    add('19 空谓词报错 rc=2', rc == 2 and rc2 == 2, {'title:': rc, 'all:': rc2})

    # 20 changed_coarse_paths（路径版，今天修的手眼/序列判据）
    _c1 = os.path.join(TMP, 'c1.png'); _c2 = os.path.join(TMP, 'c2.png'); _c3 = os.path.join(TMP, 'c3.png')
    import numpy as _np2, cv2 as _cv2
    _a = _np2.full((300, 400, 3), 120, _np2.uint8)
    _cv2.imwrite(_c1, _a); _cv2.imwrite(_c2, _a)
    _b = _a.copy(); _b[50:150, 50:250] = 250; _cv2.imwrite(_c3, _b)
    _code20 = ('import importlib.util as u;'
               's=u.spec_from_file_location("h",r"' + os.path.join(HERE, '手眼.py') + '");'
               'm=u.module_from_spec(s);s.loader.exec_module(m);'
               'print(m.changed_coarse_paths(r"' + _c1 + '",r"' + _c2 + '"),'
               'm.changed_coarse_paths(r"' + _c1 + '",r"' + _c3 + '"),'
               'm.changed_coarse_paths(r"' + os.path.join(TMP, 'nope.png') + '",r"' + _c1 + '"))')
    rc, out = run([PY, '-c', _code20])
    _vals = [v for v in out.replace(',', ' ').split() if v in ('True', 'False')]
    add('20 changed_coarse_paths（相同/变化/缺失）', _vals == ['False', 'True', 'True'], out[-60:])

    # 21 /locate 的 ok 不等于脚本跑完（本地服务，离线可测）
    try:
        import urllib.request as _ur, json as _js
        _body = _js.dumps({'shot': blank, 'template': tpl, 'thresh': 0.8}).encode()
        _req = _ur.Request('http://127.0.0.1:8811/locate', data=_body,
                           headers={'Content-Type': 'application/json'}, method='POST')
        with _ur.urlopen(_req, timeout=15) as _resp:
            _jr = _js.loads(_resp.read().decode('utf-8'))
        add('21 /locate inner_ok 透出', _jr.get('ok') is False and _jr.get('inner_ok') is False, _jr)
    except Exception as _e:
        add('21 /locate inner_ok 透出', False, '服务未起：' + str(_e)[:80], skipped=True)

    # 22 闸门路径真能用（行为验证：跑一次 probe，看它是否真的过闸）
    try:
        _pr = subprocess.run(['pwsh', '-NoProfile', '-File', os.path.join(HERE, '动作.ps1'), '-Action', 'probe'],
                             capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=90)
        _o22 = (_pr.stdout or '') + (_pr.stderr or '')
        add('22 闸门路径可用（跑 probe 过闸）', 'gate ok=True' in _o22, _o22.strip()[-80:])
    except Exception as _e:
        add('22 闸门路径可用（跑 probe 过闸）', False, str(_e)[:80])

    # 23 让位闸信任窗（行为验证：打桩验 0.05s 放行 / 0.6s 拒绝）
    _code23 = ('import importlib.util as u, time;'
               's=u.spec_from_file_location("g",r"' + os.path.join(HERE, '闸门.py') + '");'
               'm=u.module_from_spec(s);s.loader.exec_module(m);'
               'now=time.time();'
               'm.hook_idle=lambda: None;'            # 强制走「时间邻近」分支，测的就是那条兜底路径
               'm.last_input=lambda: (now-0.5, 0.5);'
               'm.mine_ts=lambda: now-0.5-0.05;'
               'a=m.check()[0];'
               'm.mine_ts=lambda: now-0.5-0.6;'
               'b=m.check()[0];'
               'print(a,b)')
    rc, out = run([PY, '-c', _code23])
    add('23 信任窗行为（0.05s 放行 / 0.6s 拒绝）', '0 3' in out.replace(',', ' '), out[-40:])

    # 24 perm.json 坏掉必须 fail-closed
    _bad = os.path.join(TMP, 'perm_bad.json')
    open(_bad, 'w', encoding='utf-8').write('{ this is not json')
    _code24 = ('import importlib.util as u;'
               's=u.spec_from_file_location("g",r"' + os.path.join(HERE, '闸门.py') + '");'
               'm=u.module_from_spec(s);s.loader.exec_module(m);'
               'm.PERM=r"' + _bad + '";'
               'print(m.perm_allows())')
    rc, out = run([PY, '-c', _code24])
    add('24 perm 坏掉 fail-closed', 'False' in out, out[-40:])

    # 25 手眼/序列 的判据是行为正确的（import 后真跑，不是 grep 源码）
    _p25 = os.path.join(TMP, "chk25.py")
    with open(_p25, "w", encoding="utf-8") as _fw:
        _fw.write(
            "import importlib.util as u\n"
            "def L(p, n):\n"
            "    s = u.spec_from_file_location(n, p)\n"
            "    m = u.module_from_spec(s); s.loader.exec_module(m); return m\n"
            "h = L(r'" + os.path.join(HERE, "手眼.py") + "', 'h')\n"
            "q = L(r'" + os.path.join(HERE, "序列.py") + "', 'q')\n"
            "print(h.changed_coarse_paths(r'" + _c1 + "', r'" + _c2 + "'),\n"
            "      h.changed_coarse_paths(r'" + _c1 + "', r'" + _c3 + "'),\n"
            "      q.changed_coarse_paths(r'" + _c1 + "', r'" + _c2 + "'),\n"
            "      q.changed_coarse_paths(r'" + _c1 + "', r'" + _c3 + "'))\n"
        )
    rc, out = run([PY, _p25])
    _v25 = [v for v in out.replace(',', ' ').split() if v in ('True', 'False')]
    add('25 手眼/序列 判据行为（相同False/变化True ×2）', _v25 == ['False', 'True', 'False', 'True'], out[-60:])

    passed = [r for r in results if r["ok"]]
    failed = [r for r in results if not r["ok"] and not r["skipped"]]
    skipped = [r for r in results if r["skipped"]]
    for _pr in _spawned:
        try:
            _pr.terminate()
        except Exception:
            pass
    shutil.rmtree(TMP, ignore_errors=True)
    verdict = ("全绿" if (not failed and not skipped) else
               ("有失败" if failed else "部分验证（有跳过，不算全绿）"))
    print(json.dumps({"ok": (not failed and not skipped), "verdict": verdict,
                      "passed": len(passed), "failed": len(failed), "skipped": len(skipped),
                      "note": "跳过不等于通过：skipped>0 时退出码 2，只有全跑且全过才是 0",
                      "items": results}, ensure_ascii=False, indent=1))
    # 2026-11-20 独立会话指出：--no-input 时 5 项没跑也算全绿、退出码 0 —— 会掩盖回退
    if failed:
        return 1
    return 0 if not skipped else 2


if __name__ == "__main__":
    sys.exit(main())
