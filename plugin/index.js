// 薄插件：只做转调，不含任何坐标/业务逻辑（内核在 工具\界面反射\ + 8811 服务）
export const name = "dsh-reflex";
export const inject = ["tools"];

const BASE = "http://127.0.0.1:8811";

async function call(path, body) {
  const opt = body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {};
  const r = await fetch(BASE + path, opt);
  const t = await r.text();
  try { return JSON.parse(t); } catch { return { ok: false, raw: t.slice(0, 800) }; }
}

import { execFile } from "node:child_process";
import { promisify } from "node:util";
import path from "node:path";
import { fileURLToPath } from "node:url";
const execFileP = promisify(execFile);
const PY = process.env.SPINE_PY || "python";
const SPINE_DIR = process.env.SPINE_DIR || path.join(path.dirname(fileURLToPath(import.meta.url)), "..");
const SPINE = path.join(SPINE_DIR, "脊髓统.py");
const CLICK_VERIFY = path.join(SPINE_DIR, "点并验.py");

// 动词 → 脊髓统.py 的参数
function spineArgs(a) {
  const v = a.verb;
  const out = [SPINE, v];
  if (v === "see") {
    if (a.kind) out.push("--kind", a.kind);
    if (a.tpl) out.push("--tpl", a.tpl);
    // 2026-11-22（剪辑线缺口 1）：不给窗口就按「当前前台」——从 DSH 里调时常抓到 DSH 自己。
    // 现在可以显式指定目标窗口。
    if (a.window) out.push("--window", String(a.window));
    if (a.click) out.push("--click");
  } else if (v === "icons") {
    if (a.want) out.push("--want", String(a.want || 20));
    if (a.window) out.push("--window", String(a.window));
  } else if (v === "learn") {
    out.push("--tag", String(a.tag || ""), "--index", String(a.index || 1), "--name", String(a.name || ""));
    if (a.dry) out.push("--dry");
  } else if (v === "observe") {
    out.push("--seconds", String(a.seconds || 3));
  } else if (v === "click") {
    // 2026-11-21 统一实现（剪辑线 b0dc182c 实测指出「两套实现打架」）：
    // 原来这里走 点并验.py，而 脊髓统.py 的 click 分支才有 D7 判据
    //（control_before/after、into_input、像素三合一），结果那套在插件路径上全是死代码，
    // 只有直接敲 CLI 才跑。现在统一到 脊髓统.py click 一处实现。
    // --expect（回读目标态）已从 点并验.py 搬进 脊髓统.py click。
    out.push("--x", String(a.x), "--y", String(a.y));
    if (a.expect) out.push("--expect", String(a.expect));
  } else if (v === "type") {
    out.push("--text", String(a.text || ""));
  } else if (v === "key") {
    // 2026-11-20：key 现在可以指定窗口（前台守卫 + 地址栏先取焦点）；不传就按当前前台窗口。
    out.push("--keys", String(a.keys || ""));
    if (a.window) out.push("--window", String(a.window));
  } else if (v === "scroll") {
    out.push("--dir", a.dir || "down", "--amount", String(a.amount || 3));
  } else if (v === "find") {
    out.push("--name", String(a.name || ""));
    // 2026-11-22（缺口 2）：可指定「应该在哪个窗口上找」，跨窗口模板直接拒，别给假阳性
    if (a.window) out.push("--window", String(a.window));
  } else if (v === "hover") {
    out.push("--x", String(a.x), "--y", String(a.y));
  } else if (v === "wait") {
    if (a.stable) out.push("--stable"); else out.push("--seconds", String(a.seconds || 1));
  }
  return out;
}

