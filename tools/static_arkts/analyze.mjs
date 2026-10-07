#!/usr/bin/env node
/**
 * ArkTS 源码静态分析器（**零依赖**，只用 Node 内置 fs/path）
 * ==========================================================
 *
 * 为什么要用 Node 而不是 Python 来做这一件事
 * ------------------------------------------
 * 调研（2026-09-23）发现业界做 HarmonyOS/OpenHarmony 应用分析的项目都落在
 * **Node/TypeScript 生态**上：
 *   * HapTest（SMAT-Lab）的 `--policy static_guided` 走静态分析模块；
 *   * HmTest（南方科大，JCST 2025）用 `arkanalyzer`（npm 包）做 Targeted Exploration；
 *   * OpenHarmony 官方工具链（ohpm / hvigor / ArkTS 编译器）本身也在 Node 生态里。
 *
 * 本项目的主控是 Python（AI/ML、编排、图像处理都在那边），但**「读 ArkTS 源码」**
 * 这件事归 Node 更顺手，两边各做各擅长的一半，中间用 JSON 交换 ——
 * 这就是所谓「多技术栈各用一段」的实际形态：不是把整个项目重写，
 * 而是把一件 Python 做起来别扭的事切出去。
 *
 * 本脚本**不需要 npm install**：只用内置 fs/path，纯正则 + JSON 解析。
 *
 * 用法::
 *
 *     node tools/static_arkts/analyze.mjs <工程根目录> [-o out.json]
 *
 * 输出 JSON 结构见 README「静态分析」一节。
 */
import fs from 'node:fs';
import path from 'node:path';

/** 危险控件文案模式 —— 与 ohauto/explorer.py 的 SafetyPolicy 口径保持一致。 */
const DANGER_PATTERNS = [
  /删除|移除|清空|注销|重置|恢复出厂|支付|付款|转账|下单|提交订单|确认支付/i,
];

