#!/usr/bin/env python3
"""
wiring_ui.py -- Single line-CAD UI（本地网站）

界面：左边从块库选块(可重复)，右边组成一条“链”，点“生成连线” ->
      按顺序放块、解插入点、接点对齐、连完线，输出 DXF(保图层) + 预览。

用法:  python wiring_ui.py [--port 8770]
数据:  blocklib/_manifest.csv + blocklib/blocks/*.dxf
输出:  out/wiring_<时间戳>.dxf
"""

import argparse
import datetime
import json
import os
import socketserver
import threading
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler

import blockui_server as ui
import connect_library as cl
import wiring_raw as wr
import array_gen as ag
import cad_draw as cd
import i18n                      # 界面中英文对照表（页面里按中文原句查表翻译）

try:                                  # 在线更新（可选，缺了也不影响生成）
    import app_update as upd
except Exception:
    upd = None


HERE = os.path.dirname(os.path.abspath(__file__))
BLOCKS_DIR = ui.BLOCKS_DIR
try:                                  # 打包成 exe 后走 runtime_paths（输出落在 exe 旁边）
    import runtime_paths as _rp
    OUTDIR = _rp.OUTDIR
    FRAMES_DIR = _rp.FRAMES_DIR
except Exception:                     # 源码运行：维持原样
    OUTDIR = os.path.join(HERE, "out")
    FRAMES_DIR = os.path.join(HERE, "templates")
DEFAULT_PORT = 8770


def vendor_dir():
    """vendor/（morphicons 那套图标变形库）在哪儿。

    打包成 exe 时走 _MEIPASS（spec 里把 vendor 打进去了）；源码运行就是同目录。
    """
    outs = []
    try:
        outs.append(os.path.join(_rp.APP_ROOT, "vendor"))
    except Exception:
        pass
    try:
        import sys as _sys
        mp = getattr(_sys, "_MEIPASS", "")
    except Exception:
        mp = ""
    if mp:
        outs.append(os.path.join(mp, "vendor"))
    outs.append(os.path.join(HERE, "vendor"))
    for p in outs:
        if p and os.path.isdir(p):
            return p
    return outs[-1]


def list_frames():
    if not os.path.isdir(FRAMES_DIR):
        return []
    return [f for f in sorted(os.listdir(FRAMES_DIR)) if f.lower().endswith(".dxf")]


