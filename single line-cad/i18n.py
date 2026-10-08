#!/usr/bin/env python3
"""i18n.py -- 界面中英文两套文案（中文是原文，英文按中文原句查表）。

为什么用「中文原句当 key」：
  界面上的中文散在两处 —— HTML 静态标签、以及 Python 生成后发给界面的日志
  （wiring_raw / cad_draw / array_gen 里那几百句）。要是一句句改成 t("key")，
  改动面太大、还容易漏。这里改成**在界面这一层翻译**：程序照旧只说中文，
  页面拿到中文后按表翻成英文；表里没有的原样显示，绝不会因为少写一条就崩。

表里带占位符的条目写法：
    "放块 %s 于 (%.2f, %.2f)" : "Place block {} at ({}, {})"
  左边是 Python 里 printf 的原样写法（用来还原成通配匹配），右边的 {} 按顺序
  填入左边匹配到的内容。两边个数要一样。
"""

import json
import os
import threading

LANGS = ("zh", "en")
DEFAULT = "zh"

# ---------------------------------------------------------------- 对照表 ----
EN = {
    # ================= 界面：标题与工具栏 =================
    "版本 %s（%s）": "Version {} ({})",
    "检查更新": "Check for updates",
    "选块（可重复）→ 组成链 → 生成连完线的产品 · 代码版本 %s（换过代码要重启窗口，否则跑的还是旧代码）":
        "Pick blocks (repeatable) → build the chain → generate a fully wired drawing · code version {} "
        "(restart the window after a code change, otherwise the old code keeps running)",

    # ================= 界面：两块主面板 =================
    "① 块库（点击加入链）": "① Block library (click to add to the chain)",
    "① 板子 · 块库 + 阵列": "① Panels · block library + array",
    "② 线束 · 块库 + 连线（留空也能排：末端母头 + 正极支线×(串数-1) + 末端公头）":
        "② Harness · block library + wiring (optional: leave empty to auto-arrange "
        "“tail female + positive feeder × (strings-1) + tail male”)",
    "点上面的线束块加入…": "Click a harness block above to add it…",
    "方案": "Scheme",
    "主方案": "Main scheme",
    "跳线方案": "Jumper scheme",
    "（不加跳线）": "(no jumper)",
    "外框图模板": "Frame template",
    "另存 DWG 到…": "Save DWG as…",
    "打开所在文件夹": "Open containing folder",
    "已保存到 ": "Saved to ",
    "跨支架距离": "Cross-bracket distance",
    "① 板子": "① Panels",
    "② 线束": "② Harness",
    "先把板子这一页填好，点下面的“下一步：填线束”":
        "Fill in this panel page first, then click “Next: harness” below",
    "这一页填线束（线号、支线块、末端接头），填完按“生成”":
        "Fill in the harness here (wire numbers, feeder blocks, end connectors), then click “Generate”",
    "下一步：填线束 →": "Next: harness →",
    "← 上一步": "← Back",
    "板子：先选“组件 首块”": "Panels: choose the first module block first",
    "板子：先选“尾块”": "Panels: choose the last module block first",
    "板子：串数没填": "Panels: the string count is empty",
    "板子预览（实时）": "Panel preview (live)",
    "刷新": "Refresh",
    "（填完组件和串数就会出现预览）": "(the preview appears once the modules and string count are set)",
    "更新中…": "Updating…",
    "② 链（按顺序摆放并连线）": "② Chain (placed in order and wired up)",
    "② 线束（可以不填：留空自动排“末端母头 + 正极支线×(串数-1) + 末端公头”；想带保险丝等串联块就把块点上来）":
        "② Harness (optional: leave it empty to auto-arrange “tail female plug + positive feeder × (strings-1) "
        "+ tail male plug”; click blocks in if you want fuses or other in-line blocks)",
    "点左边块加入…": "Click a block on the left to add it…",
    "移除": "Remove",
    "库": "lib",
    "来自块库，生成时自动并入外框": "From the block library; merged into the frame when generating",

    # ================= 界面：模式与阵列参数 =================
    "模式": "Mode",
    "光伏阵列 + 线束（一张图）": "PV array + harness (one drawing)",
    "批量（一行一张 · 全部拼进同一张图纸）":
        "Batch (one drawing per row, all combined into a single sheet)",
    "组件 首块": "Module: first",
    "中间块": "Middle",
    "尾块": "Last",
    "每串板数": "Modules per string",
    "串数": "Strings",
    "板间净空": "Module gap",
    "串间净空": "String gap",
    "板间/串间净空": "Panel / string gap",
    "串的排法": "String layout",
    "从左往右接": "Left to right",
    "从上往下叠": "Top to bottom",
    "画阵列↔线束跨接线": "Draw array↔harness jumpers",
    "线束接点对齐缩放": "Scale harness to align contacts",
    "正极支线块": "Positive feeder block",
    "负极支线块": "Negative feeder block",
    "（选链里的块）": "(pick from the chain)",
    "（选块）": "(pick a block)",
    "（不指定）": "(none)",
    "主线线号": "Main wire AWG",
    "支线线号": "Feeder wire AWG",
    "主线上标的线号（标在块与块之间的连线上）；按载流量选：线越长、串数越多用越粗的":
        "Wire size printed on the main run (labelled on the links between blocks); "
        "pick it by ampacity: longer runs and more strings need thicker wire",
    "支线线号（板与板之间不标字，只写进线长清单 CSV）":
        "Feeder wire size (not labelled between modules, only written into the wire-length CSV)",
    "线号标注": "Wire label style",
    "文字": "Text",
    "标注外观（普通实体）": "Dimension look (plain entities)",
    "CAD 原生标注(DIMENSION)": "Native CAD dimension (DIMENSION)",
    "线束缩放": "Harness scale",
    "FUSE间距": "FUSE spacing",
    "块固定间距": "Fixed block spacing",
    "语言 / Language": "Language",
    "起始块": "Head block",
    "起始块间距": "Head block gap",
    "负极支线旋转": "Negative feeder rotation",
    "负极行间距": "Negative row gap",
    "末端公头块": "Tail male plug block",
    "末端母头块": "Tail female plug block",
    "允许放大到占满": "Allow enlarging to fill",
    "保留手工层": "Keep manual layers",
    "不保留": "Do not keep",
    "直接画到 CAD(COM)": "Draw into CAD directly (COM)",
    "画在原外框文件上（默认画副本）": "Draw on the original frame file (default: on a copy)",
    "不用": "None",
    "间隔 GAP": "Gap",
    "占框比例%": "Fill ratio %",
    "自动缩放对齐接点": "Auto-scale to align contacts",
    "标注线长": "Label wire length",
    "不展平(保留原始实体)": "Do not flatten (keep original entities)",
    "外框图": "Frame",
    "生成连线": "Generate wiring",
    "清空": "Clear",

    # ================= 界面：电机 / BHA 桩位置 =================
    "电机 / BHA 桩位置": "Motor / BHA stub position",
    "加一处": "Add one",
    "存为方案": "Save as scheme",
    "删方案": "Delete scheme",
    "先在“串数”那一格填好串型（例如 4 或 3+3），再存为方案":
        "Fill the string layout in the \u201cStrings\u201d box first (e.g. 4 or 3+3), then save it as a scheme",
    "给这个方案起个名字（例如 4串 / 3+3 两段）":
        "Name this scheme (e.g. 4 strings / 3+3 sections)",
    "删掉方案：": "Delete scheme: ",
    "串数：从右下角“方案”里选串型，也可以自己填（4 或 2+3 分段）":
        "Strings: pick a scheme from the bottom-right box, or type it yourself (4, or 2+3 with sections)",
    "整排中间插一处": "One at the middle of the row",
    "按整排总块数取中间：4 串 × 20 块 → 第 40 块之后":
        "Takes the middle of the whole row: 4 strings × 20 modules → after module 40",
    "每段中点插一处": "One per bracket section, at mid",
    "每个支架（每段）中点各一处，串数写 3+3 时就是前后各一个":
        "One at the middle of each bracket section; with 3+3 strings that is one in front and one behind",
    "两块板之间插一个 BHA 桩块（可再挂电机）；位置 = 整排第几块之后（不分串）：4 串 × 20 块共 80 块，填 40 就是正中间；0 或留空 = 最前面；写“每段” = 每段中点各一处。留空整列 = 不插桩。":
        "Insert a BHA stub block between two modules (optionally with a motor block); the position is "
        "\u201cafter which module of the whole row\u201d (counting across strings): 4 strings × 20 modules = 80 "
        "modules, so 40 is the exact middle; 0 or empty = very front; write \u201ceach section\u201d for the mid of "
        "every bracket section. Empty table = no stubs.",
    "整排第几块之后": "After module # (whole row)",
    "串数": "Strings",
    "这一行管几串的结构：留空 = 图里所有结构都插；填 4 = 只插 4 串那种结构":
        "Which structure this row applies to: empty = every structure in the drawing; "
        "4 = only the 4-string structures",
    "整排第几块之后（不分串）：0/留空=最前面；4 串 × 20 块填 40 = 正中间；填得比总块数大就排到最后；写“每段”=每段中点各一处":
        "After which module of the whole row (counting across strings): 0/empty = very front; "
        "4 strings × 20 modules → 40 = the middle; larger than the total = at the very end; "
        "write \u201ceach section\u201d for the mid of every bracket section",
    "BHA 桩块": "BHA stub block",
    "电机块": "Motor block",
    "电机旋转": "Motor rotation",
    "桩左净空": "Gap left of stub",
    "桩右净空": "Gap right of stub",
    "BHA位置": "BHA position",
    "（桩块）": "(stub block)",
    "（不带电机）": "(no motor)",
    "跟随板间净空": "same as module gap",
    "同上": "same as above",
    "全部": "all",
    "电机块绕插入点转多少度（0 / 90 / 180 / 270）":
        "Rotation of the motor block around its insertion point (0 / 90 / 180 / 270)",
    "桩这一侧的净空；留空=跟随“板间净空”":
        "Clearance on this side of the stub; empty = follow the module gap",
    "写法：整排第几块之后:桩块:电机块:旋转:左净空:右净空，多处用 ; 隔开；位置按整排连续数（不分串）：4 串 × 20 块填 40 = 正中间；写“每段”=每段中点各一处。例 40:BHA:MOTOR:0 或 20:BHA:MOTOR:90;60:BHA。留空 = 沿用上面那张 BHA 表":
        "Format: after-module-of-the-row:stub:motor:rotation:gap-left:gap-right, several entries separated by ;. "
        "The position counts across the whole row (not per string): 4 strings × 20 modules → 40 = the middle; "
        "write \u201ceach section\u201d for the mid of every bracket section. "
        "Examples: 40:BHA:MOTOR:0 or 20:BHA:MOTOR:90;60:BHA. Empty = use the BHA table above",

    # ================= 界面：批量模式 =================
    "份数": "Count",
    "起始串数": "First strings",
    "每张 +": "Step",
    "起始图号": "First drawing no.",
    "按上面参数铺出 N 行": "Fill N rows from the settings above",
    "清空行": "Clear rows",
    "图号": "Drawing no.",
    "备注": "Note",
    "生成全部（每行一张）": "Generate all (one file per row)",
    "一行 = 一张图：每张都重新调一次外框模板，参数互不影响。 左侧块链和上面那套间距/标注/支线块是各行的公共参数。":
        "One row = one drawing: every row re-opens the frame template, so parameters do not affect each other. "
        "The chain on the left and the spacing/label/feeder settings above are shared by all rows.",
    "（不选）": "(none)",
    "删": "Del",

    # ================= 界面：批量（多张拼一张） =================
    "每行放": "Per row",
    "图间距 X": "Sheet gap X",
    "图间距 Y": "Sheet gap Y",
    "排法": "Order",
    "先横后竖（左→右，然后下一行）": "Row first (left→right, then the next row)",
    "先竖后横（上→下，然后下一列）": "Column first (top→bottom, then the next column)",
    "一行 = 一张图：每张都调一份新的外框模板（互相独立）， 全部排进同一个 DXF；在 CAD 里每张是一个整块（块名 = 图号），想挪就整块挪。":
        "One row = one drawing: every row re-opens a fresh frame template (fully independent), and all of them go "
        "into a single DXF. In CAD each drawing is one whole block (block name = drawing no.), so you can move it "
        "as a unit.",
    "生成（全部拼进同一张图纸）": "Generate (all combined into one drawing)",
    "（同上）": "(same as above)",
    "批量还没有要画的图：先填份数、点“铺出 N 行”":
        "Nothing to draw yet: set the count and press “Fill N rows from the settings above”.",
    "批量：没有一张选了外框图": "Batch: none of the rows has a frame template",
    "还没有要画的图：先填份数，点“按上面参数铺出 N 行”":
        "Nothing to draw yet: set the count and press “Fill N rows from the settings above”.",
    " 张拼进同一张图纸": " drawing(s) combined into one sheet",
    "生成": "Generate",

    # ================= 界面：输入框提示（悬停说明） =================
    "text=普通文字（最稳）；shape=画成标注外观（尺寸线/界线/箭头，普通实体，任何 CAD 都能开）；dim=CAD 原生 DIMENSION（可拖动关联，但 ZWCAD 2025 会判无效）":
        "text = plain text (safest); shape = drawn to look like a dimension (dimension/extension lines and arrows "
        "as plain entities, opens in any CAD); dim = native CAD DIMENSION (draggable and associative, but ZWCAD "
        "2025 reports it as invalid)",
    "摆在阵列最左边、与板子固定距离的块":
        "Block placed at the far left of the array, at a fixed distance from the modules",
    "负极支线块转多少度：0=正放（插头朝上，和正极行一样，推荐）；180=翻过来挂（块看着是倒的）。公头/母头块不看这个值，自动朝链内":
        "How far to rotate the negative feeder block: 0 = upright (plug up, same as the positive row — recommended); "
        "180 = flipped over (the block looks upside down). Male/female plug blocks ignore this and always face into "
        "the chain.",
    "负极行和正极行的净空；负极支线块是竖的，程序会自动把它的身子让出来（行线再往下挪一个块高），不会压住正极行":
        "Clearance between the negative and the positive row. The negative feeder blocks are vertical, so the program "
        "automatically makes room for them (the row line drops one block height lower) and never covers the positive row.",
    "填了就自动补到链尾、顶最后一串的正极；想让它排在头部就把这里清空、自己放进链里":
        "When filled in, it is appended to the end of the chain and caps the positive of the last string. To put it at "
        "the head instead, clear this and add the block to the chain yourself.",
    "填了才会自动生成负极那一行（头部公头 + 中间负极支线 + 末端母头）":
        "Only when filled in does the negative row get generated automatically (male plug at the head + negative "
        "feeders in the middle + female plug at the tail).",

    # ================= 界面：运行时的按钮/提示 =================
    "开始…": "Starting…",
    "完成": "Done",
    "生成中…": "Generating…",
    "提交…": "Submitting…",
    "共 %s 张，成功 %s 张": "{} drawings, {} succeeded",
    "用CAD打开": "Open in CAD",
    "DXF存桌面": "Save DXF to desktop",
    "CSV存桌面": "Save CSV to desktop",
    "下载DXF": "Download DXF",
    "下载CSV": "Download CSV",
    "下载全部(zip)": "Download all (zip)",
    "打开输出文件夹": "Open output folder",
    "用默认程序打开(DXF)": "Open with default app (DXF)",
    "保存到桌面": "Save to desktop",
    "线长清单存到桌面": "Save wire list to desktop",
    "下载 DXF": "Download DXF",
    "下载线长清单 CSV": "Download wire list CSV",
    "还没有生成文件": "Nothing generated yet",
    "处理中…": "Working…",
    "已保存到 %s": "Saved to {}",
    "已在文件夹里定位": "Shown in the folder",
    "还没有要画的图：先填份数，点“按上面参数铺出 N 行”":
        "Nothing to draw yet: set the count above, then click “Fill N rows from the settings above”",
    "批量模式要先选外框图": "Batch mode needs a frame selected first",
    "阵列模式必须先选外框图": "Array mode needs a frame selected first",
    "先选块": "Pick blocks first",
    "%s%%  %s": "{}%  {}",

    # ================= 界面：在线更新 =================
    "检查中…": "Checking…",
    "检查失败": "Check failed",
    "发现新版本 %s（当前 %s）": "New version {} available (current {})",
    "已是最新（%s）": "Already up to date ({})",
    "现在下载更新吗？": "Download the update now?",
    "（下载完关掉窗口重新打开即生效）": "(close the window and reopen the app after the download to apply it)",
    "版本 %s（已下载，重启生效）": "Version {} (downloaded; restart to apply)",
    "更新已下载完成。": "The update has been downloaded.",
    "请关掉本窗口，重新双击程序即生效。": "Close this window and start the program again to apply it.",
    "✗ 网络错误：%s": "✗ Network error: {}",
    "更新模块不可用（app_update.py 缺失）": "Update module unavailable (app_update.py is missing)",
    "开始下载更新": "Starting the update download",
    "更新下载完成": "Update downloaded",
    "更新失败": "Update failed",
    "连不上更新源（%s）": "Cannot reach the update source ({})",
    "更新服务器上还没有 version.json（HTTP 404）：先用 make_release.py --push 发一次版":
        "No version.json on the update server yet (HTTP 404): publish once with make_release.py --push",
    "%s: HTTP %d（仓库可能是私有的，需要 update_token.txt）":
        "{}: HTTP {} (the repository may be private and needs update_token.txt)",
    "下载代码包失败（%s）": "Failed to download the code package ({})",
    "写临时文件失败（%s: %s）": "Failed to write the temporary file ({}: {})",
    "解压失败（%s: %s）": "Failed to unzip ({}: {})",
    "代码包里没找到可用的 .py 文件": "The package contains no usable .py files",
    "更新已下载：%d 个文件（关掉窗口重新打开即生效）":
        "Update downloaded: {} files (close the window and reopen the app to apply it)",
    "已应用在线更新（%s）": "Online update applied ({})",
    "内置版本": "built-in version",
    "连接更新服务器（%s）": "Connecting to the update server ({})",
    "下载代码包 %d%%": "Downloading the code package {}%",
    "解压": "Extracting",
    "更新已下载": "Update downloaded",

    # ================= 生成结果：进度阶段 =================
    "准备": "Preparing",
    "开始": "Starting",
    "写 DXF 文件": "Writing the DXF file",
    "批量完成": "Batch finished",
    "写入 DXF 字节": "Writing DXF bytes",
    "块定义已并入外框图": "Block definitions merged into the frame",
    "阵列/线束排布完成": "Array/harness layout done",
    "缩放/定位完成": "Scaling/positioning done",
    "画到 CAD 完成（还没保存，你在 CAD 里确认后自己存）":
        "Done drawing into CAD (not saved yet; check it in CAD and save yourself)",

    # ================= 生成结果：批量 / 阵列总流程 =================
    "批量模式还没有要画的图：先填份数、点“铺出 N 行”":
        "Batch mode has nothing to draw: set the count and click “Fill N rows” first",
    "—— 第 %d/%d 张 %s：没选外框图，跳过 ——": "—— {} of {} · {}: no frame selected, skipped ——",
    "—— 第 %d/%d 张 %s（%s，%d 串 × %d 块）——":
        "—— {} of {} · {} ({}, {} strings × {} modules) ——",
    "第 %d/%d 张 · %s": "{} of {} · {}",
    "⚠ %s 没画出来，跳过这张": "⚠ {} could not be generated, this drawing is skipped",
    "—— 第 %d/%d 张画到 CAD ——": "—— {} of {}: drawing into CAD ——",
    "—— 画到 CAD ——": "—— Drawing into CAD ——",
    "⚠ 打包 zip 失败: %s": "⚠ Failed to build the zip: {}",
    "⚠ 线长清单没写成: %s": "⚠ Could not write the wire list: {}",
    "⚠ 画到 CAD 失败: %s: %s": "⚠ Drawing into CAD failed: {}: {}",
    "批量完成：共 %d 张，成功 %d 张": "Batch finished: {} drawings, {} succeeded",
    "⚠ 代码在你启动之后又改过了（启动时 %s，现在 %s）：**关掉这个黑窗口、重新跑一次 python wiring_ui.py**，否则跑的还是旧代码，报错会误导人。":
        "⚠ The code changed after you started this process (started at {}, now {}): **close this window and run "
        "python wiring_ui.py again**, otherwise the old code is still running and error messages will mislead you.",
    "已自动重载改过的代码（%s）": "Reloaded the changed code ({})",
    "生成失败: %s: %s": "Generation failed: {}: {}",
    "没有可用块": "No usable blocks",
    "跳过(无几何/连接点): %s": "Skipped (no geometry/connection points): {}",
    "不认识的操作：%s": "Unknown action: {}",
    "找不到文件：%s": "File not found: {}",
    "复制失败：%s: %s": "Copy failed: {}: {}",

    # ================= 生成结果：链 / 排版 =================
    "%s -> %s : 连 %d 条线": "{} -> {}: {} wires",
    "  线%d: 长 %.2f (由插入位置得出)": "  wire {}: length {} (from the insertion positions)",
    "  线%d: 长 %.2f": "  wire {}: length {}",
    "放块 %s 于 (%.2f, %.2f)": "Place block {} at ({}, {})",
    "放块 %s 于 (%.2f, %.2f) 缩放 x%.4f": "Place block {} at ({}, {}), scale x{}",
    "每串拼法: %s": "String layout: {}",
    "配置: 组件 %s/%s/%s  每串%d块×%d串  板净空%.1f 串净空%.1f":
        "Config: modules {}/{}/{}  {} modules × {} strings  module gap {}  string gap {}",
    "     线束链 %s | 正极支线块=%s 末端公头=%s 末端母头=%s | 负极自动=%s 负极支线块=%s":
        "     harness chain {} | positive feeder={} tail male={} tail female={} | negative auto={} negative feeder={}",
    "     起始块=%s 间距%.0f | 跨接线=%s": "     head block={} gap{} | jumpers={}",
    "阵列: %d 串 x %d 块，%s，板间净空 %.1f / 串间净空 %.1f，内容 %.1f x %.1f":
        "Array: {} strings × {} modules, {}, module gap {} / string gap {}, content {} × {}",
    "BHA/电机: %d 处（%s）": "BHA / motor: {} position(s) ({})",
    "BHA/电机: 位置写“每段”的条目按“每段中点各一处”处理（共 %d 段）→ 落在 %s":
        "BHA / motor: entries placed at \u201ceach section\u201d become one per bracket section "
        "({} section(s)) → landing at {}",
    "⚠ BHA/电机: 这些条目的位置算不出落在哪一串（共 %d 串），已跳过：%s":
        "⚠ BHA / motor: these positions cannot be mapped to a string (out of {} strings) and were skipped: {}",
    "整排第 %d 块处（串%d内第 %d 块之后）: %s （桩宽 %.1f，左净空 %.1f 右净空 %.1f）→ 它右边的板整体右移 %.1f%s":
        "After module {} of the row (string {} after module {}): {} (stub width {}, gap left {} gap right {}) "
        "→ modules to its right shift right by {}{}",
    "；%s 挂在桩上，旋转 %.0f°": "; {} mounted on the stub, rotated {}°",
    "BHA 桩: 插了 %d 处（%s）；阵列宽度按插入后的实际排布重算":
        "BHA stubs: {} inserted ({}); the array width is recomputed from the actual layout",
    "BHA 桩/电机: 画了 %d 个槽位": "BHA stub / motor: {} slot(s) drawn",
    "提示: BHA 桩块里没有左右 CONN 接点 —— 串内连线会从板直接连到板、穿过桩；想要线接在桩上，在桩块左右各标一个 CONN 层的 POINT":
        "Note: the BHA stub block has no left/right CONN points — the in-string wire goes from module "
        "to module straight through the stub; to land the wire on the stub, put one CONN POINT on each "
        "side of it in CAD",
    "⚠ 外框图和块库里都没有块 %s（BHA 桩/电机），这一处跳过":
        "⚠ Neither the frame nor the block library has block {} (BHA stub / motor); this position is skipped",
    "串内连线: 桩块 %s 没有左右接点，这几条线从板直接连到板（穿过桩）":
        "In-string wiring: stub block(s) {} have no left/right contact points, so those wires run from "
        "module to module (through the stub)",
    "串从左往右接": "strings laid out left to right",
    "串从上往下叠": "strings stacked top to bottom",
    "（只缩不放）": " (shrink only, never enlarge)",
    "在外框内": "inside the frame",
    "⚠ 超出外框了": "⚠ outside the frame",
    "⚠ 板间净空 %.2f < 0，相邻两块会重叠": "⚠ Module gap {} < 0: adjacent modules will overlap",
    "⚠ 串间净空 %.2f < 0，相邻两串会重叠": "⚠ String gap {} < 0: adjacent strings will overlap",
    "可用区 %.0f x %.0f，内容 %.0f x %.0f，k=%.4f%s": "Usable area {} × {}, content {} × {}, k={}{}",
    "k=%.3f 偏小，按 13.7 先把间距压到下限再算一次":
        "k={} is too small; according to rule 13.7 the gaps are first squeezed to the lower bound and recomputed",
    "压缩间距后: 板间净空 %.1f 串间净空 %.1f，k=%.4f":
        "After squeezing the gaps: module gap {} string gap {}, k={}",
    "⚠ 装不进当前外框：%d 串 x %d 块（k<%.2f）在可用区里最多约 %d 串 x %d 块，请减少串数/板数，或换更大的框":
        "⚠ Does not fit the current frame: {} strings × {} modules (k<{}) fits at most about {} strings × {} "
        "modules; reduce the strings/modules or use a bigger frame",
    "整体适配画图区: 等比 x%.4f": "Fit to the drawing area: uniform scale x{}",
    "套用外框图: %s (画图区 %s, 内容偏移 %.1f, %.1f)":
        "Applied frame: {} (drawing area {}, content offset {}, {})",
    "套用外框图: %s（画图区 %s，内容偏移 %.1f, %.1f）":
        "Applied frame: {} (drawing area {}, content offset {}, {})",
    "套用外框图: %s (内容偏移 %.1f, %.1f)": "Applied frame: {} (content offset {}, {})",
    "已在每串首块正极打 CONN_POS、末块负极打 CONN_NEG（各 %d 个）":
        "Added CONN_POS to the positive of each string's first module and CONN_NEG to the negative of the last "
        "({} each)",
    "线束缩放: %s（所有线束块按“接点间距一致”缩放；板子出线点间距 %.2f）":
        "Harness scale: {} (all harness blocks are scaled so their contact spacing matches; module lead spacing {})",
    "⚠ 外框图里没有标注样式(DIMSTYLE)，线号退回文字标注":
        "⚠ The frame has no dimension style (DIMSTYLE); wire numbers fall back to plain text",
    "串内不画连线（组件块的图形本身就是贴着的；要画就把 string_wires 打开）":
        "No wiring inside a string (module blocks already touch each other; turn string_wires on to draw it)",
    "线号标注: 标注外观（尺寸线/尺寸界线/箭头+文字，全是普通实体）%d 个":
        "Wire numbers: dimension look (dimension/extension lines and arrows + text, all plain entities) {} pcs",
    "线号标注: CAD 原生线性标注 %d 个（样式 %s，标注点=CONN-Label）":
        "Wire numbers: {} native CAD linear dimensions (style {}, points = CONN-Label)",
    "⚠ 原生标注块并入后结构检查没过，线号退回文字标注: %s":
        "⚠ Structural check failed after merging the native dimension blocks; wire numbers fall back to text: {}",
    "⚠ 原生标注生成失败(%s)，线号退回文字标注":
        "⚠ Failed to create native dimensions ({}); wire numbers fall back to text",

    # ================= 生成结果：补块 / 手工内容 / 检查 =================
    "合并子块定义 %d 个": "Merged {} sub-block definitions",
    "⚠ 外框图里没有 %s 的块定义，跳过": "⚠ The frame has no block definition for {}; skipped",
    "⚠ 块库里没有 %s，外框里也没有定义，跳过":
        "⚠ {} is not in the block library and not defined in the frame; skipped",
    "注意：外框图本身就有结构问题（%s），补块只看新增问题":
        "Note: the frame itself already has structural problems ({}); only newly added problems are checked",
    "⚠ 补块定义后结构检查没过，退回外框原字节: %s":
        "⚠ Structural check failed after adding block definitions; falling back to the original frame bytes: {}",
    "已把块库定义并入外框图: %s": "Merged block-library definitions into the frame: {}",
    "⚠ 补块定义失败(%s)，这些块可能不显示":
        "⚠ Failed to add block definitions ({}); these blocks may not show up",
    "⚠ 保留手工内容失败(读不了 %s): %s": "⚠ Could not keep manual content (cannot read {}): {}",
    "保留手工内容: 从 %s 搬来 %d 个 %s* 层实体":
        "Kept manual content: from {} moved {} entities on {}* layers",
    "保留手工内容: %s 里没有 %s* 层的实体": "Kept manual content: {} has no entities on {}* layers",
    "⚠ 手工内容里引用了外框图里没有的块(可能不显示): %s":
        "⚠ The manual content references blocks the frame does not define (they may not show up): {}",
    "⚠ 找不到要保留手工内容的文件: %s": "⚠ Cannot find the file to keep manual content from: {}",
    "⚠ 外框里没有这些块定义(可能不显示): %s":
        "⚠ The frame has no definitions for these blocks (they may not show up): {}",
    "⚠ 连线端点检查: %d 个端点没落在 CONN 点上 %s":
        "⚠ Wire endpoint check: {} endpoints do not land on CONN points: {}",
    "连线端点检查: 全部落在 CONN 点上（图上共 %d 个 CONN 点）":
        "Wire endpoint check: all endpoints land on CONN points ({} CONN points on the drawing)",
    "⚠ 连线端点检查: %d 个端点没落在接点/锚点上 %s":
        "⚠ Wire endpoint check: {} endpoints do not land on contacts/anchors: {}",
    "连线端点检查: 全部落在接点/锚点上（图上 CONN 点 %d 个，阵列端子/线束顶端锚点 %d 个）":
        "Wire endpoint check: all endpoints land on contacts/anchors ({} CONN points, {} array-terminal/harness-top "
        "anchors)",
    "连线端点检查失败: %s": "Wire endpoint check failed: {}",
    "内容范围 x %.0f..%.0f  y %.0f..%.0f（画图区 %.0f..%.0f / %.0f..%.0f）：%s":
        "Content extents x {}..{}  y {}..{} (drawing area {}..{} / {}..{}): {}",
    "内容范围检查失败: %s": "Content extents check failed: {}",
    "缺文件: %s": "Missing file: {}",
    "块 %s 没有 CONN_POS/CONN_NEG 点，用底边兜底：左1/3=正极，右2/3=负极":
        "Block {} has no CONN_POS/CONN_NEG points; falling back to the bottom edge: left 1/3 = positive, "
        "right 2/3 = negative",

    # ================= 生成结果：支线 / 线束 =================
    "线束链没填：自动生成 %s": "Harness chain is empty: auto-generated {}",
    "正极支线 %d 根、串数 %d：自动补到 %d 根":
        "Positive feeders {} for {} strings: auto-extended to {}",
    "⚠ “正极支线块”填的 %s 不在线束链里（链里是 %s）：暂时改用链里第一个块 %s 当正极支线（按各串 CONNPOS 从左往右钉）":
        "⚠ The “positive feeder block” {} is not in the harness chain (the chain has {}): temporarily using the "
        "first block of the chain, {}, as the positive feeder (pinned left to right on each string's CONNPOS)",
    "⚠ “正极支线块”填的 %s 不在线束链里，而且链是空的":
        "⚠ The “positive feeder block” {} is not in the harness chain, and the chain is empty",
    "⚠ “负极支线块”填的 %s 不在线束链里":
        "⚠ The “negative feeder block” {} is not in the harness chain",
    "⚠ 正极支线只有 %d 根、串数 %d：多出来的串没有正极支线（把末端公头块填上，它顶一根）":
        "⚠ Only {} positive feeders for {} strings: the extra strings get no positive feeder (fill in the tail "
        "male plug block — it covers one)",
    "⚠ 正极支线只有 %d 个、串数 %d：多出来的串不画跨接线":
        "⚠ Only {} positive feeders for {} strings: the extra strings get no jumper",
    "⚠ 负极支线只有 %d 根、串数 %d": "⚠ Only {} negative feeders for {} strings",
    "跨接线：按你的要求不画（正极支线不接板子）；需要时打开“阵列↔线束 跨接线”":
        "Jumpers: not drawn as requested (the positive feeders do not touch the modules); turn on "
        "“array↔harness jumpers” when you need them",
    "⚠ 线束里没有 %s，跨接线不画": "⚠ {} is not in the harness; jumpers are not drawn",
    "线束里没填负极支线块/末端母头块，负极跨接线不画":
        "No negative feeder block / tail female plug block in the harness: negative jumpers are not drawn",

    # ================= 生成结果：外框图 / DXF 结构诊断（blockpack） =================
    "合并块 %s（%d 个子块, %d 个实体）": "Merge block {} ({} sub-blocks, {} entities)",
    "外框图里已有，跳过: %s": "Already present in the frame; skipped: {}",
    "跳过 %s（读取失败: %s）": "Skipped {} (read failed: {})",
    "跳过 %s（model space 没有实体）": "Skipped {} (no entities in model space)",
    "结尾没有换行": "The file does not end with a newline",
    "结尾没有 EOF": "The file does not end with EOF",
    "SECTION / ENDSEC 数量不匹配": "SECTION / ENDSEC count mismatch",
    "BLOCK / ENDBLK 数量不匹配": "BLOCK / ENDBLK count mismatch",
    "句柄重复 %d 个（例: %s）": "{} duplicate handles (e.g. {})",
    "$HANDSEED(%s) 不大于最大句柄(%X)": "$HANDSEED({}) is not greater than the largest handle ({})",
    "$HANDSEED 不是十六进制": "$HANDSEED is not hexadecimal",
    "没有 BLOCK_RECORD 表": "No BLOCK_RECORD table",
    "没有 BLOCKS 段": "No BLOCKS section",
    "块 %r 有定义但没有 BLOCK_RECORD": "Block {} is defined but has no BLOCK_RECORD",
    "块 %r 有 BLOCK_RECORD 但没有定义": "Block {} has a BLOCK_RECORD but no definition",
    "缺少块记录: %s": "Missing block record: {}",
    "缺少块定义: %s": "Missing block definition: {}",
    "缺少段: %s": "Missing section: {}",

    # ================= 生成结果：画到 CAD =================
    "⚠ 画到 CAD 需要 pywin32：装一个 pip install pywin32":
        "⚠ Drawing into CAD needs pywin32: install it with pip install pywin32",
    "⚠ 画到 CAD 需要 pywin32：pip install pywin32":
        "⚠ Drawing into CAD needs pywin32: pip install pywin32",
    "已连上 CAD: %s（版本 %s）": "Connected to CAD: {} (version {})",
    "已连上 CAD: %s": "Connected to CAD: {}",
    "⚠ 连不上 CAD（ZWCAD 装了没 / 是不是被权限挡住）: %s":
        "⚠ Cannot connect to CAD (is ZWCAD installed / blocked by permissions?): {}",
    "⚠ 块 %s 在图里和 DXF 里都没有定义，跳过":
        "⚠ Block {} is defined neither in the drawing nor in the DXF; skipped",
    "块 %s 已存在但是空的，补画了 %d 个图元":
        "Block {} already existed but was empty; drew {} entities into it",
    "⚠ 检查已有块 %s 失败: %s": "⚠ Failed to inspect the existing block {}: {}",
    "⚠ 建块 %s 失败: %s": "⚠ Failed to create block {}: {}",
    "现造块定义 %s（展平后 %d 个图元；原来是嵌套块/样条线的话会变成折线）":
        "Created the block definition {} on the fly ({} entities after flattening; nested blocks/splines become "
        "polylines)",
    "正在画 %d 个实体（块定义/连线/文字）…": "Drawing {} entities (block definitions/wires/text)…",
    "正在画实体 %d/%d…": "Drawing entity {}/{}…",
    "⚠ 画 %s 失败: %s": "⚠ Failed to draw {}: {}",
    "正在加标注（长度 %d 个 + 线号）…": "Adding labels ({} lengths + wire numbers)…",
    "⚠ 加尺寸标注失败: %s": "⚠ Failed to add dimensions: {}",
    "⚠ 加引线标注失败(%s)，改画普通文字: %s":
        "⚠ Failed to add leader dimensions ({}); drawing plain text instead: {}",
    "⚠ 找不到 DWG: %s（画到 CAD 需要 DWG，不能是 DXF）":
        "⚠ DWG not found: {} (drawing into CAD needs a DWG, not a DXF)",
    "正在连接 CAD（ZWCAD / AutoCAD）…（没开的话会自动启动，可能要等十几秒）":
        "Connecting to CAD (ZWCAD / AutoCAD)… (it will be launched if it is not open; this can take ~10 seconds)",
    "⚠ ZWCAD 里还有命令在跑（CMDACTIVE=%s）：回到命令提示符、把弹窗关掉，再点一次生成。":
        "⚠ A command is still running inside ZWCAD (CMDACTIVE={}): get back to the command prompt, close any dialog "
        "box, then click Generate again.",
    "准备目标图：%s": "Preparing the target drawing: {}",
    "⚠ 复制副本失败(%s)，直接画在原文件上":
        "⚠ Failed to make a copy ({}); drawing on the original file instead",
    "画到: %s%s": "Drawing on: {}{}",
    "清掉上次程序画的连线/标注…": "Clearing the wires/labels drawn by the previous run…",
    "先清掉上一次程序画的内容 %d 个（只删 %s 层和这几个块的插入）":
        "Cleared {} items from the previous run (only the {} layers and the inserts of these blocks)",
    "实体画完，正在加长度标注/线号，并缩放到范围":
        "Entities drawn; adding length dimensions/wire numbers and zooming to the extents",
    "画进 CAD: INSERT %d、直线 %d、多段线 %d、文字 %d、点 %d（跳过的 %d），另外现造块定义用了 %d 个图元":
        "Drawn into CAD: INSERT {}, lines {}, polylines {}, text {}, points {} (skipped {}), plus {} entities for "
        "blocks created on the fly",
    "没有自动保存，你在 CAD 里看过再决定存不存。":
        "Nothing is saved automatically — check it in CAD and decide whether to save.",
}


