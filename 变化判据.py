# 变化判据.py —— 三层判据（按豆包技术建议实现，2026-11-19 23:4x）
# 用法: python 变化判据.py --window <标题片段> --roi x,y,w,h [--save-baseline p] [--baseline p] [--frames N] [--roll] [--side] [--delta 8]
# 原理：
#  1) null 模型：动作前采 K 帧，逐像素 σ + flicker mask（idle 时动过的像素不计）
#  2) 判据：逐像素 |Δ|>max(3σ, 8) 且 1px 膨胀后最大连通域 ≥ 16px²
#  3) 持久性：3 帧里 ≥2 帧在同一位置报变，才算 changed；否则 uncertain
import sys, json, time, argparse, ctypes, ctypes.wintypes as wt
try: sys.stdout.reconfigure(encoding='utf-8')
except Exception: pass
from PIL import ImageGrab
import numpy as np

def rect_for(title):
    u = ctypes.windll.user32
    hits = []
    EP = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(h, l):
        if u.IsWindowVisible(h):
            n = u.GetWindowTextLengthW(h)
            if n:
                b = ctypes.create_unicode_buffer(n + 1); u.GetWindowTextW(h, b, n + 1)
                if title.lower() in b.value.lower():
                    r = wt.RECT(); u.GetWindowRect(h, ctypes.byref(r))
                    hits.append((r.left, r.top, r.right, r.bottom))
        return True
    u.EnumWindows(EP(cb), 0)
    return hits[0] if hits else None

def frames(box, k, gap):
    out = []
    for _ in range(k):
        im = ImageGrab.grab(all_screens=True).crop(box).convert('L')
        out.append(np.asarray(im, dtype=np.int16))
        time.sleep(gap)
    return out

def calibrate(box, k=10, gap=0.15):
    fs = frames(box, k, gap)
    stack = np.stack(fs).astype(np.int16)
    sigma = stack.std(axis=0)
    diffmax = stack.max(axis=0) - stack.min(axis=0)
    flicker = diffmax > 8          # idle 时动过的像素：掩掉
    return sigma, flicker

def anim_mask(frames_stack, k_sigma=3.0):
    """动画区检测（2026-11-20）：时域方差 + 帧间差稳定性
    - σ 大的像素 = 一直在动
    - 逐像素看相邻帧差是否几乎相同（周期性运动特征）
    返回 (anim_mask, sigma)"""
    st = np.stack(frames_stack).astype(np.int16)
    sigma = st.std(axis=0)
    # 相邻帧差
    diffs = np.abs(np.diff(st, axis=0))
    if diffs.shape[0] >= 2:
        # 帧差本身的波动小 → 周期性/持续运动
        dstd = diffs.std(axis=0)
        periodic = (sigma >= k_sigma * max(1.0, float(np.median(sigma)) )) & (dstd <= np.maximum(2.0, np.median(dstd) * 2))
    else:
        periodic = sigma > 8
    anim = (sigma > max(3.0, float(np.percentile(sigma, 60)))) | periodic
    return anim, sigma


def diff_mask(a, b, sigma, flicker, k_sigma=3.0, floor=8):
    thr = np.maximum(k_sigma * sigma, floor)
    d = np.abs(a - b)
    mask = (d > thr) & (~flicker)
    return mask

def blob_info(mask):
    """返回 (最大连通域面积, 质心 x, 质心 y, ROI 内相对坐标)"""
    import cv2
    m = mask.astype(np.uint8)
    if m.sum() == 0:
        return 0, -1, -1
    n, lab, stats, cents = cv2.connectedComponentsWithStats(m, 8)
    best, bi = 0, -1
    for i in range(1, n):
        if stats[i, cv2.CC_STAT_AREA] > best:
            best = stats[i, cv2.CC_STAT_AREA]; bi = i
    if bi < 0:
        return 0, -1, -1
    return int(best), float(cents[bi][0]), float(cents[bi][1])