const RE_ID = /\.id\(\s*['"]([^'"]+)['"]\s*\)/g;
const RE_TEXT = /\bText\(\s*(?:'([^']*)'|"([^"]*)")\s*\)/g;
const RE_TEXT_R = /\bText\(\s*\$r\(\s*['"]app\.string\.(\w+)['"]\s*\)\s*\)/g;
const RE_ONCLICK = /\.onClick\s*\(/;
const RE_ROUTER_PUSH = /router\.(?:pushUrl|replaceUrl|pushNamedRoute)\(\s*\{\s*url\s*:\s*['"]([^'"]+)['"]/g;
const RE_ENTRY = /@Entry\b/;
const RE_STRUCT = /\bstruct\s+(\w+)/;
/** 字符串资源表：app.string.xxx → 文案（读 element/string.json 填） */
const stringRes = new Map();

function walk(dir, out = []) {
  let entries;
  try {
    entries = fs.readdirSync(dir, { withFileTypes: true });
  } catch {
    return out;
  }
  for (const e of entries) {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) {
      // 跳过构建产物目录：里面的 .ets 是生成的，会污染统计
      if (['build', '.hvigor', 'node_modules', 'oh_modules', '.git'].includes(e.name)) continue;
      walk(p, out);
    } else if (e.isFile()) {
      out.push(p);
    }
  }
  return out;
}

function readJson5Like(file) {
  const raw = fs.readFileSync(file, 'utf8');
  // json5 允许注释/尾逗号 —— 这里只做最小清理，够读结构清晰的配置文件
  const cleaned = raw
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/(^|\s)\/\/.*$/gm, '$1')
    .replace(/,(\s*[}\]])/g, '$1');
  try {
    return JSON.parse(cleaned);
  } catch {
    return null;
  }
}

function main() {
  const args = process.argv.slice(2);
  const root = args.find((a) => !a.startsWith('-'));
  if (!root) {
    console.error('用法: node analyze.mjs <工程根目录> [-o out.json]');
    process.exit(2);
  }
  const oIdx = args.indexOf('-o');
  const outPath = oIdx >= 0 ? args[oIdx + 1] : null;

  const files = walk(root).filter((f) => f.endsWith('.ets'));
  const pages = new Set();          // 应用内页面（main_pages.json + @Entry，排除卡片）
  const widgetPages = new Set();    // form_config.json 声明的桌面卡片 src
  const widgetDecls = new Set();    // 实际命中的卡片源文件（相对路径）
  const routes = new Set();
  const controls = [];
  const moduleAbilities = [];
  const dialogFiles = new Set();    // 弹窗内容文件（其控件**按需渲染**，见下方说明）

  // ---- 0. 弹窗组件名：两条线索取并集
  //   a) `builder: XxxDialog()` —— CustomDialogController 挂的 builder；
  //   b) `@CustomDialog` 装饰的 struct。
  // 这些 struct 里的控件**只有弹窗打开时才进控件树**——把它们算进
  // 「真缺失」会造成结构性误报（2026-10-06 官方样例实测：cancel/confirm
  // 被报缺失，实际只是弹窗没开）。
  const dialogBuilders = new Set();
  for (const f of files) {
    let src0;
    try {
      src0 = fs.readFileSync(f, 'utf8');
    } catch {
      continue;
    }
    for (const m of src0.matchAll(/builder:\s*([A-Za-z_$][\w$]*)\s*\(/g)) {
      dialogBuilders.add(m[1]);
    }
    if (/@CustomDialog/.test(src0)) {
      for (const m of src0.matchAll(/struct\s+([A-Za-z_$][\w$]*)/g)) {
        dialogBuilders.add(m[1]);
      }
    }
  }

  // ---- 1. 路由：main_pages.json（配置声明，最权威）
  for (const f of walk(root)) {
    if (path.basename(f) === 'main_pages.json') {
      const j = readJson5Like(f);
      for (const s of (j && j.src) || []) pages.add(s);
    }
    // 桌面卡片页面（form_config.json 的 src）—— **不是应用内页面**。
    // ⚠️ 实测踩到：WidgetCard.ets 也在 pages/ 目录下、也有 @Entry，
    //    早先版本把它当成页面路由，结果静态声明比实际多一个到不了的页面。
    //    若拿这份清单去「引导探索」，等于让探索器去追一个进不去的页面。
    if (path.basename(f) === 'form_config.json') {
      const j = readJson5Like(f);
      for (const fm of (j && j.forms) || []) {
        if (fm && fm.src) widgetPages.add(String(fm.src).replace(/^\.\//, ''));
      }
    }
    // ---- module.json5：abilities → 入口 ability 名
    if (path.basename(f) === 'module.json5' || path.basename(f) === 'module.json') {
      const j = readJson5Like(f);
      const abs = (j && (j.module && j.module.abilities)) || j?.abilities || [];
      for (const a of abs) if (a && a.name) moduleAbilities.push(a.name);
    }
    // ---- 字符串资源（供 $r('app.string.xxx') 还原文案）
    if (f.endsWith('element/string.json')) {
      const j = readJson5Like(f);
      for (const s of (j && j.string) || []) {
        if (s && s.name && s.value !== undefined) stringRes.set(s.name, s.value);
      }
    }
  }

  // ---- 2. 源码：页面 / 控件 / 路由跳转
  for (const f of files) {
    let src;
    try {
      src = fs.readFileSync(f, 'utf8');
    } catch {
      continue;
    }
    const rel = path.relative(root, f).split(path.sep).join('/');

    // 弹窗内容文件：文件里的 struct 被 builder 引用，或文件带 @CustomDialog
    const structNames = [...src.matchAll(/\bstruct\s+([A-Za-z_$][\w$]*)/g)].map((m) => m[1]);
    const isDialogFile = structNames.some((n) => dialogBuilders.has(n)) || /@CustomDialog/.test(src);
    if (isDialogFile) dialogFiles.add(rel);

    // 页面：@Entry 装饰的 struct 视为一个页面 —— **但要把桌面卡片排除掉**。
    // 判定依据是 form_config.json 声明的 src（路径后缀匹配），不是目录名：
    // 卡片和页面都可能放在 `pages/` 下，靠目录区分不了。
    const isWidget = [...widgetPages].some((w) => rel.endsWith(w));
    if (RE_ENTRY.test(src) && !isWidget) {
      const m = RE_STRUCT.exec(src);
      const name = m ? m[1] : '';
      const guess = rel.match(/pages\/([\w/]+)\.ets$/);
      if (guess) pages.add('pages/' + guess[1]);
      else if (name) pages.add(name);
    } else if (RE_ENTRY.test(src) && isWidget) {
      widgetDecls.add(rel);
    }

    for (const m of src.matchAll(RE_ROUTER_PUSH)) routes.add(m[1]);

    // 控件：按「.id(...) / Text(...) / onClick」三件一起描述一个控件
    // 说明：这是**正则级**的近似，不是 AST 解析 —— 够用来做探索引导和清单增强，
    // 但不能当编译器用（做不到就如实说，别吹成 AST）。
    const ids = [...src.matchAll(RE_ID)].map((m) => m[1]);
    const texts = [
      ...[...src.matchAll(RE_TEXT)].map((m) => m[1] ?? m[2] ?? ''),
      ...[...src.matchAll(RE_TEXT_R)].map((m) => stringRes.get(m[1]) ?? `$r(app.string.${m[1]})`),
    ].filter((t) => t && t.trim());

    for (const id of ids) {
      controls.push({ file: rel, id, clickable: RE_ONCLICK.test(src), dialog: isDialogFile, dangerous: false });
    }
    for (const t of texts) {
      const dangerous = DANGER_PATTERNS.some((re) => re.test(t));
      controls.push({ file: rel, id: '', text: t, clickable: RE_ONCLICK.test(src), dialog: isDialogFile, dangerous });
    }
  }

  const result = {
    root,
    generated_at: new Date().toISOString(),
    analyzer: 'ohauto/static_arkts (node, zero-dep, regex-level)',
    ets_files: files.length,
    pages: [...pages].sort(),
    // 桌面卡片页面：**不是应用内页面**，别拿去当探索目标
    widget_pages: [...widgetDecls].sort(),
    // 弹窗内容文件：其中的控件**按需渲染**，缺失不算缺陷
    dialog_files: [...dialogFiles].sort(),
    routes_in_source: [...routes].sort(),
    abilities: [...new Set(moduleAbilities)].sort(),
    controls,
    dangerous_controls: controls.filter((c) => c.dangerous),
    strings: Object.fromEntries(stringRes),
  };

  const json = JSON.stringify(result, null, 2);
  if (outPath) {
    fs.mkdirSync(path.dirname(path.resolve(outPath)), { recursive: true });
    fs.writeFileSync(outPath, json, 'utf8');
    console.error(`[analyze] 已写入 ${outPath}`);
  }
  console.log(json);
}

main();