def js_table():
    """给界面注入的 JSON 对照表（中文原句 -> 英文）。"""
    return json.dumps(EN, ensure_ascii=False)


# ------------------------------------------------------------ 记住语言选择 ----
_LOCK = threading.Lock()
_cache = {"lang": None}


def _settings_path():
    """语言选择存在用户目录里（不放程序目录，免得污染 git 仓库）。"""
    root = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(root, "Single line-CAD", "settings.json")


def get_pref():
    with _LOCK:
        if _cache["lang"] in LANGS:
            return _cache["lang"]
        try:
            with open(_settings_path(), encoding="utf-8") as f:
                lang = str(json.load(f).get("lang", "")).strip().lower()
        except Exception:
            lang = ""
        _cache["lang"] = lang if lang in LANGS else DEFAULT
        return _cache["lang"]


def set_pref(lang):
    lang = str(lang or "").strip().lower()
    if lang not in LANGS:
        return get_pref()
    with _LOCK:
        _cache["lang"] = lang
    try:
        p = _settings_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        data = {}
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    data = json.load(f) or {}
            except Exception:
                data = {}
        data["lang"] = lang
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass                     # 存不下来也不影响本次使用
    return lang


if __name__ == "__main__":
    print("语言:", get_pref())
    print("对照表 %d 条" % len(EN))