def make_server(port0=DEFAULT_PORT, tries=20):
    """起一个多线程 HTTP 服务。端口被占就从 port0 往后试，返回 (httpd, port)。

    多线程的原因：生成请求跑着的时候，界面上的进度轮询还得能进来。
    """
    class S(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    port = port0
    for _ in range(tries):
        try:
            return S(("127.0.0.1", port), Handler), port
        except OSError:
            port += 1
    raise OSError("端口 %d 起不来（%d 个都被占用）" % (port0, tries))


CODE_FILES = ("wiring_raw.py", "wiring_ui.py", "array_gen.py",
              "connect_library.py", "blockui_server.py", "blockpack.py",
              "i18n.py")


def code_version():
    """最近一次改代码的时间。界面上会显示，用来一眼看出跑的是不是旧进程。"""
    ts = 0.0
    for f in CODE_FILES:
        p = os.path.join(HERE, f)
        if os.path.exists(p):
            ts = max(ts, os.path.getmtime(p))
    return datetime.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M:%S")


LOAD_VER = code_version()      # 本进程启动时代码的版本


def maybe_reload():
    """代码在本进程启动之后改过 → 自动重载那几个模块，省得反复重启对不上。

    注意：wiring_ui.py 自己改不了自己（正在跑的就是它），那一部分还是要重启；
    但排版/生成/CAD 这些都在别的模块里，自动重载就能生效。
    """
    global LOAD_VER
    now = code_version()
    if now == LOAD_VER:
        return None
    import importlib
    for name in ("connect_library", "blockui_server", "blockpack",
                 "wiring_raw", "array_gen", "cad_draw", "i18n"):
        m = sys.modules.get(name)
        if m is not None:
            try:
                importlib.reload(m)
            except Exception:
                pass
    LOAD_VER = now
    return "已自动重载改过的代码（%s）" % now

# ----------------------------- 进度 -----------------------------
PROGRESS = {"pct": 0, "stage": "", "running": False, "seq": 0, "tail": []}


def set_progress(pct, stage="", tail=None):
    PROGRESS["pct"] = int(max(0, min(100, pct)))
    PROGRESS["stage"] = stage
    if tail is not None:
        PROGRESS["tail"] = [str(x) for x in list(tail)[-8:]]
    PROGRESS["seq"] += 1


def progress_cb(pct, stage=""):
    set_progress(pct, stage)



def stale_warning():
    """代码在本进程启动之后又改过了 → 让界面直接说清楚，别让人对着旧代码找 bug。"""
    now = code_version()
    if now != LOAD_VER:
        return ("⚠ 代码在你启动之后又改过了（启动时 %s，现在 %s）："
                "**关掉这个黑窗口、重新跑一次 python wiring_ui.py**，"
                "否则跑的还是旧代码，报错会误导人。" % (LOAD_VER, now))
    return None


def list_blocks():
    names = []
    if os.path.isdir(BLOCKS_DIR):
        for f in sorted(os.listdir(BLOCKS_DIR)):
            if f.lower().endswith(".dxf"):
                names.append(os.path.splitext(f)[0])
    return names


# ------------------------- 块库分两组 -------------------------
# 界面里“① 板子”一块库、“② 线束”一块库，各管各的填写，逻辑上分开不混。
# 生成时两块库的块都会并进外框图，所以归到哪一组都不影响能不能画出来 ——
# 只影响它们在界面上的位置。
#   板子那一组：阵列/板子自己用的块（含起始块 CBX，CBX 属于板子这页）
#   线束那一组：组成线束链的块
BOARD_BLOCKS = ("PV-POS", "MIDDLE-PV", "END-NEG", "CBX", "MOTOR", "BHA", "BHA-PILE")


def block_group(name):
    return ("board" if str(name or "").strip().upper() in
            {x.upper() for x in BOARD_BLOCKS} else "harness")


def app_version():
    """给界面显示的版本号：在线更新过就显示更新后的版本。"""
    if upd is not None:
        try:
            return upd.local_version(), upd.local_note()
        except Exception:
            pass
    return code_version(), "内置版本"


# ----------------------- 桌面窗口里的文件操作 -----------------------
# 打包成桌面窗口（pywebview）后，界面里的 <a download> 点了没反应 —— WebView2
# 不会像浏览器那样弹下载框。所以这几件事改由程序自己做：
#   保存到桌面 / 打开输出文件夹 / 用默认程序打开（DXF 一般直接进 ZWCAD）
def _desktop_dir():
    for p in (os.path.join(os.path.expanduser("~"), "Desktop"),
              os.path.join(os.path.expanduser("~"), "OneDrive", "Desktop"),
              os.path.expanduser("~")):
        if os.path.isdir(p):
            return p
    return os.path.expanduser("~")


def _safe_out_file(name):
    """只允许碰输出目录里的文件（防目录穿越）。"""
    p = os.path.join(OUTDIR, os.path.basename(name or ""))
    return p if os.path.isfile(p) else None


def export_file(name, where="desktop"):
    """把输出文件复制到桌面/下载目录，返回 (ok, 说明或目标路径)。"""
    src = _safe_out_file(name)
    if not src:
        return False, "找不到文件：%s" % name
    if where == "downloads":
        dst_dir = os.path.join(os.path.expanduser("~"), "Downloads")
        if not os.path.isdir(dst_dir):
            dst_dir = _desktop_dir()
    else:
        dst_dir = _desktop_dir()
    dst = os.path.join(dst_dir, os.path.basename(src))
    try:
        i = 1
        base, ext = os.path.splitext(os.path.basename(src))
        while os.path.exists(dst):          # 重名就加序号，不覆盖用户已有文件
            dst = os.path.join(dst_dir, "%s (%d)%s" % (base, i, ext))
            i += 1
        import shutil as _sh
        _sh.copy2(src, dst)
        return True, dst
    except Exception as ex:
        return False, "复制失败：%s: %s" % (type(ex).__name__, ex)


def reveal_file(name=""):
    """在资源管理器里定位输出文件（没给名字就打开输出目录）。"""
    import subprocess
    p = _safe_out_file(name) if name else None
    try:
        if p:
            subprocess.Popen(["explorer", "/select,", os.path.normpath(p)])
        else:
            os.makedirs(OUTDIR, exist_ok=True)
            os.startfile(OUTDIR)                      # noqa: S606（Windows 专用）
        return True, p or OUTDIR
    except Exception as ex:
        return False, "%s: %s" % (type(ex).__name__, ex)


def open_with_default(name):
    """用系统默认程序打开输出文件（DXF 通常会直接进 ZWCAD）。"""
    p = _safe_out_file(name)
    if not p:
        return False, "找不到文件：%s" % name
    try:
        os.startfile(p)                               # noqa: S606
        return True, p
    except Exception as ex:
        return False, "%s: %s" % (type(ex).__name__, ex)


# ----------------------------- HTTP -----------------------------
HTML = r"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Single line-CAD</title>
<style>
 /* 配色/控件风格参照用户另一套 CAD-MAP 编排器的 QSS（深色 + 蓝色主色 + 橙色强调点） */
 :root{--bg:#131415;--card:#17181a;--field:#1f2124;--log:#101113;--line:#2a2c2e;
       --ink:#DADFE3;--ink2:#B9BEC3;--muted:#727577;--hint:#5c6064;
       --brand:#0432FA;--brand2:#0a46ff;--accent:#F5A800}
 *{box-sizing:border-box}
 body{margin:0;font-family:"Microsoft YaHei","SimHei","Segoe UI",system-ui,sans-serif;
      font-size:13px;background:var(--bg);color:var(--ink)}
 ::-webkit-scrollbar{width:10px;height:10px}
 ::-webkit-scrollbar-thumb{background:#2a2c2e;border-radius:8px}
 ::-webkit-scrollbar-track{background:transparent}
 /* 整页按“一屏放下”收紧：目标 1280×800 不用上下滚 */
 header{background:var(--bg);border-bottom:1px solid var(--line);padding:9px 20px}
 header .logo{color:var(--brand);font-size:23px;font-weight:800;letter-spacing:-1px}
 header h1{margin:0;font-size:15px;font-weight:600}
 header .sub{font-size:11px;color:var(--muted);margin-top:1px}
 #verTxt{color:var(--muted);font-size:11px}
 #updMsg{color:var(--accent);font-size:11px}
 .wrap{display:grid;grid-template-columns:1.05fr 1fr;gap:10px;padding:6px 20px 4px}
 .panel{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px 12px}
 .panel h2{margin:0 0 6px;font-size:14px;font-weight:600;display:flex;align-items:center;gap:6px}
 .panel h2::before{content:"";width:8px;height:8px;border-radius:50%;background:var(--accent);
                   flex:0 0 auto}
 .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px;
       max-height:220px;overflow:auto}
 .bcard{background:var(--field);border:1px solid var(--line);border-radius:8px;padding:6px;
        text-align:center;cursor:pointer;transition:border-color .15s,background .15s}
 .bcard:hover{border-color:var(--brand);background:rgba(4,50,250,.10)}
 .bcard .nm{font-size:11px;margin-top:3px;color:var(--ink)}
 .bcard .badge{display:inline-block;margin-left:5px;padding:0 5px;border-radius:8px;
               font-size:10px;background:rgba(4,50,250,.18);color:#9fc0ff;vertical-align:1px}
 /* 预览框：给 SVG 一个真正的画布尺寸（以前是 max-height:74px，扁的块按比例
    缩完只剩几像素高，看着就是一条糊线） */
 .bcard .thumb{background:#eef1f5;border-radius:6px;padding:4px;display:flex;
        align-items:center;justify-content:center;height:72px;overflow:hidden}
 .bcard .thumb svg{width:100%;height:100%;display:block}
 .chain{display:flex;flex-wrap:wrap;gap:6px;min-height:48px;padding:8px;
        background:var(--log);border:1px dashed var(--line);border-radius:8px}
 .chain>span{color:var(--hint)!important}
 .chip{background:rgba(4,50,250,.15);color:#9fc0ff;border:1px solid rgba(4,50,250,.35);
       border-radius:20px;padding:5px 12px;font-size:13px;display:flex;gap:8px;align-items:center}
 .chip b{cursor:pointer;color:#ff8a80}
 .row{display:flex;gap:8px;align-items:center;margin-top:6px;flex-wrap:wrap}
 .row label{font-size:12px;color:var(--muted)}
 input[type=number],input[type=text],select{background:var(--field);border:1px solid var(--line);
       border-radius:8px;padding:4px 8px;color:var(--ink);font-size:12px;font-family:inherit}
 input[type=number]{width:90px}
 input[type=text]:focus,input[type=number]:focus,select:focus{outline:none;border-color:var(--brand)}
 input[type=checkbox],input[type=radio]{accent-color:var(--brand)}
 button{background:var(--brand);color:#fff;border:0;border-radius:8px;padding:6px 14px;
        font-size:13px;cursor:pointer;font-weight:600;font-family:inherit}
 button:hover{background:var(--brand2)}
 button.ghost{background:rgba(255,255,255,.06);color:var(--ink);font-weight:500;
        border:1px solid rgba(255,255,255,.10)}
 button.ghost:hover{background:rgba(255,255,255,.12)}
 .out{margin-top:12px}
 a.dl{display:inline-block;margin-top:8px;color:#9fc0ff}
 .dlrow{margin-top:10px;display:flex;flex-wrap:wrap;gap:8px;align-items:center}
 .dlrow button{padding:6px 14px;font-size:13px}
 .dlrow a.dl{margin-top:0}
 #log{white-space:pre-wrap;font-size:12px;color:#a8adb2;margin-top:10px;padding:10px 12px;
      background:var(--log);border:1px solid var(--line);border-radius:8px;max-height:260px;overflow:auto;
      font-family:Consolas,"Microsoft YaHei",monospace}
 #progWrap{background:var(--card);border:1px solid var(--line);border-radius:10px;
      padding:10px 12px;margin-top:12px}
 #progTxt{color:var(--ink2)}
 #progLog{background:var(--log);border:1px solid var(--line);border-radius:8px;color:#a8adb2;
      font-family:Consolas,"Microsoft YaHei",monospace}
 .out .dlrow{background:var(--log);border:1px solid var(--line);border-radius:8px;padding:8px 10px}
 /* 语言：一个地球图标，点开才是 中文 / English（原来是个一直占位置的下拉框） */
 .langbox{position:relative}
 #langMenu{position:absolute;right:0;top:112%;z-index:60;background:var(--card);
      border:1px solid var(--line);border-radius:8px;padding:4px;min-width:118px;
      box-shadow:0 10px 24px rgba(0,0,0,.5)}
 #langMenu button{display:block;width:100%;text-align:left;background:transparent;
      color:var(--ink);font-weight:500;padding:7px 10px;border-radius:6px;font-size:13px}
 #langMenu button:hover{background:rgba(255,255,255,.10)}
 #langMenu button.on{color:#9fc0ff;background:rgba(4,50,250,.18)}
 /* 两步走：① 板子 → 点下一步 → ② 线束（同一屏只显示当前这一步） */
 .stepbar{display:flex;align-items:center;gap:10px;padding:5px 20px 0}
 .stepbtn{background:rgba(255,255,255,.06);color:var(--ink2);font-weight:600;
      border:1px solid var(--line);border-radius:20px;padding:5px 15px;font-size:13px}
 .stepbtn:hover{background:rgba(255,255,255,.12)}
 .stepbtn.on{background:var(--brand);border-color:var(--brand);color:#fff}
 .steparrow{color:var(--hint);font-size:15px}
 #stepHint{margin-left:auto;font-size:12px;color:var(--muted)}
 .wrap.one{grid-template-columns:1fr}
 /* 板子实时预览 */
 .prevbox{overflow:auto;max-height:300px}
 .prevbox svg{display:block}
 /* 屏幕更矮（比如 1366×768 的笔记本）时再收紧一档，仍然一屏放下 */
 @media (max-height: 790px){
   header .sub{display:none}
   header{padding:6px 20px}
   .bcard .thumb{height:56px}
   .grid{max-height:180px}
   .bcard .nm{font-size:10px}
   #bhaBox{max-height:86px!important}
   .prevbox svg{height:82px!important}
   .panel{padding:6px 12px}
   .row{margin-top:4px}
 }
</style></head>
<body>
<div id="szBox" style="position:fixed;right:14px;bottom:14px;z-index:99;background:var(--card);
     border:1px solid var(--line);border-radius:10px;padding:8px 10px;display:flex;gap:6px;align-items:center">
  <b style="font-size:13px">方案</b>
  <select id="scheme" style="max-width:190px" onchange="onSchemeChange()"
    title="方案的生成逻辑逐个补；现在只有“串的排法”按方案自动定：带 LYNX 的方案从下往上排，其余从左往右排">
    <option value="Harness">Harness</option>
    <option value="ALEX">ALEX</option>
    <option value="IBEX+AI跳线">IBEX+AI跳线</option>
    <option value="IBEX PLUS">IBEX PLUS</option>
    <option value="IBEX+CU跳线">IBEX+CU跳线</option>
    <option value="LYNX+CU跳线">LYNX+CU跳线</option>
    <option value="LYNX+AI跳线">LYNX+AI跳线</option>
    <option value="LYNX+Harness">LYNX+Harness</option>
    <option value="LYNX+IBEX">LYNX+IBEX</option>
  </select>
</div>
<header>
  <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
    <span class="logo">SL</span>
    <h1 style="flex:0 0 auto">Single line-CAD</h1>
    <div style="flex:1 1 320px;display:flex;align-items:center;gap:10px;flex-wrap:wrap;justify-content:flex-end">
      <span class="langbox">
        <button class="ghost" id="langBtn" onclick="toggleLang(event)"
                title="语言 / Language"
                style="padding:4px 9px;line-height:0;display:inline-flex;align-items:center;gap:6px">
          <morph-icon id="langIcon" size="18" label="Language"></morph-icon>
          <span id="langTag" style="font-size:12px;line-height:1">中</span></button>
        <div id="langMenu" style="display:none">
          <button id="langZh" onclick="setLang('zh')">中文</button>
          <button id="langEn" onclick="setLang('en')">English</button>
        </div>
      </span>
      <span id="verTxt">版本 {{VER}}（{{VERNOTE}}）</span>
      <button class="ghost" id="updBtn" onclick="checkUpdate()"
              title="检查更新 / Check for updates"
              style="padding:4px 10px;font-size:13px;display:inline-flex;align-items:center;gap:6px">
        <morph-icon id="updIcon" size="16" label="Update"></morph-icon><span id="updTag">检查更新</span></button>
      <span id="updMsg"></span>
    </div>
  </div>
  <div class="sub">选块（可重复）→ 组成链 → 生成连完线的产品 · 代码版本 {{VER}}（换过代码要重启窗口，否则跑的还是旧代码）</div>
</header>
<div class="stepbar">
  <button class="stepbtn on" id="stepBtn1" onclick="gotoStep(1)">① 板子</button>
  <span class="steparrow">→</span>
  <button class="stepbtn" id="stepBtn2" onclick="gotoStep(2)">② 线束</button>
  <span id="stepHint">先把板子这一页填好，点下面的“下一步：填线束”</span>
</div>
<div class="wrap one" id="step1">
  <div class="panel"><h2>① 板子 · 块库 + 阵列</h2>
    <div class="grid" id="boardBlocks"></div>
    <div class="row" style="margin-top:10px">
      <label>模式</label>
    <label><input type="radio" name="mode" value="array" checked onchange="setMode('array');schedulePreview()"> 光伏阵列 + 线束（一张图）</label>
    <label><input type="radio" name="mode" value="batch" onchange="setMode('batch');schedulePreview()"> 批量（一行一张 · 每张一个 DXF）</label>
    </div>
    <div class="row" id="arrayRow" style="display:none">
      <!-- 组件块不让用户挑了：三个下拉藏起来，由“哪一端靠近汇流箱”自动决定首/中/尾块 -->
      <span id="modBox" style="display:none">
        <label>组件 首块</label><select id="mod1"></select>
        <label>中间块</label><select id="mod2"></select>
        <label>尾块</label><select id="mod3"></select>
      </span>
      <label>组件朝向</label><select id="polnear" onchange="applyPolarityNear();schedulePreview()"
        title="哪一端靠近汇流箱(CBX)：选正极就把正极出线那端排在汇流箱旁；选负极就整串反过来排，负极那端贴着汇流箱">
        <option value="pos">正极靠近汇流箱</option>
        <option value="neg">负极靠近汇流箱</option>
      </select>
      <label>每串板数</label><input type="number" id="nper" value="20" min="2" step="1">
      <label>串数</label><input type="text" id="nstr" value="4" style="width:92px"
        title="支持分段：4 = 一组 4 串；2+3 / 3+2 = 支架两侧各多少串，段与段之间走“跨支架距离”">
      <label>跨支架距离</label><input type="number" id="brkgap" value="30" step="1"
        title="串数写成分段（如 2+3）时，支架两侧之间的固定距离">
      <label>板间净空</label><input type="number" id="gapx" value="1" step="1"
        title="同一串里，板与板之间的净空">
      <label>串间净空</label><input type="number" id="gapy" value="2" step="1"
        title="串与串之间的净空；留空 = 跟板间净空一样（贴紧）。默认 2（贴紧排，跟板间净空一个口径）">
      <label>串的排法</label><select id="dir">
        <option value="right">从左往右接</option>
        <option value="down">从上往下叠</option></select>
      <label>起始块</label><select id="headblk" style="max-width:130px"
        title="摆在阵列最左边、与板子固定距离的块（默认 CBX，从**板子块库**里选）"></select>
      <label>起始块间距</label><input type="number" id="headgap" value="60" step="5">
      <label><input type="checkbox" id="enlarge"> 允许放大到占满</label>
    </div>
    <div class="row" id="bhaRow" style="display:none;flex-direction:column;align-items:stretch">
      <div class="row" style="margin-top:0;align-items:center;gap:8px">
        <b>电机 / BHA 桩位置</b>
        <button class="ghost" onclick="addBha()">加一处</button>
        <button class="ghost" onclick="addBhaMid()" title="按整排总块数取中间：4 串 × 20 块 → 第 40 块之后">整排中间插一处</button>
        <button class="ghost" onclick="addBhaSeg()" title="每个支架（每段）中点各一处，串数写 3+3 时就是前后各一个">每段中点插一处</button>
        <button class="ghost" onclick="clearBha()">清空</button>
        <span id="bhaHint" style="font-size:12px;color:var(--muted)">
          两块板之间插一个 BHA 桩块（可再挂电机）；位置 = 整排第几块之后（不分串）：4 串 × 20 块共 80 块，填 40 就是正中间；0 或留空 = 最前面；写“每段” = 每段中点各一处。留空整列 = 不插桩。
          桩块/电机块从块库里选；块库里还没有 BHA 桩块时，可以先用电机块（MOTOR）顶 —— 插进去照样按桩算净空、它右边的板整体右移。</span>
      </div>
      <div id="bhaBox" style="max-height:118px;overflow:auto;border:1px solid var(--line);border-radius:8px">
        <table style="width:100%;border-collapse:collapse;font-size:13px">
          <thead><tr>
            <th style="text-align:left;padding:6px">串数</th>
            <th style="text-align:left;padding:6px">整排第几块之后</th>
            <th style="text-align:left;padding:6px">BHA 桩块</th>
            <th style="text-align:left;padding:6px">电机块</th>
            <th style="text-align:left;padding:6px">电机旋转</th>
            <th style="text-align:left;padding:6px">桩左净空</th>
            <th style="text-align:left;padding:6px">桩右净空</th>
            <th style="text-align:left;padding:6px">备注</th><th></th>
          </tr></thead>
          <tbody id="bhaBody"></tbody>
        </table>
      </div>
    </div>
    <div class="row" id="batchRow" style="display:none;flex-direction:column;align-items:stretch">
      <div class="row" style="margin-top:0">
        <label>份数</label><input type="number" id="bcount" value="3" min="1" max="60" step="1" style="width:70px">
        <label>起始串数</label><input type="number" id="bstart" value="3" min="1" step="1" style="width:70px">
        <label>每张 +</label><input type="number" id="bstep" value="1" step="1" style="width:70px">
        <label>每串板数</label><input type="number" id="bnper" value="20" min="1" step="1" style="width:80px">
        <label>起始图号</label><input type="text" id="bno" value="SLD-001" style="width:110px">
        <button class="ghost" onclick="fillBatch()">按上面参数铺出 N 行</button>
        <button class="ghost" onclick="clearBatch()">清空行</button>
      </div>
      <div style="max-height:130px;overflow:auto;border:1px solid var(--line);border-radius:8px">
        <table style="width:100%;border-collapse:collapse;font-size:13px">
          <thead><tr>
            <th style="text-align:left;padding:6px">图号</th>
            <th style="text-align:left;padding:6px">外框图</th>
            <th style="text-align:left;padding:6px">串数</th>
            <th style="text-align:left;padding:6px">每串板数</th>
            <th style="text-align:left;padding:6px">主线线号</th>
            <th style="text-align:left;padding:6px">支线线号</th>
            <th style="text-align:left;padding:6px">BHA位置</th>
            <th style="text-align:left;padding:6px">备注</th><th></th>
          </tr></thead>
          <tbody id="bBody"></tbody>
        </table>
      </div>
      <div class="row" style="margin-top:0">
        <label><input type="checkbox" id="bsheet"> 拼成一张图纸</label>
        <span style="font-size:12px;color:var(--muted)">一行 = 一张图（各调一份新外框模板，互相独立）：默认
          <b>每张各自出一个 DXF</b>（＋一个打包 zip）；勾上“拼成一张图纸”才全部拼进同一个 DXF。
          勾了“直接画到 CAD”时：连着画的这些图<b>不清</b>前面的，都留在同一张 DWG 里、
          位置自动错开（同一列先从上往下排，排满了往右挪一列）。
          填完点“下一步：填线束”，在线束页按“生成”一次画完。
          每行的<b>BHA位置</b>留空 = 沿用“光伏阵列”模式下那张桩表。</span>
      </div>
    </div>
    <div class="row" id="sheetRow" style="display:none">
      <label>每行放</label><input type="number" id="sheetc" value="2" min="1" max="20" step="1" style="width:60px"
        title="拼成一张图纸时每行放几张；连着画到 CAD 时也按这个数——同一列先从上往下排这么多张，排满了往右挪一列">
      <label>图间距 X</label><input type="number" id="sheetgx" value="300" step="50" style="width:80px"
        title="图与图之间的左右净空（拼图排格子、连着画到 CAD 都用它）">
      <label>图间距 Y</label><input type="number" id="sheetgy" value="300" step="50" style="width:80px"
        title="图与图之间的上下净空（拼图排格子、连着画到 CAD 都用它）">
      <label>排法</label><select id="sheetorder" style="max-width:260px">
        <option value="row">先横后竖（左→右，然后下一行）</option>
        <option value="col">先竖后横（上→下，然后下一列）</option>
      </select>
    </div>
    <div class="row" style="margin-top:14px">
      <button onclick="nextStep()">下一步：填线束 →</button>
      <span style="font-size:12px;color:var(--muted)">
        板子这一页填完就可以进线束那一页；想回来改，点上面的“① 板子”或线束页的“← 上一步”。</span>
    </div>
    <div class="row" style="margin-top:14px;flex-direction:column;align-items:stretch">
      <div style="display:flex;align-items:center;gap:10px">
        <b>板子预览（实时）</b>
        <span id="prevHint" style="font-size:12px;color:var(--muted)">
          改参数即时重画（缩到一屏）；串按<b>段</b>标注（2+3 → “2 串”“3 串”）。
          <b style="color:#c0392b">红</b>=首块、
          <b style="color:#8a8f94">灰</b>=中间块、
          <b style="color:#2e7d32">绿</b>=尾块、
          <b style="color:#1466c8">蓝</b>=BHA 桩、
          <b style="color:#8e44ad">紫</b>=电机。</span>
        <button class="ghost" style="margin-left:auto" onclick="doPreview()">刷新</button>
      </div>
      <div id="arrPrev" class="prevbox"
           style="background:#fff;border-radius:8px;min-height:130px;padding:8px">
        <div style="color:#727577;font-size:12px">（填完组件和串数就会出现预览）</div>
      </div>
    </div>
  </div>
</div>

<div class="wrap one" id="step2" style="display:none">
  <div class="panel"><h2>② 线束 · 块库 + 连线（留空也能排：末端母头 + 正极支线×(串数-1) + 末端公头）</h2>
    <div class="grid" id="harnessBlocks"></div>
    <div class="chain" id="chain"><span style="color:#aab">点上面的线束块加入…</span></div>
    <div class="row" id="harnRow">
      <label>正极支线块</label><select id="posfeed"><option value="">（选链里的块）</option></select>
      <label>负极支线块</label><select id="negfeed"><option value="">（不指定）</option></select>
      <label>末端公头块</label><input type="text" id="posplug" value="Male" style="width:80px"
             title="填了就自动补到链尾、顶最后一串的正极；想让它排在头部就把这里清空、自己放进链里">
      <label>末端母头块</label><input type="text" id="negplug" value="Fmale" style="width:80px"
             title="填了才会自动生成负极那一行（头部接头 + 中间负极支线 + 末端母头）">
      <label>主线线号</label><select id="awgmain" style="width:110px"
        title="主线 = 正极/负极支线块之间连的线 + 第一个接头到第一根正极/负极支线之间的连线；标的就是这个线号（图上标注一律写成 #线号，如 2/0 AWG → #2/0、10 AWG → #10）">
        <option>750 MCM</option>
        <option>500 MCM</option>
        <option selected>2/0 AWG</option>
        <option>4 AWG</option>
        <option>6 AWG</option>
        <option>8 AWG</option>
        <option>10 AWG</option>
        <option value="">（不指定）</option>
      </select>
      <label>支线线号</label><select id="awgbranch" style="width:100px"
        title="支线 = 正极/负极支线块 + 最后一根支线块到公头/母头之间的连线；标的就是这个线号（图上标注一律写成 #线号，如 10 AWG → #10）">
        <option selected>10 AWG</option>
        <option>12 AWG</option>
        <option value="">（不指定）</option>
      </select>
      <label>线号标注</label><select id="annot"
        title="text=普通文字（最稳）；shape=画成标注外观（尺寸线/界线/箭头，普通实体，任何 CAD 都能开）；dim=CAD 原生 DIMENSION（可拖动关联，但 ZWCAD 2025 会判无效）">
        <option value="text">文字</option>
        <option value="shape">标注外观（普通实体）</option>
        <option value="dim">CAD 原生标注(DIMENSION)</option>
      </select>
      <label>线束缩放</label><input type="number" id="hscale" value="1" step="0.1" min="0.05">
      <label>块固定间距</label><input type="number" id="fixgap" value="30" step="5"
             title="除“正极支线/负极支线/公头/母头”（这四个按板子接点定位）以外，
                    其余块（FUSE、CU-AL、起始块…）之间统一的固定间距">
      <label>负极行间距</label><input type="number" id="neggap" value="30" step="5"
             title="负极行和正极行的净空；负极支线块是竖的，程序会自动把它的身子让出来（行线再往下挪一个块高），不会压住正极行">
      <label><input type="checkbox" id="link"> 画阵列↔线束跨接线</label>
      <label><input type="checkbox" id="hspan" checked> 线束接点对齐缩放</label>
      <label><input type="checkbox" id="pts"> 画连接点(POINT)</label>
      <label>间隔 GAP</label><input type="number" id="gap" value="40" step="5">
      <label>外框图</label><select id="frame" onchange="onFrameChange()"><option value="">不用</option></select>
      <label><input type="checkbox" id="tocad" title="画进 CAD 里那张外框图上；连着生成几张时不删前面的图，一张一张错开排（同一列先从上往下，排满了往右一列）"> 直接画到 CAD(COM)</label>
      <button onclick="gen()">生成</button>
      <button class="ghost" onclick="gotoStep(1)">← 上一步</button>
      <button class="ghost" onclick="clearChain()">清空</button>
    </div>
    <div class="out" id="out"></div>
      <div id="progWrap" style="display:none">
        <div style="height:8px;background:rgba(255,255,255,.12);border-radius:8px;overflow:hidden">
          <div id="progBar" style="height:100%;width:0%;background:var(--brand);border-radius:8px;transition:width .25s"></div>
        </div>
        <div id="progTxt" style="font-size:13px;margin-top:6px;font-weight:600"></div>
        <pre id="progLog" style="display:none;margin:8px 0 0;padding:8px 10px;max-height:170px;
             overflow:auto;font-size:12px;line-height:1.5;white-space:pre-wrap"></pre>
      </div>
  </div>
</div>
<script>
// ==================== 中英文切换 ====================
// 程序和 HTML 里写的都是中文；英文按“中文原句”查表翻。表在 i18n.py 里，
// 页面加载时由服务端注入成 EN_TEXT。表里没有的原样显示 —— 少写一条也不会坏。
let LANG='{{LANG}}';
const EN_TEXT={{I18N_EN}};
const _ZH=/[\u4e00-\u9fff]/;
let _rules=null;
const _zhText=new WeakMap(), _zhAttr=new WeakMap();
function _escRe(s){return s.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');}
function _buildRules(){
  _rules=[];
  const spec=/%(?:[-+ #0]*\d*(?:\.\d+)?[diouxXeEfFgGscr])/g;
  Object.keys(EN_TEXT).forEach(function(k){
    if(k.indexOf('%')<0) return;                      // 没有占位符的走精确匹配
    const parts=k.replace(/%%/g,'\u0000').split(spec);
    const pat='^'+parts.map(_escRe).join('(.*?)').replace(/\u0000/g,'%')+'$';
    _rules.push({re:new RegExp(pat,'s'), en:EN_TEXT[k]});
  });
  // 长的先试：不然“线束缩放: %s”会先把“线束缩放: %s（…）”那句截胡
  _rules.sort(function(a,b){return b.re.source.length-a.re.source.length;});
}
// 翻一句：整句匹配；带换行的按行翻；没中文的直接还回去。翻不出来返回 null
function _T1(s,d){
  if(_rules===null) _buildRules();
  if(EN_TEXT[s]!==undefined) return EN_TEXT[s];
  for(let i=0;i<_rules.length;i++){
    const m=_rules[i].re.exec(s);
    if(!m) continue;
    let n=0;
    return _rules[i].en.replace(/\{(\d*)\}/g,function(_,idx){
      const gi=(idx==='')?(n++):parseInt(idx);
      const raw=m[gi+1];
      if(raw===undefined) return '';
      return (d<2)?T(raw,d+1):raw;    // 参数里还有中文（“第 1/3 张 · 准备”）就接着翻
    });
  }
  return null;
}
function T(s,depth){
  if(LANG==='zh'||s==null) return s;
  s=String(s);
  if(s.indexOf('\n')>=0) return s.split('\n').map(function(x){return T(x,depth);}).join('\n');
  if(!_ZH.test(s)) return s;
  const d=depth||0;
  let out=_T1(s,d);
  if(out!==null) return out;
  const t=s.trim();                    // HTML 排版留下的缩进/空格：去掉空白再试一次
  if(t && t!==s){
    out=_T1(t,d);
    if(out!==null) return s.replace(t,out);
  }
  return s;                            // 表里没有：保持中文，不乱改
}
// 把一段 DOM 的文字和悬停提示按当前语言刷一遍（记下原中文，切回来能还原）
const _SKIP={SCRIPT:1,STYLE:1,SVG:1,TEXTAREA:1};
function RT(root){
  root=root||document.body;
  if(!root) return;
  function doText(n){
    const p=n.parentNode;
    if(!p||_SKIP[p.nodeName]) return;
    let zh=_zhText.get(n);
    if(zh===undefined){ zh=n.nodeValue; _zhText.set(n,zh); }
    const want=(LANG==='en')?T(zh):zh;
    if(n.nodeValue!==want) n.nodeValue=want;
  }
  function doElem(e){
    if(_SKIP[e.nodeName]) return;
    if(e.hasAttribute){
      ['title','placeholder'].forEach(function(a){
        if(!e.hasAttribute(a)) return;
        let box=_zhAttr.get(e);
        if(!box){ box={}; _zhAttr.set(e,box); }
        if(box[a]===undefined) box[a]=e.getAttribute(a);
        const want=(LANG==='en')?T(box[a]):box[a];
        if(e.getAttribute(a)!==want) e.setAttribute(a,want);
      });
    }
    if(e.id==='langMenu'||e.id==='langBtn') return;      // 语言菜单自己不翻
    for(let c=e.firstChild;c;c=c.nextSibling){
      if(c.nodeType===3) doText(c);
      else if(c.nodeType===1) doElem(c);
    }
  }
  if(root.nodeType===3) doText(root);
  else if(root.nodeType===1) doElem(root);
}
function setLang(v){
  LANG=(v==='en')?'en':'zh';
  const sel=document.getElementById('langSel');
  if(sel) sel.value=LANG;
  const lm=document.getElementById('langMenu');
  if(lm) lm.style.display='none';
  langMark();
  document.documentElement.lang=(LANG==='en')?'en':'zh';
  RT(document.body);
  try{ fetch('/api/lang?set='+LANG); }catch(e){}       // 记住选择，下次开窗还是这个语言
}
// 语言菜单：点地球图标展开，点别处收起；当前语言高亮
function toggleLang(ev){
  if(ev){ev.stopPropagation();}
  const m=document.getElementById('langMenu');
  if(!m) return;
  m.style.display=(m.style.display==='block')?'none':'block';
  langMark();
}
function langMark(){
  const zh=document.getElementById('langZh'), en=document.getElementById('langEn');
  if(zh) zh.className=(LANG==='zh')?'on':'';
  if(en) en.className=(LANG==='en')?'on':'';
}
document.addEventListener('click',function(){
  const m=document.getElementById('langMenu'); if(m) m.style.display='none';
});

let chain=[];
let lastOut={dxf:'', csv:''};   // 最近一次生成的文件名（桌面窗口的“保存/打开”要用）
let framesReady=false;
let lastBlocks=[];      // 块库里所有块名（给 BHA 桩/电机下拉用）
let boardBlocks=[];     // 板子块库（画板子/阵列用）
let harnessBlocks=[];   // 线束块库（组成线束链用）
let mode='array';
let frameList=[];       // 外框图列表（批量模式的外框图下拉要用）
let batchRows=[];       // 批量模式：一行 = 一张图（各自独立参数）
let bhaRows=[];         // 电机/BHA 桩：一行 = 一处插入（整排第几块之后 / 桩块 / 电机块 …）
const AWG_MAIN=['750 MCM','500 MCM','2/0 AWG','4 AWG','6 AWG','8 AWG','10 AWG'];
const AWG_BRANCH=['10 AWG','12 AWG'];
function setMode(m){
  mode=(m==='batch')?'batch':'array';
  document.getElementById('arrayRow').style.display='flex';
  // 批量模式下这张桩表**照样显示**（只在批量时压矮一点）：它是给所有批量行用的
  // “默认一套” —— 每行的“BHA位置”那格留空就沿用它，填了就只按那一行自己的来。
  // （以前这里把整张表藏起来，结果批量页里根本找不到地方加电机/BHA。）
  document.getElementById('bhaRow').style.display='flex';
  document.getElementById('batchRow').style.display=(mode==='batch')?'flex':'none';
  document.getElementById('sheetRow').style.display=(mode==='batch')?'flex':'none';
  // 批量模式内容多，把两行说明收起来，保证整页还是“一屏放下”（不用上下滚）
  ['bhaHint','prevHint'].forEach(function(id){
    const e=document.getElementById(id); if(e) e.style.display=(mode==='batch')?'none':'';
  });
  const bb=document.getElementById('bhaBox');      // 批量页内容多，桩表压矮一点
  if(bb) bb.style.maxHeight=(mode==='batch')?'64px':'118px';
  RT();
}

// ---------- 两步走：① 板子 → 下一步 → ② 线束 ----------
let step=1;
function gotoStep(n){
  step=(n===2)?2:1;
  const s1=document.getElementById('step1'), s2=document.getElementById('step2');
  if(s1) s1.style.display=(step===1)?'grid':'none';
  if(s2) s2.style.display=(step===2)?'grid':'none';
  const b1=document.getElementById('stepBtn1'), b2=document.getElementById('stepBtn2');
  if(b1) b1.className='stepbtn'+(step===1?' on':'');
  if(b2) b2.className='stepbtn'+(step===2?' on':'');
  const h=document.getElementById('stepHint');
  if(h) h.textContent=T(step===1?
      '先把板子这一页填好，点下面的“下一步：填线束”':
      '这一页填线束（线号、支线块、末端接头），填完按“生成”');
  RT(document.querySelector('.stepbar'));
  try{ window.scrollTo(0,0); }catch(e){}
  if(step===1) schedulePreview();     // 回到板子页就把预览刷新一下
}
// 进线束页之前做个轻检查：板子关键项没填就先提醒一下
function nextStep(){
  const m1=document.getElementById('mod1'), m3=document.getElementById('mod3');
  if(m1 && !m1.value){ alert(T('板子：先选“组件 首块”')); return; }
  if(m3 && !m3.value){ alert(T('板子：先选“尾块”')); return; }
  const ns=document.getElementById('nstr');
  if(ns && !String(ns.value||'').trim()){ alert(T('板子：串数没填')); return; }
  gotoStep(2);
}

// ---------- 板子实时预览 ----------
// 改任何一个板子参数 -> 350ms 防抖 -> 调 /api/preview_array -> 换掉预览。
// 后端只算布局（不读外框图、不打包、不出 DXF），所以是毫秒级的。
let prevTimer=null, prevSeq=0;
const PREV_IDS=['scheme','mod1','mod2','mod3','nper','nstr','brkgap','gapx','gapy',
                'dir','headblk','headgap'];
function bindPreview(){
  // 最外层兜底：板子这一页里**任何**输入/下拉改了都重画预览。
  // 电机/BHA 桩那张表是动态生成的行，按固定 id 绑不住（以前就是漏了这里，
  // 所以“桩插在第几块之后”改了、预览却一直停在原地不动）。
  const s1=document.getElementById('step1');
  if(s1){
    s1.addEventListener('input',schedulePreview);
    s1.addEventListener('change',schedulePreview);
  }
  PREV_IDS.forEach(function(id){
    const e=document.getElementById(id); if(!e) return;
    e.addEventListener('input',schedulePreview);
    e.addEventListener('change',schedulePreview);
  });
}
function schedulePreview(){
  if(step!==1) return;
  if(prevTimer) clearTimeout(prevTimer);
  prevTimer=setTimeout(doPreview, 350);
}
async function doPreview(){
  applyPolarityNear();          // 组件朝向 -> 首/中/尾块（藏起来的三个下拉）
  if(prevTimer){clearTimeout(prevTimer);prevTimer=null;}
  if(mode==='batch'){ return doPreviewBatch(); }     // 批量：按表格每一行各画一张
  const box=document.getElementById('arrPrev'); if(!box) return;
  const my=++prevSeq;
  box.innerHTML='<div style="color:#727577;font-size:12px;padding:6px">更新中…</div>';
  const body={scheme:v('scheme','Harness'),
              module_first:v('mod1',''), module_mid:v('mod2',''), module_last:v('mod3',''),
              n_per:parseInt(v('nper',20))||20,
              n_strings:String(v('nstr','4')||'4').trim(),
              bracket_gap:parseFloat(v('brkgap',4))||4,
              gap_x:parseFloat(v('gapx',1))||0,
              // 串间净空：界面上单独一格；留空 = 跟随板间净空
              gap_y:((String(v('gapy','')).trim()==='')?null:(parseFloat(v('gapy',2))||0)),
              dir:v('dir','right'),
              head_block:String(v('headblk','CBX')||'').trim(),
              head_gap:parseFloat(v('headgap',60))||60,
              bha:bhaPayload()};
  try{
    const r=await fetch('/api/preview_array',{method:'POST',
              headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const d=await r.json();
    if(my!==prevSeq) return;                  // 后发的请求已经把这次挤掉了，丢弃
    box.innerHTML=(d.svg||'<div style="color:#727577;font-size:12px;padding:6px">'
                  +T(d.info||'没有预览')+'</div>')
                 + (d.svg&&d.info? '<div style="font-size:12px;color:#727577;padding:6px 2px">'
                    +T(d.info)+'</div>' : '');
    RT(box);
  }catch(e){
    if(my===prevSeq)
      box.innerHTML='<div style="color:#e57373;font-size:12px;padding:6px">预览失败：'
                    +e+'</div>';
  }
}

// 批量模式的预览：一行一张小图（用那一行自己的串数/板数），最多画前 8 行
async function doPreviewBatch(){
  applyPolarityNear();
  const box=document.getElementById('arrPrev'); if(!box) return;
  const my=++prevSeq;
  const rows=batchRows.slice(0,8);
  if(!rows.length){
    box.innerHTML='<div style="color:#727577;font-size:12px;padding:6px">'
      +T('批量表还是空的：先填份数，点“按上面参数铺出 N 行”')+'</div>';
    RT(box); return;
  }
  box.innerHTML='<div style="color:#727577;font-size:12px;padding:6px">更新中…</div>';
  const out=[];
  for(let i=0;i<rows.length;i++){
    const r=rows[i];
    const body={scheme:v('scheme','Harness'),
                module_first:v('mod1',''), module_mid:v('mod2',''), module_last:v('mod3',''),
                n_per:parseInt(r._nperEl?r._nperEl.value:r.n_per)||parseInt(v('nper',20))||20,
                n_strings:String((r._nstrEl?r._nstrEl.value:r.n_str)||v('nstr','4')||'4').trim(),
                bracket_gap:parseFloat(v('brkgap',4))||4,
                gap_x:parseFloat(v('gapx',1))||0,
                gap_y:((String(v('gapy','')).trim()==='')?null:(parseFloat(v('gapy',2))||0)),
                dir:v('dir','right'),
                head_block:String(v('headblk','CBX')||'').trim(),
                head_gap:parseFloat(v('headgap',60))||60,
                bha:(String(r.bha||'').trim()||bhaPayload())};
    try{
      const rr=await fetch('/api/preview_array',{method:'POST',
                 headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
      const d=await rr.json();
      if(my!==prevSeq) return;                 // 后面的请求已经把这次挤掉
      out.push('<div style="border-top:1px solid #2a2c2e;padding:5px 2px">'+
        '<div style="font-size:12px;color:#B9BEC3">'+(r.no||('SLD-'+pad3(i+1)))+
        ' · '+T('串数')+' '+String((r._nstrEl?r._nstrEl.value:r.n_str)||'')+
        ' · '+T('每串板数')+' '+String((r._nperEl?r._nperEl.value:r.n_per)||'')+'</div>'+
        (d.svg||('<div style="color:#727577;font-size:12px">'+T(d.info||'没有预览')+'</div>'))+
        '</div>');
    }catch(e){
      out.push('<div style="color:#e57373;font-size:12px;padding:4px">预览失败：'+e+'</div>');
    }
  }
  if(my!==prevSeq) return;
  box.innerHTML=out.join('')+(batchRows.length>rows.length
    ? '<div style="font-size:12px;color:#727577;padding:6px 2px">'
      +T('（还有 '+(batchRows.length-rows.length)+' 行没画，预览最多显示前 8 行）')+'</div>' : '');
  RT(box);
}
async function loadBlocks(){
  const fs=document.getElementById('frame');
  const fr=fs.value;
  const r=await fetch('/api/blocks'+(fr?('?frame='+encodeURIComponent(fr)):'')); const d=await r.json();
  if(!framesReady){
    fs.innerHTML='<option value="">不用</option>';
    frameList=(d.frames||[]).slice();
    (d.frames||[]).forEach(f=>{ const o=document.createElement('option'); o.value=f; o.textContent=f; fs.appendChild(o); });
    framesReady=true;
    if((d.frames||[]).length){ fs.value=d.frames[0]; return loadBlocks(); }
  }
  const picks=[['mod1','PV-POS'],['mod2','MIDDLE-PV'],['mod3','END-NEG']];
  const all=(d.blocks||[]);
  lastBlocks=all.map(b=>b.name);
  // 两块库分开：画板子的块归 ①板子，画线束的块归 ②线束（后端 /api/blocks 给 group）
  boardBlocks=all.filter(b=>b.group==='board');
  harnessBlocks=all.filter(b=>b.group!=='board');
  picks.forEach(([id,def])=>{
    const ms=document.getElementById(id);
    if(!ms) return;
    const old=ms.value;
    ms.innerHTML='';
    boardBlocks.forEach(b=>{ const o=document.createElement('option'); o.value=b.name; o.textContent=b.name; ms.appendChild(o); });
    const want=[...ms.options].find(o=>o.value===def);
    if(old && [...ms.options].some(o=>o.value===old)) ms.value=old;
    else if(want) ms.value=def;
  });
  // “起始块”从板子块库里选（默认 CBX）
  const hb=document.getElementById('headblk');
  if(hb){
    const old=hb.value;
    hb.innerHTML='';
    const o0=document.createElement('option'); o0.value=''; o0.textContent='（不用）'; hb.appendChild(o0);
    boardBlocks.forEach(b=>{const o=document.createElement('option');o.value=b.name;o.textContent=b.name;hb.appendChild(o);});
    const has=(x)=>[...hb.options].some(o=>o.value===x);
    hb.value=(old && has(old))?old:(has('CBX')?'CBX':'');
  }
  renderLib('boardBlocks', boardBlocks, false);
  renderLib('harnessBlocks', harnessBlocks, true);
  // 启动时就把“正极支线块 / 负极支线块”下拉填上。
  // 以前这里没调 renderChain()，那两个下拉要等你**先点一个块**才会有内容 ——
  // 看着就是“下拉里什么都没有”。
  renderChain();
  renderBha();                      // BHA 桩块/电机块下拉从整个块库里选
  RT();
  schedulePreview();                // 块库到位了，把板子预览先画一张
}
// 画一块库：clickable=true 的块点一下加进线束链；板子块只用来在上面几个下拉里选
function renderLib(id, list, clickable){
  const g=document.getElementById(id); if(!g) return;
  g.innerHTML='';
  (list||[]).forEach(b=>{
    const c=document.createElement('div'); c.className='bcard';
    const badge=(b.src==='lib')?'<span class="badge" title="来自块库，生成时自动并入外框">库</span>':'';
    c.innerHTML='<div class="thumb">'+(b.svg||'')+'</div>'+'<div class="nm">'+b.name+badge+'</div>';
    if(clickable){ c.onclick=()=>{chain.push(b.name); renderChain();}; }
    else{
      c.style.cursor='default';
      c.title='板子块：在上面的“组件 首块 / 中间块 / 尾块 / 起始块”里选';
    }
    g.appendChild(c);
  });
}
function onFrameChange(){ chain=[]; renderChain(); loadBlocks(); }
function renderChain(){
  const c=document.getElementById('chain');
  if(!chain.length){c.innerHTML='<span style="color:#aab">点上面的线束块加入…</span>';}
  else{
    c.innerHTML='';
    chain.forEach((n,i)=>{
      const d=document.createElement('div'); d.className='chip';
      d.innerHTML=n+' <b title="移除">×</b>';
      d.querySelector('b').onclick=()=>{chain.splice(i,1);renderChain();};
      c.appendChild(d);
    });
  }
  // “正极/负极支线块”从**线束块库**里选（不是只从链里选，否则没进链的块就没法指定）
  const uniq=[...new Set(chain)];
  const lib=[...new Set(uniq.concat((harnessBlocks||[]).map(b=>b.name)))];
  [['posfeed','（选块）'],['negfeed','（不指定）']].forEach(function(pair){
    const id=pair[0], blank=pair[1];
    const s=document.getElementById(id); if(!s) return;
    const old=s.value;
    s.innerHTML='<option value="">'+blank+'</option>';
    lib.forEach(function(n){const o=document.createElement('option');o.value=n;o.textContent=n;s.appendChild(o);});
    if(old && lib.includes(old)) s.value=old;
    else if(id==='posfeed' && lib.includes('POS')) s.value='POS';
    else if(id==='negfeed' && lib.includes('NEG')) s.value='NEG';
  });
  RT();
}
function clearChain(){chain=[];renderChain();document.getElementById('out').innerHTML='';}
// 取输入值：元素不存在（多半是浏览器缓存了旧页面）就返回默认值，绝不抛错
function v(id, dft){ const e=document.getElementById(id); return e? e.value : (dft===undefined?'':dft); }
function ck(id, dft){ const e=document.getElementById(id); return e? e.checked : !!dft; }
let progTimer=null;
function progStart(txt){
  document.getElementById('progWrap').style.display='block';
  document.getElementById('progBar').style.width='0%';
  document.getElementById('progTxt').textContent=txt||'开始…';
  RT(document.getElementById('progWrap'));
  const _pl=document.getElementById('progLog');
  if(_pl){_pl.style.display='none';_pl.textContent='';}
  if(progTimer) clearInterval(progTimer);
  progTimer=setInterval(async ()=>{
    try{
      const r=await fetch('/api/progress'); const d=await r.json();
      document.getElementById('progBar').style.width=(d.pct||0)+'%';
      document.getElementById('progTxt').textContent=(d.pct||0)+'%  '+(d.stage||'');
      const lg=document.getElementById('progLog');
      if(lg && d.tail && d.tail.length){
        lg.style.display='block';
        lg.textContent=d.tail.join('\n');
        lg.scrollTop=lg.scrollHeight;
      }
      RT(document.getElementById('progWrap'));
    }catch(e){}
  },200);
}
function progStop(finalText){
  if(progTimer){clearInterval(progTimer);progTimer=null;}
  document.getElementById('progBar').style.width='100%';
  document.getElementById('progTxt').textContent=finalText||'完成';
  setTimeout(()=>{document.getElementById('progWrap').style.display='none';},1500);
  RT(document.getElementById('progWrap'));
}
// 组件朝向：哪一端靠近汇流箱(CBX)。
// before = {'pos': 正极靠近（默认，块序 PV-POS → MIDDLE-PV → END-NEG）,
//           'neg': 负极靠近（块序反过来，负极那端排到左边贴着 CBX）}
const POL_BLOCKS={pos:{f:'PV-POS',m:'MIDDLE-PV',l:'END-NEG'},
                  neg:{f:'END-NEG',m:'MIDDLE-PV',l:'PV-POS'}};
function applyPolarityNear(){
  const sel=document.getElementById('polnear'); if(!sel) return;
  const b=POL_BLOCKS[sel.value]||POL_BLOCKS.pos;
  const set=(id,val)=>{
    const e=document.getElementById(id); if(!e) return;
    if(!Array.prototype.some.call(e.options,function(o){return o.value===val;})){
      const o=document.createElement('option'); o.value=val; o.textContent=val; e.appendChild(o);
    }
    e.value=val;
  };
  set('mod1',b.f); set('mod2',b.m); set('mod3',b.l);
}
// 阵列模式的一组公共参数（阵列模式与批量模式共用；批量模式每行再覆盖串数/板数）
function arrayCommon(){
  applyPolarityNear();          // 生成前再对齐一次，保证和“组件朝向”一致
  return {harness:chain, gap:parseFloat(v('gap',40))||40,
          scheme:v('scheme','Harness'),
          module_first:v('mod1',''), module_mid:v('mod2',''), module_last:v('mod3',''),
          n_per:parseInt(v('nper',20))||20,
          // 串数允许写成分段：4 / 2+3 / 3+2（段间走“跨支架距离”）
          n_strings:String(v('nstr','4')||'4').trim(),
          bracket_gap:parseFloat(v('brkgap',4))||4,
          gap_x:parseFloat(v('gapx',1))||0,
          gap_y:((String(v('gapy','')).trim()==='')?null:(parseFloat(v('gapy',2))||0)),
          dir:v('dir','right'),
          harness_scale:parseFloat(v('hscale',1))||1,
          fixed_gap:parseFloat(v('fixgap',30))||30,
          head_block:String(v('headblk','CBX')||'').trim(),
          head_gap:parseFloat(v('headgap',60))||60,
          // 负极支线块不再填角度：程序按“接线头对准板子负极”自动摆
          neg_rotate:0,
          neg_gap:parseFloat(v('neggap',30))||30,
          pos_plug:String(v('posplug','Male')||'').trim(),
          neg_plug:String(v('negplug','Fmale')||'').trim(),
          link_array:ck('link'),
          match_span:ck('hspan',true),
          draw_points:ck('pts'),        // 默认不画：图里不需要点
          pos_feeder:String(v('posfeed','')||'').trim(),
          neg_feeder:String(v('negfeed','')||'').trim(),
          awg_main:String(v('awgmain','')||'').trim(),
          awg_branch:String(v('awgbranch','')||'').trim(),
          annot:v('annot','text')||'text',
          allow_enlarge:ck('enlarge'),
          // 连着画到 CAD 时的落点：一列几张 + 图间距（拼图排格子也用这几个数）
          cols:parseInt(v('sheetc',2))||2,
          gap_sheet_x:parseFloat(v('sheetgx',300))||0,
          gap_sheet_y:parseFloat(v('sheetgy',300))||0,
          bha:bhaPayload()};
}
// 方案的生成逻辑逐个补；现在只有“串的排法”按方案自动定：
// 带 LYNX 的方案从下往上排，其余方案从左往右排。
function onSchemeChange(){
  const s=document.getElementById('scheme'); if(!s) return;
  const d=document.getElementById('dir'); if(!d) return;
  d.value=(String(s.value).toUpperCase().indexOf('LYNX')>=0)?'down':'right';
}

// ---------- 电机 / BHA 桩位置（一行 = 一处插入） ----------
// 在两块板之间插一个 BHA 桩块（可以再挂一个电机块）。位置填**整排第几块之后**
// （整个阵列连续数、不分串：4 串 × 20 块填 40 = 正中间），也可以写“每段”。
// 桩右边的板整体右移、阵列自动变长（位移 = 桩宽 + 左净空 + 右净空 - 板间净空）。
function addBha(pos,stub,motor,rot,gl,gr,note,nstr){
  bhaRows.push({nstr:(nstr===undefined?'':String(nstr)),
                pos:(pos===undefined?'':String(pos)),
                stub:stub||'', motor:motor||'',
                rot:(rot===undefined?'':String(rot)),
                gap_l:(gl===undefined?'':String(gl)),
                gap_r:(gr===undefined?'':String(gr)), note:note||''});
  renderBha();
}
function bhaTotalBlocks(){               // 整排总块数 = 各段串数之和 × 每串板数
  const nper=parseInt(document.getElementById('nper').value)||0;
  const gs=(document.getElementById('nstr').value||'').match(/\d+/g)||[];
  const ns=gs.reduce((a,b)=>a+parseInt(b),0)||0;
  return ns*nper;
}
function addBhaMid(){                    // 整排正中间插一处（4 串 × 20 块 → 第 40 块之后）
  const total=bhaTotalBlocks();
  addBha(Math.max(0,Math.floor(total/2)), '', '', 0);
}
function addBhaSeg(){                    // 每段（每支架）中点各一处
  addBha('每段', '', '', 0);
}
function clearBha(){ bhaRows=[]; renderBha(); }
function bhaPayload(){
  return bhaRows.map(r=>({nstr:r.nstr, pos:r.pos, stub:r.stub, motor:r.motor,
                          rot:r.rot, gap_l:r.gap_l, gap_r:r.gap_r}))
                .filter(r=>(r.stub||r.motor));      // 空行不送
}
function renderBha(){
  const tb=document.getElementById('bhaBody'); if(!tb) return;
  tb.innerHTML='';
  bhaRows.forEach((r,i)=>{
    const tr=document.createElement('tr'); tr.style.borderTop='1px solid var(--line)';
    const td=()=>{const c=document.createElement('td');c.style.padding='4px';tr.appendChild(c);return c;};
    // 注意：每个输入框必须是**各自的 const**。
    // 以前这里所有格子共用同一个 let e，回调里写 e.value 读到的其实是**最后一个**
    // 输入框（备注）的值 —— 于是“插在第几块之后”填什么都会被存成空串（=0），
    // 桩永远排在每串最前面；串号、电机旋转也一样读错框。
    let c=td(); const eNS=document.createElement('input');
    eNS.type='text'; eNS.value=r.nstr; eNS.placeholder='全部';
    eNS.title='这一行管几串的结构：留空 = 图里所有结构都插；填 4 = 只插 4 串那种结构（也能写 3+3）';
    eNS.style.width='70px'; eNS.oninput=()=>{r.nstr=eNS.value;}; c.appendChild(eNS);
    c=td(); const eS=document.createElement('input');
    eS.type='text'; eS.value=r.pos; eS.placeholder='留空=最前面';
    eS.title='整排第几块之后（不分串）：0/留空=最前面；4 串 × 20 块填 40 = 正中间；'
           + '填得比总块数大就排到最后；写“每段”=每段中点各一处';
    eS.style.width='110px'; eS.oninput=()=>{r.pos=eS.value;}; c.appendChild(eS);
    [['stub','（桩块）'],['motor','（不带电机）']].forEach(function(pair){
      const key=pair[0], blank=pair[1];
      const cc=td(); const s=document.createElement('select'); s.style.maxWidth='150px';
      const o0=document.createElement('option'); o0.value=''; o0.textContent=blank; s.appendChild(o0);
      (lastBlocks||[]).forEach(function(n){
        const o=document.createElement('option'); o.value=n; o.textContent=n; s.appendChild(o); });
      if(r[key] && (lastBlocks||[]).indexOf(r[key])<0){
        const o=document.createElement('option'); o.value=r[key]; o.textContent=r[key]; s.appendChild(o); }
      s.value=r[key]||''; s.onchange=()=>{r[key]=s.value;}; cc.appendChild(s);
    });
    c=td(); const eR=document.createElement('input'); eR.type='number'; eR.step='90';
    eR.value=r.rot; eR.title='电机块绕插入点转多少度（0 / 90 / 180 / 270）';
    eR.style.width='70px'; eR.oninput=()=>{r.rot=eR.value;}; c.appendChild(eR);
    [['gap_l','跟随板间净空'],['gap_r','跟随板间净空']].forEach(function(pair){
      const key=pair[0], ph=pair[1];
      const cc=td(); const ee=document.createElement('input'); ee.type='number'; ee.step='1';
      ee.value=r[key]; ee.placeholder=ph;
      ee.title='桩这一侧的净空；留空=跟随“板间净空”';
      ee.style.width='80px'; ee.oninput=()=>{r[key]=ee.value;}; cc.appendChild(ee);
    });
    c=td(); const eN=document.createElement('input'); eN.type='text';
    eN.value=r.note||''; eN.style.width='100%'; eN.oninput=()=>{r.note=eN.value;}; c.appendChild(eN);
    c=td(); const b=document.createElement('button'); b.className='ghost'; b.textContent='删';
    b.onclick=()=>{bhaRows.splice(i,1);renderBha();}; c.appendChild(b);
    tb.appendChild(tr);
  });
  RT(document.getElementById('bhaRow'));
  schedulePreview();                // 桩表改了，预览跟着重画
}

// ---------- 批量（一行 = 一张图，逐张独立） ----------
function pad3(n){ return String(n).padStart(3,'0'); }
function fillBatch(){
  const cnt=Math.max(1,Math.min(60,parseInt(v('bcount',3))||3));
  const s0=Math.max(1,parseInt(v('bstart',3))||1);
  const st=parseInt(v('bstep',1)); const step=isNaN(st)?1:st;
  const np=Math.max(1,parseInt(v('bnper',20))||20);
  const fr=document.getElementById('frame').value||'';
  const raw=(v('bno','SLD-001')||'SLD-001').trim();
  const m=raw.match(/^(.*?)(\d+)\s*$/);          // SLD-001 → 前缀 SLD-、起始 1
  const pre=m?m[1]:raw, n0=m?parseInt(m[2]):1;
  batchRows=[];
  for(let i=0;i<cnt;i++){
    batchRows.push({no:pre+pad3(n0+i), frame:fr,
                    n_str:Math.max(1,s0+i*step), n_per:np, note:'',
                    awg_main:'', awg_branch:'', bha:''});   // 留空 = 沿用上面那套
  }
  renderBatch();
}
function clearBatch(){ batchRows=[]; renderBatch(); }
function renderBatch(){
  const tb=document.getElementById('bBody'); tb.innerHTML='';
  batchRows.forEach((r,i)=>{
    const tr=document.createElement('tr'); tr.style.borderTop='1px solid var(--line)';
    const td=()=>{const c=document.createElement('td');c.style.padding='4px';tr.appendChild(c);return c;};
    let c=td(), e=document.createElement('input');
    e.type='text'; e.value=r.no; e.style.width='96px'; e.oninput=()=>{r.no=e.value;}; c.appendChild(e);
    c=td(); const s=document.createElement('select'); s.style.maxWidth='240px';
    ['',...(frameList||[])].forEach(f=>{const o=document.createElement('option');o.value=f;o.textContent=f||'（不选）';s.appendChild(o);});
    s.value=r.frame||''; s.onchange=()=>{r.frame=s.value;}; c.appendChild(s);
    c=td(); e=document.createElement('input'); e.type='text';
    e.value=r.n_str; e.style.width='70px'; e.title='串数：4 或 2+3（分段，段间走跨支架距离）';
    e.oninput=()=>{r.n_str=String(e.value||'').trim()||'1';};
    r._nstrEl=e;                       // 预览直接读这一格当前显示的值，别用可能过期的对象值
    c.appendChild(e);
    c=td(); e=document.createElement('input'); e.type='number'; e.min='1';
    e.value=r.n_per; e.style.width='70px'; e.oninput=()=>{r.n_per=parseInt(e.value)||1;};
    r._nperEl=e; c.appendChild(e);
    // 每张图自己的线号：留空 = 沿用上面“主线线号/支线线号”那套
    [['awg_main',AWG_MAIN],['awg_branch',AWG_BRANCH]].forEach(([key,opts])=>{
      c=td(); const s=document.createElement('select'); s.style.width='96px';
      const o0=document.createElement('option'); o0.value=''; o0.textContent='（同上）'; s.appendChild(o0);
      opts.forEach(w=>{const o=document.createElement('option'); o.value=w; o.textContent=w; s.appendChild(o);});
      s.value=r[key]||''; s.onchange=()=>{r[key]=s.value;}; c.appendChild(s);
    });
    // 这一张图自己的 BHA 桩/电机位置：留空 = 沿用上面那张表
    c=td(); e=document.createElement('input'); e.type='text';
    e.value=r.bha||''; e.placeholder='同上';
    e.title='写法：整排第几块之后:桩块:电机块:旋转:左净空:右净空（后面的都能省），多处用 ; 隔开；' +
            '位置按整排连续数（不分串）：4 串 × 20 块填 40 = 正中间；写“每段”=每段中点各一处。' +
            '例 40:BHA:MOTOR:0（BHA 桩 + 电机；块库里没有 BHA 时自动拿电机块当桩）' +
            '或 20:MOTOR::0（只放一个电机）。留空 = 沿用上面那张桩表；填了就只按这一行来。';
    e.style.width='190px'; e.oninput=()=>{r.bha=e.value;}; c.appendChild(e);
    c=td(); e=document.createElement('input'); e.type='text';
    e.value=r.note||''; e.style.width='100%'; e.oninput=()=>{r.note=e.value;}; c.appendChild(e);
    c=td(); const b=document.createElement('button'); b.className='ghost'; b.textContent='删';
    b.onclick=()=>{batchRows.splice(i,1);renderBatch();}; c.appendChild(b);
    tb.appendChild(tr);
  });
  RT(document.getElementById('batchRow'));
}
async function genBatch(){
  if(!batchRows.length){ alert(T('还没有要画的图：先填份数，点“按上面参数铺出 N 行”')); return; }
  const frame0=document.getElementById('frame').value||'';
  if(!batchRows.some(r=>r.frame||frame0)){ alert(T('批量模式要先选外框图')); return; }
  document.getElementById('out').innerHTML='生成中…';
  RT(document.getElementById('out'));
  progStart('提交…');
  const body=arrayCommon();
  // 关键：按**表格格子里当前显示的值**组行，别用行对象里那份可能过期的值
  // （以前就是这里读旧值，于是三张图都按同一套参数画，长度/板子/线束一模一样）。
  const trs=Array.prototype.slice.call(document.querySelectorAll('#bBody tr'));
  const rowsDom=trs.map(function(tr){
    const c=tr.querySelectorAll('td');
    const val=function(k){ const cell=c[k]; if(!cell) return '';
      const e=cell.querySelector('input,select'); return e?String(e.value||'').trim():''; };
    return {no:val(0), frame:val(1), n_str:val(2), n_per:val(3),
            awg_main:val(4), awg_branch:val(5), bha:val(6), note:val(7)};
  }).filter(function(r){ return r.n_str||r.no; });
  body.frame=frame0;
  body.rows=(rowsDom.length?rowsDom:batchRows);
  body.cols=parseInt(v('sheetc',2))||2;
  body.gap_sheet_x=parseFloat(v('sheetgx',300))||0;
  body.gap_sheet_y=parseFloat(v('sheetgy',300))||0;
  body.order=(document.getElementById('sheetorder')||{}).value||'row';
  body.separate=!((document.getElementById('bsheet')||{}).checked);   // 默认：一行一张、各自出 DXF
  body.to_cad=document.getElementById('tocad').checked;
  body.cad_original=false;          // 画到 CAD 一律画副本，不动外框原文件
  const r=await fetch('/api/generate_batch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const d=await r.json();
  progStop('完成');
  if(d.files){                       // 每张一个 DXF（默认）
    lastOut={dxf:(d.files[0]?d.files[0].name:''), csv:''};
    const rowsHtml=d.files.map(f=>
        '<div class="dlrow"><b style="min-width:110px">'+f.no+'</b>'+
        '<button class="ghost" onclick="fileAct(\'open\',\''+f.name+'\')">用CAD打开</button> '+
        '<button class="ghost" onclick="fileAct(\'export\',\''+f.name+'\')">DXF存桌面</button> '+
        (f.csv_url?('<button class="ghost" onclick="fileAct(\'export\',\''+f.csv+'\')">CSV存桌面</button> '):'')+
        (f.url?('<a class="dl" href="'+f.url+'" download>下载 DXF</a>'):'')+'</div>').join('');
    document.getElementById('out').innerHTML =
      '<div class="dlrow"><b>共 '+(d.total||d.files.length)+' 张，成功 '+d.files.length+' 张</b>'+
        (d.zip_url?('<a class="dl" href="'+d.zip_url+'" download>下载全部(zip)</a> '):'')+
        '<button class="ghost" onclick="fileAct(\'reveal\')">打开输出文件夹</button>'+
        '<span id="fileMsg" style="font-size:12px;color:var(--muted);margin-left:8px"></span></div>'+
      rowsHtml+'<div id="log">'+(d.log||[]).join('\n')+'</div>';
    RT(document.getElementById('out'));
    return;
  }
  lastOut={dxf:d.dxf_name||'', csv:d.csv_name||''};
  document.getElementById('out').innerHTML =
    (d.svg||'') +
    '<div class="dlrow"><b>'+((d.sheets||[]).length)+' 张拼进同一张图纸</b> ' +
      '<button class="ghost" onclick="fileAct(\'open\')">用默认程序打开(DXF)</button> ' +
      '<button class="ghost" onclick="fileAct(\'export\')">保存到桌面</button> ' +
      '<button class="ghost" onclick="fileAct(\'reveal\')">打开输出文件夹</button> ' +
      (d.csv_url?('<button class="ghost" onclick="fileAct(\'export\',lastOut.csv)">线长清单存到桌面</button> '):'') +
      (d.dxf_url?('<a class="dl" href="'+d.dxf_url+'" download>下载 DXF</a> '):'') +
      '<span id="fileMsg" style="font-size:12px;color:var(--muted);margin-left:8px"></span></div>' +
    '<div id="log">'+(d.log||[]).join('\n')+'</div>';
  RT(document.getElementById('out'));
}

async function gen(){
  if(mode==='batch'){ return genBatch(); }
  if(!document.getElementById('frame').value){alert(T('阵列模式必须先选外框图'));return;}
  document.getElementById('log') && (document.getElementById('log').textContent='');
  document.getElementById('out').innerHTML='生成中…';
  RT(document.getElementById('out'));
  progStart('提交…');
  const body=arrayCommon();
  body.frame=document.getElementById('frame').value;
  body.to_cad=document.getElementById('tocad').checked;
  body.cad_original=false;        // 画到 CAD 一律画副本，不动外框原文件
  const r=await fetch('/api/generate_array',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const d=await r.json();
  progStop('完成');
  const dxf=(d.dxf_file||'').split(/[\\/]/).pop();
  const csv=((d.csv_url||'').split('/').pop());
  lastOut={dxf:dxf, csv:decodeURIComponent(csv||'')};
  document.getElementById('out').innerHTML =
    (d.svg||'') +
    '<div class="dlrow">' +
      '<button class="ghost" onclick="fileAct(\'open\')">用默认程序打开(DXF)</button> ' +
      '<button class="ghost" onclick="fileAct(\'export\')">保存到桌面</button> ' +
      '<button class="ghost" onclick="fileAct(\'reveal\')">打开输出文件夹</button> ' +
      (d.csv_url?('<button class="ghost" onclick="fileAct(\'export\',lastOut.csv)">线长清单存到桌面</button> '):'') +
      (d.dxf_url?('<a class="dl" href="'+d.dxf_url+'" download>下载 DXF</a> '):'') +
      (d.csv_url?('<a class="dl" href="'+d.csv_url+'" download>下载线长清单 CSV</a> '):'') +
      '<span id="fileMsg" style="font-size:12px;color:var(--muted);margin-left:8px"></span>' +
    '</div>' +
    '<div id="log">'+(d.log||[]).join('\n')+'</div>';
  RT(document.getElementById('out'));
}
// 打包成桌面窗口时，<a download> 点了没反应（WebView2 不弹下载框），
// 所以窗口里改用程序自己的保存/打开能力；浏览器模式还走原来的下载链接。
// 注意：pywebview 的 api 是页面加载后异步注入的，所以这里用函数现查，别写成常量
function inApp(){ return !!(window.pywebview && window.pywebview.api); }
async function fileAct(act, name){
  const f = name || lastOut.dxf;
  const box = document.getElementById('fileMsg');
  if(!f){ if(box) box.textContent=T('还没有生成文件'); return; }
  if(box) box.textContent=T('处理中…');
  try{
    const r = await fetch('/api/file/'+act+'?name='+encodeURIComponent(f));
    const d = await r.json();
    if(box) box.textContent = (d.ok? '✓ ' : '✗ ') +
      (act==='export' ? ('已保存到 ' + d.msg) : (act==='reveal' ? '已在文件夹里定位' : d.msg));
  }catch(e){ if(box) box.textContent='✗ '+e; }
  if(box) RT(box);
}
loadBlocks();
setMode('array');
gotoStep(1);            // 两步走：先板子，填完点“下一步”到线束
bindPreview();          // 板子参数一改就重画预览
setLang(LANG);          // 按上次选的语言把界面刷一遍（默认中文，等于没改）

// ---------- 在线更新 ----------
function updMsg(t,color){const e=document.getElementById('updMsg');e.textContent=t;e.style.color=color||'';RT(e);}
async function checkUpdate(apply){
  const b=document.getElementById('updBtn'); b.disabled=true; updMsg('检查中…');
  try{
    const r=await fetch('/api/update'+(apply?'?apply=1':'')); const d=await r.json();
    if(!d.ok){ updMsg('✗ '+(d.error||'检查失败'),'#ffd7d7'); return; }
    if(d.message) updMsg(d.message, d.applied? '#c8f7d0':'#ffd7d7');
    else if(d.newer) updMsg('发现新版本 '+d.remote+'（当前 '+d.local+'）','#fff3c4');
    else updMsg('已是最新（'+d.local+'）','#c8f7d0');
    if(d.newer && !apply){
      if(confirm(T('发现新版本 '+d.remote+'（当前 '+d.local+'）\n'+(d.notes||'')+'\n\n现在下载更新吗？\n（下载完关掉窗口重新打开即生效）'))){
        return checkUpdate(true);
      }
    }
    if(d.applied){
      document.getElementById('verTxt').textContent='版本 '+d.remote+'（已下载，重启生效）';
      RT(document.getElementById('verTxt'));
      alert(T('更新已下载完成。\n\n请关掉本窗口，重新双击程序即生效。'));
    }
  }catch(e){ updMsg('✗ 网络错误：'+e,'#ffd7d7'); }
  finally{ b.disabled=false; }
}
checkUpdate();     // 启动时静默检查一次（失败不影响使用）
</script>
<script type="module">
// morphicons：两个图标的“变形”切换（无框架、离线可用，文件在 /vendor/morphicons/）
import { defineMorphIcon } from '/vendor/morphicons/element.js';
defineMorphIcon();
const GLOBE=[['circle',{cx:'12',cy:'12',r:'10'}],
             ['path',{d:'M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20'}],
             ['path',{d:'M2 12h20'}]];
const LANGS=[['path',{d:'m5 8 6 6'}],['path',{d:'m4 14 6-6 2-3'}],['path',{d:'M2 5h12'}],
             ['path',{d:'M7 2h1'}],['path',{d:'m22 22-5-10-5 10'}],['path',{d:'M14 18h6'}]];
const DOWN=[['path',{d:'M12 15V3'}],
            ['path',{d:'M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4'}],
            ['path',{d:'m7 10 5 5 5-5'}]];
const REFRESH=[['path',{d:'M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8'}],
               ['path',{d:'M21 3v5h-5'}],
               ['path',{d:'M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16'}],
               ['path',{d:'M8 16H3v5'}]];
function setI(id,node){const e=document.getElementById(id); if(e) e.icon=node;}
function goI(id,node){const e=document.getElementById(id); if(e&&e.morphTo) e.morphTo(node);}
try{ setI('langIcon', LANG==='en'?LANGS:GLOBE); setI('updIcon', REFRESH); }catch(e){}
const _setLang=window.setLang;
window.setLang=function(v){ _setLang(v);
  const tag=document.getElementById('langTag'); if(tag) tag.textContent=(v==='en')?'EN':'中';
  goI('langIcon', v==='en'?LANGS:GLOBE);
};
const _checkUpdate=window.checkUpdate;
window.checkUpdate=async function(apply){
  try{ goI('updIcon', REFRESH); }catch(e){}
  const r=await _checkUpdate(apply);
  try{ const m=((document.getElementById('updMsg')||{}).textContent||'');
       const tag=document.getElementById('updTag'); if(tag) tag.textContent=m||'';
       goI('updIcon', /新版本|更新已下载|有更新/.test(m)?DOWN:REFRESH); }catch(e){}
  return r;
};
</script>
</body></html>
"""


def place_opts(req):
    """连续画到 CAD 的落点参数：一列排几张 + 图与图的净空。

    和“拼成一张图纸”的排格子共用界面上那组数（每行放 / 图间距 X、Y）——
    用户看到的排法只有一处可调，CAD 里连着画的间距跟拼图预览是对得上的。
    """
    try:
        cols = int(req.get("cols", 2) or 2)
    except (TypeError, ValueError):
        cols = 2

    def f(v, d):
        try:
            return float(v)
        except (TypeError, ValueError):
            return d

    return {"place_per_col": max(1, cols),
            "place_gap": (f(req.get("gap_sheet_x"), 300.0),
                          f(req.get("gap_sheet_y"), 300.0))}


def array_spec(req, over=None):
    """把界面送来的一堆阵列参数拼成 build_array_frame 要的 spec。

    批量模式每张图参数不同：把这一张要覆盖的字段放进 over（空值不覆盖），
    其余全部沿用界面上那一套（块链、支线块、间距、标注方式……）。
    """
    spec = dict(module=req.get("module", ""), n_per=req.get("n_per", 20),
                module_first=req.get("module_first", ""),
                module_mid=req.get("module_mid", ""),
                module_last=req.get("module_last", ""),
                n_strings=req.get("n_strings", 1), gap_x=req.get("gap_x", 1),
                # dir 留空时由生成器按方案定（带 LYNX 的方案从下往上排，其余从左往右）
                # 串间净空留空 = 跟板间净空一致（界面上已合并成一个值）
                gap_y=(req.get("gap_y") or None), dir=(req.get("dir") or None),
                scheme=req.get("scheme", "Harness"),
                bracket_gap=req.get("bracket_gap", 4.0),
                harness_scale=req.get("harness_scale", 1.0),
                fixed_gap=req.get("fixed_gap", 30.0),
                head_block=req.get("head_block", ""),
                head_gap=req.get("head_gap", 30.0),
                pos_plug=req.get("pos_plug", ""),
                neg_plug=req.get("neg_plug", ""),
                link_array=bool(req.get("link_array")),
                match_span=bool(req.get("match_span", True)),
                harness=req.get("harness", []), gap=req.get("gap", 40),
                pos_feeder=req.get("pos_feeder", ""),
                neg_feeder=req.get("neg_feeder", ""),
                awg_main=req.get("awg_main", ""),
                awg_branch=req.get("awg_branch", ""),
                annot=req.get("annot", "text"),
                neg_rotate=req.get("neg_rotate", 0.0),
                neg_gap=req.get("neg_gap", 30.0),
                bha=req.get("bha", []),
                allow_enlarge=bool(req.get("allow_enlarge")),
                draw_points=bool(req.get("draw_points")),
                keep_from=(os.path.join(OUTDIR, os.path.basename(req["keep_from"]))
                           if req.get("keep_from") else ""))
    for k, v in (over or {}).items():
        if v is None or v == "":
            continue
        spec[k] = v
    return spec


def safe_name(s, dflt="SLD"):
    """图号当文件名用：去掉 Windows 不认的字符，空的就用默认名。"""
    out = "".join(("-" if c in '\\/:*?"<>|' else c) for c in str(s or "")).strip(" .")
    return out or dflt


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _api_update(self, path):
        """在线更新接口。

        GET  /api/update           → 查有没有新版本
        GET  /api/update?apply=1   → 下载并装上新版本（装完要重启程序才生效）
        """
        if upd is None:
            self._send(200, json.dumps({"ok": False,
                                        "error": "更新模块不可用（app_update.py 缺失）"}
                                       ).encode("utf-8"), "application/json")
            return
        try:
            if "apply=1" in path:
                set_progress(1, "开始下载更新")
                ok, msg = upd.download_and_install(progress=progress_cb)
                set_progress(100 if ok else 0, "更新下载完成" if ok else "更新失败")
                info = upd.check()
                info.update({"applied": ok, "message": msg})
                self._send(200, json.dumps(info).encode("utf-8"), "application/json")
                return
            self._send(200, json.dumps(upd.check()).encode("utf-8"), "application/json")
        except Exception as ex:
            import traceback
            self._send(200, json.dumps(
                {"ok": False, "error": "%s: %s" % (type(ex).__name__, ex),
                 "trace": traceback.format_exc().strip().splitlines()[-3:]}
            ).encode("utf-8"), "application/json")

    def _api_file(self, path):
        """桌面窗口里的文件操作（浏览器里不需要，浏览器直接下载就行）。

        GET /api/file/export?name=x.dxf&to=desktop|downloads  → 另存到桌面/下载
        GET /api/file/reveal?name=x.dxf                      → 资源管理器里定位文件
        GET /api/file/open?name=x.dxf                        → 用默认程序打开（进 CAD）
        """
        from urllib.parse import unquote, parse_qs
        q = parse_qs(path.split("?", 1)[1]) if "?" in path else {}
        name = unquote((q.get("name") or [""])[0])
        action = path.split("?", 1)[0].rsplit("/", 1)[-1]
        try:
            if action == "export":
                ok, msg = export_file(name, (q.get("to") or ["desktop"])[0])
            elif action == "reveal":
                ok, msg = reveal_file(name)
            elif action == "open":
                ok, msg = open_with_default(name)
            else:
                ok, msg = False, "不认识的操作：%s" % action
        except Exception as ex:
            ok, msg = False, "%s: %s" % (type(ex).__name__, ex)
        self._send(200, json.dumps({"ok": ok, "msg": str(msg),
                                    "name": name, "action": action}
                                   ).encode("utf-8"), "application/json")

    def do_GET(self):
        if self.path.startswith("/api/progress"):
            self._send(200, json.dumps(PROGRESS).encode("utf-8"), "application/json")
            return
        if self.path.startswith("/api/lang"):
            # 界面切中英文时调一下，把选择记下来（下次开窗口还是这个语言）
            from urllib.parse import parse_qs
            q = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            lang = (i18n.set_pref((q.get("set") or [""])[0]) if q.get("set")
                    else i18n.get_pref())
            self._send(200, json.dumps({"ok": True, "lang": lang}).encode("utf-8"),
                       "application/json")
            return
        if self.path.startswith("/api/update"):
            self._api_update(self.path)
            return
        if self.path.startswith("/api/file/"):
            self._api_file(self.path)
            return
        if self.path.startswith("/api/blocks"):
            from urllib.parse import unquote
            q = self.path.split("?", 1)[1] if "?" in self.path else ""
            frame_name = ""
            for kv in q.split("&"):
                if kv.startswith("frame="):
                    frame_name = unquote(kv[6:])
            # 界面里只列**块库**(blocklib/blocks) 里的块。
            # 以前连外框图模板自带的那一堆老块（防尘塞/L1/T3/CU - AL/lynx 1-4/旧框…）
            # 也一起列了 —— 它们是画在模板 DXF 里的，删块库文件删不掉，所以看着像
            # “老块库没删干净”。生成时用到的块由程序自动从块库并进外框图，
            # 这里的列表只用来挑线束链上的块，不需要把图框自带的块露出来。
            out, in_frame = [], set()
            for name in list_blocks():
                if name in in_frame:
                    continue
                out.append({"name": name, "svg": wr.block_file_svg(name), "src": "lib",
                            "group": block_group(name)})
            outs = []
            if os.path.isdir(OUTDIR):
                fs = [f for f in os.listdir(OUTDIR) if f.lower().endswith(".dxf")]
                fs.sort(key=lambda f: os.path.getmtime(os.path.join(OUTDIR, f)), reverse=True)
                outs = fs[:30]
            self._send(200, json.dumps({"blocks": out, "frames": list_frames(),
                                        "out_files": outs}).encode("utf-8"),
                       "application/json")
        elif self.path.startswith("/vendor/"):
            # morphicons 的 ESM 文件：必须用 text/javascript 送，浏览器才肯 import
            rel = self.path.split("?", 1)[0][len("/vendor/"):]
            p = os.path.normpath(os.path.join(vendor_dir(), rel.replace("/", os.sep)))
            root = os.path.normpath(vendor_dir())
            if not p.startswith(root) or not os.path.isfile(p):
                self._send(404, b"not found"); return
            ct = ("text/javascript; charset=utf-8" if p.lower().endswith(".js")
                  else ("application/json" if p.lower().endswith(".json")
                        else "text/plain; charset=utf-8"))
            body = open(p, "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/out/"):
            fn = os.path.basename(self.path)
            p = os.path.join(OUTDIR, fn)
            if os.path.exists(p):
                ct = ("text/csv; charset=utf-8" if fn.lower().endswith(".csv")
                      else ("application/zip" if fn.lower().endswith(".zip")
                            else "application/dxf"))
                # 带上附件头：浏览器模式下点了就直接下载（窗口模式走 /api/file/*）
                self.send_response(200)
                self.send_header("Content-Type", ct)
                self.send_header("Content-Disposition",
                                 'attachment; filename="%s"' % fn.encode("ascii", "replace").decode())
                self.send_header("Cache-Control", "no-store")
                body = open(p, "rb").read()
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self._send(404, b"not found")
        else:
            _v, _vn = app_version()
            lang = i18n.get_pref()
            if "?" in self.path:                 # 临时看另一种语言：http://127.0.0.1:8770/?lang=en
                from urllib.parse import parse_qs   # （只影响这一次打开，不改变记住的设置）
                want = (parse_qs(self.path.split("?", 1)[1]).get("lang") or [""])[0].strip().lower()
                if want in i18n.LANGS:
                    lang = want
            page = (HTML.replace("{{VER}}", _v).replace("{{VERNOTE}}", _vn)
                        .replace("{{LANG}}", lang)
                        .replace("{{I18N_EN}}", i18n.js_table()))
            self._send(200, page.encode("utf-8"))

    def do_POST(self):
        try:
            self._do_post()
        except Exception as ex:                      # 出错也要回一个可读的结果，
            import traceback                        # 不能让界面一直卡在“生成中…”
            tb = traceback.format_exc().strip().splitlines()
            head = ["生成失败: %s: %s" % (type(ex).__name__, ex)]
            sw = stale_warning()
            if sw:
                head.insert(0, sw)                  # 先说是旧进程，别对着旧代码找 bug
            self._send(200, json.dumps(
                {"svg": "", "log": head + tb[-5:]}
            ).encode("utf-8"), "application/json")

    def _do_post(self):
        # 只有两种出图方式：生成 DXF（阵列一张 / 批量拼一张），
        # 勾了“直接画到 CAD(COM)”时再顺带用 COM 画进 CAD。
        if self.path == "/api/generate_array":
            self._generate_array(); return
        if self.path == "/api/generate_batch":
            self._generate_batch(); return
        if self.path == "/api/preview_array":
            self._preview_array(); return
        self._send(404, b"not found")


    def _preview_array(self):
        """板子布局的实时预览：只算布局，不读外框图、不出文件、不连 CAD。"""
        try:
            ln = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(ln).decode("utf-8")) if ln else {}
            svg, info = wr.array_preview_svg(array_spec(req))
        except Exception as ex:
            svg, info = "", "预览失败：%s: %s" % (type(ex).__name__, ex)
        self._send(200, json.dumps({"svg": svg, "info": info}).encode("utf-8"),
                   "application/json")


    def _generate_array(self):
        """阵列 + 线束（手册第 13 章）：界面上的线束链复用“块库 + 链”两块。"""
        PROGRESS["running"] = True
        set_progress(1, "准备")
        # 进度回调：把当前阶段 + 最近几行日志一起报给界面（画到 CAD 那段尤其需要，
        # 否则 CAD 连上/在画什么，界面上什么都看不到）
        _box = {"log": []}

        def pg(pct, stage):
            set_progress(pct, stage, _box["log"][-8:])
        ln = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(ln).decode("utf-8"))
        frame_name = req.get("frame", "") or ""
        if not frame_name:
            self._send(200, json.dumps({"svg": "", "log": ["阵列模式必须先选外框图"]}
                                       ).encode("utf-8"), "application/json"); return
        spec = array_spec(req)
        _st = {}                       # build_array_frame 回填“这张图用了哪些块”
        text, log, wires = wr.build_array_frame(os.path.join(FRAMES_DIR, frame_name), spec,
                                                log=_box["log"], progress=pg, stats=_st)
        _box["log"] = log          # 后面 CAD 那段继续往这个列表里追加，界面能看到
        if not text:
            PROGRESS["running"] = False
            self._send(200, json.dumps({"svg": "", "log": log}).encode("utf-8"),
                       "application/json"); return
        set_progress(85, "写 DXF 文件")
        pg(85, "写 DXF 文件")
        os.makedirs(OUTDIR, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        fn = "array_%s.dxf" % ts
        outpath = os.path.join(OUTDIR, fn)
        with open(outpath, "w", encoding="latin-1", newline="") as f:
            f.write(text)
        csv_fn = ""
        try:
            ag.write_csv(os.path.splitext(outpath)[0] + ".csv", wires)
            csv_fn = os.path.splitext(fn)[0] + ".csv"
        except Exception as ex:
            log.append("⚠ 线长清单没写成: %s" % ex)
        if req.get("to_cad"):
            log.append("—— 画到 CAD ——")
            dwg = os.path.join(FRAMES_DIR, os.path.splitext(frame_name)[0] + ".dwg")
            _blks = set(_st.get("blocks") or [])
            if not _blks:
                # 兜底：生成器没回填清单时，按参数自己拼一份（含自动补的公头/母头/
                # 负极支线），**不要**退回“全部块回放” —— 那会把外框图自带的
                # Frame1 / SLD_NOTES 也当成我们的块删掉重建。
                _blks = set([x for x in (spec.get("module", ""), spec.get("module_first", ""),
                                         spec.get("module_mid", ""), spec.get("module_last", ""),
                                         spec.get("head_block", ""), spec.get("pos_plug", ""),
                                         spec.get("neg_plug", ""), spec.get("neg_head", ""),
                                         spec.get("pos_feeder", ""), spec.get("neg_feeder", ""),
                                         spec.get("fuse_block", ""))
                             if x] + list(spec.get("harness") or [])
                            + wr.bha_block_names(spec.get("bha"), 0, 0))
                log.append("⚠ 拿不到这张图用到的块清单，退回按参数拼的名单（%d 个块）"
                           % len(_blks))
            try:
                cd.draw_dxf_into_cad(outpath, dwg, log,
                                     use_original=bool(req.get("cad_original")),
                                     copy_dir=OUTDIR,
                                       # 只回放**这张图真正画出来的块**（由生成器回填）。
                                       # 以前是按界面参数自己拼名单，自动补出来的
                                       # 起始块(CBX)/公头/母头/负极支线不在名单里，
                                       # 画到 CAD 时就整条丢了 —— 现在不会了。
                                       only_blocks=_blks,
                                       # 连续画图：不清前面那张，接着在同一张 DWG 里
                                       # 错开排（从上往下、排满往右一列）
                                       auto_place=True, clear_first=False,
                                       **place_opts(req),
                                       progress=pg)
            except Exception as ex:
                import traceback
                log.append("⚠ 画到 CAD 失败: %s: %s" % (type(ex).__name__, ex))
                log.extend(traceback.format_exc().strip().splitlines()[-4:])
        svg = ""
        try:
            s2, _o = wr.parse_sections_text(wr.read_dxf_text(outpath))
            svg = wr.entities_to_svg(wr.group_entities(s2.get("ENTITIES", [])),
                                     wr._blocks_map(s2))
        except Exception:
            pass
        resp = {"svg": svg, "dxf_url": "/out/" + fn,
                "csv_url": ("/out/" + csv_fn) if csv_fn else "",
                "log": log, "dxf_file": outpath}
        set_progress(100, "完成", log[-8:])
        PROGRESS["running"] = False
        sw = stale_warning()
        if sw:                       # 代码在本进程启动之后改过：成功也要提醒，不然会对不上
            log = [sw] + list(log)
        self._send(200, json.dumps(resp).encode("utf-8"), "application/json")

    def _generate_batch(self):
        """批量：一行 = 一张图，全部拼进**同一张图纸**（每张各自调一份外框模板）。

        每张各自调一份新的外框模板独立生成（参数互不影响），然后整张打包成一个
        块（块名 = 图号）并进底图，按“从左到右、从上到下”排格子，
        输出一个 DXF（+ 一张带图号的线长清单 CSV）。
        """
        PROGRESS["running"] = True
        set_progress(1, "准备")
        box = {"log": []}
        ln = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(ln).decode("utf-8"))
        rows = [r for r in (req.get("rows") or []) if isinstance(r, dict)]
        log = []
        frame0 = (req.get("frame") or "").strip()
        if not rows:
            PROGRESS["running"] = False
            self._send(200, json.dumps(
                {"svg": "", "log": ["批量还没有要画的图：先填份数、点“铺出 N 行”"]}
            ).encode("utf-8"), "application/json")
            return

        items = []
        for n, row in enumerate(rows, 1):
            frame_name = (row.get("frame") or frame0 or "").strip()
            no = safe_name(row.get("no"), "SLD-%03d" % n)
            if not frame_name:
                log.append("—— 第 %d/%d 张 %s：没选外框图，跳过 ——" % (n, len(rows), no))
                continue
            try:                       # 每行单独的串数/板数/线号；空着沿用界面上那套
                # 串数允许写成分段（2+3），所以按字符串原样传，解析交给生成器
                # 注意：界面批量表的列名是 n_str，命令行/接口那边写 n_strings —— 两个都认。
                # （以前只读 n_strings，于是**每一张图都在用界面上的串数**，
                #   “批量每行单独填串数”等于没生效 —— 这就是批量图和单独生成对不上的原因之一）
                _nv = row.get("n_strings")
                if _nv is None:
                    _nv = row.get("n_str")
                _ns = "" if _nv is None else str(_nv).strip()
                over = {"n_strings": (_ns or None),
                        "n_per": int(row.get("n_per") or 0) or None,
                        "awg_main": str(row.get("awg_main") or "").strip() or None,
                        "awg_branch": str(row.get("awg_branch") or "").strip() or None,
                        # 这一行自己的 BHA 桩/电机位置；空 = 沿用界面上那张表
                        "bha": str(row.get("bha") or "").strip() or None}
            except (TypeError, ValueError):
                over = {}
            items.append({"name": no,
                          "frame": os.path.join(FRAMES_DIR, os.path.basename(frame_name)),
                          "spec": array_spec(req, over)})
        if not items:
            PROGRESS["running"] = False
            self._send(200, json.dumps(
                {"svg": "", "log": log + ["批量：没有一张选了外框图"]}
            ).encode("utf-8"), "application/json")
            return

        box["log"] = log
        # separate：一行一张、各自出一个 DXF（默认）；不勾才拼成一张图纸
        separate = bool(req.get("separate", True))
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        os.makedirs(OUTDIR, exist_ok=True)

        if separate:
            files, total = [], len(items)
            for n, it in enumerate(items, 1):
                def pg(pct, stage, _n=n, _total=total):
                    set_progress(int(((_n - 1) + max(0.0, min(100.0, float(pct))) / 100.0)
                                     * 100.0 / _total),
                                 "第 %d/%d 张 · %s" % (_n, _total, stage), box["log"][-8:])

                box["log"] = box["log"] + ["—— 第 %d/%d 张 %s（%s）——"
                                           % (n, total, it["name"],
                                              os.path.basename(it["frame"]))]
                spec = it["spec"]
                _st = {}
                text, log2, wires = wr.build_array_frame(it["frame"], spec,
                                                         log=box["log"], progress=pg,
                                                         stats=_st)
                box["log"] = log2 or box["log"]
                if not text:
                    box["log"].append("⚠ %s 没画出来，跳过这张" % it["name"])
                    continue
                fn = "%s_%s.dxf" % (safe_name(it["name"]), stamp)
                outpath = os.path.join(OUTDIR, fn)
                with open(outpath, "w", encoding="latin-1", newline="") as f:
                    f.write(text)
                csv_fn = ""
                try:
                    ag.write_csv(os.path.splitext(outpath)[0] + ".csv", wires)
                    csv_fn = os.path.splitext(fn)[0] + ".csv"
                except Exception as ex:
                    box["log"].append("⚠ 线长清单没写成: %s" % ex)
                if req.get("to_cad"):
                    box["log"].append("—— 第 %d/%d 张画到 CAD ——" % (n, total))
                    dwg = os.path.join(FRAMES_DIR,
                                       os.path.splitext(os.path.basename(it["frame"]))[0] + ".dwg")
                    try:
                        cd.draw_dxf_into_cad(
                            outpath, dwg, box["log"], use_original=False, copy_dir=OUTDIR,
                            # 只回放**这张图真正画出来的块**（生成器回填 stats["blocks"]）。
                            # 以前这里按界面参数自己拼名单：链是空的、自动补出来的
                            # CBX / 正极支线 / 公头 / 母头 / 负极支线都不在名单里，
                            # 画到 CAD 时整条就丢了 —— 批量模式一直在踩这个坑。
                            only_blocks=(set(_st.get("blocks") or []) or set(
                                [x for x in (spec.get("module", ""), spec.get("module_first", ""),
                                             spec.get("module_mid", ""), spec.get("module_last", ""))
                                 if x] + list(spec.get("harness") or [])
                                + wr.bha_block_names(spec.get("bha"), 0, 0))),
                            # 连续画图：每张都接着上一张往下排（不清图），
                            # 一列排满“每行放”那么多张就往右挪一列
                            auto_place=True, clear_first=False,
                            **place_opts(req),
                            progress=pg)
                    except Exception as ex:
                        import traceback
                        box["log"].append("⚠ 画到 CAD 失败: %s: %s" % (type(ex).__name__, ex))
                        box["log"].extend(traceback.format_exc().strip().splitlines()[-4:])
                files.append({"no": it["name"], "name": fn, "url": "/out/" + fn,
                              "csv": csv_fn,
                              "csv_url": ("/out/" + csv_fn) if csv_fn else ""})
            zip_fn = ""
            if files:
                zip_fn = "批量_%s.zip" % stamp
                try:
                    with zipfile.ZipFile(os.path.join(OUTDIR, zip_fn), "w",
                                         zipfile.ZIP_DEFLATED) as z:
                        for f in files:
                            z.write(os.path.join(OUTDIR, f["name"]), f["name"])
                            if f["csv"]:
                                z.write(os.path.join(OUTDIR, f["csv"]), f["csv"])
                except Exception as ex:
                    box["log"].append("⚠ 打包 zip 失败: %s" % ex)
                    zip_fn = ""
            box["log"].append("批量完成：共 %d 张，成功 %d 张（每张一个 DXF）"
                              % (total, len(files)))
            set_progress(100, "批量完成", box["log"][-8:])
            PROGRESS["running"] = False
            sw = stale_warning()
            if sw:
                box["log"] = [sw] + list(box["log"])
            self._send(200, json.dumps(
                {"files": files, "total": total, "separate": True,
                 "zip_url": ("/out/" + zip_fn) if zip_fn else "",
                 "zip_name": zip_fn, "off": log,
                 "log": box["log"]}).encode("utf-8"), "application/json")
            return

        def pg(pct, stage):
            set_progress(pct, stage, box["log"][-8:])

        text, log, wires, names = wr.build_multi_frame(
            items, cols=req.get("cols", 2),
            gap_x=req.get("gap_sheet_x", 300), gap_y=req.get("gap_sheet_y", 300),
            order=req.get("order", "row"), log=box["log"], progress=pg)
        box["log"] = log
        if not text:
            PROGRESS["running"] = False
            self._send(200, json.dumps({"svg": "", "log": log}).encode("utf-8"),
                       "application/json")
            return

        set_progress(90, "写 DXF 文件")
        os.makedirs(OUTDIR, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        fn = "拼图_%s.dxf" % stamp
        outpath = os.path.join(OUTDIR, fn)
        with open(outpath, "w", encoding="latin-1", newline="") as f:
            f.write(text)
        csv_fn = ""
        try:
            ag.write_csv_multi(os.path.splitext(outpath)[0] + ".csv", wires)
            csv_fn = os.path.splitext(fn)[0] + ".csv"
        except Exception as ex:
            log.append("⚠ 线长清单没写成: %s" % ex)
        if req.get("to_cad"):
            log.append("—— 画到 CAD（%d 张一起画进当前图）——" % len(names))
            dwg = os.path.join(FRAMES_DIR,
                               os.path.splitext(os.path.basename(items[0]["frame"]))[0] + ".dwg")
            try:
                cd.draw_dxf_into_cad(outpath, dwg, log,
                                     use_original=False,      # 一律画副本，不动外框原文件
                                     copy_dir=OUTDIR, only_blocks=set(names),
                                     # 连续画图：整张拼图也当成“一张”，不清前面的
                                     auto_place=True, clear_first=False,
                                     **place_opts(req),
                                     progress=pg, sheet_names=names)
            except Exception as ex:
                import traceback
                log.append("⚠ 画到 CAD 失败: %s: %s" % (type(ex).__name__, ex))
                log.extend(traceback.format_exc().strip().splitlines()[-4:])
        svg = ""
        try:
            s2, _o = wr.parse_sections_text(wr.read_dxf_text(outpath))
            svg = wr.entities_to_svg(wr.group_entities(s2.get("ENTITIES", [])),
                                     wr._blocks_map(s2))
        except Exception:
            pass
        resp = {"svg": svg, "dxf_url": "/out/" + fn, "dxf_file": outpath, "dxf_name": fn,
                "csv_url": ("/out/" + csv_fn) if csv_fn else "", "csv_name": csv_fn,
                "sheets": names, "log": log}
        set_progress(100, "批量完成", log[-8:])
        PROGRESS["running"] = False
        sw = stale_warning()
        if sw:
            log = [sw] + list(log)
        self._send(200, json.dumps(resp).encode("utf-8"), "application/json")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--browser", action="store_true",
                    help="强制用浏览器打开（默认：能开桌面窗口就用桌面窗口）")
    ap.add_argument("--title", default="Single line-CAD")
    args = ap.parse_args()

    httpd, port = make_server(args.port)
    url = "http://127.0.0.1:%d" % port
    print("Single line-CAD 已启动:", url)
    print("代码版本:", code_version(), "(改过代码要重启本进程才生效)")
    print("块库:", BLOCKS_DIR)
    print("输出:", OUTDIR)

    # ---- 优先开一个**真正的桌面窗口**（pywebview + Edge WebView2）----
    # 打包成 exe 后用户要的是“双击出窗口”，不是“双击开浏览器”。
    # 拿不到窗口能力时（没装 WebView2 / 没装 pywebview）自动退回浏览器，不会开不起来。
    if not args.browser:
        try:
            import webview
        except Exception as ex:
            print("没装桌面窗口组件（%s），改用浏览器打开" % type(ex).__name__)
        else:
            t = threading.Thread(target=httpd.serve_forever, daemon=True)
            t.start()
            print("正在打开桌面窗口…（关掉窗口即退出程序）")
            try:
                webview.create_window(args.title, url, width=1280, height=860,
                                      min_size=(960, 640), text_select=True)
                webview.start()          # 阻塞到窗口关闭
            except Exception as ex:
                print("桌面窗口启动失败（%s: %s），改用浏览器打开" % (type(ex).__name__, ex))
            else:
                print("窗口已关闭，程序退出。")
                return

    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()