def max_blob(mask):
    # 1px 膨胀（4 邻域）后取最大连通域面积，避免散点噪声
    m = mask.copy()
    g = m | np.roll(m, 1, 0) | np.roll(m, -1, 0) | np.roll(m, 1, 1) | np.roll(m, -1, 1)
    lab = np.zeros(g.shape, dtype=np.int32)
    cur = 0; best = 0
    h, w = g.shape
    for y in range(h):
        for x in range(w):
            if g[y, x] and lab[y, x] == 0:
                cur += 1; stack = [(y, x)]; lab[y, x] = cur; area = 0
                while stack:
                    cy, cx = stack.pop(); area += 1
                    for dy, dx in ((1,0),(-1,0),(0,1),(0,-1)):
                        ny, nx = cy+dy, cx+dx
                        if 0 <= ny < h and 0 <= nx < w and g[ny, nx] and lab[ny, nx] == 0:
                            lab[ny, nx] = cur; stack.append((ny, nx))
                best = max(best, area)
    return best

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--window', required=True)
    ap.add_argument('--min-blob', type=int, default=16)
    ap.add_argument('--roi', default='', help='x,y,w,h；给了就只在这个小区域里判（远离动画）')
    ap.add_argument('--frames', type=int, default=5, help='存基线时取多少帧的中位数（默认 5）')
    ap.add_argument('--save-baseline', default='', help='把当前帧存成基线（动作前调用）')
    ap.add_argument('--baseline', default='', help='与该基线比较（动作后调用）')
    ap.add_argument('--roll', action='store_true', help='比对后把当前态写回基线（滚动背景，下次比对以新状态为底）')
    ap.add_argument('--side', action='store_true', help='同时输出非像素旁证（前台标题 / 焦点控件）')
    ap.add_argument('--delta', type=int, default=8, help='逐像素差阈值（深色主题可降到 5）')
    ap.add_argument('--anchor', default='', help='x,y：要求变化发生在该点附近（空间锚定，防"点了A、B处弹窗"算成功）')
    ap.add_argument('--max-shift', type=int, default=180, help='变化连通域质心离 anchor 的最大像素距离')
    ap.add_argument('--calibrate', action='store_true')
    a = ap.parse_args()
    u = ctypes.windll.user32
    try: ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception: u.SetProcessDPIAware()
    box = rect_for(a.window)
    if a.roi:
        try:
            rx, ry, rw, rh = [int(v) for v in a.roi.split(',')]
            box = (rx, ry, rx + rw, ry + rh)
        except Exception:
            print(json.dumps({'ok': False, 'why': 'ROI 格式应为 x,y,w,h'}, ensure_ascii=False)); return 1
    if not box:
        print(json.dumps({'ok': False, 'why': '找不到窗口'}, ensure_ascii=False)); return 1
    if a.save_baseline:
        from PIL import Image
        # 2026-11-20：基线取 N 帧中位数，压掉闪烁/动画带来的噪声
        stack = np.stack(frames(box, max(1, a.frames), 0.12)).astype(np.int16)
        med = np.median(stack, axis=0).astype(np.uint8)
        Image.fromarray(med).save(a.save_baseline)
        print(json.dumps({'ok': True, 'saved': a.save_baseline, 'box': list(box)}, ensure_ascii=False))
        return 0
    if a.baseline:
        from PIL import Image
        base = np.asarray(Image.open(a.baseline).convert('L'), dtype=np.int16)
        # 2026-11-20（按 Kimi 建议）：当前态取 N 帧 → 时域方差判动画区 → 动画像素从判据里剔除
        stack = frames(box, max(3, a.frames), 0.1)
        cur = np.median(np.stack(stack), axis=0).astype(np.int16)
        if base.shape != cur.shape:
            print(json.dumps({'ok': False, 'why': '尺寸不一致', 'base': list(base.shape), 'cur': list(cur.shape)}, ensure_ascii=False)); return 1
        anim, sigma = anim_mask(stack)
        anim_capped = False
        if float(anim.mean()) > 0.5:
            # 2026-11-20（挑错者指出）：相对阈值在噪声多时会掩掉大半 ROI，把真实变化也吃掉
            anim = np.zeros_like(anim)
            anim_capped = True
        raw = np.abs(base - cur)
        mask = (raw > a.delta) & (~anim)
        # 持续性：连续 3 帧同位置都超阈值（且面积不漂移超过 50%）
        votes, blobs = 0, []
        for i in range(3):
            time.sleep(0.12)
            f = frames(box, 1, 0)[0]
            m = (np.abs(base - f) > a.delta) & (~anim)
            b = max_blob(m)
            blobs.append(b)
            if b >= a.min_blob:
                votes += 1
        mb = max_blob(mask)
        changed = mb >= a.min_blob and votes >= 2
        transient = False
        if changed:
            # 变化后 0.6s 再采两帧：若新状态自身还在动（光标闪烁/动画），多半是瞬态
            time.sleep(0.6)
            s1 = frames(box, 1, 0)[0]; time.sleep(0.25); s2 = frames(box, 1, 0)[0]
            if s1.shape == s2.shape:
                drift = float(((np.abs(s1 - s2) > a.delta) & (~anim)).mean())
                transient = drift > 0.02
        side = {}
        if a.side:
            try:
                import uiautomation as _ua
                _ua.SetGlobalSearchTimeout(0.6)
                foc = _ua.GetFocusedControl()
                side['focus'] = ((foc.ControlTypeName or '') + ':' + (foc.Name or ''))[:50] if foc else None
                fgw = u.GetForegroundWindow()
                _b = ctypes.create_unicode_buffer(128); u.GetWindowTextW(fgw, _b, 128)
                side['foreground'] = _b.value[:40]
            except Exception:
                pass
        if a.roll:
            try:
                med2 = np.median(np.stack(frames(box, max(3, a.frames), 0.08)), axis=0).astype(np.uint8)
                Image.fromarray(med2).save(a.baseline)
                side['baseline_rolled'] = True
            except Exception:
                side['baseline_rolled'] = False
        anchor_fail = False
        if a.anchor and changed:
            try:
                ax, ay = [int(v) for v in a.anchor.split(',')]
            except Exception:
                ax = ay = -1
            if ax >= 0:
                mb2, cx2, cy2 = blob_info(mask)
                if mb2 > 0:
                    # ROI 左上角 + 质心 = 屏幕坐标
                    gx, gy = box[0] + cx2, box[1] + cy2
                    dist = ((gx - ax) ** 2 + (gy - ay) ** 2) ** 0.5
                    if dist > a.max_shift:
                        anchor_fail = True
                        changed = False
        if changed and transient:
            changed = False
        verdict = 'changed' if changed else ('uncertain' if votes >= 1 else 'unchanged')
        print(json.dumps({'ok': changed, 'verdict': verdict,
                          'mode': 'baseline+anim', 'side': side,
                          'blob': mb, 'blobs': blobs, 'votes': votes, 'transient': transient, 'anchor_fail': anchor_fail,
                          'anim_capped': anim_capped, 'delta': a.delta,
                          'min_blob': a.min_blob, 'anim_pct': round(float(anim.mean()), 4),
                          'changed_pct': round(float(mask.mean()), 5), 'box': list(box)}, ensure_ascii=False))
        return 0
    sigma, flicker = calibrate(box)
    if a.calibrate:
        print(json.dumps({'ok': True, 'calibrated': True, 'window': a.window,
                          'flicker_pct': round(float(flicker.mean()), 4)}, ensure_ascii=False))
        return 0
    base = frames(box, 1, 0)[0]
    votes = 0; blobs = []
    for _ in range(3):
        time.sleep(0.15)
        cur = frames(box, 1, 0)[0]
        if cur.shape != base.shape:
            blobs.append(0); continue
        mask = diff_mask(base, cur, sigma, flicker)
        mb = max_blob(mask)
        blobs.append(mb)
        if mb >= a.min_blob:
            votes += 1
    changed = votes >= 2
    verdict = 'changed' if changed else ('uncertain' if votes == 1 else 'unchanged')
    print(json.dumps({'ok': changed, 'verdict': verdict, 'window': a.window,
                      'blobs': blobs, 'votes': votes, 'min_blob': a.min_blob,
                      'flicker_pct': round(float(flicker.mean()), 4)}, ensure_ascii=False))
    return 0

if __name__ == '__main__':
    sys.exit(main())