export function apply(ctx) {
  ctx.tools.register({
    name: "spine",
    description: "脊髓（本地手眼，0 token）：status 看状态 / see 找元素并可选点 / icons 列可点图标 / learn 学一个元素 / observe 看几秒变化 / click type key scroll wait 基本动作。所有动作自动过让位闸与规则。",
    parameters: {
      type: "object",
      properties: {
        verb: { type: "string", description: "动作名：status/see/icons/learn/observe/click/type/key/scroll/wait/gate/find/hover/sheet" },
        kind: { type: "string", description: "see 用：input_box | overlay | icon" },
        tpl: { type: "string", description: "see 用：模板图片路径" },
        click: { type: "boolean", description: "see 时找到就点（true/false）" },
        want: { type: "string", description: "icons 返回几个候选，默认 20（传字符串）" },
        tag: { type: "string", description: "learn 用：界面名（探针 tag）" },
        index: { type: "string", description: "learn 用：第几个候选（从 1 开始，传字符串）" },
        name: { type: "string", description: "learn 用：给元素起的名字；find 用：要按名字找的元素名" },
        dry: { type: "boolean", description: "learn 只解析不点" },
        seconds: { type: "string", description: "observe/wait 的秒数（传字符串）" },
        stable: { type: "boolean", description: "wait 时改为等界面稳定" },
        x: { type: "string", description: "click 用的屏幕 X（传字符串）" },
        y: { type: "string", description: "click 用的屏幕 Y（传字符串）" },
        expect: { type: "string", description: "click 用：点完要等到的目标状态，如 fg-title:记事本 / title:另存为 / bgone:计算器 / pixel:x,y,#hex,tol" },
        text: { type: "string", description: "type 要输入的文字" },
        keys: { type: "string", description: "key 要按的键，如 ENTER；^L 会先把焦点落到地址栏再报结果" },
        window: { type: "string", description: "key/see/find/icons 用：目标窗口标题片段（不给=当前前台；从 DSH 里调时必须显式给，否则会抓到 DSH 自己）" },
        dir: { type: "string", description: "scroll 方向：up/down" },
        amount: { type: "string", description: "scroll 格数，默认 3（传字符串）" }
      },
      required: ["verb"],
      additionalProperties: false
    },
    output: {
      schema: { type: "object", additionalProperties: true, properties: { ok: { type: "boolean" }, out: { type: "string" }, error: { type: "string" } } },
      render(_args, v) { return [{ type: "text", text: String((v && v.out) || JSON.stringify(v || {}, null, 2)).slice(0, 4000) }] }
    },
    async execute(args) {
      // 2026-11-21 修（D2）：缺坐标不许补 0，直接拒绝
      if (args.verb === "click" || args.verb === "hover") {
        const x = Number(args.x), y = Number(args.y);
        if (args.x === undefined || args.y === undefined || !Number.isFinite(x) || !Number.isFinite(y)) {
          return { ok: false, error: args.verb + " 需要 x、y（屏幕像素坐标，必须是数字）；缺参数不执行" };
        }
        // 2026-11-21 修（D3）：越界直接拒，不夹边
        if (x < 0 || y < 0 || x > 4000 || y > 3000) {
          return { ok: false, error: args.verb + " 坐标越界：(" + x + "," + y + ") 不在可用屏幕范围内（0..4000 / 0..3000），已拒绝，未移动鼠标" };
        }
      }
      try {
        const r = await execFileP(PY, spineArgs(args), { timeout: 300000, windowsHide: true, encoding: "utf-8" });
        const out = (r.stdout || "").trim();
        // 2026-11-21 修（D1）：透传内层 ok——找不到/没落到位时内层是 ok:false，不许再报 ok:true
        // 2026-11-21 修（D1 二次）：脊髓统.py 输出的是**多行 pretty JSON**，
        // 原来只按「单行以 { 开头」找，永远解析不到 → 假绿（see 找不到仍报 ok:true）。
        // 改成：先把整段 out 里第一个 { 到最后一个 } 之间整体试一次；不行再退回逐行试。
        let inner = null;
        const t = String(out || "").trim();
        const s0 = t.indexOf("{"), e0 = t.lastIndexOf("}");
        if (s0 >= 0 && e0 > s0) {
          try { inner = JSON.parse(t.slice(s0, e0 + 1)); } catch {}
        }
        if (!inner) {
          const lines = t.split("\n").map((l) => l.trim()).filter((l) => l.startsWith("{"));
          for (let i = lines.length - 1; i >= 0; i--) {
            try { inner = JSON.parse(lines[i]); break; } catch {}
          }
        }
        if (inner && typeof inner.ok === "boolean" && inner.ok === false) {
          return { ok: false, out, error: String(inner.why || inner.error || "内部判据未通过（见 out）").slice(0, 800) };
        }
        return { ok: true, out };
      } catch (e) {
        return { ok: false, error: String((e && (e.stdout || e.message)) || e).slice(0, 1200) };
      }
    }
  });

  ctx.tools.register({
    name: "reflex_status",
    description: "反射层状态：界面列表、脚本是否就绪、服务是否在跑（本机 127.0.0.1:8811）。",
    parameters: { type: "object", properties: {}, additionalProperties: false },
    output: {
      schema: { type: "object", additionalProperties: true, properties: { ok: { type: "boolean" }, count: { type: "number" }, interfaces: { type: "object" }, error: { type: "string" } } },
      render(_args, v) { return [{ type: "text", text: JSON.stringify(v ?? {}, null, 2) }] }
    },
    async execute() {
      try { return await call("/status"); }
      catch (e) { return { ok: false, error: "反射服务没在跑：" + String(e) }; }
    }
  });

  ctx.tools.register({
    name: "reflex_run",
    description: "跑一套界面动作（钉钉/B站/抖音/Edge）。默认 dry=true 只检查不动手。",
    parameters: {
      type: "object",
      properties: {
        interface: { type: "string", description: "钉钉 / B站 / 抖音 / Edge" },
        action: { type: "string", description: "发文件 / 投稿 / 启动" },
        params: { type: "object", description: "path/title/cover 等" },
        dry: { type: "boolean", description: "true=只检查（默认）" }
      },
      required: ["interface", "action"],
      additionalProperties: false
    },
    output: {
      schema: { type: "object", additionalProperties: true, properties: { ok: { type: "boolean" }, rc: { type: "number" }, out: { type: "string" }, error: { type: "string" } } },
      render(_args, v) { return [{ type: "text", text: JSON.stringify(v ?? {}, null, 2) }] }
    },
    async execute(args) {
      const body = { interface: args.interface, action: args.action, params: args.params || {}, dry: args.dry !== false };
      try { return await call("/run", body); }
      catch (e) { return { ok: false, error: "反射服务没在跑：" + String(e) }; }
    }
  });

}
