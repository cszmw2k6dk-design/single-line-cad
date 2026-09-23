#!/usr/bin/env python3
"""
wiring_raw.py -- Single line-CAD【不展平】：把块里的实体原样搬运(仅整体平移)，保留所有实体类型。

原理：
  - 以第一个块文件作为“容器”(host)，保留它的 HEADER/TABLES/BLOCKS(含嵌套块/图层定义)。
  - 每个实例：把该块文件 model-space 的实体【原样复制】，坐标整体平移到自己插入点；
    坐标码 10/20(及 11-14/21-24，排除 ELLIPSE/MTEXT 的方向向量)加偏移。
  - 复制时去掉 handle(5) 与 owner(330)，避免句柄冲突(AutoCAD 导入时重新分配)。
  - 其它块的图层/嵌套块定义按需合并进 host。
  - 最后追加我们的连线(WIRE)与线长标注(TEXT)。

这样 SPLINE/ELLIPSE/HATCH 等全部原样保留，画面与块库一致。
"""

import os
import sys
import math
import re
import shutil
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import blockui_server as ui
import connect_library as cl
import blockpack as bp


def _num(v, d):
    try:
        return "%.6f" % (float(v) + d)
    except (TypeError, ValueError):
        return v


def parse_sections_text(txt):
    """把 DXF 文本切成 ({section_name: [(code, value), ...]}, [顺序])。"""
    txt = txt.replace("\r\n", "\n").replace("\r", "\n")
    lines = txt.split("\n")
    pairs = []
    i = 0
    while i + 1 < len(lines):
        pairs.append((lines[i].strip(), lines[i + 1].strip()))
        i += 2
    sections = {}
    order = []
    j = 0
    while j < len(pairs):
        if pairs[j] == ("0", "SECTION"):
            name = pairs[j + 1][1] if j + 1 < len(pairs) and pairs[j + 1][0] == "2" else None
            body = []
            k = j + 2
            while k < len(pairs) and pairs[k] != ("0", "ENDSEC"):
                body.append(pairs[k])
                k += 1
            if name:
                sections[name] = body
                order.append(name)
            j = k + 1
        else:
            j += 1
    return sections, order


def parse_sections_bytes(data, enc="latin-1"):
    """解析内存里的 DXF 字节（配合 blockpack 的定点插入）。"""
    return parse_sections_text(data.decode(enc, errors="replace"))


def parse_sections(path, enc="latin-1"):
    """返回 ({section_name: [ (code, value), ... ]}, [顺序])。"""
    # 用 latin-1 读(字节透明)：保留 ANSI_936(GBK) 等原字节，避免中文被损坏
    txt = open(path, encoding=enc, errors="replace").read()
    return parse_sections_text(txt)


def read_dxf_text(path):
    """读 DXF 文本：文件常见 ANSI_936(GBK)，也有 UTF-8；都不行就按 latin-1 字节透明读。

    预览要显示块里的中文时用它——用错编码中文会变成乱码。
    """
    data = open(path, "rb").read()
    for enc in ("gbk", "utf-8"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def max_handle(sections):
    mx = 0
    for body in sections.values():
        for c, v in body:
            if c == "5":
                try:
                    mx = max(mx, int(v, 16))
                except (TypeError, ValueError):
                    pass
    return mx


def model_space_handle(sections):
    t = sections.get("TABLES", [])
    i = 0
    while i < len(t):
        if t[i] == ("0", "BLOCK_RECORD"):
            j = i + 1; name = None; hnd = None
            while j < len(t) and t[j][0] != "0":
                if t[j][0] == "2" and name is None:
                    name = t[j][1]
                if t[j][0] == "5" and hnd is None:
                    hnd = t[j][1]
                j += 1
            if name and name.upper() == "*MODEL_SPACE":
                return hnd
            i = j
        else:
            i += 1
    return None


def clone_entity(ent, dx, dy, handle, owner):
    """复制实体：分配新句柄、合并 owner，并平移坐标(不删句柄)。"""
    etype = ent[0][1]
    out = []
    has5 = has330 = False
    skip102 = False
    for c, v in ent:
        # 去掉 102 {ACAD_XDICTIONARY / ACAD_REACTORS ... } 组：跨文件后引用会悬空
        if c == "102":
            if str(v).strip() in ("{ACAD_XDICTIONARY", "{ACAD_REACTORS"):
                skip102 = True
                continue
            if str(v).strip() == "}":
                skip102 = False
                continue
        if skip102:
            continue
        if c == "5":
            out.append((c, handle)); has5 = True; continue
        if c == "330":
            out.append((c, owner if owner else v)); has330 = True; continue
        if c == "10":
            v = _num(v, dx)
        elif c == "20":
            v = _num(v, dy)
        elif c in ("11", "12", "13", "14"):
            if not (etype in _NO_OFF and c in _NO_OFF[etype]):
                v = _num(v, dx)
        elif c in ("21", "22", "23", "24"):
            if not (etype in _NO_OFF and c in _NO_OFF[etype]):
                v = _num(v, dy)
        out.append((c, v))
    if not has5:
        out.insert(1, ("5", handle))
    if not has330 and owner:
        out.insert(2, ("330", owner))
    return out


def update_handseed(header, h):
    out = []
    i = 0
    while i < len(header):
        if header[i] == ("9", "$HANDSEED"):
            out.append(header[i]); out.append(("5", "%X" % h)); i += 2; continue
        out.append(header[i]); i += 1
    return out


def emit_section(name, body):
    parts = ["0", "SECTION", "2", name]
    for it in body:
        if it and isinstance(it[0], str):
            parts += [it[0], it[1]]
        else:
            for c, v in it:
                parts += [c, v]
    parts += ["0", "ENDSEC"]
    return "\n".join(parts) + "\n"


def add_layers(tables, desired, have, next_handle):
    """把需要的图层(desired=[(name,color)])并入 host 的 LAYER 表(不重复)。"""
    add = []
    for nm, color in desired:
        if nm and nm not in have:
            have.add(nm)
            add.append([("0", "LAYER"), ("5", next_handle()), ("330", "0"),
                        ("100", "AcDbSymbolTableRecord"), ("100", "AcDbLayerTableRecord"),
                        ("2", nm), ("70", "0"), ("62", str(color)), ("6", "CONTINUOUS")])
    if not add:
        return tables
    out = list(tables)
    for i in range(len(out) - 1):
        if out[i] == ("0", "TABLE") and out[i + 1] == ("2", "LAYER"):
            k = i + 2
            cnt_idx = None; endtab = None
            while k < len(out):
                if out[k][0] == "70" and cnt_idx is None:
                    cnt_idx = k
                if out[k] == ("0", "ENDTAB"):
                    endtab = k; break
                k += 1
            if cnt_idx is not None:
                try:
                    out[cnt_idx] = ("70", str(int(out[cnt_idx][1]) + len(add)))
                except (TypeError, ValueError):
                    pass
            if endtab is not None:
                ins = []
                for rec in add:
                    ins += rec
                out[endtab:endtab] = ins
            break
    return out


def group_entities(pairs):
    """把一串 (code,value) 按 0 分组为实体。"""
    ents = []
    cur = None
    for c, v in pairs:
        if c == "0":
            if cur is not None:
                ents.append(cur)
            cur = [(c, v)]
        else:
            if cur is None:
                cur = [(c, v)]
            else:
                cur.append((c, v))
    if cur is not None:
        ents.append(cur)
    return ents


_NO_OFF = {"ELLIPSE": {"11", "21"}, "MTEXT": {"11", "21"}}  # 这些是方向向量，不平移


def translate_entity(ent, dx, dy):
    etype = ent[0][1]
    out = []
    for c, v in ent:
        if c in ("5", "330"):          # 去掉句柄/所有者，避免冲突
            continue
        if c == "10":
            v = _num(v, dx)
        elif c == "20":
            v = _num(v, dy)
        elif c in ("11", "12", "13", "14"):
            if not (etype in _NO_OFF and c in _NO_OFF[etype]):
                v = _num(v, dx)
        elif c in ("21", "22", "23", "24"):
            if not (etype in _NO_OFF and c in _NO_OFF[etype]):
                v = _num(v, dy)
        out.append((c, v))
    return out


def count_types(ents):
    from collections import Counter
    return Counter(e[0][1] for e in ents if e and e[0][0] == "0")


def layer_records(tables):
    """从 TABLES 段里取所有 LAYER 记录。"""
    recs = []
    i = 0
    while i < len(tables):
        if tables[i] == ("0", "LAYER"):
            cur = [(tables[i][0], tables[i][1])]
            i += 1
            while i < len(tables) and tables[i][0] != "0":
                cur.append(tables[i]); i += 1
            recs.append(cur)
        else:
            i += 1
    return recs


def layer_name(rec):
    for c, v in rec:
        if c == "2":
            return v
    return None


def _tbl_handle(tables, table_name):
    for i in range(len(tables) - 1):
        if tables[i] == ("0", "TABLE") and tables[i + 1] == ("2", table_name):
            k = i + 2
            while k < len(tables) and tables[k][0] != "0":
                if tables[k][0] == "5":
                    return tables[k][1]
                k += 1
    return None


def _br_names(tables):
    names = set(); i = 0
    while i < len(tables):
        if tables[i] == ("0", "BLOCK_RECORD"):
            j = i + 1; nm = None
            while j < len(tables) and tables[j][0] != "0":
                if tables[j][0] == "2" and nm is None:
                    nm = tables[j][1]
                j += 1
            if nm:
                names.add(nm)
            i = j
        else:
            i += 1
    return names


def _insert_into_table(tables, name, entry):
    out = list(tables)
    for i in range(len(out) - 1):
        if out[i] == ("0", "TABLE") and out[i + 1] == ("2", name):
            k = i + 2; cnt_idx = None; endtab = None
            while k < len(out):
                if out[k][0] == "70" and cnt_idx is None:
                    cnt_idx = k
                if out[k] == ("0", "ENDTAB"):
                    endtab = k; break
                k += 1
            if cnt_idx is not None:
                try:
                    out[cnt_idx] = ("70", str(int(out[cnt_idx][1]) + 1))
                except (TypeError, ValueError):
                    pass
            if endtab is not None:
                out[endtab:endtab] = entry
            break
    return out


def merge_blocks(blocks_body, tables_body, other_paths, next_handle, mspace, log):
    """把其它文件的块定义(BLOCK + BLOCK_RECORD)合并进 host，供 INSERT 解析。"""
    have = _br_names(tables_body)
    brt = _tbl_handle(tables_body, "BLOCK_RECORD")
    added = 0
    for path in other_paths:
        osec, _o = parse_sections(path)
        ob = group_entities(osec.get("BLOCKS", []))
        i = 0
        while i < len(ob):
            if ob[i][0][1] == "BLOCK":
                name = _g1(ob[i], "2")
                inner = []; j = i + 1
                while j < len(ob) and ob[j][0][1] != "ENDBLK":
                    inner.append(ob[j]); j += 1
                endblk = ob[j] if j < len(ob) and ob[j][0][1] == "ENDBLK" else None
                if name and not name.startswith("*") and name not in have:
                    have.add(name)
                    brh = next_handle()
                    entry = [("0", "BLOCK_RECORD"), ("5", brh), ("330", brt or "0"),
                             ("100", "AcDbSymbolTableRecord"), ("100", "AcDbBlockTableRecord"),
                             ("2", name)]
                    tables_body = _insert_into_table(tables_body, "BLOCK_RECORD", entry)
                    blocks_body = blocks_body + clone_entity(ob[i], 0, 0, next_handle(), mspace or "0")
                    for e in inner:
                        blocks_body = blocks_body + clone_entity(e, 0, 0, next_handle(), brh)
                    if endblk is not None:
                        blocks_body = blocks_body + clone_entity(endblk, 0, 0, next_handle(), mspace or "0")
                    added += 1
                i = j + 1 if endblk is not None else j
            else:
                i += 1
    if added:
        log.append("合并子块定义 %d 个" % added)
    return blocks_body, tables_body


def _prims_bbox(prims):
    xs = []; ys = []
    for p in prims:
        # MLEADER 层是“多行引线”抢救出来的文字/引线：坐标常常在块外很远，
        # 算包围盒时跳过它，免得把块撑大（排版会跟着跑偏）。
        _lay = (p[3] if (p[0] == "poly" and len(p) > 3)
                else p[4] if (p[0] == "circle" and len(p) > 4)
                else p[5] if len(p) > 5 else "")
        if str(_lay or "").upper() == "MLEADER":
            continue
        if p[0] == "poly":
            for x, y in p[1]:
                xs.append(x); ys.append(y)
        elif p[0] == "circle":
            xs += [p[1] - p[3], p[1] + p[3]]; ys += [p[2] - p[3], p[2] + p[3]]
        elif p[0] == "text":
            xs.append(p[1]); ys.append(p[2])
    return (min(xs), max(xs), min(ys), max(ys)) if xs else None


def shift_prims(prims, dx, dy):
    """把展平图元整体平移（预览用；不改原列表）。"""
    out = []
    for p in prims:
        if p[0] == "poly":
            out.append(("poly", [(x + dx, y + dy) for x, y in p[1]],
                        p[2], p[3] if len(p) > 3 else "",
                        p[4] if len(p) > 4 else 0))
        elif p[0] == "circle":
            out.append(("circle", p[1] + dx, p[2] + dy, p[3],
                        p[4] if len(p) > 4 else "", p[5] if len(p) > 5 else 0))
        elif p[0] == "text":
            out.append(("text", p[1] + dx, p[2] + dy, p[3], p[4],
                        p[5] if len(p) > 5 else "", p[6] if len(p) > 6 else 0))
    return out


def rect_prims(x0, y0, x1, y1, layer="0", color=0):
    """一个方框（预览里的“一块板/一个桩”）。"""
    return [("poly", [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)],
             True, layer, color)]


def _records_bbox(records, blocks):
    pr = []
    _prim_list(records, blocks, (1, 0, 0, 1, 0, 0), 0, pr)
    return _prims_bbox(pr)


def frame_draw_rect(sec, min_ratio=0.05):
    """外框图的“画图区”矩形 = **内框**（纸边往里那一圈），再扣掉标题栏那一条。

    以前取的是面积最大的闭合矩形，那是**纸边/外框**：内容会画到内框外面，
    甚至压到标题栏上（用户要的是“绘图区域不要超过里面的内框”）。现在的口径：
      1. 面积最大的闭合矩形 = 外框（纸边）；
      2. 它里面、面积 ≥ 外框 60% 的最大闭合矩形 = 内框（往里那一圈）；
      3. 内框里“从上到下贯通”的竖线 = 标题栏左边线；“从左到右贯通”的横线 =
         标题栏上边线。有就把标题栏那一条切掉，只留真正的绘图区。
    找不到闭合矩形时返回 None。
    """
    bmap = _blocks_map(sec)
    prims = []
    _prim_list(group_entities(sec.get("ENTITIES", [])), bmap,
               (1, 0, 0, 1, 0, 0), 0, prims)
    allbox = _prims_bbox(prims)
    rects = []
    for p in prims:
        if p[0] != "poly" or len(p) < 3 or not p[2] or len(p[1]) < 4:
            continue
        xs = [q[0] for q in p[1]]
        ys = [q[1] for q in p[1]]
        w, h = max(xs) - min(xs), max(ys) - min(ys)
        if w <= 1e-6 or h <= 1e-6:
            continue
        if allbox:
            if w * h < (allbox[1] - allbox[0]) * (allbox[3] - allbox[2]) * min_ratio:
                continue
        rects.append((w * h, (min(xs), max(xs), min(ys), max(ys))))
    if not rects:
        return None
    rects.sort(reverse=True)
    outer = rects[0][1]
    inner = None
    for area, bb in rects[1:]:
        if area < rects[0][0] * 0.6:
            break
        if (bb[0] >= outer[0] - 1e-6 and bb[1] <= outer[1] + 1e-6
                and bb[2] >= outer[2] - 1e-6 and bb[3] <= outer[3] + 1e-6):
            inner = bb
            break
    x0, x1, y0, y1 = inner or outer
    w, h = x1 - x0, y1 - y0
    # 标题栏：内框里贯通整高/整宽的边线（取最靠里的那条，宁可留多点白）
    cut_r = cut_t = None
    for p in prims:
        if p[0] != "poly" or len(p) < 2:
            continue
        v = p[1]
        ring = v + ([v[0]] if (len(p) > 2 and p[2]) else [])
        for a, b in zip(v, ring[1:]):
            ax, ay = a[0], a[1]
            bx, by = b[0], b[1]
            if abs(ax - bx) < 1e-6:                    # 竖线
                ya, yb = min(ay, by), max(ay, by)
                if ((yb - ya) >= h * 0.8 and ya >= y0 - 1.0 and yb <= y1 + 1.0
                        and x0 + w * 0.3 < ax < x1 - 1e-6):
                    cut_r = ax if cut_r is None else min(cut_r, ax)
            elif abs(ay - by) < 1e-6:                  # 横线
                xa, xb = min(ax, bx), max(ax, bx)
                if ((xb - xa) >= w * 0.8 and xa >= x0 - 1.0 and xb <= x1 + 1.0
                        and y0 + h * 0.3 < ay < y1 - 1e-6):
                    cut_t = ay if cut_t is None else max(cut_t, ay)
    if cut_r is not None:
        x1 = cut_r
    if cut_t is not None:
        y0 = cut_t
    return (x0, x1, y0, y1)


# ================= 线号标注：CAD 原生线性标注（DIMENSION） =================
# 用户要求：标注要用 CAD 原生的“线性标注”，标注点取块里 CONN-Label 层的点。
# 原生标注 = DIMENSION 实体 + 它引用的（缓存）块 —— 块里装尺寸界线/尺寸线/箭头/文字。
# 这里完全照外框模板里 ZWCAD 自己写出来的那份结构生成（模板里的 *D14 就是这个格式）：
#   DIMENSION：10=尺寸线位置 11=文字中点(= CONN-Label 点) 13/14=两条界线原点
#              42=实测长度 1=文字覆盖 3=标注样式 50=旋转角 70=128
#              末尾带 ACAD/DSTYLE xdata：140=文字高 41=箭头大小（这样在 CAD 里
#              “标注更新/拉伸”之后还是同一副样子）
#   缓存块  ：2×尺寸界线 + 1×尺寸线 + 2×箭头(SOLID) + 1×MTEXT + 3×DEFPOINTS 点

# 标注样式优先级：ISO-25 是外框模板里 ZWCAD 自己那批标注在用的样式（值也正常）；
# SLDDIMSTYLE2 那套的值是按别的比例做的（DIMTXT=700），放最后。
_DIM_STYLE_PREF = ("ISO-25", "Voltage", "SLDDIMSTYLE2", "Standard")


def dim_style_name(sec):
    """外框里已有的标注样式名，优先项目自己的；一个都没有就返回空串。"""
    names = []
    tb = sec.get("TABLES", [])
    i = 0
    while i < len(tb):
        if tb[i] == ("0", "DIMSTYLE"):
            j, nm = i + 1, ""
            while j < len(tb) and tb[j][0] != "0":
                if tb[j][0] == "2" and not nm:
                    nm = tb[j][1]
                j += 1
            if nm:
                names.append(nm)
            i = j
        else:
            i += 1
    for p in _DIM_STYLE_PREF:
        if p in names:
            return p
    return names[0] if names else ""


def _dim_line(p1, p2, layer="0", color=0):
    # 记录里必须带 owner(330)：没有的话并进外框后就是“没有属主的实体”，CAD 会判文件无效。
    # pack 会把它改写成新块记录的句柄。
    out = [("0", "LINE"), ("330", "0"), ("100", "AcDbEntity"), ("8", layer)]
    if color:
        out.append(("62", str(int(color))))
    out += [("100", "AcDbLine"),
            ("10", "%.6f" % p1[0]), ("20", "%.6f" % p1[1]), ("30", "0.0"),
            ("11", "%.6f" % p2[0]), ("21", "%.6f" % p2[1]), ("31", "0.0")]
    return out


def _dim_solid(tip, base1, base2, layer="0", color=0):
    """箭头：尖在 tip，底边是 base1-base2（照模板用 SOLID）。"""
    out = [("0", "SOLID"), ("330", "0"), ("100", "AcDbEntity"), ("8", layer)]
    if color:
        out.append(("62", str(int(color))))
    out += [("100", "AcDbTrace"),
            ("10", "%.6f" % base1[0]), ("20", "%.6f" % base1[1]), ("30", "0.0"),
            ("11", "%.6f" % base2[0]), ("21", "%.6f" % base2[1]), ("30", "0.0"),
            ("12", "%.6f" % tip[0]), ("22", "%.6f" % tip[1]), ("32", "0.0"),
            ("13", "%.6f" % tip[0]), ("23", "%.6f" % tip[1]), ("33", "0.0")]
    return out


def _dim_mtext(pos, txt, h, ang, layer="0", color=0):
    ca, sa = math.cos(ang), math.sin(ang)
    out = [("0", "MTEXT"), ("330", "0"), ("100", "AcDbEntity"), ("8", layer)]
    if color:
        out.append(("62", str(int(color))))
    out += [("100", "AcDbMText"),
            ("10", "%.6f" % pos[0]), ("20", "%.6f" % pos[1]), ("30", "0.0"),
            ("40", "%.4f" % h), ("41", "0.0"), ("46", "0.0"),
            ("71", "5"), ("72", "1"), ("1", txt),
            ("11", "%.6f" % ca), ("21", "%.6f" % sa), ("31", "0.0"),
            ("73", "1"), ("44", "1.0")]
    return out


def _dim_points(pts, layer="DEFPOINTS"):
    out = []
    for x, y in pts:
        out += [("0", "POINT"), ("330", "0"), ("100", "AcDbEntity"), ("8", layer),
                ("100", "AcDbPoint"), ("10", "%.6f" % x), ("20", "%.6f" % y), ("30", "0.0")]
    return out


def dim_geom(a, b, anchor, txt, th):
    """一条“对齐线性标注”的图元（块内容，用图纸坐标）。

    a / b   = 这条线上两个接点（尺寸界线的原点）
    anchor  = 块里 CONN-Label 层的点（尺寸线过它，文字压在它旁边）
    返回 (实体对列表, 几何)，线段长度为 0 时返回 (None, None)。
    """
    dx, dy = b[0] - a[0], b[1] - a[1]
    L = math.hypot(dx, dy)
    if L < 1e-6:
        return None, None
    ux, uy = dx / L, dy / L
    nx, ny = -uy, ux
    asz = th * 0.83                                  # 箭头长（照模板 6.0 字 : 5.0 箭头）
    exo = th * 0.10                                  # 界线起点离原点的距离
    exe = th * 0.21                                  # 界线超出尺寸线的长度
    d = (anchor[0] - a[0]) * nx + (anchor[1] - a[1]) * ny     # 尺寸线相对线段的偏移
    sgn = 1.0 if d >= 0 else -1.0
    if abs(d) < th:                       # 锚点几乎落在导线上：尺寸线让开一点，
        # 否则尺寸线会和导线重合、看不出是标注；完全在导线上时往**下**让（上面是阵列）
        d = (-1.0 if ny > 0 else 1.0) * th * 1.4 if abs(d) < 1e-9 else sgn * th * 1.4
    A = (a[0] + nx * d, a[1] + ny * d)
    B = (b[0] + nx * d, b[1] + ny * d)
    nxs, nys = nx * sgn, ny * sgn
    ents = []
    for (ox, oy), (px, py) in ((a, A), (b, B)):      # 两条尺寸界线
        ents.append(_dim_line((ox + nxs * exo, oy + nys * exo),
                              (px + nxs * exe, py + nys * exe)))
    ents.append(_dim_line((A[0] + ux * asz, A[1] + uy * asz),
                          (B[0] - ux * asz, B[1] - uy * asz)))   # 尺寸线
    hw = asz / 3.0
    ents.append(_dim_solid(A, (A[0] + ux * asz - uy * hw, A[1] + uy * asz + ux * hw),
                           (A[0] + ux * asz + uy * hw, A[1] + uy * asz - ux * hw)))
    ents.append(_dim_solid(B, (B[0] - ux * asz - uy * hw, B[1] - uy * asz + ux * hw),
                           (B[0] - ux * asz + uy * hw, B[1] - uy * asz - ux * hw)))
    tpos = (anchor[0] + nxs * th * 0.62, anchor[1] + nys * th * 0.62)
    ents.append(_dim_mtext(tpos, txt, th, math.atan2(uy, ux)))    # 文字
    ents.append(_dim_points([a, b, A]))                           # 定义点
    return ents, {"A": A, "B": B, "len": L, "ang": math.degrees(math.atan2(uy, ux)),
                  "asz": asz}


def dim_geom_h(o1, o2, y_line, txt, th, asz, color=5):
    """水平“长度标注”的图元：界线从两个 CONN-Label 点竖着上去，尺寸线在同一个高度。

    用户口径（2026-09-22）：
      · 标注写的是**长度**（不是线号）；
      · 左右边界点 = 块里 CONN-Label 层的点（不是接线点）；
      · 标注在线束上方、所有标注线高度一致 → 尺寸线是水平的一条线，y 由调用方
        统一给出；
      · 箭头 3、文字高 7、所有标注线蓝色(5)。
    返回 (实体列表, 几何)。
    """
    (x1, y1), (x2, y2) = o1, o2
    if x2 < x1:                       # 统一成左 -> 右
        (x1, y1), (x2, y2) = (x2, y2), (x1, y1)
    if abs(x2 - x1) < 1e-6:
        return None, None
    sgn = 1.0 if y_line >= max(y1, y2) else -1.0     # 尺寸线在上方还是下方
    exo = th * 0.10                                  # 界线起点离原点的距离
    exe = th * 0.21                                  # 界线超出尺寸线的长度
    ents = []
    for x, y in ((x1, y1), (x2, y2)):                # 两条竖直尺寸界线
        ents.append(_dim_line((x, y + sgn * exo), (x, y_line + sgn * exe),
                              color=color))
    A = (x1, y_line)
    B = (x2, y_line)
    ents.append(_dim_line((x1 + asz, y_line), (x2 - asz, y_line), color=color))
    hw = asz / 3.0
    ents.append(_dim_solid(A, (x1 + asz, y_line + hw), (x1 + asz, y_line - hw),
                           color=color))
    ents.append(_dim_solid(B, (x2 - asz, y_line + hw), (x2 - asz, y_line - hw),
                           color=color))
    # 文字统一放在尺寸线上方（不跟着方向翻）：几行标注的文字才对得齐
    tpos = ((x1 + x2) / 2.0, y_line + th * 0.62)
    ents.append(_dim_mtext(tpos, txt, th, 0.0, color=color))
    return ents, {"x1": x1, "x2": x2, "y": y_line, "len": abs(x2 - x1),
                  "o1": o1, "o2": o2, "asz": asz}


def dim_entity_pairs_h(name, style, o1, o2, y_line, txt, g, th):
    """水平长度标注的 DIMENSION 实体（用“对齐标注”，两端点取 CONN-Label 点）。

    ZWCAD/AutoCAD 的线性标注就是这么存的：13/23、14/24 是两条尺寸界线的原点，
    10/20 是尺寸线要经过的点。这里把尺寸线放在统一高度 y_line 上。
    """
    x1, x2 = g["x1"], g["x2"]
    mx = (x1 + x2) / 2.0
    return [("0", "DIMENSION"), ("100", "AcDbEntity"), ("8", "WIRE_LABEL"),
            ("100", "AcDbDimension"), ("280", "0"),
            ("2", name),
            ("10", "%.6f" % mx), ("20", "%.6f" % y_line), ("30", "0.0"),
            ("11", "%.6f" % mx), ("21", "%.6f" % (y_line + th * 0.62)), ("31", "0.0"),
            ("12", "0.0"), ("22", "0.0"), ("32", "0.0"),
            ("70", "128"),
            ("1", txt), ("71", "5"), ("42", "%.6f" % g["len"]),
            ("73", "0"), ("74", "0"), ("75", "0"),
            ("3", style),
            ("100", "AcDbAlignedDimension"),
            ("13", "%.6f" % o1[0]), ("23", "%.6f" % o1[1]), ("33", "0.0"),
            ("14", "%.6f" % o2[0]), ("24", "%.6f" % o2[1]), ("34", "0.0"),
            ("50", "0.0"),
            ("100", "AcDbRotatedDimension"),
            ("1001", "ACAD"), ("1000", "DSTYLE"), ("1002", "{"),
            ("1070", "140"), ("1040", "%.4f" % th),
            ("1070", "41"), ("1040", "%.4f" % g["asz"]),
            ("1002", "}")]


def dim_entity_pairs(name, style, a, b, anchor, txt, g, th):
    """DIMENSION 实体（对齐线性标注），字段顺序照 ZWCAD 写出来的那份。"""
    return [("0", "DIMENSION"), ("100", "AcDbEntity"), ("8", "WIRE_LABEL"),
            ("100", "AcDbDimension"), ("280", "0"),
            ("2", name),
            ("10", "%.6f" % g["A"][0]), ("20", "%.6f" % g["A"][1]), ("30", "0.0"),
            ("11", "%.6f" % anchor[0]), ("21", "%.6f" % anchor[1]), ("31", "0.0"),
            ("12", "0.0"), ("22", "0.0"), ("32", "0.0"),
            ("70", "128"),
            ("1", txt), ("71", "5"), ("42", "%.6f" % g["len"]),
            ("73", "0"), ("74", "0"), ("75", "0"),
            ("3", style),
            ("100", "AcDbAlignedDimension"),
            ("13", "%.6f" % a[0]), ("23", "%.6f" % a[1]), ("33", "0.0"),
            ("14", "%.6f" % b[0]), ("24", "%.6f" % b[1]), ("34", "0.0"),
            ("50", "%.4f" % g["ang"]),
            ("100", "AcDbRotatedDimension"),
            ("1001", "ACAD"), ("1000", "DSTYLE"), ("1002", "{"),
            ("1070", "140"), ("1040", "%.4f" % th),
            ("1070", "41"), ("1040", "%.4f" % g["asz"]),
            ("1002", "}")]


def dim_block_file(name, ent_records, path):
    """把标注块内容写成一个“块库式”小 DXF（交给 blockpack.pack_into 并进外框）。

    文件名 = 块名；ENTITIES 段 = 块内容；顺带把 WIRE_LABEL 图层定义带上，
    这样外框里没有这个层时也会被补出来（标注实体本身就在这个层上）。
    """
    out = []

    def sec(nm, body):
        out.append(("0", "SECTION"))
        out.append(("2", nm))
        out.extend(body)
        out.append(("0", "ENDSEC"))

    # 图层记录必须是 R2000+ 的完整写法：句柄/owner + 两条 100 子类标记 + 名字。
    # 少写 330 或 100（或者把 2 放到 100 前面）→ CAD 判“无效或不完整的 DXF 输入”，
    # 整张图被放弃。这里照外框图里自带图层记录的格式写。
    sec("TABLES", [("0", "TABLE"), ("2", "LAYER"), ("70", "1"),
                   ("0", "LAYER"), ("5", "1"), ("330", "0"),
                   ("100", "AcDbSymbolTableRecord"), ("100", "AcDbLayerTableRecord"),
                   ("2", "WIRE_LABEL"), ("70", "0"),
                   ("62", "7"), ("6", "Continuous"),
                   ("0", "ENDTAB")])
    sec("ENTITIES", [p for rec in ent_records for p in rec])
    out.append(("0", "EOF"))
    with open(path, "wb") as f:
        f.write("".join("%s\r\n%s\r\n" % (c, v) for c, v in out).encode("utf-8"))
    return path


def renumber_handles(data, handle):
    """把这段实体里的句柄(5)整体换一段新的（返回 bytes，并把 handle[0] 推到后面）。

    用在哪：我们自己画的实体和“后并进来的标注块”都从外框原最大句柄往上发号，
    不挪一段就会撞句柄（CAD 里会报重复句柄/实体错乱）。
    """
    out = bytearray()
    prev = 0
    for g in bp.iter_groups(data):
        start, vstart, vend, end, code, _val = g
        out += data[prev:start]
        if code == b"5":
            newv = ("%X" % handle[0]).encode("ascii")
            handle[0] += 1
            out += data[start:vstart] + newv + data[vend:end]
        else:
            out += data[start:end]
        prev = end
    out += data[prev:]
    return bytes(out)


def _content_bbox(insts, places, scales):
    xs = []; ys = []
    for idx, it in enumerate(insts):
        s = scales[idx]; P = places[idx]["P"]; b = cl.bbox(it["prims"])
        xs += [P[0] + b[0] * s, P[0] + b[1] * s]
        ys += [P[1] + b[2] * s, P[1] + b[3] * s]
    return (min(xs), max(xs), min(ys), max(ys)) if xs else None


def _match_pairs(a_pts, b_pts):
    """按 Y 就近把两组接点一一配对(贪心)。返回 [(i, j), ...]。

    比距离之前，先用两组的“中心高度差”估一个纵向偏移：
    块与块之间的纵向偏移常常比两个接点自己的间距还大，直接比绝对 Y 会把
    上面的接点配到下面那个上（一个对齐、一个错开一整格），线就成斜的了。
    """
    if not a_pts or not b_pts:
        return []
    off = (sum(p[1] for p in a_pts) / len(a_pts)
           - sum(p[1] for p in b_pts) / len(b_pts))
    cand = sorted((abs(a[1] - (b[1] + off)), i, j)
                  for i, a in enumerate(a_pts)
                  for j, b in enumerate(b_pts))
    pi, pj, res = set(), set(), []
    for d, i, j in cand:
        if i in pi or j in pj:
            continue
        pi.add(i); pj.add(j); res.append((i, j))
    return res


def _match_span_scales(insts):
    """接点对齐缩放：把每块“右侧接点间距”缩放到几何平均，返回每块的缩放。

    和链模式第 5 章同一套口径。块右侧接点不足 2 个的（或间距为 0）保持 1.0。
    """
    spans = []
    for it in insts:
        r = cl.side_ports(it["prims"], it["pts"], "right")
        if len(r) >= 2:
            sp = r[0][1] - r[-1][1]
            if sp > 1e-6:
                spans.append(sp)
    if not spans:
        return [1.0] * len(insts)
    target = math.prod(spans) ** (1.0 / len(spans))
    out = []
    for it in insts:
        r = cl.side_ports(it["prims"], it["pts"], "right")
        s = 1.0
        if len(r) >= 2:
            sp = r[0][1] - r[-1][1]
            if sp > 1e-6:
                s = target / sp
        out.append(s)
    return out


def _place_chain(insts, gap, match_span, kscale=1.0):
    """计算每块的插入位置/缩放(与 build_chain_raw 同算法)。返回 (places, scales, log)。

    kscale：把“整个世界”等比放大 kscale 倍（块、间隔一起放大）。
    用于整体适配画图区——只放大间隔、不放大块，会让连线端点对不上接点。
    """
    log = []
    scales = [1.0] * len(insts)
    if match_span:
        spans = []
        for it in insts:
            r = cl.side_ports(it["prims"], it["pts"], "right")
            if len(r) >= 2:
                sp = r[0][1] - r[-1][1]
                if sp > 1e-6:
                    spans.append(sp)
        if spans:
            target = math.prod(spans) ** (1.0 / len(spans))
            for idx, it in enumerate(insts):
                r = cl.side_ports(it["prims"], it["pts"], "right")
                if len(r) >= 2:
                    sp = r[0][1] - r[-1][1]
                    if sp > 1e-6:
                        scales[idx] = target / sp
    if abs(kscale - 1.0) > 1e-12:
        scales = [s * kscale for s in scales]
    places = []
    prev_outs = None; prev_right = None
    for idx, it in enumerate(insts):
        s = scales[idx]
        r = [(x * s, y * s) for x, y in cl.side_ports(it["prims"], it["pts"], "right")]
        l = [(x * s, y * s) for x, y in cl.side_ports(it["prims"], it["pts"], "left")]
        b = cl.bbox(it["prims"])
        bl = (b[0] * s, b[1] * s, b[2] * s, b[3] * s)
        if idx == 0:
            P = (0.0, 0.0)
        else:
            pr = _match_pairs(prev_outs, l)
            offs = sorted(prev_outs[i][1] - l[j][1] for (i, j) in pr)
            off = offs[len(offs) // 2] if offs else 0.0
            P = (prev_right + gap * kscale - bl[0], off)
        outs = [(x + P[0], y + P[1]) for x, y in r]
        lins = [(x + P[0], y + P[1]) for x, y in l]
        places.append({"P": P, "s": s, "outs": outs, "lins": lins, "right": P[0] + bl[1]})
        if idx > 0:
            log.append("%s -> %s : 连 %d 条线" %
                       (insts[idx-1]["name"], it["name"], len(_match_pairs(prev_outs, lins))))
        log.append("放块 %s 于 (%.2f, %.2f) 缩放 x%.4f" % (it["name"], P[0], P[1], s))
        prev_outs = outs; prev_right = places[-1]["right"]
    return places, scales, log


def conn_points_on_drawing(sec):
    """图纸上所有“实际”的 CONN 点坐标。

    做法：遍历 ENTITIES 里的 INSERT，用它的插入点/缩放/旋转，把块定义内部的
    CONN* 层 POINT 变换到图纸坐标。用来核对连线端点是否真的落在接点上。
    """
    bmap = _blocks_map(sec)
    out = []
    for rec in group_entities(sec.get("ENTITIES", [])):
        if rec[0][1] != "INSERT":
            continue
        nm = _g1(rec, "2")
        px, py = _gf(rec, "10"), _gf(rec, "20")
        sx = _gf(rec, "41", 1.0) or 1.0
        sy = _gf(rec, "42", 1.0) or 1.0
        rr = math.radians(_gf(rec, "50", 0.0))
        ca, sa = math.cos(rr), math.sin(rr)
        for x, y, _lay in _conn_points(bmap.get(nm, []), bmap):
            x, y = x * sx, y * sy
            out.append((px + x * ca - y * sa, py + x * sa + y * ca))
    return out


def _conn_points(recs, bmap, mtx=(1, 0, 0, 1, 0, 0), depth=0):
    """把一个块定义里的 CONN* 层 POINT 收集出来（**会递归进子块**），返回块局部坐标。

    为什么必须递归：从别的图里抽出来的块（extract_blocks.py 的产物），它的图形
    和接点都在**子块**里（顶层只有一个 INSERT），只读顶层等于没有接点。
    """
    out = []
    if depth > 8 or not recs:
        return out
    for r in recs:
        if not r:
            continue
        t = r[0][1] if r[0][0] == "0" else ""
        if t == "POINT":
            lay = _g1(r, "8") or ""
            if lay.upper().startswith("CONN"):
                x, y = _apply(mtx, _gf(r, "10"), _gf(r, "20"))
                out.append((x, y, lay))
        elif t == "INSERT":
            nm = _g1(r, "2")
            sub = bmap.get(nm)
            if not sub:
                continue
            px, py = _gf(r, "10"), _gf(r, "20")
            sx = _gf(r, "41", 1.0) or 1.0
            sy = _gf(r, "42", 1.0) or 1.0
            rr = math.radians(_gf(r, "50", 0.0))
            T = (1, 0, 0, 1, px, py)
            R = (math.cos(rr), math.sin(rr), -math.sin(rr), math.cos(rr), 0, 0)
            S = (sx, 0, 0, sy, 0, 0)
            out.extend(_conn_points(sub, bmap, _matmul(mtx, _matmul(_matmul(T, R), S)), depth + 1))
    return out


def wires_on_conn(sec, tol=1e-3):
    """检查每条 WIRE 的两个端点是否都落在 CONN 点上。

    返回 (CONN点列表, 没落在接点上的端点列表)。
    """
    return wires_check(sec, (), tol)


def wires_check(sec, extra=(), tol=1e-3):
    """同 wires_on_conn，但允许额外的“锚点”（阵列端子、线束顶端）。

    返回 (CONN点列表, 没落在任何接点/锚点上的端点列表)。
    """
    pts = conn_points_on_drawing(sec)
    allowed = pts + list(extra)
    bad = []
    for rec in group_entities(sec.get("ENTITIES", [])):
        if (_g1(rec, "8") or "").upper() != "WIRE":
            continue
        et = rec[0][1]
        if et == "LINE":
            ends = [(_gf(rec, "10"), _gf(rec, "20")),
                    (_gf(rec, "11"), _gf(rec, "21"))]
        elif et == "LWPOLYLINE":
            v = _lw_verts(rec)          # 折线只查两个“线头”，拐点不是接点
            ends = [(v[0][0], v[0][1]), (v[-1][0], v[-1][1])] if len(v) >= 2 else []
        else:
            continue
        for x, y in ends:
            if not allowed or min(math.hypot(x - a, y - b) for a, b in allowed) > tol:
                bad.append((x, y))
    return pts, bad


def frame_block_names(frame):
    """列出外框图里的用户块名(排除匿名块)。"""
    if not (frame and os.path.exists(frame)):
        return []
    sec, _o = parse_sections(frame, "utf-8")
    return [n for n in _br_names(sec.get("TABLES", []))
            if n and not n.startswith("*") and not n.startswith("A$")]


def frame_block_svg(frame, name):
    """渲染外框图里某块的 SVG 预览。"""
    try:
        sec, _o = parse_sections_text(read_dxf_text(frame))
        bmap = _blocks_map(sec)
        recs = bmap.get(name, [])
        return entities_to_svg(recs, bmap) if recs else ""
    except Exception:
        return ""


def block_file_svg(name):
    """渲染块库某个块文件（blocklib/blocks/<name>.dxf）的 SVG 预览。

    和生成结果用同一套渲染器（SPLINE/HATCH/椭圆/多段线凸度都认），
    这样界面上的块卡片和最后画到图上的样子是一致的。
    """
    p = os.path.join(ui.BLOCKS_DIR, name + ".dxf")
    if not os.path.exists(p):
        return ""
    try:
        sec, _o = parse_sections_text(read_dxf_text(p))
        return entities_to_svg(group_entities(sec.get("ENTITIES", [])),
                               _blocks_map(sec))
    except Exception:
        return ""


def _block_insts(bmap, names, log=None):
    """从块映射里取“几何 + CONN 接点”，得到 build_chain_frame 用的 insts。

    没有 CONN 点时兜底用包围盒四边中点（别再取所有端点，会连出大量乱线）。
    """
    insts = []
    for name in names:
        recs = bmap.get(name)
        if not recs:
            if log is not None:
                log.append("⚠ 外框图里没有 %s 的块定义，跳过" % name)
            continue
        prims = []
        _prim_list(recs, bmap, (1, 0, 0, 1, 0, 0), 0, prims)
        # 递归进子块拿接点：抽出来的块，接点在子块里（顶层只有一个 INSERT）
        # CONN-Label 层的点是“线号标注落点”，不是接线点：混在接点里会被当成块最外侧
        # 那一列接点（Male 的标签在右、Fmale 的在左），于是连线画到标签上、公头母头
        # 也对着标签摆（负极行偏 9 个单位的根源）。
        _conn = _conn_points(recs, bmap)
        pts = [(x, y) for x, y, lay in _conn if "LABEL" not in (lay or "").upper()]
        if not pts:
            pts = [(x, y) for x, y, _lay in _conn]     # 只有标签点的块：退回老口径
        if not pts:
            bb = _prims_bbox(prims)
            if bb:
                cx = (bb[0] + bb[1]) / 2.0
                cy = (bb[2] + bb[3]) / 2.0
                pts = [(cx, bb[3]), (cx, bb[2]), (bb[0], cy), (bb[1], cy)]
        if prims and pts:
            insts.append({"name": name, "prims": prims, "pts": pts})
    return insts


def _pack_missing(fb, sec, names, log):
    """链里用到、外框图里没有的块，定点把块库里的定义并进外框字节。

    返回 (fb, sec, fr_blocks)。fb 是 bytes；没补块时原样返回。
    """
    fr_blocks = set(_br_names(sec.get("TABLES", [])))
    need, miss = [], []
    for n in names:
        if n in fr_blocks or n in need:
            continue
        if os.path.exists(os.path.join(ui.BLOCKS_DIR, n + ".dxf")):
            need.append(n)
        elif n not in miss:
            miss.append(n)
    for n in miss:
        log.append("⚠ 块库里没有 %s，外框里也没有定义，跳过" % n)
    if not need:
        return fb, sec, fr_blocks
    plog = []
    try:
        base_bad = bp.verify(fb)               # 外框图自己就有的毛病（比如自带重复句柄）
        fb2, info = bp.pack_into(fb, [os.path.join(ui.BLOCKS_DIR, n + ".dxf")
                                      for n in need], plog)
        pb = bp.verify(fb2, need)
        if base_bad:
            # 外框原字节本来就不合格时，不能把它算成“我们补块弄坏的”：
            # 只关心“新增”的问题；老问题原样保留，照原样写进日志。
            pb = [x for x in pb if x not in base_bad]
            log.append("注意：外框图本身就有结构问题（%s），补块只看新增问题"
                       % "; ".join(base_bad))
        if pb:
            log.append("⚠ 补块定义后结构检查没过，退回外框原字节: " + "; ".join(pb))
        else:
            fb = fb2
            sec, _o = parse_sections_bytes(fb, "utf-8")
            fr_blocks = set(_br_names(sec.get("TABLES", [])))
            log.append("已把块库定义并入外框图: " + ", ".join(need))
    except Exception as ex:
        log.append("⚠ 补块定义失败(%s)，这些块可能不显示" % ex)
    log.extend(plog)
    return fb, sec, fr_blocks


def _splice_entities(fb, content, next_handle):
    """把内容插到 ENTITIES 段的 ENDSEC 前，并同步 $HANDSEED。返回 bytes 或 None。

    插入点必须精确落在 ENDSEC 那“一个组”的行首：用组解析定位，不能用正则，
    否则会吃掉上一条实体结尾的换行、把新增实体整体错行（12.1 的真 bug）。
    """
    gl = list(bp.iter_groups(fb))
    rng = bp.section_range(gl, b"ENTITIES")
    if rng is None:
        return None
    at = gl[rng[1]][0]
    out = fb[:at] + bytes(content) + fb[at:]
    seed = b"%X" % (next_handle + 1)
    return re.sub(rb"(\$HANDSEED\r?\n[ \t]*5\r?\n[ \t]*)([0-9A-Fa-f]+)",
                  lambda mm: mm.group(1) + seed, out, count=1)


# ================= 拼图：一行一张，全部拼进同一张图纸 =================
# 思路：每张先按老流程**单独生成**（各自调一份新的外框模板，互不影响），
# 再把“整张图”打包成一个块（块名 = 图号），并进底图，最后按格子摆 INSERT。
# 好处：外框图/图形/标注原样保留，不用逐条改坐标；在 CAD 里每张图还是一个整块。

def _def_name(defn):
    """块定义（BLOCK...ENDBLK 那一串记录）的块名。"""
    for c, v in (defn[0] if defn else []):
        if c == "2":
            return v
    return ""


def _block_defs(pairs):
    """把 BLOCKS 段的组按 BLOCK...ENDBLK 切成一个个块定义。"""
    defs, cur = [], None
    for rec in group_entities(pairs):
        if rec and rec[0] == ("0", "BLOCK"):
            cur = [rec]
        elif cur is not None:
            cur.append(rec)
            if rec and rec[0] == ("0", "ENDBLK"):
                defs.append(cur)
                cur = None
    return defs


def _rects_of(sec):
    """外框图里所有“闭合矩形”的 (面积, (x0,x1,y0,y1))，大的排前面。"""
    bmap = _blocks_map(sec)
    prims = []
    _prim_list(group_entities(sec.get("ENTITIES", [])), bmap,
               (1, 0, 0, 1, 0, 0), 0, prims)
    out = []
    for p in prims:
        if p[0] != "poly" or len(p) < 3 or not p[2] or len(p[1]) < 4:
            continue
        xs = [q[0] for q in p[1]]
        ys = [q[1] for q in p[1]]
        w, h = max(xs) - min(xs), max(ys) - min(ys)
        if w <= 1e-6 or h <= 1e-6:
            continue
        out.append((w * h, (min(xs), max(xs), min(ys), max(ys))))
    out.sort(key=lambda t: t[0], reverse=True)
    return out


def sheet_rect_of(frame_path):
    """这一张图纸的“纸面”范围（拼图排格子用）。

    优先取最大的闭合矩形 = 纸边（含图框外沿，排在一起才不挤）；没有闭合
    矩形就退回画图区，再不行用整体范围。返回 ((x0,x1,y0,y1), 依据)。
    """
    try:
        sec, _o = parse_sections(frame_path, "utf-8")
    except Exception as ex:
        return None, "读不了（%s）" % ex
    rs = _rects_of(sec)
    if rs:
        return rs[0][1], "纸边"
    r = frame_draw_rect(sec)
    if r:
        return r, "画图区"
    bmap = _blocks_map(sec)
    bb = _records_bbox(group_entities(sec.get("ENTITIES", [])), bmap)
    return bb, "整体范围"


def _clear_entities(fb):
    """清空 ENTITIES 段：拼图时外框自己的图元跟着各自的块走，底图不留。"""
    gl = list(bp.iter_groups(fb))
    rng = bp.section_range(gl, b"ENTITIES")
    if rng is None or rng[0] >= len(gl) or rng[0] == rng[1]:
        return fb
    return bp.apply_edits(fb, [(gl[rng[0]][0], gl[rng[1]][0], b"")])


def safe_block_name(name, i, used):
    """图号 -> 块名：只留 ASCII（中文块名在 DXF 里容易变乱码），重名自动加后缀。"""
    s = "".join((ch if (ch.isalnum() and ord(ch) < 128) or ch in "._-" else "_")
                for ch in str(name or ""))
    s = s.strip("._-")
    if not any(ch.isalnum() for ch in s):
        s = "SHEET-%03d" % i
    base, k = s[:40], 2
    s = base
    while s.upper() in used:
        s = "%s-%d" % (base, k)
        k += 1
    used.add(s.upper())
    return s


def sheet_block_file(name, dxf_text, skip_names, path):
    """把一张已经生成好的图写成“一个块定义”的 DXF（给 blockpack.pack_into 用）。

    pack_into 的规矩：文件名（词干）= 块名；ENTITIES 段 = 块内容；BLOCKS 段 = 这个
    块自己的子块。skip_names 是底图里已经有的块定义（外框图自带的 + 块库补进去的），
    那些不用每张各带一份。返回 (搬了几个子块, 搬了几个实体)。
    """
    sec, _order = parse_sections_text(dxf_text)
    ents = sec.get("ENTITIES", [])
    defs = [d for d in _block_defs(sec.get("BLOCKS", []))
            if _def_name(d) and _def_name(d).upper() not in skip_names]
    parts = []
    body = []
    for rec in layer_records(sec.get("TABLES", [])):
        body.extend(rec)
    if body:
        parts.append(emit_section("TABLES", body))
    body = []
    for d in defs:
        for rec in d:
            body.extend(rec)
    if body:
        parts.append(emit_section("BLOCKS", body))
    parts.append(emit_section("ENTITIES", ents))
    with open(path, "w", encoding="latin-1", newline="") as f:
        f.write("".join(parts) + "0\r\nEOF\r\n")
    return len(defs), len(ents)


def build_multi_frame(items, cols=2, gap_x=0.0, gap_y=0.0, order="row",
                      log=None, progress=None, content_only=False):
    """拼图：一行 = 一张图，各自参数，全部生成到同一张图纸上。

    items = [{"name": 图号, "frame": 外框图路径, "spec": {...}}, ...]
      1. 每张先按老流程单独生成（各自调一份新的外框模板 → 图形互不影响）；
      2. 每张整份内容打包成一个块，块名 = 图号（纯 ASCII，重名自动加后缀）；
      3. 全部并进底图（第一张的外框图），按 从左到右、从上到下 排格子，每张一个 INSERT。

    cols: 每行放几张；order: row=先横后竖（默认），col=先竖后横。
    返回 (dxf_text, log, wires, block_names)；wires 每项 (图号, 范围, 线号, 长度)。
    """
    log = list(log) if log else []
    wires = []
    if not items:
        return None, log + ["拼图：没有要画的图"], wires, []
    total = len(items)
    cols = max(1, int(cols or 1))
    col_first = str(order or "row").lower().startswith("col")

    # ---- 1) 每张单独生成 ----
    sheets = []
    for i, it in enumerate(items, 1):
        no = str(it.get("name") or ("SLD-%03d" % i))
        frm = it.get("frame") or ""
        sp = it.get("spec") or {}

        def pg(pct, stage, _i=i):
            if progress:
                try:
                    progress(((_i - 1) + max(0.0, min(100.0, float(pct))) / 100.0)
                             * 90.0 / total,
                             "第 %d/%d 张 · %s" % (_i, total, stage))
                except Exception:
                    pass

        log.append("—— 第 %d/%d 张 %s（%s，%s 串 × %s 块）——"
                   % (i, total, no, os.path.basename(frm),
                      sp.get("n_strings", ""), sp.get("n_per", "")))
        pg(1, "开始")
        text, rlog, w = build_array_frame(frm, sp, log=log, progress=pg)
        log = rlog or log
        if not text:
            log.append("⚠ %s 没画出来，跳过这张" % no)
            continue
        if content_only:
            # 只保留程序画的内容（去掉外框图自带实体）→ 拼出来的块不带外框
            text = strip_frame_entities(text, frm)
        sheets.append({"name": no, "frame": frm, "text": text})
        for (a, b, c) in (w or []):
            wires.append((no, a, b, c))
    if not sheets:
        return None, log + ["拼图失败：一张都没画出来"], wires, []

    # ---- 2) 底图：第一张的外框图；块库定义先补一份（所有图共用） ----
    base_frame = sheets[0]["frame"]
    try:
        base = open(base_frame, "rb").read()
    except OSError as ex:
        return None, log + ["拼图：读不了外框图 %s（%s）" % (base_frame, ex)], wires, []
    secB, _o = parse_sections_bytes(base, "utf-8")
    need = []
    for it in items:
        sp = it.get("spec") or {}
        for k in ("module", "module_first", "module_mid", "module_last",
                  "pos_plug", "neg_plug", "head_block"):
            v = sp.get(k)
            if isinstance(v, str) and v.strip():
                need.append(v.strip())
        need += [x for x in (sp.get("harness") or []) if x]
    base, secB, _fr = _pack_missing(base, secB, list(dict.fromkeys(need)), log)

    # 每张的**实际范围**（排格子用）
    #
    # 以前这里量的是外框模板里的“纸边矩形”，可模板 ENTITIES 里的 Frame1 块实际
    # 范围比纸边大得多（模板里带了图框以外的东西）：格子按 1652 宽算，单张却有
    # 6890 宽 —— 几张图直接糊在彼此身上。这就是“批量画出来的图和单独生成的完全
    # 不一样”的原因。现在改成量**这张图生成之后的真实范围**，排出来不会叠。
    rects = {}
    for sh in sheets:
        r = None
        try:
            _sS, _oS = parse_sections_text(sh["text"])
            r = _records_bbox(group_entities(_sS.get("ENTITIES", [])),
                              _blocks_map(_sS))
        except Exception as ex:
            log.append("⚠ %s 量不出内容范围(%s)" % (sh["name"], ex))
        if not r:
            r, kind = sheet_rect_of(sh["frame"])
            log.append("⚠ %s 量不出内容范围，退回按%s算" % (sh["name"], kind))
        if not r:
            r = (0.0, 0.0, 0.0, 0.0)
        sh["rect"] = r
        rects[sh["name"]] = (r, "内容")
        log.append("拼图：%s 单张范围 %.0f × %.0f（x %.0f..%.0f，y %.0f..%.0f）"
                   % (sh["name"], r[1] - r[0], r[3] - r[2], r[0], r[1], r[2], r[3]))
    base = _clear_entities(base)
    skip = {n.upper() for n in _br_names(secB.get("TABLES", []))}

    # ---- 3) 每张写成一个“块文件”，再一起并进底图 ----
    # 注意：这里用 makedirs 而不是 mkdtemp —— 某些受限环境下 mkdtemp 建出来的
    # 目录写不进去（权限被拦），makedirs 出来的普通目录没事。
    tmpdir = os.path.join(tempfile.gettempdir(), "sld_sheets_" + uuid.uuid4().hex[:10])
    os.makedirs(tmpdir, exist_ok=True)
    paths, names, used = [], [], set()
    try:
        for i, sh in enumerate(sheets, 1):
            bn = safe_block_name(sh["name"], i, used)
            p = os.path.join(tmpdir, bn + ".dxf")
            ndef, nent = sheet_block_file(bn, sh["text"], skip, p)
            sh["block"] = bn
            paths.append(p)
            names.append(bn)
            log.append("拼图：%s → 块 %s（%d 个子块, %d 个实体）"
                       % (sh["name"], bn, ndef, nent))
        base_bad = bp.verify(base)          # 外框图自带的老毛病不算我们的
        base2, info = bp.pack_into(base, paths, log)
        pb = [x for x in bp.verify(base2, names) if x not in base_bad]
        if pb:
            log.append("⚠ 拼图打包后结构检查没过: " + "; ".join(pb))
            return None, log, wires, []
        log.append("拼图：%d 张已并进底图（结构检查通过）" % len(names))
    except Exception as ex:
        log.append("⚠ 拼图打包失败(%s: %s)" % (type(ex).__name__, ex))
        return None, log, wires, []
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    # ---- 4) 排格子：从左到右、从上到下（或先竖后横） ----
    cw = max(r[1] - r[0] for (r, _k) in rects.values())
    ch = max(r[3] - r[2] for (r, _k) in rects.values())

    def _f(v, d):
        try:
            return float(v)
        except (TypeError, ValueError):
            return d

    cell_w, cell_h = cw + _f(gap_x, 0.0), ch + _f(gap_y, 0.0)
    nrow = (len(sheets) + cols - 1) // cols
    handle = [int(info.get("next_handle", 0)) + 1]
    mspace = model_space_handle(secB)
    content = bytearray()

    def nh():
        v = "%X" % handle[0]
        handle[0] += 1
        return v

    def blk(c, v):
        return (c + "\r\n" + str(v) + "\r\n").encode("utf-8")

    for k, sh in enumerate(sheets):
        if col_first:
            row, col = k % nrow, k // nrow
        else:
            row, col = k // cols, k % cols
        r = sh["rect"]
        ox, oy = col * cell_w - r[0], -row * cell_h - r[2]
        sh["pos"] = (ox, oy)
        for c, v in [("0", "INSERT"), ("5", nh()), ("330", mspace or "0"),
                     ("100", "AcDbEntity"), ("8", "0"),
                     ("100", "AcDbBlockReference"), ("2", sh["block"]),
                     ("10", "%.6f" % ox), ("20", "%.6f" % oy), ("30", "0.0"),
                     ("41", "1.0"), ("42", "1.0"), ("43", "1.0"), ("50", "0.0")]:
            content.extend(blk(c, v))
    out = _splice_entities(base2, content, handle[0])
    if out is None:
        return None, log + ["拼图：底图没有 ENTITIES 段"], wires, []
    if progress:
        try:
            progress(95, "排好格子")
        except Exception:
            pass
    log.append("拼图：%d 张排成 %d 行 × %d 列（%s），单张 %.0f × %.0f，"
               "图间距 %.0f / %.0f，块名 %s"
               % (len(sheets), nrow, cols, "先竖后横" if col_first else "先横后竖",
                  cw, ch, cell_w - cw, cell_h - ch, "、".join(names)))
    return out.decode("latin-1"), log, wires, names


def keep_hand_entities(src_path, prefix, mspace, nh, log=None):
    """从旧输出里挑出手工画的实体（图层名以 prefix 开头），重新编号后返回。

    用途：程序画到 COM 端为止，剩下的线人在 CAD 里接；下次重新生成时，
    把上一版里 HAND_ 层的实体原样搬过来，不用重画。
    返回 [(code, value), ...] 的扁平列表（直接塞进 ENTITIES 段）。
    """
    log = log if log is not None else []
    out = []
    try:
        sec3, _o = parse_sections_text(read_dxf_text(src_path))
    except Exception as ex:
        log.append("⚠ 保留手工内容失败(读不了 %s): %s" % (os.path.basename(src_path), ex))
        return [], set()
    pre = (prefix or "HAND").upper()
    recs = group_entities(sec3.get("ENTITIES", []))
    parent = None            # 上一条 POLYLINE 分到的新句柄（VERTEX/SEQEND 要认它）
    n = 0
    miss = set()
    for rec in recs:
        if not rec:
            continue
        t = rec[0][1]
        if t in ("VERTEX", "SEQEND"):
            if parent is None:
                continue                      # 上一条不是我们保留的多段线
            for c, v in clone_entity(rec, 0.0, 0.0, nh(), parent):
                out.append((c, v))
            continue
        parent = None
        lay = (_g1(rec, "8") or "").upper()
        if not lay.startswith(pre):
            continue
        h = nh()
        for c, v in clone_entity(rec, 0.0, 0.0, h, mspace or "0"):
            out.append((c, v))
        n += 1
        if t == "INSERT":
            miss.add(_g1(rec, "2") or "")
        if t == "POLYLINE":
            parent = h
    if n:
        log.append("保留手工内容: 从 %s 搬来 %d 个 %s* 层实体"
                   % (os.path.basename(src_path), n, pre))
    else:
        log.append("保留手工内容: %s 里没有 %s* 层的实体" % (os.path.basename(src_path), pre))
    return out, miss


def build_chain_frame(frame, chain, gap=40.0, match_span=True, show_len=True, fit=0.55):
    """【最稳】把内容“插入”到外框图原始字节里，其余字节不动。
    内容 = 每个块一个 INSERT(引用块名, 用框内已有定义) + 连线 + 线长。
    外框里没有的块，先从块库把块定义定点插进来（否则 AutoCAD 不显示任何东西）。
    返回 (dxf_text_latin1, log)。
    """
    if not (frame and os.path.exists(frame)):
        return None, ["没有外框图"]
    log = []
    fb = open(frame, "rb").read()
    sec, _order = parse_sections(frame, "utf-8")

    # === 缺块就补：把块库里的块定义定点插进外框字节 ===
    fb, sec, fr_blocks = _pack_missing(fb, sec, chain, log)

    mspace = model_space_handle(sec)
    maxh = max_handle(sec)

    bmap = _blocks_map(sec)
    insts = _block_insts(bmap, chain)
    if not insts:
        return None, ["没有可用块"]

    places, scales, place_log = _place_chain(insts, gap, match_span)

    # 整体适配：把整条链等比缩放，塞进“画图区”矩形，再居中。
    rect = frame_draw_rect(sec)
    fbx = rect or _records_bbox(group_entities(sec.get("ENTITIES", [])),
                                _blocks_map(sec))
    cb = _content_bbox(insts, places, scales)
    k = 1.0
    if fit and cb and fbx and (cb[1] - cb[0]) > 1e-6 and (cb[3] - cb[2]) > 1e-6:
        k = min((fbx[1] - fbx[0]) * fit / (cb[1] - cb[0]),
                (fbx[3] - fbx[2]) * fit / (cb[3] - cb[2]))
        k = max(k, 1e-4)
        if abs(k - 1.0) > 1e-6:
            # 整条链等比放大 k 倍（块 + 间隔一起），再重解一次位置
            places, scales, place_log = _place_chain(insts, gap, match_span, k)
            cb = _content_bbox(insts, places, scales)
            log.append("整体适配画图区: 等比 x%.4f" % k)
    off = (0.0, 0.0)
    if cb and fbx:
        off = ((fbx[0] + fbx[1]) / 2 - (cb[0] + cb[1]) / 2,
               (fbx[2] + fbx[3]) / 2 - (cb[2] + cb[3]) / 2)
    log.extend(place_log)
    log.append("套用外框图: %s (画图区 %s, 内容偏移 %.1f, %.1f)" %
               (os.path.basename(frame),
                ("%.0fx%.0f" % (fbx[1] - fbx[0], fbx[3] - fbx[2])) if fbx else "无",
                off[0], off[1]))

    handle = [maxh + 1]

    def nh():
        v = "%X" % handle[0]; handle[0] += 1; return v

    def blk(c, v):
        return (c + "\r\n" + str(v) + "\r\n").encode("utf-8")

    content = bytearray()
    for idx, it in enumerate(insts):
        P = places[idx]["P"]; s = scales[idx]
        rec = [("0", "INSERT"), ("5", nh()), ("330", mspace or "0"),
               ("100", "AcDbEntity"), ("8", "0"), ("100", "AcDbBlockReference"),
               ("2", it["name"]),
               ("10", "%.6f" % (P[0] + off[0])), ("20", "%.6f" % (P[1] + off[1])),
               ("30", "0.0"), ("41", "%.6f" % s), ("42", "%.6f" % s),
               ("43", "1.0"), ("50", "0.0")]
        for c, v in rec:
            content += blk(c, v)
    for idx in range(1, len(insts)):
        pr = _match_pairs(places[idx-1]["outs"], places[idx]["lins"])
        for (i, j) in pr:
            a = places[idx-1]["outs"][i]; b = places[idx]["lins"][j]
            a = (a[0] + off[0], a[1] + off[1]); b = (b[0] + off[0], b[1] + off[1])
            for c, v in [("0", "LINE"), ("5", nh()), ("330", mspace or "0"),
                         ("100", "AcDbEntity"), ("8", "WIRE"), ("100", "AcDbLine"),
                         ("10", "%.6f" % a[0]), ("20", "%.6f" % a[1]), ("30", "0.0"),
                         ("11", "%.6f" % b[0]), ("21", "%.6f" % b[1]), ("31", "0.0")]:
                content += blk(c, v)
            if show_len:
                L = ((a[0]-b[0]) ** 2 + (a[1]-b[1]) ** 2) ** 0.5
                h = max(4.0, gap * 0.15)
                for c, v in [("0", "TEXT"), ("5", nh()), ("330", mspace or "0"),
                             ("100", "AcDbEntity"), ("8", "TEXT"), ("100", "AcDbText"),
                             ("10", "%.6f" % ((a[0]+b[0])/2)),
                             ("20", "%.6f" % ((a[1]+b[1])/2 + h*1.3)), ("30", "0.0"),
                             ("40", "%.4f" % h), ("1", "%.1f" % L), ("50", "0.0")]:
                    content += blk(c, v)

    # 插入点必须精确落在 ENTITIES 段 ENDSEC 那“一个组”的行首。
    # （以前用 \s*0\r?\nENDSEC 正则会吃掉上一条实体结尾的换行，把值行粘坏。）
    out = _splice_entities(fb, content, handle[0])
    if out is None:
        return None, ["外框图无 ENTITIES 段"]
    missing = [it["name"] for it in insts if it["name"] not in fr_blocks]
    if missing:
        log.append("⚠ 外框里没有这些块定义(可能不显示): " + ", ".join(missing))
    try:
        pts, bad = wires_on_conn(parse_sections_bytes(out, "utf-8")[0])
        if bad:
            log.append("⚠ 连线端点检查: %d 个端点没落在 CONN 点上 %s"
                       % (len(bad), ["(%.2f, %.2f)" % b for b in bad[:4]]))
        else:
            log.append("连线端点检查: 全部落在 CONN 点上（图上共 %d 个 CONN 点）" % len(pts))
    except Exception as ex:
        log.append("连线端点检查失败: %s" % ex)
    return out.decode("latin-1"), log


# ================= 阵列 + 线束（交接手册第 13 章 · 阶段①②） =================

def _terminal_pair(recs, prims, bmap, name="", log=None):
    """模块块的正/负出线点（块局部坐标）。

    优先读 CONN_POS / CONN_NEG 层上的 POINT（**递归进子块**），两个层都齐了才用它。
    缺任何一个就按 13.8 的临时兜底：底边左 1/3 = 正极，底边右 2/3 = 负极。
    等你在 CAD 里把两个 POINT 补上，这段兜底自动失效、不用改代码。
    """
    pos, neg, fb = [], [], False
    for x, y, ln in _conn_points(recs, bmap):
        ln = (ln or "").upper()
        if "POS" in ln:
            pos.append((x, y))
        elif "NEG" in ln:
            neg.append((x, y))
    bb = _prims_bbox(prims)
    if bb is None:
        return None, None, False
    if not pos or not neg:
        w = bb[1] - bb[0]
        if not pos:
            pos = [(bb[0] + w / 3.0, bb[2])]
        if not neg:
            neg = [(bb[0] + 2.0 * w / 3.0, bb[2])]
        fb = True
        if log is not None:
            log.append("块 %s 没有 CONN_POS/CONN_NEG 点，用底边兜底："
                       "左1/3=正极，右2/3=负极" % (name or "?"))
    return sorted(pos)[0], sorted(neg)[-1], fb


# 位置那格写这些词 = “每段（每个支架）中点各一处”
BHA_SEG_WORDS = ("每段", "每支架", "每跨", "段", "mid")


def bha_pos_to_cell(pos, n_str, n_per):
    """整排“第几块之后” -> (0 基串号, 该串里第几块之后)。

    位置口径（v1.3 起）：**整个阵列连续数**的板号，不分串。
        0  = 最前面（第 1 串第 1 块之前）
        1  = 第 1 块之后
        40 = 4 串 × 20 块（共 80 块）时，正好落在第 2 串和第 3 串中间
    填得比总块数还大 = 排到最后一块之后。
    返回 None 表示这一格写的是“每段”，交给 bha_group_expand 按段展开。
    """
    t = str(pos if pos is not None else "").strip()
    if t in BHA_SEG_WORDS or t.lower() in BHA_SEG_WORDS:
        return None
    try:
        n = int(float(t)) if t else 0
    except (TypeError, ValueError):
        n = 0
    n = max(0, n)
    ns = max(1, int(n_str or 1))
    per = int(n_per or 0)
    if per < 1:                     # 还不知道每串多少块：退化成“全插在第 1 串”
        return 0, n
    if n >= ns * per:
        return ns - 1, per
    s = n // per
    return s, n - s * per


def _is_bha_pos_field(txt):
    """这一格像不像“位置”（数字 / 每段 / 空）—— 用来判断位置字段有没有写。"""
    t = str(txt if txt is not None else "").strip()
    return (t == "" or t in BHA_SEG_WORDS or t.lower() in BHA_SEG_WORDS
            or bool(re.fullmatch(r"[+-]?\d+(\.\d+)?", t)))


def _bha_nstr(x):
    """“串数”那格 -> 总串数：4 → 4；“3+3” → 6；空/认不出 → 0（= 所有结构都插）。"""
    s = str(x if x is not None else "").strip()
    if not s:
        return 0
    if s.isdigit():
        return int(s)
    nums = [int(t) for t in re.findall(r"\d+", s)]
    return sum(nums) if nums else 0


def parse_bha(v, n_str=0, n_per=0):
    """把“电机 / BHA 桩位置”归一成条目表（界面表格、批量行、命令行三种写法都吃）。

    支持的写法：
      · 结构化行（界面表格）：{"pos":40, "stub":"BHA", "motor":"MOTOR",
                               "rot":0, "gap_l":2, "gap_r":2}
      · 一行文字（批量模式每行、命令行）："40:BHA:MOTOR:0:2:2"
            整排第几块之后 : 桩块 : 电机块 : 电机旋转 : 桩左净空 : 桩右净空
            位置留空 = 最前面(0)；写“每段” = 每段（每个支架）中点各一处；
            后面的字段都可以省。
      · 多条用 ";" 或换行隔开。

    位置一律按**整个阵列连续数**的板号（不分串，见 bha_pos_to_cell）：
    4 串 × 20 块共 80 块，填 40 就插在正中间（第 2 串和第 3 串之间）。
    返回 [{"strings": None 或 0 基集合, "after": 该串内第几块之后, "pos": 原始位置,
           "stub": str, "motor": str, "rot": float,
           "gap_l": float|None, "gap_r": float|None}]，空/坏行直接跳过。
    """
    if not v:
        return []
    if isinstance(v, str):
        v = [x for x in re.split(r"[;\n；]+", v) if x.strip()]
    elif isinstance(v, dict):
        v = [v]

    def _num(x, dflt=None):
        try:
            s = str(x).strip()
            return dflt if s == "" else float(s)
        except (TypeError, ValueError):
            return dflt

    def mk(pos, stub, motor, rot, gl, gr, nstr=0):
        """一行 -> 条目（位置换算成“哪一串的第几块之后”）。

        nstr = 这一行管几串的结构（空/0 = 所有结构都插）：一张图里同时有
        2 串、3 串、4 串几种结构时，靠它分清“整排第 40 块”说的是哪一种。
        """
        cell = bha_pos_to_cell(pos, n_str, n_per)
        if cell is None:                      # 写了“每段”：每段中点各一处
            strings, after = None, max(1, int(n_per or 0) // 2)
        else:
            strings, after = {cell[0]}, cell[1]
        return {"strings": strings, "after": after, "pos": pos, "nstr": _bha_nstr(nstr),
                "stub": stub, "motor": motor, "rot": rot,
                "gap_l": gl, "gap_r": gr}

    out = []
    for it in v:
        if isinstance(it, dict):
            stub = str(it.get("stub") or it.get("bha") or "").strip()
            motor = str(it.get("motor") or "").strip()
            if not (stub or motor):
                continue
            out.append(mk(it.get("pos", it.get("after")), stub, motor,
                          float(_num(it.get("rot"), 0.0) or 0.0),
                          _num(it.get("gap_l")), _num(it.get("gap_r")),
                          it.get("nstr", it.get("strings_n"))))    # 原样传：可能是 "3+3"
            continue
        line = str(it).strip()
        if not line:
            continue
        fields = [p.strip() for p in line.split(":")]
        f0 = fields[0] if fields else ""
        f1 = fields[1] if len(fields) > 1 else ""
        # 前两格都是数字 = “串数:位置”；否则第一格就是位置（没写 = 最前面 0）
        if (re.fullmatch(r"\d+", f0 or "") and re.fullmatch(r"\d+(\.\d+)?", f1 or "")):
            nstr, f = int(f0), (fields[1:] + [""] * 6)[:6]
        elif f0 and _is_bha_pos_field(f0):
            nstr, f = 0, (fields + [""] * 6)[:6]
        else:
            nstr, f = 0, ["0"] + (fields + [""] * 6)[:5]
        if not (f[1] or f[2]):
            continue
        out.append(mk(f[0], f[1], f[2], float(_num(f[3], 0.0) or 0.0),
                      _num(f[4]), _num(f[5]), nstr))
    return out


def bha_block_names(v, n_str=0, n_per=0):
    """条目表里用到的块名（桩块 + 电机块），给“画到 CAD 只回放这些块”用。"""
    out = []
    for e in parse_bha(v, n_str, n_per):
        for nm in (e["stub"], e["motor"]):
            if nm and nm not in out:
                out.append(nm)
    return out


def bha_pick_block(e, has):
    """这一处电机/BHA 用哪个块占位：优先“桩块”，桩块不在库里就退回“电机块”。

    为什么要有这个兜底：块库里不一定有 BHA 桩块（现在就没有，只有 MOTOR 等）。
    批量行按提示写 “30:BHA:MOTOR:0” 时主块名是 BHA —— 以前整条会被丢掉，于是
    “批量里明明填了电机/BHA位置，图里却什么都没画出来”。现在退回用电机块当桩：
    插入位置、左右净空、板子右移这些照样生效。

    has：判断某个块在不在库里的函数（预览、真生成各有一套查找）。
    返回 (用的块名, 是不是退回了电机块)；两个都找不到返回 ("", False)。
    """
    stub = str(e.get("stub") or "")
    motor = str(e.get("motor") or "")
    if stub and has(stub):
        return stub, False
    if motor and has(motor):
        return motor, True
    return "", False


def parse_string_groups(v, dflt=1):
    """串数 -> 分组列表。

    "4" = 一组 4 串；"2+3" / "3+2" = 支架左边 2 串、右边 3 串（**段与段之间**
    是“跨支架距离”，段内部才是“串间净空”）。返回 [2, 3] 这样的列表。
    """
    if isinstance(v, (list, tuple)):
        out = [int(x) for x in v if str(x).strip().isdigit() and int(x) > 0]
        return out or [max(1, int(dflt))]
    s = str(v if v is not None else "").strip()
    if not s:
        return [max(1, int(dflt))]
    out = []
    for p in re.split(r"[+＋,，;；\s]+", s):
        if p.isdigit() and int(p) > 0:
            out.append(int(p))
    return out or [max(1, int(dflt))]


def bha_group_starts(groups):
    """串数分段（如 [2,4]）时，每一段的**第一串**的 0 基串号。"""
    out, i = [], 0
    for g in (groups or [1]):
        out.append(i)
        i += max(1, int(g))
    return out


def bha_group_expand(bha, groups):
    """把位置写“每段”的 BHA/电机条目收缩成**每一段一条**。

    为什么：串数写成 3+3 这种分段时，物理上是**一个支架（一段）配一个电机** ——
    前面那 3 串一个、后面那 3 串一个：3+3 → 2 个、2 → 1 个、2+4 → 2 个。
    写了具体块号（整排第几块之后）的条目**原样不动**，落在哪一串就是哪一串。
    返回 (新列表, 是否收缩过)。
    """
    starts = bha_group_starts(groups)
    changed = False
    out = []
    for e in bha or []:
        if e.get("strings") is None and starts:
            e = dict(e)
            e["strings"] = set(starts)
            changed = True
        out.append(e)
    return out, changed


def _place_array(tmpls, n_strings, pitch, gap, s_gap, dir="right",
                 groups=None, group_gap=None):
    """排“串 × 槽位”阵列（单位空间，k=1）。返回 (cells, 内容包围盒)。

    tmpls：**每串一份**块序列（len = 串数）。序列里除了组件块，还可以有 BHA 桩
           这类“占位块”（item["kind"] == "stub"；桩上挂的电机跟着桩画）。
    cells：每块一条 {"i"(槽位号), "mi"(第几块组件；桩是 None), "s", "P", "name",
                    "bb", "pos_l", "neg_l", "kind", "gap_l", "gap_r", "motor", ...}
    pitch：两个组件之间插入点的距离（= 板宽 + 板间净空；老口径，一块不变）。
    gap  ：板与板之间的净空（只用在桩的左右）。
    s_gap：串与串之间的净空。
    dir  ："right" = 每串一行、第 1 串在最左，下一串接在右边；
           "down"  = 每串一行、第 1 串在最上，下一串叠在下面。
    groups：串的分段（如 [2,3] = 支架左边 2 串 + 右边 3 串）。给了就按段排：
            段**内部**用 s_gap（串间净空），**段与段之间**用 group_gap（跨支架距离）。

    桩把一串断开：桩左边留它自己的 gap_l、右边留 gap_r（默认 = 板间净空），
    所以它右边的板整体右移（位移 = 桩宽 + 左净空 + 右净空 - 板间净空）。
    组件与组件之间照旧按 pitch 走 —— 没插桩时排出来的位置和以前一模一样。
    """
    tmpls = list(tmpls or [])
    if not tmpls:
        return [], (0.0, 0.0, 0.0, 0.0)
    while len(tmpls) < n_strings:
        tmpls.append(tmpls[-1])

    def offsets(t):
        """一串里每块的插入点相对第 1 块插入点的偏移。"""
        o = [0.0]
        for i in range(1, len(t)):
            a, b = t[i - 1], t[i]
            if a.get("kind") == "stub" or b.get("kind") == "stub":
                g = a.get("gap_r") if a.get("kind") == "stub" else None
                if g is None:
                    g = b.get("gap_l") if b.get("kind") == "stub" else None
                if g is None:
                    g = gap
                # 按外形边算：左块的右边缘 -> 净空 -> 右块的左边缘
                o.append(o[-1] + a["bb"][1] + g - b["bb"][0])
            else:
                o.append(o[-1] + pitch)
        return o

    offs = [offsets(t) for t in tmpls]
    spans = [(o[-1] + (t[-1]["bb"][1] - t[-1]["bb"][0])) if t else 0.0
             for o, t in zip(offs, tmpls)]
    # 每串自己占的高度（串间净空按**相邻两串的实际高度**留，串高不等时也准）
    hgts = [max((it["bb"][3] - it["bb"][2] for it in t), default=0.0)
            for t in tmpls]
    # 第 s 串**前面**那一段间隔：段内 = 串间净空，段与段之间 = 跨支架距离
    ggap = s_gap if group_gap is None else group_gap
    gaps_before = []
    if groups and len(groups) > 1:
        for gi, gc in enumerate(groups):
            for k in range(max(1, int(gc))):
                gaps_before.append(ggap if (k == 0 and gi > 0) else s_gap)
    while len(gaps_before) < n_strings:
        gaps_before.append(s_gap)
    cells = []
    ox, oy, prev_span, prev_h = 0.0, 0.0, 0.0, 0.0
    for s in range(n_strings):
        t, o = tmpls[s], offs[s]
        if s:
            if dir == "down":
                oy -= prev_h + gaps_before[s]
            else:
                ox += prev_span + gaps_before[s]
        mi = 0
        for i, it in enumerate(t):
            is_stub = (it.get("kind") == "stub")
            cells.append({"i": i, "mi": (None if is_stub else mi), "s": s,
                          "P": (ox + o[i], oy), "name": it["name"],
                          "bb": it["bb"],
                          "pos_l": it.get("pos_l"), "neg_l": it.get("neg_l"),
                          "kind": it.get("kind"), "motor": it.get("motor"),
                          "rot": it.get("rot", 0.0),
                          "gap_l": it.get("gap_l"), "gap_r": it.get("gap_r"),
                          "left_pt": it.get("left_pt"),
                          "right_pt": it.get("right_pt")})
            if not is_stub:
                mi += 1
        prev_span, prev_h = spans[s], hgts[s]
    x0 = min(c["P"][0] + c["bb"][0] for c in cells)
    x1 = max(c["P"][0] + c["bb"][1] for c in cells)
    y0 = min(c["P"][1] + c["bb"][2] for c in cells)
    y1 = max(c["P"][1] + c["bb"][3] for c in cells)
    return cells, (x0, x1, y0, y1)


_BLOCK_GEO = {}


def _block_geo(name):
    """块库里的一个块 -> (展平图元, 包围盒)；带缓存（按块文件改动时间失效）。

    板子布局预览靠它拿“这块板到底多大”，方框才是按真实外框画的。
    """
    p = os.path.join(ui.BLOCKS_DIR, str(name or "") + ".dxf")
    try:
        mt = os.path.getmtime(p)
    except OSError:
        return None
    key = (str(name), mt)
    if key in _BLOCK_GEO:
        return _BLOCK_GEO[key]
    out = None
    try:
        sec, _o = parse_sections_text(read_dxf_text(p))
        bmap = _blocks_map(sec)
        prims = []
        _prim_list(group_entities(sec.get("ENTITIES", [])), bmap,
                   (1, 0, 0, 1, 0, 0), 0, prims)
        bb = _prims_bbox(prims)
        if bb:
            out = (prims, bb)
    except Exception:
        out = None
    _BLOCK_GEO[key] = out
    if len(_BLOCK_GEO) > 60:              # 改了块库会不断出新 key，别让它无限长
        for k in list(_BLOCK_GEO)[:-30]:
            _BLOCK_GEO.pop(k, None)
    return out


def array_preview_svg(spec, width=1000, pad=16):
    """板子（阵列）的**实时布局预览**，返回 (svg, 说明文字)。

    只算布局：不读外框图、不打包块、不出 DXF、也不画线束（线束是界面第二步的事）。
    每块板按它自己块文件的**真实外框大小**画成方框（BHA 桩、电机也是方框），
    所以“串数 / 每串板数 / 板间净空 / 串间净空 / 跨支架距离 / 桩插在哪里”一眼可辨。
    """
    def _n(key, dflt):
        v = spec.get(key)
        try:
            return dflt if v is None or v == "" else float(v)
        except (TypeError, ValueError):
            return dflt

    module  = (spec.get("module") or "").strip()
    m_first = (spec.get("module_first") or "").strip()
    m_mid   = (spec.get("module_mid") or "").strip()
    m_last  = (spec.get("module_last") or "").strip()
    n_per   = max(0, int(_n("n_per", 0)))
    groups  = parse_string_groups(spec.get("n_strings"), 1)
    n_str   = sum(groups)
    gap_x   = _n("gap_x", 1.0)
    gap_y   = _n("gap_y", gap_x) if spec.get("gap_y") not in (None, "") else gap_x
    brk     = _n("bracket_gap", 4.0)
    scheme  = (spec.get("scheme") or "Harness").strip() or "Harness"
    dirn    = spec.get("dir") or ("down" if "LYNX" in scheme.upper() else "right")
    head_blk = (spec.get("head_block") or "").strip()
    head_gap = _n("head_gap", 60.0)

    seq_mode = bool(m_first and m_mid and m_last)
    if n_per < 1 or n_str < 1 or not (seq_mode or module):
        return "", "组件（首块/中间块/尾块）没选全，或串数/每串板数是 0"
    if seq_mode:
        seq = [m_first] + [m_mid] * max(0, n_per - 2) + [m_last]
        if n_per == 1:
            seq = [m_last]
    else:
        seq = [module] * n_per

    missing = [nm for nm in dict.fromkeys(seq) if not _block_geo(nm)]
    if missing:
        return "", "块库里没有这些块：%s" % "、".join(missing)
    kinds = {nm: {"name": nm, "bb": _block_geo(nm)[1]}
             for nm in dict.fromkeys(seq)}
    tmpl = [kinds[nm] for nm in seq]
    mw = max(t["bb"][1] - t["bb"][0] for t in tmpl)
    mh = max(t["bb"][3] - t["bb"][2] for t in tmpl)

    # BHA 桩：位置写“每段” = 每段一个（和真正生成时同一条规则）
    bha_raw = [e for e in parse_bha(spec.get("bha"), n_str, n_per)
               if not e.get("nstr") or int(e["nstr"]) == n_str]   # 串数不符的不插
    bha_raw, _shrunk = bha_group_expand(bha_raw, groups)
    # 一个条目要能画出来：桩块在库里就用桩块，桩块不在就退回电机块（块库里可以没有
    # BHA 桩块）；两个都不在的条目才丢掉 —— 以前是“桩块不在库就整条丢”，于是
    # 批量行按提示写 “30:BHA:MOTOR:0” 时电机也一起没了。
    bha, _fell_names, _fell_stub = [], set(), set()
    for e in bha_raw:
        nm, fell = bha_pick_block(e, lambda n: bool(_block_geo(n)))
        if not nm:
            continue
        bha.append((e, nm, fell))
        if fell:
            _fell_names.add(nm)
            _fell_stub.add(str(e.get("stub") or ""))
    tmpls, n_stub, stub_pos = [], 0, []
    for s in range(n_str):
        items = list(tmpl)
        mine = [x for x in bha if x[0]["strings"] is None or s in x[0]["strings"]]
        mine.sort(key=lambda x: x[0]["after"], reverse=True)
        for e, nm, fell in mine:
            g = _block_geo(nm)
            if not g:
                continue
            k = max(0, min(len(tmpl), int(e["after"])))
            items.insert(k, {"name": nm, "bb": g[1],
                             "kind": "stub",
                             "gap_l": (gap_x if e["gap_l"] is None else e["gap_l"]),
                             "gap_r": (gap_x if e["gap_r"] is None else e["gap_r"]),
                             # 退回电机块时它就是这一处的主体，别再挂一次
                             "motor": ("" if fell else e["motor"]),
                             "rot": e["rot"]})
            n_stub += 1
            stub_pos.append((e["strings"], k))
        tmpls.append(items)

    cells, abox = _place_array(tmpls, n_str, mw + gap_x, gap_x, gap_y, dirn,
                               groups=groups, group_gap=brk)

    prims = []
    # 起始块（CBX…）：摆在阵列左边 head_gap 处，和生成时同一个口径
    if head_blk:
        g = _block_geo(head_blk)
        if g:
            b = g[1]
            x1 = abox[0] - head_gap                 # 它的右边缘
            prims += shift_prims(g[0], x1 - b[1],
                                 (abox[2] + abox[3]) / 2.0 - (b[2] + b[3]) / 2.0)
            abox = (min(abox[0], x1 - (b[1] - b[0])), abox[1], abox[2], abox[3])

    # 每块板画成一个方框，颜色标身份：红=这一串的首块、绿=尾块、灰=中间块、
    # 蓝=BHA 桩、紫=电机。整张缩到卡片宽 —— 看的是“块数/位置/宽窄”，不看细节。
    num_h, str_h = 9.0, 12.0
    for c in cells:
        p, b = c["P"], c["bb"]
        if c.get("kind") == "stub":
            col = 5
        elif c["mi"] == 0:
            col = 1
        elif c["mi"] == n_per - 1:
            col = 3
        else:
            col = 7
        prims += rect_prims(p[0] + b[0], p[1] + b[2], p[0] + b[1], p[1] + b[3],
                            "0", col)
        if c.get("motor"):
            mg = _block_geo(c["motor"])
            if mg:
                mb = mg[1]
                prims += rect_prims(p[0] + mb[0], p[1] + mb[2],
                                    p[0] + mb[1], p[1] + mb[3], "0", 6)
    # 按**段**标注：每段上面画一条括号线 + “N 串” —— 2+3 就是前一段标“2 串”、
    # 后一段标“3 串”。不再一串一串地写（一行一串的标签在图上又密又乱）。
    sc0 = (width - 2 * pad) / max(abox[1] - abox[0], 1e-6)
    mark_nums = (mw * sc0) >= 16.0
    _starts = bha_group_starts(groups)
    for gi, gc in enumerate(groups or [n_str]):
        s0 = _starts[gi]
        cs = [c for c in cells if s0 <= c["s"] < s0 + max(1, int(gc))]
        if not cs:
            continue
        x0 = min(c["P"][0] + c["bb"][0] for c in cs)
        x1 = max(c["P"][0] + c["bb"][1] for c in cs)
        ytop = max(c["P"][1] + c["bb"][3] for c in cs)
        by = ytop + str_h * 0.9
        prims.append(("poly", [(x0, by), (x1, by)], False, "0", 4))
        prims.append(("poly", [(x0, by), (x0, by + str_h * 0.55)], False, "0", 4))
        prims.append(("poly", [(x1, by), (x1, by + str_h * 0.55)], False, "0", 4))
        prims.append(("text", (x0 + x1) / 2.0, by + str_h * 1.7,
                      "%d 串" % max(1, int(gc)), str_h, "0", 4))
    # 板上序号：一块板在图上放得下就每块都标；放不下就**每 5 块标一个**
    # （5/10/15…再加上最后一块）。这样几十块板一条串时也能一眼核对
    # “电机插在第几块之后、头一块和尾块在哪”。
    step = 1 if mark_nums else 5
    for s in range(n_str):
        mi = 0
        for c in [c for c in cells if c["s"] == s]:
            if c.get("kind") == "stub":
                continue
            mi += 1
            if step > 1 and (mi % step) and mi != n_per:
                continue
            prims.append(("text", c["P"][0] + (c["bb"][0] + c["bb"][1]) / 2.0,
                          c["P"][1] + (c["bb"][2] + c["bb"][3]) / 2.0 + num_h * 0.35,
                          str(mi), num_h, "0", 5))
    # 桩插在哪：直接把“第几块之后”写出来 —— 图上桩是方块，几十块板时肉眼分不出
    # 是排在第 3 块还是第 30 块，写在文字里就不会看错。
    _pk = []
    for seq, aft in stub_pos:
        # 口径：**整排连续**第几块之后（4 串 × 20 块时，40 = 正中间）
        t = ("每段中点各一处" if seq is None
             else "整排第 %d 块处" % (min(seq) * n_per + aft))
        if t not in _pk:
            _pk.append(t)
    info = ("%d 串 × %d 块%s ｜ 净空 %.1f、跨支架 %.1f ｜ %s%s%s"
            % (n_str, n_per,
               ("（%s）" % "+".join(str(x) for x in groups)) if len(groups) > 1 else "",
               gap_x, brk,
               "串从下往上排" if dirn == "down" else "串从左往右排",
               ("；BHA/电机 %d 处：%s" % (n_stub, "、".join(_pk))) if n_stub else "",
               ("（块库里没有 %s，用 %s 顶）"
                % ("、".join(sorted(x for x in _fell_stub if x)),
                   "、".join(sorted(_fell_names)))) if _fell_names else ""))
    # 整张缩到卡片宽：这是“示意”不是实际比例 —— 一屏就能看全，
    # 板特别多时自动不标序号（标了也糊），位置/宽窄/块数照样看得清。
    # 预览固定 140px 高：不管串数/排法怎么变，整块高度不变，界面一屏放得下
    return _svg_from_prims(prims, width, pad, min_h=110, min_font=12.0,
                           css_h=112), info


def _blk_pair(c, v):
    """DXF 的一组“组码 + 值”编码（和主流程里的 blk() 完全一致）。"""
    return (c + "\r\n" + str(v) + "\r\n").encode("utf-8")


def _max_handle_all(sec):
    """整份 DXF 里出现过的最大句柄（ENTITIES + BLOCKS + TABLES + OBJECTS…）。

    只看 ENTITIES 会低估——块定义/表记录里可能有更大的句柄，合并时新实体就会撞号
    （撞号的文件 CAD 会判无效，表现就是打开黑屏）。
    """
    m = 0
    for k, body in (sec or {}).items():
        if k in ("HEADER", "CLASSES"):
            continue
        try:
            m = max(m, max_handle(body))
        except Exception:
            pass
    return m


def _max_handle_text(raw):
    """直接扫 DXF 文本里所有“5 + 句柄”取最大（不依赖分段解析，最稳）。"""
    m = 0
    lines = raw.replace("\r\n", "\n").split("\n")
    for i, ln in enumerate(lines[:-1]):
        if ln.strip() != "5":
            continue
        v = lines[i + 1].strip()
        try:
            m = max(m, int(v, 16))
        except ValueError:
            pass
    return m


def strip_frame_entities(text, frame_path):
    """把生成结果里**外框图自带的实体**去掉，只留程序画的内容（做“只含内容”的块用）。

    判据两条，任一成立就算程序画的：
      · 句柄 > 外框图里的最大句柄（build_array_frame 画的实体都排在它后面）；
      · 图层是程序用的那几层（WIRE / WIRE_LABEL / CONN_POS / CONN_NEG）或块名是我们并进去的。
    """
    try:
        with open(frame_path, "rb") as _f:
            fmax = _max_handle_text(_f.read().decode("latin-1", "replace"))
    except Exception:
        fmax = 0
    ours_layer = ("WIRE", "WIRE_LABEL", "CONN_POS", "CONN_NEG")
    lines = text.replace("\r\n", "\n").split("\n")
    try:
        i = next(k for k, ln in enumerate(lines) if ln.strip() == "ENTITIES")
    except StopIteration:
        return text
    j = i + 1
    while j < len(lines) and lines[j].strip() != "ENDSEC":
        j += 1
    body = lines[i + 1:j]
    pairs = [(body[t].strip(), body[t + 1]) for t in range(0, len(body) - 1, 2)]
    # 用主流程同样的解析方式先算出“哪些句柄是程序画的”（外框图自带的实体句柄都 <= fmax）
    keep_hand = set()
    try:
        sec_all, _oa = parse_sections_text(text)
        for e in group_entities(sec_all.get("ENTITIES", [])):
            h = _g1(e, "5")
            try:
                hi = int(str(h), 16)
            except (TypeError, ValueError):
                hi = 0
            if hi > fmax or (_g1(e, "8") or "").upper() in ours_layer:
                keep_hand.add(str(h))
    except Exception:
        pass
    # 按“组码 0”把段体切成一个个实体组，再逐组看它自己的句柄/图层
    groups_out, cur = [], None
    t = 0
    while t + 1 < len(body):
        c, v = body[t].strip(), body[t + 1]
        if c == "0":
            cur = []
            groups_out.append(cur)
        if cur is not None:
            cur.append((c, v))
        t += 2
    keep = []
    for grp in groups_out:
        h = next((str(v).strip() for c, v in grp if c == "5"), "")
        lay = next((str(v).strip().upper() for c, v in grp if c == "8"), "")
        try:
            hi = int(h, 16)
        except ValueError:
            hi = 0
        if h in keep_hand or hi > fmax or lay in ours_layer:
            keep.append(grp)
    out = bytearray()
    for e in keep:
        for c, v in e:
            out.extend(_blk_pair(c, v))
    head = "\n".join(lines[:i + 1]) + "\n"
    tail = "\n" + "\n".join(lines[j:])
    return head + out.decode("utf-8") + tail


def _shift_entity(ent, s, dx, dy):
    """把一条实体按比例 s 缩放、再平移 (dx, dy)。s=1 时只平移。"""
    e = list(ent)
    if abs(s - 1.0) > 1e-9:
        e = scale_entity(e, s)
    return translate_entity(e, dx, dy)


def build_multi_segments(frame, spec, groups, log=None, progress=None, stats=None):
    """分段串数（2+2 / 3+3 / 2+3+2）：每段各画一套“阵列+线束”。

    每段单独调一次 build_array_frame（_no_split 防递归）→ 拿那段画出来的实体 →
    横向并排（段间留跨支架距离）、第 k 段整体下移把线束错开 → 统一缩放 →
    把用到的**块定义和图层**补进外框图 → 句柄统一重排 → 合并进外框图。
    """
    log = list(log) if log else []
    if not (frame and os.path.exists(frame)):
        return None, ["没有外框图"], []
    # 每段先各自生成一张（和单段完全同一条成熟路径，保证文件有效），
    # 再用批量“拼成一张图纸”的那套机制把几段当块插进同一张图 —— 句柄、块定义、
    # 图层全由那套代码处理，不再自己拼实体（自己拼会出重复/缺失句柄，CAD 判无效）。
    tag = str(spec.get("sheet_no") or spec.get("row_no") or "SEG")
    items = []
    for i, g in enumerate(groups, 1):
        sub = dict(spec)
        sub["n_strings"] = str(int(g))
        sub["_no_split"] = True
        items.append({"name": "%s-%d" % (tag, i), "frame": frame, "spec": sub})
    gap = float(spec.get("bracket_gap") or 30.0)
    text, lg, wires, names = build_multi_frame(
        items, cols=len(items), gap_x=gap, gap_y=gap,
        order="row", log=log, progress=progress, content_only=True)
    log = lg or log
    if text:
        log.append("分段绘制：%d 段（%s）各自生成后当块拼进同一张图（块名 %s）"
                   % (len(items), "+".join(str(int(g)) for g in groups),
                      "、".join(names or [it["name"] for it in items])))
    return text, log, wires or []


def harness_fuse_chain(chain, branch_names, fuse_name="FUSE"):
    """Harness 方案：自动把保险丝排进线束链里（用户口径）。

    · **主线上一根**：插在第一根支线之前（也就是头部接头/汇流箱之后那一段主线上）；
    · **最后一根支线上也一根**：插在正极行最后那一块（末端公头）之前。
    这两个位置本来就有保险丝（用户自己点过链）时不再重复插 —— 链是用户点的就照他的点。

    chain：现成的线束链（正极行，负极那一段还没拼上来）；
    branch_names：算“支线”的块名（正极支线块 / 末端公头）。
    返回 (新链, [新插进去的那几根保险丝在链里的下标])。
    """
    chain = list(chain or [])
    fuse_name = (fuse_name or "").strip()
    if not chain or not fuse_name:
        return chain, []

    def sq(n):
        return re.sub(r"[\s\-_]+", "", str(n or "")).upper()

    fq = sq(fuse_name)
    br = {sq(x) for x in (branch_names or []) if x}
    first_br = next((i for i, x in enumerate(chain) if sq(x) in br), len(chain))
    last_br = max((i for i, x in enumerate(chain) if sq(x) in br), default=None)
    slots = set()
    if not (first_br > 0 and sq(chain[first_br - 1]) == fq):      # 主线那根
        slots.add(first_br)
    if last_br is not None and not (last_br > 0 and sq(chain[last_br - 1]) == fq):
        slots.add(last_br)                                        # 最后一根支线那根
    out, ins = [], []
    for i in range(len(chain) + 1):
        if i in slots:
            ins.append(len(out))
            out.append(fuse_name)
        if i < len(chain):
            out.append(chain[i])
    return out, ins


def harness_cual_chain(chain, branch_names, cual_name="CU-AI", per_gap=2):
    """IBEX 方案：在 Harness 的基础上，支线那几段线束里各加 per_gap 个 CU-AI 转接。

    用户口径（2026-09-22）：IBEX = Harness + “每个支线线束段”两个 CU-AI（每两个
    支线块之间、以及正极/负极支线中间那段线束）。合起来一条规则：
    链里**每个挨着支线块的间隔**插 per_gap 个 CU-AI —— 正极行、负极行都按这条走。
    间隔里已经有 CU-AI 的不重复插（用户自己点过链就照他的点）。

    返回 (新链, 新插进去的块在链里的下标)。
    """
    chain = list(chain or [])
    name = (cual_name or "").strip()
    if not chain or not name:
        return chain, []

    def sq(n):
        return re.sub(r"[\s\-_]+", "", str(n or "")).upper()

    br = {sq(x) for x in (branch_names or []) if x}
    cq = sq(name)
    out, ins = [], []
    for i, blk in enumerate(chain):
        out.append(blk)
        if i + 1 >= len(chain):
            continue
        nxt = chain[i + 1]
        if sq(blk) == cq or sq(nxt) == cq:
            continue                              # 这一段里已经有 CU-AI 了
        if (sq(blk) in br) or (sq(nxt) in br):    # 挨着支线块的那一段线束
            for _ in range(max(1, int(per_gap))):
                ins.append(len(out))
                out.append(name)
    return out, ins


def wire_label(txt):
    """线号标注的文字：一律“# + 数字”（用户口径）。

    界面上填的线号是线规写法（2/0 AWG、10 AWG、750 MCM…），图上写的是
    “#2/0”“#10”这种 —— “#”本身就代表 AWG，所以 AWG 后缀去掉；
    MCM 这类不是 AWG 的单位留着，免得“750”被当成 750 AWG。
    认不出来的（比如用户自己敲的名字）原样加个 #，总之全图一个格式。
    已经带 # 的原样返回（重复调用不会叠成 ##）。
    """
    t = str(txt or "").strip()
    if not t:
        return t
    if t.startswith("#"):
        return t
    m = re.match(r"^([0-9]+(?:/[0-9]+)?)\s*(.*)$", t)
    if m:
        num, unit = m.group(1), m.group(2).strip()
        if unit.upper() == "AWG":
            unit = ""                      # # 就是 AWG，不再写单位
        return ("#%s %s" % (num, unit)).strip()
    return "#" + t


def build_array_frame(frame, spec, log=None, progress=None, stats=None):
    """阵列（组件）+ 线束 生成，写进外框字节。返回 (dxf_text, log, wires)。

    画面（手册 13.1）：上=组件阵列，中=跨接线，下=线束。
    整体只等比缩一次：k 同时作用于块、间距和接点坐标（12.8 踩过的坑）。

    stats：可选 dict，生成完往里回填 {"blocks": [这张图用到的块名, ...]}。
           给“画到 CAD 只回放我们画的块”用 —— 以前那边是靠界面参数**猜**的，
           自动补出来的块（CBX / 公头 / 母头 / 负极支线）没猜进去，
           画到 CAD 时就整个丢了。现在直接回真实的清单。
    """
    log = list(log) if log else []
    wires = []

    def pg(pct, stage):
        if progress:
            try:
                progress(pct, stage)
            except Exception:
                pass

    pg(2, "开始")
    if not (frame and os.path.exists(frame)):
        return None, ["没有外框图"], wires

    def _n(key, dflt):
        v = spec.get(key)
        return dflt if v is None or v == "" else float(v)

    module   = (spec.get("module") or "").strip()
    m_first  = (spec.get("module_first") or "").strip()   # 每串第一块（带正极出线）
    m_mid    = (spec.get("module_mid") or "").strip()     # 每串中间块（重复）
    m_last   = (spec.get("module_last") or "").strip()    # 每串最后一块（带负极出线）
    n_per    = int(_n("n_per", 0))
    # 串数支持分段：4 / 2+3 / 3+2+2（段内=串间净空，段间=跨支架距离）
    s_groups = parse_string_groups(spec.get("n_strings"), 1)
    n_str    = sum(s_groups)
    gap_x    = _n("gap_x", 1.0)          # 板与板之间的净空（默认 1）
    # 串与串之间的净空：**默认跟板间净空一致**（用户口径）。只有显式填了 gap_y
    # （老批量行 / 命令行）才用单独的值。
    _gy_indep = spec.get("gap_y") not in (None, "")
    gap_y    = _n("gap_y", gap_x) if _gy_indep else gap_x
    brk_gap  = _n("bracket_gap", 4.0)    # 跨支架距离（串分段时，段与段之间）
    # 方案（Harness / ALEX / IBEX…）：现在只是记下来（各方案的差异逻辑后面逐个补），
    # 唯一已经生效的是串的排法：带 LYNX 的方案从下往上排，其余从左往右。
    scheme   = (spec.get("scheme") or "Harness").strip() or "Harness"
    dirn     = spec.get("dir") or ("down" if "LYNX" in scheme.upper() else "right")
    link     = bool(spec.get("link_array"))  # 是否画“阵列 ↔ 线束”的跨接线（默认不画）
    match_h  = spec.get("match_span")
    match_h  = True if match_h is None else bool(match_h)   # 线束各块接点对齐缩放
    h_scale  = _n("harness_scale", 1.0)                     # 线束整体微调倍率
    sw_in    = spec.get("string_wires")                     # 串内要不要画连线
    fix_gap  = _n("fixed_gap", 30.0)                        # FUSE / CU-AL 与相邻块的固定间距
    # 块名里的空格/减号写法各家不一样（模板里叫 “CU - AL”，手输常常是 “CU-AL”），
    # 比较时统一去掉空格和减号，免得因为一个空格就对不上。
    fix_gnames = spec.get("fixed_gap_names") or ["FUSE", "CU - AL"]

    def _squash(n):
        return re.sub(r"[\s\-_]+", "", str(n or "")).upper()

    fix_gset = {_squash(x) for x in fix_gnames}

    def is_fix(n):
        return _squash(n) in fix_gset
    # ---- 方案差异（各方案逐个补）：Harness 方案的保险丝 ----
    #   Harness 方案自动排两根保险丝：主线上一根 + 最后一根支线上也一根
    #   （位置口径见 harness_fuse_chain）。
    #   这两根与相邻块的间距固定 fuse_gap（默认 20），不跟界面的“块固定间距”走：
    #   用户口径是“fuse 和靠近的那个块的距离保持 20”。
    _is_ibex = "IBEX" in scheme.upper()
    # IBEX 就是“Harness + CU-AI 转接”，所以 Harness 那套（自动加保险丝等）对它一样生效
    _is_harness = ("HARNESS" in scheme.upper()) or _is_ibex
    fuse_blk = (spec.get("fuse_block") or "FUSE").strip()
    fuse_gap = _n("fuse_gap", 50.0)      # 用户口径（2026-09-23）：fuse 与相邻块距离默认 50
    # IBEX 方案：支线线束段里自动加 CU-AI 转接（间距 50，用户口径 2026-09-22）
    cual_blk = (spec.get("cual_block") or "CU-AI").strip()
    cual_gap = _n("cual_gap", 50.0)
    fuse_at = []                     # 自动补进去的保险丝在链里的下标（每段重算）
    head_blk = (spec.get("head_block") or "").strip()      # 摆在阵列最左边、与板子固定距离的块
    head_gap = _n("head_gap", 60.0)                         # 它与阵列左边缘的距离
    neg_auto = spec.get("neg_auto")
    neg_auto = True if neg_auto is None else bool(neg_auto)  # 负极那一行按正极自动生成
    neg_gap  = _n("neg_gap", 30.0)                           # 负极行到正极行的距离
    neg_head = (spec.get("neg_head") or "").strip()          # 负极行最左边那块（公头）
    # 负极支线块的朝向：**不需要填角度**。逻辑就是“负极支线块的接线头去对应板子的
    # 负极（接线头朝上、落在负极行线上）”，也就是固定正放（0°）；公头/母头另外
    # 自动朝链内，不看这个值。界面上的“负极支线旋转”那一栏因此已去掉。
    neg_rot  = 0.0
    pos_plug = (spec.get("pos_plug") or "").strip()         # 最右边那根支线的正极上插什么块（公头）
    neg_plug = (spec.get("neg_plug") or "").strip()         # 最右边那串的负极上插什么块（母头）
    keep_from   = (spec.get("keep_from") or "").strip()     # 手工内容（HAND_ 层）从哪搬
    keep_prefix = (spec.get("keep_prefix") or "HAND").strip()
    harness  = [n for n in (spec.get("harness") or []) if n]
    _pos_feed = (spec.get("pos_feeder") or "").strip()
    # ---- 线束链不填（或填得不够）也能生成 ----
    #   没填链：自动排一条最基本的正极行 = 末端母头 + 正极支线×(串数-1) + 末端公头
    #   （负极行本来就是自动补的；FUSE 这类串联块想加就自己在链里加）
    if not harness:
        _auto = [x for x in (neg_plug or pos_plug,
                             *([_pos_feed] * max(0, n_str - 1)),
                             pos_plug) if x]
        if _auto:
            harness = _auto
            log.append("线束链没填：自动生成 " + "→".join(_auto))
    #   填了链但正极支线不够根数：把“正极支线块”补到够（每串一根，末端公头顶最后一串）
    if _pos_feed:
        _nf = sum(1 for x in harness if x in (_pos_feed, pos_plug))
        _plus = 1 if (pos_plug and pos_plug not in harness) else 0   # 公头后面会补到链尾
        _need = n_str - _plus
        if _nf < _need:
            _at = next((i for i, x in enumerate(harness)
                        if x in (_pos_feed, pos_plug)), len(harness))
            harness[_at:_at] = [_pos_feed] * (_need - _nf)
            log.append("正极支线 %d 根、串数 %d：自动补到 %d 根" % (_nf, n_str, _need))
    # 公头/母头是线束那一排的一员（和正极支线同一条水平线），不是插在串的正极旁边。
    # 用户没把它们放进链里的话，这里补到链尾。
    # 末端公头补到链尾；末端母头交给“负极行自动生成”那段处理，
    # 这里别再塞一遍，否则会多出一块、还会把正极和负极连出斜线。
    for _x in ((pos_plug,) if neg_auto else (pos_plug, neg_plug)):
        if _x and _x not in harness:
            harness.append(_x)
    # 起始块（汇流箱）也自动补进链里，否则它不会被画出来
    if head_blk and head_blk not in harness:
        harness.insert(0, head_blk)
    # Harness 方案：主线 + 最后一根支线各补一根保险丝（用户自己点过的不再重复插）
    if _is_harness:
        harness, fuse_at = harness_fuse_chain(harness, [_pos_feed, pos_plug], fuse_blk)
        if fuse_at:
            log.append("方案 Harness: 自动加保险丝 %s ×%d（主线上一根、最后一根支线上一根）"
                       "；它与相邻块的间距 %.0f" % (fuse_blk, len(fuse_at), fuse_gap))
        elif any(_squash(x) == _squash(fuse_blk) for x in harness):
            log.append("方案 Harness: 链里已经有 %s，不再重复插；它与相邻块的间距 %.0f"
                       % (fuse_blk, fuse_gap))
    gap      = _n("gap", 40.0)
    pos_feed = (spec.get("pos_feeder") or "").strip()
    neg_feed = (spec.get("neg_feeder") or "").strip()
    # 负极行自动生成（和正极那一行对称）：
    #   正极行：头部接头（对齐 CBX） + 正极支线×(串数-1) + 末端公头
    #   负极行：头部接头（对齐 CBX） + 负极支线×(串数-1) + 末端母头
    # 关键在于**头部接头不占板子**：以前头部那根公头是钉在第 1 串的负极上的，
    # 于是第 1 串负极端子下面是公头、不是负极支线（用户要的是每串都有负极支线）。
    # 必须放在 _block_insts 之前补。
    neg_from = None
    if neg_auto and (neg_feed or neg_plug or neg_head) and n_str >= 1:
        _head = neg_head or pos_plug or neg_plug
        _mid  = neg_feed or neg_plug or _head
        _tail = neg_plug or neg_feed or _head
        if n_str == 1:
            _pins = [_tail]
        else:
            _pins = [_mid] * (n_str - 1) + [_tail]
        neg_seq = [_head] + _pins if _head else list(_pins)
        neg_seq = [x for x in neg_seq if x]
        if neg_seq:
            neg_from = len(harness)
            harness.extend(neg_seq)
    # 能接串的“支线” = 正极支线 + 末端公头（公头也顶一根）；负极同理用母头
    pos_names = [x for x in (pos_feed, pos_plug) if x]
    neg_names = [x for x in (neg_feed, neg_plug) if x]
    # 公头 / 母头（末端接头块）：线号规则里“最后一根支线→接头”这一段算**支线**
    plug_names = [x for x in (pos_plug, neg_plug) if x]
    awg_main = (spec.get("awg_main") or "").strip()
    awg_br   = (spec.get("awg_branch") or "").strip()
    allow_up = bool(spec.get("allow_enlarge"))
    # 连接点（CONN_POS/CONN_NEG 的 POINT）只是“用来定位”的辅助，用户要求**画出来的图
    # 不要点**，所以默认不打；需要的时候界面勾一下（draw_points）再打。
    draw_pts = bool(spec.get("draw_points"))
    clear_r  = _n("clearance_ratio", 0.5)
    label_r  = _n("label_ratio", 0.006)
    inset_x  = _n("inset_x", 0.04)
    inset_y  = _n("inset_y", 0.06)
    k_floor  = _n("k_floor", 0.35)

    # 先把“实际收到的配置”回显出来，省得某一栏空着还到处找原因
    log.append("配置: 方案 %s | 组件 %s/%s/%s  每串%d块×%s串  板净空%.1f 串净空%.1f%s" %
               (scheme, m_first or module, m_mid or module, m_last or module,
                n_per, ("+".join(str(x) for x in s_groups) if len(s_groups) > 1
                        else str(n_str)), gap_x, gap_y,
                (" 跨支架%.1f" % brk_gap) if len(s_groups) > 1 else ""))
    log.append("     线束链 %s | 正极支线块=%s 末端公头=%s 末端母头=%s | 负极自动=%s 负极支线块=%s" %
               ("→".join(harness) or "（空）", pos_feed or "（空）", pos_plug or "（空）",
                neg_plug or "（空）", "开" if neg_auto else "关", neg_feed or "（空）"))
    log.append("     起始块=%s 间距%.0f | 跨接线=%s" %
               (head_blk or "（空）", head_gap, "开" if link else "关"))

    seq_mode = bool(m_first and m_mid and m_last)
    if (not module and not seq_mode) or n_per < 1 or n_str < 1:
        return None, ["阵列参数不完整：组件块 / 每串板数 / 串数 都要填"], wires

    # ---- 电机 / BHA 桩：插在某一串的两块板之间（手册 13.5 阶段③） ----
    # 界面给的是小表（结构化），批量的每一行 / 命令行给的是一行文字（见 parse_bha）。
    # ---- 分段串数：同一次调用里按段循环画（先画前一种串数的阵列+线束，再画下一种，整体下移错开） ----
    _seg_sizes = ([int(g) for g in s_groups] if (len(s_groups) > 1 and not spec.get("_no_split")) else [n_str])
    _nseg = len(_seg_sizes)
    _need_all = []          # 各段用到的块名（收尾要把它们一起并进外框，见循环后面）
    _stats_names = []       # 各段**真正画出去**的顶层块名（回填 stats["blocks"]，见收尾）
    _seg_common = None      # 分段共用的一套排版：同一个 k + 同一条水平线（见下面“缩放”）
    for _si, _gsz in enumerate(_seg_sizes):
        n_str = _gsz
        log.append("【段 %d/%d】本段 %d 串（调试）" % (_si + 1, _nseg, _gsz))
        # ---- 每段自己的线束链 ----
        #   支线根数按**本段**串数算（2+2 → 前一段 2-1=1 根、后一段 2-1=1 根），
        #   第 2 段起不再重复摆汇流箱（CBX 只有一个，挂在第一段上）。
        if _si > 0:
            head_blk = ""
        if not (spec.get("harness") or []):
            _auto2 = [x for x in (neg_plug or pos_plug,
                                  *([_pos_feed] * max(0, n_str - 1)),
                                  pos_plug) if x]
            if _auto2:
                harness = _auto2
                log.append("段 %d：线束链自动生成 %s" % (_si + 1, "→".join(_auto2)))
        else:
            harness = [n for n in (spec.get("harness") or []) if n]
        if _pos_feed:
            _nf2 = sum(1 for x in harness if x in (_pos_feed, pos_plug))
            _plus2 = 1 if (pos_plug and pos_plug not in harness) else 0
            _need2 = n_str - _plus2
            if _nf2 < _need2:
                _at2 = next((i for i, x in enumerate(harness)
                             if x in (_pos_feed, pos_plug)), len(harness))
                harness[_at2:_at2] = [_pos_feed] * (_need2 - _nf2)
        for _x2 in ((pos_plug,) if neg_auto else (pos_plug, neg_plug)):
            if _x2 and _x2 not in harness:
                harness.append(_x2)
        if head_blk and head_blk not in harness:
            harness.insert(0, head_blk)
        # Harness 方案：每一段都按同样的口径补保险丝（分段时线束是逐段重排的）
        fuse_at = []
        if _is_harness:
            harness, fuse_at = harness_fuse_chain(harness, [_pos_feed, pos_plug], fuse_blk)
        # ---- 负极那一行也按**本段**串数重建（不然重建正极链时把负极行丢了）----
        neg_from = None
        if neg_auto and (neg_feed or neg_plug or neg_head) and n_str >= 1:
            _head_n = neg_head or pos_plug or neg_plug
            _mid_n = neg_feed or neg_plug or _head_n
            _tail_n = neg_plug or neg_feed or _head_n
            if n_str == 1:
                _pins_n = [_tail_n]
            else:
                _pins_n = [_mid_n] * (n_str - 1) + [_tail_n]
            _seq_n = ([_head_n] if _head_n else []) + _pins_n
            _seq_n = [x for x in _seq_n if x]
            if _seq_n:
                neg_from = len(harness)
                harness.extend(_seq_n)
        neg_names = [x for x in (neg_feed, neg_plug) if x]
        bha = parse_bha(spec.get("bha"), n_str, n_per)
        # 只留管这种串数结构的条目（串数留空 = 所有结构都插）
        bha = [e for e in bha if not e.get("nstr") or int(e["nstr"]) == n_str]
        # 位置写“每段” = **每段一个电机**（3+3 → 前 3 串一个、后 3 串一个）
        bha, _shrunk = bha_group_expand(bha, s_groups)
        if _shrunk:
            log.append("BHA/电机: 位置写“每段”的条目按“每段中点各一处”处理（共 %d 段）→ 落在 %s"
                       % (len(s_groups), "、".join("串%d" % (k + 1)
                                                   for k in bha_group_starts(s_groups))))
        _bad_seq = [e for e in bha if e["strings"] is not None and not e["strings"]]
        if _bad_seq:
            log.append("⚠ BHA/电机: 这些条目的位置算不出落在哪一串（共 %d 串），已跳过：%s"
                       % (n_str, "; ".join("%s 第%s块后" % (e["stub"] or e["motor"], e.get("pos"))
                                           for e in _bad_seq)))
            bha = [e for e in bha if e not in _bad_seq]
        # 只把块库里真有的名字送去“并入外框图”：块库里没有的（比如还没有 BHA 桩块）
        # 报“跳过”只会让人以为整条被丢了 —— 下面那一处会退回用电机块当桩。
        bha_names = [x for e in bha for x in (e["stub"], e["motor"])
                     if x and os.path.exists(os.path.join(ui.BLOCKS_DIR, x + ".dxf"))]
        if bha:
            # 位置在下面每一处自己的那行里报（带整排块号），这里只报几处、用了哪些块
            log.append("BHA/电机: %d 处（%s）" % (len(bha), "、".join(
                ("%s+%s" % (e["stub"], e["motor"])) if (e["stub"] and e["motor"])
                else (e["stub"] or e["motor"]) for e in bha)))

        fb = open(frame, "rb").read()
        sec, _order = parse_sections(frame, "utf-8")
        # IBEX 方案：正极行、负极行都按“每个挨着支线块的线束段插 2 个 CU-AI”加转接。
        # 必须放在**并块定义之前** —— 不然 CU-AI 的块定义不会并进外框，图里就不显示。
        if _is_ibex and cual_blk:
            harness, cual_at = harness_cual_chain(
                harness, [x for x in (pos_feed, neg_feed, pos_plug, neg_plug) if x],
                cual_blk)
            if cual_at:
                log.append("方案 IBEX: 支线线束段自动加 CU-AI 转接 ×%d（每段 2 个）；"
                           "它与相邻块/彼此的间距 %.0f" % (len(cual_at), cual_gap))
            else:
                log.append("方案 IBEX: 支线线束段里已经有 %s，不再重复插；间距 %.0f"
                           % (cual_blk, cual_gap))
        need_blocks = ([module] if module else []) + \
                      ([m_first, m_mid, m_last] if seq_mode else []) + harness + bha_names
        # 分段时每段各补一遍块定义，但补的是**这一份**外框字节（下一段又从头读一次）。
        # 所以要累计：收尾时把各段用到的块统一并进最终那一份，见循环后面。
        _need_all = list(dict.fromkeys(
            _need_all + need_blocks + [x for x in (pos_plug, neg_plug) if x]))
        fb, sec, fr_blocks = _pack_missing(fb, sec, list(dict.fromkeys(
            need_blocks + [x for x in (pos_plug, neg_plug) if x])), log)
        pg(20, "块定义已并入外框图")
        mspace = model_space_handle(sec)
        maxh = max_handle(sec)
        bmap = _blocks_map(sec)

        # ---- 组件：每串的块序列 ----
        # 新写法（手册 26 章）：每串 = 首块 + (n-2)×中间块 + 尾块，n 含首尾。
        # 老写法：整串都是同一个 module 重复 n 次。
        if seq_mode:
            seq = [m_first] + [m_mid] * max(0, n_per - 2) + [m_last]
            if n_per == 1:
                seq = [m_last]
            if sw_in is None:
                sw_in = False          # 拼块模式：组件是并排贴着的，串内默认不画线
            log.append("每串拼法: %s" % " + ".join(
                ["1×" + m_first] + (["%d×%s" % (n_per - 2, m_mid)] if n_per > 2 else []) + ["1×" + m_last]))
        else:
            seq = [module] * n_per
        if sw_in is None:
            sw_in = True               # 老模式（同一个块重复）保持画串内线
        sw_in = bool(sw_in)
        kinds = {}
        for nm in dict.fromkeys(seq):
            recs = bmap.get(nm)
            if not recs:
                return None, ["外框图和块库里都没有组件块 " + nm], wires
            pr = []
            _prim_list(recs, bmap, (1, 0, 0, 1, 0, 0), 0, pr)
            bb = _prims_bbox(pr)
            if bb is None:
                return None, ["组件块 %s 没有几何" % nm], wires
            pl, nl, _fbk = _terminal_pair(recs, pr, bmap, nm, log)
            kinds[nm] = {"name": nm, "bb": bb, "pos_l": pl, "neg_l": nl}
        tmpl = [kinds[nm] for nm in seq]
        pos_l = tmpl[0]["pos_l"]         # 串首块的出线点（正极）
        neg_l = tmpl[-1]["neg_l"]        # 串尾块的出线点（负极）
        mbb = (min(t["bb"][0] for t in tmpl), max(t["bb"][1] for t in tmpl),
               min(t["bb"][2] for t in tmpl), max(t["bb"][3] for t in tmpl))
        # 板宽/板高按“首块和中间块”算：尾块可能多伸出一截（钩子），不能拿它当间距基准
        mid_t = tmpl[1] if len(tmpl) > 1 else tmpl[0]
        mw = max(tmpl[0]["bb"][1] - tmpl[0]["bb"][0], mid_t["bb"][1] - mid_t["bb"][0])
        mh = mid_t["bb"][3] - mid_t["bb"][2]
        if gap_x < 0:
            log.append("⚠ 板间净空 %.2f < 0，相邻两块会重叠" % gap_x)
        if gap_y < 0:
            log.append("⚠ 串间净空 %.2f < 0，相邻两串会重叠" % gap_y)

        # ---- BHA 桩的几何：按外形包围盒占位；块里有 CONN 点就用它当进出线接点 ----
        stub_geo = {}
        for e in bha:
            for nm in (e["stub"], e["motor"]):
                if not nm or nm in stub_geo:
                    continue
                recs = bmap.get(nm)
                if not recs:
                    log.append("⚠ 外框图和块库里都没有块 %s（BHA 桩/电机）" % nm)
                    continue
                pr = []
                _prim_list(recs, bmap, (1, 0, 0, 1, 0, 0), 0, pr)
                bb = _prims_bbox(pr)
                if bb is None:
                    log.append("⚠ 块 %s 没有几何，BHA 桩/电机这一处跳过" % nm)
                    continue
                pts = [(x, y) for x, y, _l in _conn_points(recs, bmap)]
                cx = (bb[0] + bb[1]) / 2.0
                lp = [p for p in pts if p[0] <= cx]
                rp = [p for p in pts if p[0] > cx]
                stub_geo[nm] = {"name": nm, "bb": bb,
                                "left_pt": (min(lp, key=lambda p: p[0]) if lp else None),
                                "right_pt": (max(rp, key=lambda p: p[0]) if rp else None)}
        # 一个条目要能画出来：桩块在库里就用桩块，桩块不在就退回电机块（块库里可以
        # 没有 BHA 桩块）；两个都不在的条目才丢掉，别把整张图卡住。
        # 以前是“桩块不在库就整条丢”，所以批量行按提示写 “30:BHA:MOTOR:0” 时
        # 连电机也一起没了 —— 看着就是“批量里加不了电机/BHA 位置”。
        _kept, _fell = [], {}
        for e in bha:
            nm, fell = bha_pick_block(e, lambda n: n in stub_geo)
            if not nm:
                continue
            e = dict(e, nm=nm, fell=fell)
            if fell:
                _fell[str(e.get("stub") or "")] = nm
            _kept.append(e)
        bha = _kept
        if _fell:
            log.append("提示: 块库里没有 %s 桩块 —— 这些 BHA/电机位置改用 %s 当桩"
                       "（插入位置、左右净空、板子右移都照旧）；想把桩画成真正的块，"
                       "把它的 .dxf 放进 blocklib/blocks/ 再生成一次"
                       % ("、".join(k for k in _fell if k),
                          "、".join(sorted(set(_fell.values())))))
        if bha and not any(g["left_pt"] and g["right_pt"] for g in stub_geo.values()):
            log.append("提示: BHA 桩块里没有左右 CONN 接点 —— 串内连线会从板直接连到板、穿过桩；"
                       "想要线接在桩上，在桩块左右各标一个 CONN 层的 POINT")
        stub_used = []          # [(串号, 插在第几块之后, 条目, 生成的桩 item)]

        def bha_items_for(s):
            """这一串要插的桩：**从右往左**插，前面插进去的才不会把后面的序号挤偏。"""
            mine = [e for e in bha if e["strings"] is None or s in e["strings"]]
            mine.sort(key=lambda e: e["after"], reverse=True)
            return mine

        tmpls = []              # 每串一份块序列（组件 + 桩）
        for s in range(n_str):
            items = list(tmpl)
            for e in bha_items_for(s):
                nm = e.get("nm") or e["stub"] or e["motor"]
                g = stub_geo.get(nm)
                if not g:
                    continue
                k = max(0, min(len(tmpl), int(e["after"])))      # 0 = 第 1 块之前
                it = {"name": nm, "bb": g["bb"], "kind": "stub",
                      "gap_l": (gap_x if e["gap_l"] is None else e["gap_l"]),
                      "gap_r": (gap_x if e["gap_r"] is None else e["gap_r"]),
                      # 退回电机块时它就是这一处的主体，别再挂一次
                      "motor": ("" if e.get("fell") else e["motor"]),
                      "rot": e["rot"],
                      "left_pt": (g["left_pt"] if not e.get("fell") else None),
                      "right_pt": (g["right_pt"] if not e.get("fell") else None)}
                items.insert(k, it)
                stub_used.append((s, k, e, it))
            tmpls.append(items)
        for s, k, e, it in stub_used:
            w = it["bb"][1] - it["bb"][0]
            log.append("整排第 %d 块处（串%d内第 %d 块之后）: %s "
                       "（桩宽 %.1f，左净空 %.1f 右净空 %.1f）"
                       "→ 它右边的板整体右移 %.1f%s"
                       % (s * n_per + k, s + 1, k, it["name"],
                          w, it["gap_l"], it["gap_r"],
                          w + it["gap_l"] + it["gap_r"] - gap_x,
                          ("；%s 挂在桩上，旋转 %.0f°" % (e["motor"], it["rot"]))
                          if e["motor"] and e["stub"] and not e.get("fell") else ""))
        if bha:
            log.append("BHA 桩: 插了 %d 处（%s）；阵列宽度按插入后的实际排布重算"
                       % (len(stub_used), "→".join(
                           "%s@串%d" % (it["name"], s + 1) for s, _k, _e, it in stub_used)))
        # 真正画出去的桩/电机块名（找不到的、被丢掉的条目不在这里）
        bha_used_names = [x for x in
                          ([it["name"] for _s, _k, _e, it in stub_used]
                           + [e["motor"] for _s, _k, e, _i in stub_used if e["motor"]]) if x]

        hinsts = _block_insts(bmap, harness, log) if harness else []
        # 支线块名字填错时不能让支线“掉队”（会被当成普通块串在中间）：
        # 在排版之前就退回用链里第一个块当支线，仍然按各串 CONNPOS 从左往右钉。
        _chain = [it["name"] for it in hinsts]
        # 链里一根正极支线都没有（也没公头顶着）时才算“填错名”，否则不用兜底
        if pos_feed and pos_feed not in _chain and not (pos_plug and pos_plug in _chain):
            if _chain:
                log.append("⚠ “正极支线块”填的 %s 不在线束链里（链里是 %s）："
                           "暂时改用链里第一个块 %s 当正极支线（按各串 CONNPOS 从左往右钉）"
                           % (pos_feed, ", ".join(_chain), _chain[0]))
                pos_feed = _chain[0]
            else:
                log.append("⚠ “正极支线块”填的 %s 不在线束链里，而且链是空的" % pos_feed)
        pos_names = [x for x in (pos_feed, pos_plug) if x]
        neg_names = [x for x in (neg_feed, neg_plug) if x]
        clear = mh * clear_r

        def place_harness(modmap, abox):
            """线束定位（单位空间）。

            规则：第 k 根正极支线钉在第 k 串 CONN_POS 点的正下方，
                  第 k 根负极支线钉在第 k 串 CONN_NEG 点的正下方，
                  其余块（FUSE 之类）从上一块的右边接着排。
            所有线束块的顶边对齐到同一条线，整条线束挂在阵列下方。
            缩放：按“右侧接点间距一致”对齐（和链模式第 5 章同一套算法）。
            modmap 里是**组件块**（桩不在里面）：插了 BHA 桩的串，首尾板的位置
            跟着右移，支线自然跟着挪过去。
            """
            if not hinsts:
                return []
            # 缩放口径：**所有线束块的接点间距统一**（取各块右侧接点间距的几何平均当目标），
            # 这样块与块之间的连线才是平的、间距才一致（“连接点缩放到对齐”）。
            # 再乘一个 harness_scale 方便手工微调。
            panel_span = abs(pos_l[0] - neg_l[0])          # 板子正负极出线点的距离
            scales = [s * h_scale for s in
                      (_match_span_scales(hinsts) if match_h else [1.0] * len(hinsts))]
            px = [modmap[(0, s)]["P"][0] + pos_l[0] for s in range(n_str)]
            nx = [modmap[(n_per - 1, s)]["P"][0] + neg_l[0] for s in range(n_str)]
            places, kp, kn = [], 0, 0
            right = None
            head_right = None        # 不钉位的块（保险丝/接头/汇流箱）自己排一行，从阵列左边缘起
            # 头部这一行的起点：**对齐汇流箱(CBX)的中心**（CBX 在阵列左边）
            head_x0 = None
            if head_blk:
                for it0 in hinsts:
                    if it0["name"] == head_blk:
                        b0 = cl.bbox(it0["prims"])
                        w0 = (b0[1] - b0[0]) * scales[hinsts.index(it0)]
                        head_x0 = abox[0] - head_gap - w0 / 2.0
                        break
            elif _nseg > 1:
                # 分段画时，后面那些段没有起始块（CBX 只挂在第 1 段上），但线束起点照样
                # 按同样的“起始块间距”往左让出同样长的一段：以后从这一段引出、接到 CBX
                # 的那根线，长度才和第 1 段的出线一致；而且它在下面走，不会压到前一段线束。
                head_x0 = abox[0] - head_gap
            prev_outs = None
            prev_name = None
            # 链里第一个“支线”出现的位置：它前面的不钉位块算“头部”（排左边），
            # 它后面的不钉位块算“末端”（接在最后一块右边）→ 一条链 = 头 … 支线 … 尾
            # 只看**正极支线**：公头/母头（接头块）不算支线，否则链首那个接头会被当成
            # “第一根支线”钉到板子端子上（正极行的头块会跑到第 1 串负极下面去）。
            _fi = [i for i, it in enumerate(hinsts) if it["name"] in pos_names]
            first_feed = _fi[0] if _fi else len(hinsts)
            # 负极那一行整体往下挪：正极行里最高的块 + neg_gap（只在拿不到正极行实际位置时兜底）
            _ph = [(cl.bbox(it["prims"])[3] - cl.bbox(it["prims"])[2]) * scales[i]
                   for i, it in enumerate(hinsts) if it["name"] in pos_names]
            neg_dy = (max(_ph) if _ph else 0.0) + neg_gap

            def row_gap(_i):
                """这一块与相邻块的间距（和界面“块固定间距”同一套口径）。

                Harness 方案里保险丝用 fuse_gap（默认 20）：用户口径是
                “fuse 和靠近的那个块的距离保持 20”；其余块仍旧是界面的“块固定间距”。
                """
                # fuse：**两侧都按 fuse_gap 走**（用户口径：以 fuse 的距离为优先级，
                # 不能被“块固定间距”盖掉）。所以看这一块和它前一块。
                if _is_harness and (
                        _squash(hinsts[_i]["name"]) == _squash(fuse_blk)
                        or (_i > 0
                            and _squash(hinsts[_i - 1]["name"]) == _squash(fuse_blk))):
                    return fuse_gap
                # IBEX 的 CU-AI 转接：和相邻块、以及两个转接彼此之间都是 cual_gap(50)
                if _is_ibex and cual_blk and (
                        _squash(hinsts[_i]["name"]) == _squash(cual_blk)
                        or (_i > 0
                            and _squash(hinsts[_i - 1]["name"]) == _squash(cual_blk))):
                    return cual_gap
                return fix_gap

            # 正极行到底排在哪一行，得边排边量：正极行的 y 由链首（CBX→FUSE→支线）那串
            # 接点对齐算出来，光看“支线块的高度”是估不准的。以前用高度当代理，头部块一进链
            # 就差 30~40 个单位，负极行直接被排到正极行**上面**去了（两行叠在一起）。
            # pos_bottom = 正极行里最低的那一点（块底和接点取更低者），负极行的行线照它往下 neg_gap。
            pos_bottom = None
            neg_row_y = None          # 负极行整排共用的 y（首块定下来，后面都跟它）
            # ---- 负极行每块的朝向 ----
            #  · 竖着的“支线块”（NEG 这类：接点在块的一头、身子是竖的）**正着放**，
            #    和正极行一样：插头朝上、接点朝下落在负极行线上。以前整行硬转 180°，
            #    画出来就是“负极支线倒过来”（测试里看到的那张）。要那副样子，
            #    把界面的“负极行旋转”填 180 就行。
            #  · 横着的“接头块”（公头/母头：接点只在一侧、身子是横的）自动转到
            #    **接点朝链内、身子朝外**，不管它排在链首还是链尾（Male 在链首→180°，
            #    Fmale 在链尾→180°；反过来摆也能自己认）。
            neg_rot_by, neg_above = {}, {}   # idx -> 实际旋转角度 / 该块伸出负极行线以上的高度
            if neg_from is not None:
                for _i in range(neg_from, len(hinsts)):
                    _it = hinsts[_i]
                    _b = cl.bbox(_it["prims"])
                    _s = scales[_i]
                    _R = [(x * _s, y * _s) for x, y in
                          cl.side_ports(_it["prims"], _it["pts"], "right")]
                    _L = [(x * _s, y * _s) for x, y in
                          cl.side_ports(_it["prims"], _it["pts"], "left")]
                    _rot = neg_rot
                    _pw, _ph2 = (_b[1] - _b[0]) * _s, (_b[3] - _b[2]) * _s
                    _pins = _R + _L
                    _pxs = [p[0] for p in _pins]
                    if _pins and _pw > _ph2 and (max(_pxs) - min(_pxs)) < max(0.5, _pw * 0.1):
                        # 单侧接点的横块 = 接头：接点要朝链内（首块朝右、其余朝左）
                        _pin_right = ((min(_pxs) + max(_pxs)) / 2.0) > (_b[0] + _b[1]) / 2.0 * _s
                        _rot = 0.0 if _pin_right == (_i == neg_from) else 180.0
                    # 这块“伸出负极行线以上”多少：行线按它往下让，免得压住正极行
                    _top = (-_b[2] * _s) if _rot else (_b[3] * _s)
                    _ab = 0.0
                    for _c in (_R, _L):
                        if not _c:
                            continue
                        _mid = sum(p[1] for p in _c) / len(_c)
                        if _rot:
                            _mid = -_mid
                        _ab = max(_ab, _top - _mid)
                    neg_rot_by[_i] = _rot
                    neg_above[_i] = _ab
            for idx, it in enumerate(hinsts):
                b = cl.bbox(it["prims"])
                s = scales[idx]
                cw = (b[0] + b[1]) / 2.0 * s          # 块中心到插入点的横向距离
                bw = b[1] * s                          # 块右边界到插入点
                r = [(x * s, y * s) for x, y in cl.side_ports(it["prims"], it["pts"], "right")]
                l = [(x * s, y * s) for x, y in cl.side_ports(it["prims"], it["pts"], "left")]
                is_neg = (neg_from is not None and idx >= neg_from)
                _rot = neg_rot_by.get(idx, 0.0) if is_neg else 0.0
                if is_neg and _rot:
                    # 这块转 180°：局部坐标 (x,y) -> (-x,-y)
                    r = [(-x, -y) for x, y in r]
                    l = [(-x, -y) for x, y in l]
                    cw = -cw
                    bw = -b[0] * s
                # 横向：支线把“接点”对准板子的出线点（不是把块中心对过去）；
                #       正极支线对左接点，负极支线对右接点；其余的接着上一块排。
                if is_neg:
                    # 负极行：**头部接头**（第 1 块）对齐 CBX —— 和正极行的头部同一个横坐标，
                    # 只是排在下面那一行；其余每块钉在第 k 串的负极出线点正下方
                    # （第 1 串下面就是负极支线，不再是公头）。
                    # 块里标了 CONNNEG 就用**那个接点**去对（和正极用 CONNPOS 一个道理）；
                    # 没标就退回用块中心。
                    _k = idx - neg_from
                    if _k == 0:
                        cx = head_x0 if head_x0 is not None else (nx[0] if nx else abox[0])
                    else:
                        _j = _k - 1
                        cx = nx[_j] if _j < len(nx) else (nx[-1] if nx else abox[0])
                    _nn = [(x, y) for x, y, _ly in _conn_points(bmap.get(it["name"], []), bmap)
                           if "NEG" in (_ly or "").upper()]
                    if _k == 0:
                        # 头部接头不钉板子：按**块中心**对齐 CBX。正极行的头部块也是中心对齐，
                        # 这样两个头部的中心才在同一条竖线上（下面才算插针）
                        pxx = cx - cw
                    elif _nn:
                        px0, py0 = _nn[0]
                        # 转过 180° 的块，接点在图上跑到 -x
                        pxx = cx - ((-px0) if _rot else px0) * s
                    else:
                        # 没有 CONNNEG 点的块（公头 Male / 母头 Fmale）以前按“块中心”对，
                        # 可它们的块中心离接点 9~11 个单位，插针就落在出线点旁边。
                        # 改成按**接点竖列的中线**对：只有一列的块（公头/母头）接点正好压在
                        # 出线点正下方；左右对称的块（NEG）中线≈块中心，位置和以前一样。
                        _xs = [p1[0] for p1 in (l + r)]
                        _off = ((min(_xs) + max(_xs)) / 2.0) if _xs else cw
                        pxx = cx - _off
                # 正极支线 + 末端公头都算“正极那一路”：第 k 个钉在第 k 串出线点的正下方
                elif it["name"] in pos_names and kp < len(px) and l:
                    pxx = px[kp] - l[0][0]; kp += 1
                    # 支线是钉在板子端子上的，但头部那一行（CBX→FUSE→…）是按间距往右排的，
                    # 排过来可能**压在第一根支线身上**（用户反馈：正极出线头附近 fuse 和
                    # 正极支线重叠）。这里量一下：不够就把整个头部行往左顶开，
                    # 支线位置不动（它必须钉在板子端子上）。
                elif it["name"] in neg_names and neg_from is None and kn < len(nx) and r:
                    pxx = nx[kn] - r[0][0]; kn += 1
                elif head_blk and it["name"] == head_blk:
                    # 起始块（CBX）摆在阵列左边 head_gap 处；它的中心 x = head_x0，
                    # 正极行的头部块和负极行的头部块都对齐这个 x（上下一条竖线，互不重叠）。
                    pxx = (head_x0 if head_x0 is not None else abox[0]) - cw
                elif idx > first_feed:
                    # 支线**之后**的块（末端接头之类）：接着最后一块往右排 → 落在最右边
                    # 间距统一用**固定间距**：除正极支线/负极支线/公头/母头这四个“按
                    # 板子接点定位”的块以外，其余块（FUSE、CU-AL、接头……）之间
                    # 一律等距，不再跟着界面上那个大 GAP 走。
                    g = row_gap(idx)
                    cx = (abox[0] + cw) if right is None else (right + g + cw)
                    pxx = cx - cw
                else:
                    # FUSE / CU-AL / 起始块这类“不钉板子接点”的块：一律固定间距
                    g = row_gap(idx)
                    # 它们自己排一行，从**阵列左边缘**起头；不接着支线往后排
                    # （接着支线排的话，保险丝/接头会被推到线束中间去）
                    cx = ((head_x0 if head_x0 is not None else abox[0] + cw)
                          if head_right is None else (head_right + g + cw))
                    head_right = cx + bw
                    pxx = cx - cw
                if is_neg:
                    # 负极行整排**共一条水平线**：
                    #   每块把“朝链内那一列的接点中线”放到同一条 y 上，行内每根线两端一样高。
                    # （以前每块各自按上一块接点算，Male 的接点在块局部 ±2、NEG 的插头在
                    #   ±72，差 70 个单位，于是蓝线是斜的，看着像整排“倒过来”。）
                    # 锚点必须是同一种点：Male/Fmale 的接点在插入点两侧、NEG 的接点在块的一头，
                    # 取“离插入点最远的那个接点”当锚，公头/母头就比 NEG 高出一格接点间距，
                    # 两端的线于是斜 3~4 个单位。改成一整列接点的中线就不会错格。
                    # 行线高度 = 正极行最低点再往下 neg_gap（拿不到就用老口径 -neg_dy）。
                    _col = l if idx == neg_from else r        # 首块看“出去”那一列，其余看“进来”那一列
                    if not _col:
                        _col = l + r
                    _row = (sum(p1[1] for p1 in _col) / len(_col)) if _col else 0.0
                    if neg_row_y is None:
                        # 行线 = 正极行最低点 - neg_gap - “负极行里最高那块伸出来的高度”
                        # （块正放时它有大半个身子在行线以上，得把这段让出来，否则压住正极行）
                        _ab = max([neg_above.get(_i, 0.0)
                                   for _i in range(neg_from, len(hinsts))] or [0.0])
                        neg_row_y = ((pos_bottom - neg_gap - _ab) if pos_bottom is not None
                                     else -neg_dy)
                    pyy = neg_row_y - _row
                elif head_blk and it["name"] == head_blk:
                    # 它跟板子同一水平线（块中心对齐阵列那一行的中线），不是挂在线束行上
                    pyy = -(b[2] + b[3]) / 2.0 * s
                elif it["name"] in neg_names and neg_from is None:
                    # 负极那一行（负极端子直接放进链里、没自动补齐的情况）：顶边挂在同一条行线上
                    pyy = ((pos_bottom - neg_gap - max(neg_above.values() or [0.0]))
                           if pos_bottom is not None else -neg_dy) - b[3] * s
                elif prev_outs is None:
                    pyy = -b[3] * s                 # 第一块：顶边挂在 y=0（阵列下沿）
                else:
                    # 纵向按接点对齐：本块左接点落到上一块右接点的高度上，连线才是平的
                    pr = _match_pairs(prev_outs, [(x + pxx, y) for x, y in l])
                    offs = sorted(prev_outs[i][1] - l[j][1] for (i, j) in pr)
                    pyy = offs[len(offs) // 2] if offs else (-b[3] * s)
                if (not is_neg) and not (head_blk and it["name"] == head_blk):
                    # 记下正极行最低的那一点（块底 vs 接点，取更低者）：负极行照它往下排
                    _low = min([pyy + b[2] * s] + [pyy + p1[1] for p1 in (l + r)])
                    pos_bottom = _low if pos_bottom is None else min(pos_bottom, _low)
                P = (pxx, pyy)
                places.append({"P": P, "s": s,
                               "bb": b, "name": it["name"],
                               "rot": _rot,
                               # 转过的负极端（180°）：块局部的“左列”在图上跑到右边，
                               # 所以进线/出线要对调，线才接在**朝链内**的那一侧接点上
                               # （不换的话线会从块的外侧兜过来、压在块身上）。
                               "outs": [(x + P[0], y + P[1]) for x, y in (l if _rot else r)],
                               "lins": [(x + P[0], y + P[1]) for x, y in (r if _rot else l)],
                               "right": P[0] + bw})
                prev_outs = places[-1]["outs"]
                right = P[0] + bw
                prev_name = it["name"]
            if kp < n_str and not pos_plug:
                log.append("⚠ 正极支线只有 %d 根、串数 %d：多出来的串没有正极支线"
                           "（把末端公头块填上，它顶一根）" % (kp, n_str))
            # 收尾：头部那一行（第一根支线之前的块）与第一根支线之间的间距**顶准**。
            # 支线钉在板子端子上不能动，所以把头部行整条平移：头一行最后一块的右端
            # 到第一根支线左端，正好留 fuse 间距（用户口径：这个距离要准，不能过大/重叠）。
            if head_blk and 0 < first_feed < len(places):
                _hp = places[:first_feed]
                _fp = places[first_feed]
                try:
                    _hright = max(p["P"][0] + p["bb"][1] * p["s"] for p in _hp)
                    _fleft = _fp["P"][0] + _fp["bb"][0] * _fp["s"]
                    _fq = _squash(fuse_blk) if fuse_blk else ""
                    _near_fuse = _is_harness and _fq and (
                        _squash(_hp[-1]["name"]) == _fq or _squash(_fp["name"]) == _fq)
                    _gap_want = fuse_gap if _near_fuse else fix_gap
                    _dx = (_hright + _gap_want) - _fleft
                    if abs(_dx) > 0.01:
                        for _p in _hp:
                            _p["P"] = (_p["P"][0] - _dx, _p["P"][1])
                            _p["outs"] = [(x - _dx, y) for x, y in _p["outs"]]
                            _p["lins"] = [(x - _dx, y) for x, y in _p["lins"]]
                            _p["right"] -= _dx
                        if head_x0 is not None:
                            head_x0 -= _dx
                        log.append("头部行与第一根支线的间距校正 %.1f → %.0f"
                                   % (_dx + _gap_want, _gap_want))
                except Exception:
                    pass
            # 负极支线的根数：自动补齐时 = 负极行里钉在板子上的块数（头部的接头不算）
            _n_neg = ((len(hinsts) - neg_from - 1) if neg_from is not None else kn)
            if neg_feed and _n_neg < n_str and not neg_plug:
                log.append("⚠ 负极支线只有 %d 根、串数 %d" % (_n_neg, n_str))
            if match_h:
                log.append("线束缩放: " + ", ".join(
                    "%s x%.3f" % (hinsts[i]["name"], scales[i]) for i in range(len(hinsts)))
                    + "（所有线束块按“接点间距一致”缩放；板子出线点间距 %.2f）" % panel_span)
            return places

        def layout(gx, gy):
            """gx=板间净空, gy=串间净空（都用净空，pitch 由块宽算出来）。"""
            # 分段时这里是**一段**（外面按段循环，每段各画一套），段内只有一种净空：
            # 传整串分组的话，第 2 串之后会改用“跨支架距离”（以前第 3 串就是这么错的）。
            cells, abox = _place_array(tmpls, n_str, mw + gx, gx, gy, dirn,
                                       groups=[n_str], group_gap=brk_gap)
            modmap = {(c["mi"], c["s"]): c for c in cells if c["mi"] is not None}
            hp = place_harness(modmap, abox)
            # 起始块（CBX）钉在阵列那一排，不参与“线束行”的定位
            head_i = [i for i, p in enumerate(hp)
                      if head_blk and p.get("name") == head_blk]
            row_i = [i for i in range(len(hp)) if i not in head_i]
            hbox = (_content_bbox([hinsts[i] for i in row_i], [hp[i] for i in row_i],
                                  [hp[i]["s"] for i in row_i]) if row_i else None)
            hoff = (0.0, 0.0)
            if hbox:
                # 支线是“钉”在端子的 x 上的，所以线束整体不再横向居中，
                # 只把线束顶边挂到阵列下沿（净空 clear）下方。
                hoff = (0.0, abox[2] - clear - hbox[3])
                box = (min(abox[0], hbox[0] + hoff[0]), max(abox[1], hbox[1] + hoff[0]),
                       min(abox[2], hbox[2] + hoff[1]), max(abox[3], hbox[3] + hoff[1]))
            else:
                box = abox
            # 起始块：横向在阵列左边固定距离、纵向对齐阵列那一行的中线
            for i in head_i:
                b0 = hp[i]["bb"]; s0 = hp[i]["s"]
                # 单位空间里“阵列那一行”的中线就是 y=0，所以这里不减 hoff（它不跟着下移）
                hp[i]["y0"] = -(b0[2] + b0[3]) / 2.0 * s0
                box = (min(box[0], hp[i]["P"][0] + b0[0] * s0),
                       max(box[1], hp[i]["P"][0] + b0[1] * s0),
                       min(box[2], hp[i]["y0"] + b0[2] * s0),
                       max(box[3], hp[i]["y0"] + b0[3] * s0))
            # 标注也要留在内框里：长度标注在线束上方、红色总长在下方，
            # 以前算内容框只按块算，标注常常顶出内框（用户反馈“图超过内框”）。
            # 上下各留一段（单位空间），出图缩放 k 就会把它们一起算进去。
            _ann_pad = 26.0
            box = (box[0], box[1], box[2] - _ann_pad, box[3] + _ann_pad)
            return {"cells": cells, "modmap": modmap, "abox": abox, "hoff": hoff,
                    "box": box, "hp": hp}

        L = layout(gap_x, gap_y)
        # 这一段画出去的顶层块，**跨段累计**（起始块 CBX 只在第 1 段的链里）：
        # 只按最后一段算的话，画到 CAD 时 CBX 会被跳过 —— 分段图丢汇流箱就是这么来的。
        for _c in L["cells"]:
            for _n2 in (_c.get("name"), _c.get("motor")):
                if _n2 and _n2 not in _stats_names:
                    _stats_names.append(_n2)
        for _it2 in hinsts:
            if _it2["name"] and _it2["name"] not in _stats_names:
                _stats_names.append(_it2["name"])
        pg(45, "阵列/线束排布完成")
        log.append("阵列: %d 串 x %d 块，%s，板间净空 %.1f / 串间净空 %.1f，内容 %.1f x %.1f" %
                   (n_str, n_per, "串从左往右接" if dirn != "down" else "串从上往下叠",
                    gap_x, gap_y, L["box"][1] - L["box"][0], L["box"][3] - L["box"][2]))

        # ---- 缩放（13.7）：可用区 = 画图区 左右各内缩 inset_x、上下各内缩 inset_y ----
        rect = frame_draw_rect(sec)
        fbx = rect or _records_bbox(group_entities(sec.get("ENTITIES", [])), bmap)
        if fbx and _seg_common is None and _nseg > 1 and not (spec.get("bha") or []):
            # 分段：所有段共用**同一个 k 和同一条水平线** —— 板子才在同一高度、大小也一致。
            # 各段的“内容宽”在这里量：阵列本身用 _place_array 精确算（串数不同宽度不同），
            # 线束/起始块那些外伸量按本段实测的差值当常数补上（实测每多一串恒定 +305）。
            try:
                _arr = {}
                for _g in dict.fromkeys(_seg_sizes):
                    _c3, _b3 = _place_array([list(tmpl) for _ in range(_g)], _g,
                                            mw + gap_x, gap_x, gap_y, dirn,
                                            groups=[_g], group_gap=None)
                    _arr[_g] = _b3[1] - _b3[0]
                # 段与段之间要的是**阵列之间的跨支架距离**（用户口径 2026-09-23：
                # 填 30 就该量出 30）。以前这里把“线束/起始块那些外伸量”也加进每段宽度，
                # 于是两段阵列之间凭空多出一大截（填 30 量出 100 多）。
                _ws = {_g: _arr[_g] for _g in _arr}
                _tot = sum(_ws[_g] for _g in _seg_sizes) + brk_gap * (_nseg - 1)
                _av_w = (fbx[1] - fbx[0]) * (1 - 2 * inset_x)
                _av_h = (fbx[3] - fbx[2]) * (1 - 2 * inset_y)
                _ch = L["box"][3] - L["box"][2]
                if _tot > 1e-6 and _ch > 1e-6:
                    _kk = min(_av_w / _tot, _av_h / _ch)
                    if not allow_up:
                        _kk = min(_kk, 1.0)
                    _x_of, _cur = [], 0.0
                    for _g in _seg_sizes:
                        _x_of.append(_cur)
                        _cur += _ws[_g] + brk_gap
                    _seg_common = {"k": _kk, "tot": _tot, "x_of": _x_of,
                                   "yc": (fbx[2] + fbx[3]) / 2.0,
                                   "box_cy": (L["box"][2] + L["box"][3]) / 2.0}
            except Exception:
                _seg_common = None
        k = 1.0
        if fbx:
            av_w = (fbx[1] - fbx[0]) * (1 - 2 * inset_x)
            av_h = (fbx[3] - fbx[2]) * (1 - 2 * inset_y)
            if not _seg_common:
                av_w = av_w / float(_nseg)    # 分段但量不出共用宽度时：每段只占 1/N 宽
            cw = L["box"][1] - L["box"][0]
            ch = L["box"][3] - L["box"][2]
            if cw > 1e-6 and ch > 1e-6:
                if _seg_common:
                    k = _seg_common["k"]
                else:
                    k = min(av_w / cw, av_h / ch)
                    if not allow_up:
                        k = min(k, 1.0)
                log.append("可用区 %.0f x %.0f%s，内容 %.0f x %.0f，k=%.4f%s" %
                           (av_w, av_h,
                            ("（%d 段共用）" % _nseg) if _seg_common else "",
                            cw, ch, k, "" if allow_up else "（只缩不放）"))
                if k < k_floor and _seg_common:
                    log.append("⚠ 装不进当前外框：%s 串 × %d 块，k=%.2f 偏小，"
                               "请减少串数/板数，或换更大的框"
                               % ("+".join(str(x) for x in _seg_sizes), n_per, k))
                elif k < k_floor:
                    # 用户口径（2026-09-23）：装不下时**只用整体缩放**（k）解决，
                    # 不许为了塞进框去压缩间距 —— 压缩间距会让块贴到一起、
                    # 尺寸也不是你填的值了。所以这里不再改 gap，只报一句。
                    log.append("k=%.3f 偏小：按你填的间距等比缩到能放下；"
                               "要更清楚就减少串数/板数，或换更大的框" % k)
                    if k < k_floor:
                        log.append("⚠ 装不进当前外框：%d 串 x %d 块（k<%.2f）在可用区里最多约 "
                                   "%d 串 x %d 块，请减少串数/板数，或换更大的框"
                                   % (n_str, n_per, k_floor,
                                      max(1, int(av_h / (k_floor * max(mh + gap_y, 1e-6)))),
                                      max(1, int(av_w / (k_floor * max(mw + gap_x, 1e-6))))))

        off = (0.0, 0.0)
        if fbx:
            if _seg_common:
                # 分段：整体（各段 + 段间跨支架）在画图区居中，各段按顺序往右排；
                # 纵向共用同一条基准线 —— 所以每段的板子在同一高度，缩放也一致。
                _sc = _seg_common
                off = ((fbx[0] + fbx[1]) / 2.0 - _sc["tot"] * k / 2.0
                       + _sc["x_of"][_si] * k - L["box"][0] * k,
                       _sc["yc"] - _sc["box_cy"] * k)
            else:
                off = ((fbx[0] + fbx[1]) / 2.0 - (L["box"][0] + L["box"][1]) / 2.0 * k,
                       (fbx[2] + fbx[3]) / 2.0 - (L["box"][2] + L["box"][3]) / 2.0 * k)
                if _nseg > 1:              # 分段（量不出共用宽度时）：每段占一条带宽
                    _band = (fbx[1] - fbx[0]) / float(_nseg) if fbx else 0.0
                    off = (off[0] + (_si - (_nseg - 1) / 2.0) * _band, off[1])
        log.append("套用外框图: %s（画图区 %s，内容偏移 %.1f, %.1f）" %
                   (os.path.basename(frame),
                    ("%.0fx%.0f" % (fbx[1] - fbx[0], fbx[3] - fbx[2])) if fbx else "无",
                    off[0], off[1]))
        pg(60, "缩放/定位完成")

        def F(p):
            """单位空间 -> 图纸坐标。"""
            return (p[0] * k + off[0], p[1] * k + off[1])

        def FH(p):
            """线束专用的映射：分段时逐段往下错开，几套线束才不叠在一起。"""
            q = F(p)
            return (q[0], q[1] - _si * _stag)

        _stag = 0.0
        if _nseg > 1:
            # 分段（x+y 支架）时，后一段的线束整体再往下让开多少。
            # 用户口径（2026-09-23：“y 的线束还要再往下移，现在错开不完全”）：
            # 按**线束块自己的高度**算，而不是模块高度 —— 支线块比组件块高得多。
            # 一套线束 = 正极行 + 负极行，所以按块高 ×2.6 留（含负极行与标注）。
            _hh = 0.0
            try:
                for _it in hinsts:
                    _b = cl.bbox(_it["prims"])
                    _hh = max(_hh, _b[3] - _b[2])
            except Exception:
                pass
            _stag = max(mh, _hh) * k * 2.6

        th = max(0.5, label_r * (fbx[3] - fbx[2])) if fbx else 1.0
        # ---- 线号标注形式：CAD 原生线性标注（默认） / 老式 TEXT ----
        _dim_style = dim_style_name(sec)
        # 默认走**文字标注**：ZWCAD 2025 目前对本程序生成的 DIMENSION 会报
        # “无效或不完整的 DXF 输入 —— 图形被放弃”，等原生标注那条路验完再打开。
        # 默认就是**原生标注**（用户口径 2026-09-23：标注默认保留原生 DIMENSION）
        _annot = (spec.get("annot") or "dim").strip().lower()
        if _annot not in ("text", "shape", "dim"):
            _annot = "dim"
        _dim_ok = (_annot != "text") and bool(_dim_style)
        dim_jobs = []           # [(块名, 块内容实体)]
        dim_reqs = []           # [(a, b, anchor, txt)] —— 并入失败时退回文字用
        dim_ent_pairs = []      # DIMENSION 实体的组（句柄等块定义并进来之后再发）
        _n_shape = [0]          # “标注外观”画了多少个
        if _annot != "text" and not _dim_style:
            log.append("⚠ 外框图里没有标注样式(DIMSTYLE)，线号退回文字标注")
        # modcell：(第几块组件, 串) -> 那块（插了 BHA 桩的串，槽位号和组件号就不一样了）
        modcell = dict(L["modmap"])
        cells_by_s = {s: sorted((c for c in L["cells"] if c["s"] == s),
                                key=lambda c: c["i"]) for s in range(n_str)}

        def mod_pts(i, s):
            """(i,s) 那块在图纸上的：插入点 / 正极 / 负极 / 块中心。

            每块用自己的 geo：串首块的正极、串尾块的负极都是它自己块里的接点。
            """
            c = modcell[(i, s)]
            P = F(c["P"])
            b = c["bb"]
            return ((P[0], P[1]),
                    (P[0] + c["pos_l"][0] * k, P[1] + c["pos_l"][1] * k),
                    (P[0] + c["neg_l"][0] * k, P[1] + c["neg_l"][1] * k),
                    (P[0] + (b[0] + b[1]) / 2.0 * k, P[1] + (b[2] + b[3]) / 2.0 * k))

        def slot_pt(c, pt):
            """槽位块自己坐标系里的一个点 -> 图纸坐标（桩的左右接点用）。"""
            if not pt:
                return None
            P = F((c["P"][0] + pt[0], c["P"][1] + pt[1]))
            return P

        def stubs_between(s, i):
            """第 s 串里，夹在第 i 块和第 i+1 块组件之间的所有槽位（按从左往右）。"""
            out = []
            a = modcell.get((i, s))
            b = modcell.get((i + 1, s))
            if not a or not b:
                return out
            for c in cells_by_s.get(s, []):
                if c["kind"] == "stub" and a["i"] < c["i"] < b["i"]:
                    out.append(c)
            return out

        if _si == 0:                       # 句柄计数跨段共用，不能每段从头来（否则撞号）
            handle = [maxh + 1]

        def nh():
            v = "%X" % handle[0]; handle[0] += 1; return v

        def blk(c, v):
            return (c + "\r\n" + str(v) + "\r\n").encode("utf-8")

        if _si == 0:                       # 实体累加器也只建一次，后面的段往里追加
            content = bytearray()

        def emit_insert(name, x, y, s):
            return emit_insert_rot(name, x, y, s, 0.0)

        def emit_insert_rot(name, x, y, s, rot):
            for c, v in [("0", "INSERT"), ("5", nh()), ("330", mspace or "0"),
                         ("100", "AcDbEntity"), ("8", "0"), ("100", "AcDbBlockReference"),
                         ("2", name),
                         ("10", "%.6f" % x), ("20", "%.6f" % y), ("30", "0.0"),
                         ("41", "%.6f" % s), ("42", "%.6f" % s),
                         ("43", "1.0"), ("50", "%.4f" % rot)]:
                content.extend(blk(c, v))

        def emit_wire(a, b):
            return emit_wire_c(a, b, 1)

        def emit_wire_c(a, b, col):
            """col: 1=红(正极那一路)  7=白(负极那一路)"""
            for c, v in [("0", "LINE"), ("5", nh()), ("330", mspace or "0"),
                         ("100", "AcDbEntity"), ("8", "WIRE"), ("62", str(col)),
                         ("100", "AcDbLine"),
                         ("10", "%.6f" % a[0]), ("20", "%.6f" % a[1]), ("30", "0.0"),
                         ("11", "%.6f" % b[0]), ("21", "%.6f" % b[1]), ("31", "0.0")]:
                content.extend(blk(c, v))

        def emit_label(x, y, txt, h=None, center=False):
            """线号文字。

            center=True：传进来的 (x, y) 就是**文字的几何中心**（72=1 水平居中、
            73=2 垂直居中，11/21 是那个中心点）—— 用户口径：“用线号的几何中心去对
            这段线束中点的正上方”。
            """
            h = th if h is None else h
            rec = [("0", "TEXT"), ("5", nh()), ("330", mspace or "0"),
                   ("100", "AcDbEntity"), ("8", "WIRE_LABEL"), ("100", "AcDbText"),
                   ("10", "%.6f" % x), ("20", "%.6f" % y), ("30", "0.0"),
                   ("40", "%.4f" % h), ("1", txt), ("50", "0.0")]
            if center:
                rec += [("72", "1"), ("11", "%.6f" % x), ("21", "%.6f" % y),
                        ("31", "0.0"), ("73", "2")]
            for c, v in rec:
                content.extend(blk(c, v))

        def emit_dim(a, b, anchor, txt):
            """CAD 原生“线性标注”：界线原点 = 这条线的两个接点，尺寸线过 CONN-Label 点。

            标注实体先攒在 dim_ents 里，等标注块定义并进外框之后再一起拼进去
            （块定义没进去的话，CAD 里什么都不显示）。
            """
            if not _dim_ok:
                return False
            ents, g = dim_geom(a, b, anchor, txt, th)
            if not g:
                return False
            idx = len(dim_jobs) + 1
            temp = "SLDDIM%04d" % idx                     # 临时文件名 = 打包时的块名
            final = "*D%04d" % (9000 + idx)               # 打包后改成 CAD 自己的匿名标注块名
            dim_jobs.append((temp, final, ents))
            dim_reqs.append((a, b, anchor, txt))
            dim_ent_pairs.append(dim_entity_pairs(final, _dim_style, a, b, anchor, txt, g, th))
            return True

        def emit_shape(a, b, anchor, txt):
            """把标注**画成普通实体**：尺寸线 + 两条尺寸界线 + 两个箭头 + 文字。

            外观和线性标注一样，但全是标准 LINE/SOLID/MTEXT —— 不依赖 DIMENSION 实体，
            在任何 CAD 里都能正常打开（ZWCAD 2025 对 DIMENSION 会判无效，这条是兜底）。
            代价：不是真标注，不能像标注那样拖动/关联更新，改了要重新生成。
            """
            ents, g = dim_geom(a, b, anchor, txt, th)
            if not g:
                return False
            return emit_shape_ents(ents)

        def emit_shape_ents(ents):
            """把一组“标注图元”当普通实体画进模型空间（图层统一 WIRE_LABEL）。

            长度标注现在用水平版（dim_geom_h），图元由外面算好传进来 ——
            和老的 emit_shape 共用这一段落盘逻辑。
            """
            if not ents:
                return False
            for rec in ents:
                t = rec[0][1] if rec and rec[0][0] == "0" else ""
                if t == "POINT":
                    continue           # 定义点只有真标注才需要，画在图上反而碍事
                content.extend(blk("0", t))
                content.extend(blk("5", nh()))
                content.extend(blk("330", mspace or "0"))
                for c, v in rec[2:]:            # rec[1] 是给块用的 330，这里换成模型空间的
                    # 图层统一改到 WIRE_LABEL：dim_geom 里这些线/箭头/文字默认在 0 层，
                    # 而“画到 CAD”只回放我们自己那几层（WIRE / WIRE_LABEL / CONN_*），
                    # 落在 0 层的标注会被当成外框图自带的东西过滤掉 ——
                    # 用户看到的“长度标注没画出来”就是这么来的。
                    if c == "8":
                        v = "WIRE_LABEL"
                    content.extend(blk(c, v))
            _n_shape[0] += 1
            return True

        def emit_dim_h(o1, o2, y_line, txt, ents, g, th_dim):
            """注册一个**水平长度标注**（CAD 原生 DIMENSION），和 emit_dim 一套收尾流程。"""
            if not _dim_ok:
                return False
            idx = len(dim_jobs) + 1
            temp = "SLDDIM%04d" % idx
            final = "*D%04d" % (9000 + idx)
            dim_jobs.append((temp, final, ents))
            dim_reqs.append((o1, o2, (o1[0], y_line), txt))
            dim_ent_pairs.append(dim_entity_pairs_h(final, _dim_style, o1, o2,
                                                     y_line, txt, g, th_dim))
            return True

        def emit_annot(a, b, anchor, txt):
            """线号标注的三种画法：text=文字（默认）/ shape=标注外观 / dim=原生标注。"""
            if _annot == "dim":
                return emit_dim(a, b, anchor, txt)
            if _annot == "shape":
                return emit_shape(a, b, anchor, txt)
            return False

        def emit_point(x, y, layer):
            """打连接点：POINT 实体放在 CONN_POS / CONN_NEG 层，供后续接线引用。"""
            for c, v in [("0", "POINT"), ("5", nh()), ("330", mspace or "0"),
                         ("100", "AcDbEntity"), ("8", layer), ("100", "AcDbPoint"),
                         ("10", "%.6f" % x), ("20", "%.6f" % y), ("30", "0.0")]:
                content.extend(blk(c, v))

        def emit_poly(pts):
            return emit_poly_c(pts, 1)

        def emit_poly_c(pts, col):
            """跨接线走折线：一条线一个实体，CAD 里看也是一根线。"""
            head = [("0", "LWPOLYLINE"), ("5", nh()), ("330", mspace or "0"),
                    ("100", "AcDbEntity"), ("8", "WIRE"), ("62", str(col)),
                    ("100", "AcDbPolyline"),
                    ("90", str(len(pts))), ("70", "0")]
            for c, v in head:
                content.extend(blk(c, v))
            for x, y in pts:
                content.extend(blk("10", "%.6f" % x))
                content.extend(blk("20", "%.6f" % y))

        def poly_len(pts):
            return sum(math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1])
                       for i in range(len(pts) - 1))

        anchors = []          # 允许当连线端点的“锚点”：阵列端子 + 线束顶端

        n_stub_drawn = 0
        for s in range(n_str):
            for c in cells_by_s.get(s, []):
                P = F(c["P"])
                emit_insert(c["name"], P[0], P[1], k)
                if c["kind"] == "stub":
                    n_stub_drawn += 1
                    # 电机挂在桩上：同一个插入点，按填的旋转角转
                    # （电机相对桩怎么摆，写在电机块自己的基点上，不用在这里调）
                    if c["motor"]:
                        emit_insert_rot(c["motor"], P[0], P[1], k, c["rot"])
                    continue
                pa = (P[0] + c["pos_l"][0] * k, P[1] + c["pos_l"][1] * k)
                na = (P[0] + c["neg_l"][0] * k, P[1] + c["neg_l"][1] * k)
                anchors += [pa, na]
                if draw_pts and c["mi"] == 0:           # 每串开头的正极 -> CONN_POS
                    emit_point(pa[0], pa[1], "CONN_POS")
                if draw_pts and c["mi"] == n_per - 1:   # 每串结束的负极 -> CONN_NEG
                    emit_point(na[0], na[1], "CONN_NEG")
        log.append(("已在每串首块正极打 CONN_POS、末块负极打 CONN_NEG（各 %d 个）" % n_str)
                   if draw_pts else
                   "连接点(POINT)：按你的要求不画（图里不出现点；需要时勾“画连接点”)")
        if n_stub_drawn:
            log.append("BHA 桩/电机: 画了 %d 个槽位" % n_stub_drawn)

        hfinal = []
        for idx, it in enumerate(hinsts):
            hp = L["hp"][idx]
            sc = hp["s"] * k
            yy = hp.get("y0", hp["P"][1] + L["hoff"][1])
            P = FH((hp["P"][0] + L["hoff"][0], yy))    # 线束：分段时逐段下移错开
            emit_insert_rot(it["name"], P[0], P[1], sc, hp.get("rot", 0.0))
            # 块里 CONN-Label 层的点 = 这个块指定的“标注落点”，换算到图纸坐标备用。
            # **必须跟着块一起转**：负极那一行的块是旋转过的（接线头对准板子负极），
            # 以前这里只缩放+平移、没转，于是“负极出线头的标注点”落到了块的另一头
            # （用户反馈的那个问题）。
            _rot = math.radians(float(hp.get("rot", 0.0) or 0.0))
            _ca, _sa = math.cos(_rot), math.sin(_rot)
            labs = [((x * _ca - y * _sa) * sc + P[0], (x * _sa + y * _ca) * sc + P[1])
                    for x, y, ly in _conn_points(bmap.get(it["name"], []), bmap)
                    if "LABEL" in (ly or "").upper()]
            hfinal.append({"name": it["name"], "P": P, "s": sc,
                           "b": cl.bbox(it["prims"]), "labs": labs})

        def hcenter(h):
            return (h["P"][0] + (h["b"][0] + h["b"][1]) / 2.0 * h["s"],
                    h["P"][1] + (h["b"][2] + h["b"][3]) / 2.0 * h["s"])

        def hpt(idx, key, i):
            p = L["hp"][idx][key][i]
            # 线束上的接点：要和线束块用同一套映射（分段时会逐段下移）
            return FH((p[0] + L["hoff"][0], p[1] + L["hoff"][1]))

        def seg(a, b):
            emit_wire(a, b)
            return math.hypot(a[0] - b[0], a[1] - b[1])

        def seg_c(a, b, col):
            emit_wire_c(a, b, col)
            return math.hypot(a[0] - b[0], a[1] - b[1])

        # ---- 串内连线：第 i 块负极 -> 第 i+1 块正极（13.4） ----
        # 板与板之间只画线、不标线号（贴板时那里根本放不下字）
        if sw_in:
            _stub_no_conn = set()
            for s in range(n_str):
                for i in range(n_per - 1):
                    a = mod_pts(i, s)[2]
                    b = mod_pts(i + 1, s)[1]
                    # 这一对板之间可能插了 BHA 桩：从桩的左右接点绕过去（手册 13.5）。
                    # 桩块里没标接点就不打断这条线，直接板连板（线穿过桩的图形）。
                    _mid = stubs_between(s, i)
                    route = [a]
                    for c in _mid:
                        lp = slot_pt(c, c["left_pt"])
                        rp = slot_pt(c, c["right_pt"])
                        if lp and rp:
                            route += [lp, rp]
                        else:
                            _stub_no_conn.add(c["name"])
                    route.append(b)
                    d = 0.0
                    for p, q in zip(route, route[1:]):
                        d += seg(p, q)
                    wires.append(("串%d 第%d块负极-第%d块正极%s"
                                  % (s + 1, i + 1, i + 2,
                                     ("（经 " + "、".join(c["name"] for c in _mid) + "）")
                                     if _mid else ""), awg_br, d))
            if _stub_no_conn:
                log.append("串内连线: 桩块 %s 没有左右接点，这几条线从板直接连到板（穿过桩）"
                           % "、".join(sorted(_stub_no_conn)))
        else:
            log.append("串内不画连线（组件块的图形本身就是贴着的；要画就把 string_wires 打开）")

        # ---- 线束内部连线（复用链算法算出来的配对） ----
        # 线号标在这里：块与块之间的连线上
        n_lab_done = 0
        # 标注排队：[(a, b, cands, 线号, 长度, side)]
        #   a/b    = 这条线的两个接点（挑 CONN-Label 点、放线号文字用）
        #   cands  = 两端块里 CONN-Label 层的点（尺寸界线的左右边界就从这里取）
        #   长度   = 这条线的长度（**长度标注写的就是它**）
        # 一起收齐才好在最后定“所有标注线同一个高度”（用户口径）。
        lab_queue = []

        def label_near(a, b, cands, txt, side=None, length=None, row=0, y_ref=None,
                       text_on=None, length_txt=None):
            """排一条标注：长度标注（水平、同一行统一高度）+ 线号文字（单独贴导线旁）。

            row：0 = 正极那一行，1 = 负极那一行（**按行各自统一高度**，用户口径）。
            y_ref：这条标注属于哪一条行线的高度（算“该行统一高度”的基准）。
            a/b：尺寸界线的两个原点。跨接线要给**真正的出线头**——板子那一头的出线点
                 + 支线块顶端；text_on 是线号文字要贴的那一段（跨接线传折线中间那段，
                 文字才落在导线上）。
            length_txt：直接写死标注文字（比如 fuse 那一段按图纸口径写 “1FT”，
                 不按量出来的长度写）。
            """
            lab_queue.append((a, b, list(cands or []), wire_label(txt),
                              (float(length) if length else None), int(row),
                              (float(y_ref) if y_ref is not None else None),
                              text_on, (str(length_txt) if length_txt else None)))

        for idx in range(1, len(hinsts)):
            if head_blk and (hinsts[idx - 1]["name"] == head_blk or hinsts[idx]["name"] == head_blk):
                continue      # 起始块（CBX）是独立摆在阵列左边的，不和线束链连线
            _a, _b = hinsts[idx - 1]["name"], hinsts[idx]["name"]
            # 线号按给的定义分两种（字面就是界面上填的“主线线号 / 支线线号”）：
            #   主线 = 支线块↔支线块之间、以及第一个接头→第一根支线之间；
            #   支线 = 最后一根支线块→公头/母头之间那一段（这一段两头必有接头块）。
            _txt = awg_br if ((_a in plug_names) or (_b in plug_names)) else awg_main
            _pa = (neg_from is not None and idx - 1 >= neg_from)
            _pb = (neg_from is not None and idx >= neg_from)
            if _pa != _pb:
                continue      # 正极那一行和负极那一行之间不连线
            # 颜色按“是不是负极那一行”判（不能按块名判：同一个块名可能两边都用）
            _neg_row = (neg_from is not None)
            _col = 7 if (_neg_row and (idx >= neg_from or idx - 1 >= neg_from)) else 1
            _is_neg = (_col == 7)          # 这一对块在哪一行：正极行 0 / 负极行 1
            pr = _match_pairs(L["hp"][idx - 1]["outs"], L["hp"][idx]["lins"])
            first = None
            first_len = None
            for (i, j) in pr:
                a = hpt(idx - 1, "outs", i)
                b = hpt(idx, "lins", j)
                d = seg_c(a, b, _col)
                wires.append(("%s - %s" % (hinsts[idx - 1]["name"], hinsts[idx]["name"]),
                              _txt, d))
                if first is None:
                    first = (a, b)
                    first_len = d
            # 一对块只打一个标注（一对块之间常常有 2 根线，逐根打会叠在一起）
            if first:
                # fuse 那一段按图纸口径写 “1FT”（不按一比一量出来的长度写）
                _lt = ("1FT" if (_is_harness and fuse_blk
                                 and (_squash(_a) == _squash(fuse_blk)
                                      or _squash(_b) == _squash(fuse_blk))) else None)
                label_near(first[0], first[1],
                           (hfinal[idx - 1].get("labs") or []) + (hfinal[idx].get("labs") or []),
                           _txt, length=first_len, row=(1 if _is_neg else 0),
                           y_ref=(first[0][1] + first[1][1]) / 2.0, length_txt=_lt)

        # ---- 跨接线（默认不画：正极支线不接板子） ----
        feeds = [h for h in hfinal if h["name"] in pos_names] if pos_names else []
        # 负极那一行 = 链尾自动补出来的那几块。**只取支线/末端接头**：行首那个
        # “出线头”（对齐 CBX 的接头块）不是支线 —— 以前把它算进来，第 1 串的负极
        # 跨接线就接到出线头上去了（用户口径：负极出线头那个标注点取错了）。
        if neg_from is not None:
            nfeeds = [h for h in hfinal[neg_from:] if h["name"] in neg_names]
        else:
            nfeeds = [h for h in hfinal if h["name"] in neg_names]
        # 名单填错（比如“正极支线”这种库里已经改名/不存在的名字）会让支线钉错位，
        # 甚至让公头被当成第一根支线钉到最左边——这里直接说清楚。
        chain_names = [h["name"] for h in hinsts]
        if neg_feed and neg_feed not in chain_names and neg_from is None:
            log.append("⚠ “负极支线块”填的 %s 不在线束链里" % neg_feed)
        if not link:
            log.append("跨接线：按你的要求不画（正极支线不接板子）；需要时打开“阵列↔线束 跨接线”")
        elif harness and pos_feed and not feeds:
            log.append("⚠ 线束里没有 %s，跨接线不画" % pos_feed)
        if link and feeds and len(feeds) < n_str:
            log.append("⚠ 正极支线只有 %d 个、串数 %d：多出来的串不画跨接线" % (len(feeds), n_str))
        if link and pos_feed and not (neg_feed or neg_plug):
            log.append("线束里没填负极支线块/末端母头块，负极跨接线不画")
        y_route = (L["abox"][2] - clear * 0.5) * k + off[1]

        def top_of(h):
            return (h["P"][0] + (h["b"][0] + h["b"][1]) / 2.0 * h["s"],
                    h["P"][1] + h["b"][3] * h["s"])

        for s in range(min(n_str, len(feeds)) if link else 0):
            top = top_of(feeds[s])
            A = mod_pts(0, s)[1]
            anchors.append(top)
            p1, p2 = (A[0], y_route), (top[0], y_route)
            emit_poly_c([A, p1, p2, top], 1)          # 正极跨接线：红
            d = poly_len([A, p1, p2, top])
            # 标注落点：折线中段（和别的线号一样，统一取中点 + 固定偏移）
            lab = feeds[s].get("labs") or []
            # 跨接线 = 板子端子 -> 支线块，是支线那一根，所以标**支线线号**
            # 跨接线（阵列↔线束）单独一行：它自己的导线在 y_route 上，标注贴着它放，
            # 免得跑到正极行那一排里跟块与块之间的标注叠字（用户口径：行内同一高度）。
            # 尺寸界线的原点 = **板子的正极出线头 + 支线块顶端**（不是折线中间那一段），
            # 用户口径：出线头那一头原来取错了点。
            label_near(A, top, lab, awg_br, length=d, row=2,
                       y_ref=(p1[1] + p2[1]) / 2.0, text_on=(p1, p2))
            wires.append(("串%d 正极跨接线 -> %s" % (s + 1, pos_feed), awg_main, d))
            if s >= len(nfeeds):
                continue
            top2 = top_of(nfeeds[s])
            B = mod_pts(n_per - 1, s)[2]
            anchors.append(top2)
            q1, q2 = (B[0], y_route), (top2[0], y_route)
            emit_poly_c([B, q1, q2, top2], 7)         # 负极跨接线：白
            d2 = poly_len([B, q1, q2, top2])
            # 负极跨接线同理：一端是**板子的负极出线头**(B)，另一端是负极支线块顶端
            label_near(B, top2, [], awg_br, side=-1, length=d2, row=2,
                       y_ref=(q1[1] + q2[1]) / 2.0, text_on=(q1, q2))
            wires.append(("串%d 负极跨接线 -> %s" % (s + 1, neg_feed), awg_main, d2))

        # ---- 线号标注：统一落点（所有标注同一个高度、都压在连线中点） ----
        # 以前每条标注各自去挑“块里 CONN-Label 层离中点最近的那个点”，每块不一样 →
        # 同一排标注高度参差不齐；两端界线还会跳到隔壁块的点上。CAD 那边又按最近点
        # 重算一次，结果更飘。现在：落点 = 这条连线的**中点** + 固定的垂直偏移，
        # 尺寸界线的两端 = 这条线自己的两个接点。方向（上/下）和距离由所有
        # CONN-Label 点投票、取中位数 —— 保持图纸原来的高度习惯，但每条一样、
        # 每次跑出来也一样（DXF 和画到 CAD 用的是同一套数）。
        # ---- 标注：长度标注（水平、统一高度、界线=CONN-Label 点）+ 线号文字 ----
        # 用户口径（2026-09-22）：
        #   · 图上标注写的是**长度**（不是线号）；线号另外单独标一条文字；
        #   · 尺寸界线的左右边界点取块里 CONN-Label 层的点；
        #   · 标注在线束上方、所有标注线同一个高度；
        #   · 箭头 3、文字高 7、所有标注线蓝色。
        _dim_th = 7.0            # 标注文字高度（用户指定）
        _dim_asz = 3.0           # 箭头大小（用户指定）
        _dim_col = 5             # 蓝色
        _lab_th = _n("label_h", 4.0)   # 线号文字高度（用户口径：默认 4）

        def _lab_origins(a, b, cands):
            """尺寸界线的两个原点：两端块里离接点最近的 CONN-Label 点。"""
            if not cands:
                return a, b
            ca = min(cands, key=lambda p: (p[0] - a[0]) ** 2 + (p[1] - a[1]) ** 2)
            cb = min(cands, key=lambda p: (p[0] - b[0]) ** 2 + (p[1] - b[1]) ** 2)
            if abs(ca[0] - cb[0]) < 1e-6 and abs(ca[1] - cb[1]) < 1e-6:
                return a, b          # 两端挑到同一个点：退回接线点
            return ca, cb

        # 统一标注高度（用户口径 2026-09-22）：**正极那行的标注一个高度、负极那行一个高度**，
        # 而且贴着各自那条行线（“不要太高的，不遮挡线号就行”——线号就在行线上方一点）。
        _row_ys = {0: [], 1: [], 2: []}
        for (_a, _b, _c, _t, _L, _r, _y, _to, _lt) in lab_queue:
            if _y is not None:
                _row_ys.setdefault(_r, []).append(_y)
        y_lab_of = {}
        for _r, _ys in _row_ys.items():
            if _ys:
                _ys = sorted(_ys)
                # 标注线高度**跟着线号的高度走**（用户口径）：线号中心在线束上方
                # 0.62 个字高处、字高 _lab_th，所以标注线放在“线号上边再抬一点点”。
                y_lab_of[_r] = (_ys[len(_ys) // 2] + _lab_th * 0.62 + _lab_th / 2.0
                                + max(_dim_th * 0.35, 1.0))
        # 某一行没有“基准线”时（比如只有跨接线）：退回所有标注里最低的那条线
        _fallback = min([y for _ys in _row_ys.values() for y in _ys], default=0.0)
        for _r in (0, 1, 2):
            y_lab_of.setdefault(_r, _fallback + _lab_th * 1.2 + max(_dim_th * 0.35, 1.0))

        # 线束各块在图上的包围盒：线号文字“尽可能贴近导线、但不压到块”靠它判断
        _hboxes = []
        for _h in hfinal:
            try:
                _p, _b, _s = _h["P"], _h["b"], _h["s"]
                _hboxes.append((_p[0] + _b[0] * _s, _p[1] + _b[2] * _s,
                                _p[0] + _b[1] * _s, _p[1] + _b[3] * _s))
            except Exception:
                pass

        def _hits_block(x0, y0, x1, y1, margin=0.3):
            for (bx0, by0, bx1, by1) in _hboxes:
                if (x0 < bx1 + margin and x1 > bx0 - margin
                        and y0 < by1 + margin and y1 > by0 - margin):
                    return True
            return False

        def _perp(a, b):
            """连线中点 + 垂直方向（统一朝上为正）。"""
            dx, dy = b[0] - a[0], b[1] - a[1]
            LL = math.hypot(dx, dy)
            if LL < 1e-9:
                return None
            nx, ny = -dy / LL, dx / LL
            if ny < 0 or (abs(ny) < 1e-9 and nx < 0):
                nx, ny = -nx, -ny
            return ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0, nx, ny)

        for (a, b, cands, awg, length, row, _y_ref, text_on, length_txt) in lab_queue:
            o1, o2 = _lab_origins(a, b, cands)
            txt = length_txt if length_txt else (("%.1f" % length) if length else "")
            y_lab = y_lab_of.get(row, y_lab_of[0])
            ents, g = dim_geom_h(o1, o2, y_lab, txt, _dim_th, _dim_asz, _dim_col)
            if g:
                if not (_annot == "dim"
                        and emit_dim_h(o1, o2, y_lab, txt, ents, g, _dim_th)):
                    emit_shape_ents(ents)          # 普通实体版（最稳，箭头也在）
                n_lab_done += 1
            # 线号：单独一条文字，贴在导线上方一点点（不跟长度标注挤在一起）
            if awg:
                pa, pb = text_on if text_on else (a, b)
                pm = _perp(pa, pb)
                if pm:
                    mx, my, nx, ny = pm
                    # 几何中心对在这段线束中点的正上方，**贴到最近但不压线**：
                    # 中心离导线 0.62 个字高 → 文字下边离导线 0.12 个字高（≈0.5）
                    _cx = mx + nx * _lab_th * 0.62
                    _cy = my + ny * _lab_th * 0.62
                    # 万一这段太短、字正好压在旁边的块上：先往上让（但别顶到长度
                    # 标注那条线上），上面实在没位置就放到导线下方 —— 目标就是
                    # “尽可能靠近线束、但不和任何东西重叠”。
                    _w = _lab_th * 0.62 * max(1, len(str(awg)))
                    _y_lab = y_lab_of.get(row, _cy)
                    _cands = [_cy]
                    _up = _cy
                    for _try in range(8):
                        _up += _lab_th * 0.55
                        if _up + _lab_th / 2 > _y_lab - _lab_th * 0.8:
                            break
                        _cands.append(_up)
                    _cands.append(my - _lab_th * 0.62)          # 退路：导线下方
                    _cy = _cands[0]
                    for _c in _cands:
                        if not _hits_block(_cx - _w / 2, _c - _lab_th / 2,
                                           _cx + _w / 2, _c + _lab_th / 2):
                            _cy = _c
                            break
                    emit_label(_cx, _cy, awg, h=_lab_th, center=True)

        # ---- 总长标注（红色）：一条线束从第一个块的 CONN-Label 到最后一个块的
        # CONN-Label，放在线束的**正下方**，离行线的距离和蓝色标注一样（镜像）。
        # 用户口径（2026-09-23）：所有颜色都用红色。
        _n_total = 0
        _rows_tot = []
        if neg_from is not None and 0 < neg_from < len(hfinal):
            _rows_tot.append((0, 0, neg_from))            # 正极行
            _rows_tot.append((1, neg_from, len(hfinal)))  # 负极行
        else:
            _rows_tot.append((0, 0, len(hfinal)))
        for _row, _i0, _i1 in _rows_tot:
            _seg = hfinal[_i0:_i1]
            # 首/末块取**有 CONN-Label 的**那两块：CBX 这类起始块上没有 CONN-Label，
            # 直接拿第一块会取不到点（正极行就是这么被跳过的）。
            _seg = [x for x in _seg if (x.get("labs") or [])]
            if len(_seg) < 2:
                continue
            o1, o2 = _seg[0]["labs"][0], _seg[-1]["labs"][-1]
            if abs(o2[0] - o1[0]) < 1e-6:
                continue
            _ys = _row_ys.get(_row) or []
            if not _ys:
                continue
            _ys = sorted(_ys)
            _y_row = _ys[len(_ys) // 2]
            _off_row = max(y_lab_of.get(_row, _y_row) - _y_row, _lab_th)
            _txt_tot = "%.1f" % math.hypot(o2[0] - o1[0], o2[1] - o1[1])
            _ents, _g = dim_geom_h(o1, o2, _y_row - _off_row, _txt_tot,
                                   _dim_th, _dim_asz, 1)      # 1 = 红色
            if _g:
                emit_shape_ents(_ents)
                _n_total += 1
        if _n_total:
            log.append("总长标注：%d 条（红色，取每一条线束首/末块的 CONN-Label，"
                       "放在线束正下方、离行线距离与蓝色标注一致）" % _n_total)

        if lab_queue:
            log.append("长度标注：%d 个（水平；正极行 y=%.0f、负极行 y=%.0f、跨接线 y=%.0f 各自统一，"
                       "界线=CONN-Label 点；箭头 %.0f、文字高 %.0f、蓝色）；"
                       "线号另标一条文字（%d 条）"
                       % (n_lab_done, y_lab_of[0], y_lab_of[1], y_lab_of.get(2, 0.0),
                          _dim_asz, _dim_th, len([x for x in lab_queue if x[3]])))

        # ---- 插入外框字节 + 自检 ----
        pg(80, "写入 DXF 字节")
        # 先把上一版手工画的内容（HAND_ 层）搬过来：程序画到 COM 端为止，
        # 剩下人接的那几根线，改了参数重新生成也不用重画。
        if keep_from and os.path.exists(keep_from):
            hand, miss = keep_hand_entities(keep_from, keep_prefix, mspace, nh, log)
            for c, v in hand:
                content.extend(blk(c, v))
            miss.discard("")
            if miss:
                log.append("⚠ 手工内容里引用了外框图里没有的块(可能不显示): "
                           + ", ".join(sorted(miss)))
        elif keep_from:
            log.append("⚠ 找不到要保留手工内容的文件: %s" % keep_from)

        # ---- 原生标注：先把标注块定义并进外框，成功了才把 DIMENSION 实体拼进去 ----
        if _n_shape[0]:
            log.append("线号标注: 标注外观（尺寸线/尺寸界线/箭头+文字，全是普通实体）%d 个"
                       % _n_shape[0])
        if dim_jobs:
            _ok, _dlog, _paths = False, [], []
            try:
                for _nm, _fin, _ents in dim_jobs:
                    _p = os.path.join(tempfile.gettempdir(), _nm + ".dxf")
                    _paths.append(dim_block_file(_nm, _ents, _p))
                _base_bad = bp.verify(fb)          # 外框图自带的老毛病不算我们的
                fb2, _info = bp.pack_into(fb, _paths, _dlog)
                # 标注块按 CAD 自己的写法收尾：
                #   块名 *D<号码>；BLOCK 头 70=1（匿名）；
                #   BLOCK_RECORD 保持 70=0 并补 340/280/281（ZWCAD 自己写的 *D 块就是这样）。
                # 名字唯一，直接在这份字节上定点改；匹配不上就保持原样。
                for _nm, _fin, _ in dim_jobs:
                    _nb = _fin.encode("utf-8")
                    fb2 = fb2.replace(_nm.encode("utf-8"), _nb)
                    # BLOCK 头：跟在 70 后面的是 10（基点）→ 只把这一处的 70 改成 1
                    fb2 = re.sub(rb"(2\r\n" + re.escape(_nb) + rb"\r\n70\r\n)0\r\n(10\r\n)",
                                 rb"\g<1>1\r\n\g<2>", fb2)
                    # BLOCK_RECORD：70 后面直接是下一条记录 → 补 340/280/281，70 保持 0
                    fb2 = re.sub(rb"(2\r\n" + re.escape(_nb) + rb"\r\n)70\r\n0\r\n(0\r\n)",
                                 rb"\g<1>340\r\n0\r\n70\r\n0\r\n280\r\n1\r\n281\r\n0\r\n\g<2>",
                                 fb2)
                _pb = [x for x in bp.verify(fb2, [f for _n, f, _e in dim_jobs])
                       if x not in _base_bad]
                if _pb:
                    log.append("⚠ 原生标注块并入后结构检查没过，线号退回文字标注: "
                               + "; ".join(_pb))
                else:
                    fb = fb2
                    _ok = True
            except Exception as _ex:
                log.append("⚠ 原生标注生成失败(%s)，线号退回文字标注" % _ex)
            finally:
                for _p in _paths:
                    try:
                        os.remove(_p)
                    except OSError:
                        pass
            if _ok:
                # 打包进来的块句柄从“外框原最大句柄+1”开始，我们自己画的实体也用了同一段
                # 号段 → 会撞。把我们的实体整体挪到打包之后的空档里，再发标注实体。
                _fib = parse_sections_bytes(fb, "utf-8")[0]
                handle[0] = max_handle(_fib) + 1
                content[:] = renumber_handles(bytes(content), handle)
                for _pairs in dim_ent_pairs:
                    _rec = [("0", "DIMENSION"), ("5", nh()), ("330", mspace or "0")]
                    for _c, _v in _pairs[1:]:             # _pairs[0] 就是 ("0","DIMENSION")
                        _rec.append((_c, _v))
                    for _c, _v in _rec:
                        content.extend(blk(_c, _v))
                log.append("线号标注: CAD 原生线性标注 %d 个（样式 %s，标注点=CONN-Label）"
                           % (len(dim_jobs), _dim_style))
            else:
                for _a, _b, _anchor, _txt in dim_reqs:      # 退回老式 TEXT 标注
                    emit_label(_anchor[0], _anchor[1], _txt)
            log.extend([x for x in _dlog if "跳过" in x or "⚠" in x])

    # 分段收尾：每段只把自己要的块并进了“它那一份”外框字节，而最终输出用的是**最后
    # 一段**那份 —— 前面段落用到的块（最典型的是第 1 段的 CBX 汇流箱）在成品里就没有
    # 块定义，插进去了也画不出来（看着就是“板子最前面的 CBX 丢了”）。这里统一并一次。
    if _nseg > 1:
        _missing_final = [n for n in dict.fromkeys(_need_all)
                          if n and n not in fr_blocks]
        if _missing_final:
            _sec0 = parse_sections_bytes(fb, "utf-8")[0]
            fb, sec, fr_blocks = _pack_missing(fb, _sec0, _need_all, log)
            # 新并进来的块占掉了后面的句柄号段，自己画的实体整体挪到它们之后，免得撞号
            handle[0] = max_handle(sec) + 1
            content[:] = renumber_handles(bytes(content), handle)
    out = _splice_entities(fb, content, handle[0])
    if out is None:
        return None, ["外框图无 ENTITIES 段"], wires
    try:
        sec2, _o2 = parse_sections_bytes(out, "utf-8")
        pts, bad = wires_check(sec2, anchors)
        if bad:
            log.append("⚠ 连线端点检查: %d 个端点没落在接点/锚点上 %s"
                       % (len(bad), ["(%.2f, %.2f)" % b for b in bad[:4]]))
        else:
            log.append("连线端点检查: 全部落在接点/锚点上"
                       "（图上 CONN 点 %d 个，阵列端子/线束顶端锚点 %d 个）"
                       % (len(pts), len(anchors)))
    except Exception as ex:
        log.append("连线端点检查失败: %s" % ex)
    missing = [n for n in ([module] if module else []) + ([m_first, m_mid, m_last] if seq_mode else []) + harness
               + bha_used_names
               if n and n not in fr_blocks]
    if missing:
        log.append("⚠ 外框里没有这些块定义(可能不显示): " + ", ".join(missing))
    # 落地检查：把我们画的东西的实际范围和外框画图区比一下，超框要说出来
    try:
        s2, _o2 = parse_sections_bytes(out, "utf-8")
        ours = set(x for x in ([module] if module else []) +
                   ([m_first, m_mid, m_last] if seq_mode else []) + harness
                   + bha_used_names if x)
        keep = [e for e in group_entities(s2.get("ENTITIES", []))
                if _g1(e, "2") in ours
                or (_g1(e, "8") or "").upper() in ("WIRE", "WIRE_LABEL", "CONN_POS", "CONN_NEG")]
        cb2 = _records_bbox(keep, _blocks_map(s2))
        if cb2 and rect:
            inside = (cb2[0] >= rect[0] - 1e-6 and cb2[1] <= rect[1] + 1e-6
                      and cb2[2] >= rect[2] - 1e-6 and cb2[3] <= rect[3] + 1e-6)
            log.append("内容范围 x %.0f..%.0f  y %.0f..%.0f（画图区 %.0f..%.0f / %.0f..%.0f）：%s"
                       % (cb2[0], cb2[1], cb2[2], cb2[3], rect[0], rect[1], rect[2], rect[3],
                          "在外框内" if inside else "⚠ 超出外框了"))
    except Exception as ex:
        log.append("内容范围检查失败: %s" % ex)
    if stats is not None:
        # 这张图**真正画出去**的顶层块（阵列里的组件/桩/电机 + 线束整条链）。
        # 画到 COM 端时按这份清单回放，就不必再去猜“哪些块是我们画的”。
        _names = list(_stats_names)
        for _c in L["cells"]:
            for _n in (_c.get("name"), _c.get("motor")):
                if _n and _n not in _names:
                    _names.append(_n)
        for _it in hinsts:
            if _it["name"] and _it["name"] not in _names:
                _names.append(_it["name"])
        stats["blocks"] = _names
    return out.decode("latin-1"), log, wires


def _unused_content_bbox(insts, places, scales):
    xs = []; ys = []
    for idx, it in enumerate(insts):
        s = scales[idx]; P = places[idx]["P"]; b = cl.bbox(it["prims"])
        xs += [P[0] + b[0] * s, P[0] + b[1] * s]
        ys += [P[1] + b[2] * s, P[1] + b[3] * s]
    return (min(xs), max(xs), min(ys), max(ys)) if xs else None


def build_chain_raw(chain, gap=40.0, match_span=True, show_len=True, frame=None):
    """返回 (dxf_text, log)。不展平：实体原样搬运。"""
    global _LAST_BLOCKS
    log = []
    insts = []
    for name in chain:
        path = os.path.join(ui.BLOCKS_DIR, name + ".dxf")
        if not os.path.exists(path):
            log.append("缺文件: " + name); continue
        sec, _ord = parse_sections(path)
        ents = group_entities(sec.get("ENTITIES", []))
        prims = cl.flatten(name)
        pts = [(p["x"], p["y"]) for p in ui.capture_points(name)]
        insts.append({"name": name, "path": path, "sec": sec, "ents": ents,
                      "prims": prims, "pts": pts})
    if not insts:
        return None, ["没有可用块"]

    # 缩放：把各块“右侧接点间距”对齐到几何平均(使最大缩放倍数最小)
    scales = [1.0] * len(insts)
    if match_span:
        spans = []
        for it in insts:
            r = cl.side_ports(it["prims"], it["pts"], "right")
            if len(r) >= 2:
                sp = r[0][1] - r[-1][1]
                if sp > 1e-6:
                    spans.append(sp)
        if spans:
            target = math.prod(spans) ** (1.0 / len(spans))
            for k, it in enumerate(insts):
                r = cl.side_ports(it["prims"], it["pts"], "right")
                if len(r) >= 2:
                    sp = r[0][1] - r[-1][1]
                    if sp > 1e-6:
                        scales[k] = target / sp

    def match_pairs(a, b):
        cand = sorted((abs(x[1] - y[1]), i, j)
                      for i, x in enumerate(a) for j, y in enumerate(b))
        pi, pj, res = set(), set(), []
        for d, i, j in cand:
            if i in pi or j in pj:
                continue
            pi.add(i); pj.add(j); res.append((i, j))
        return res

    # 计算插入点(与 build_chain 同算法；接点/包围盒用缩放后的值)
    places = []
    prev_outs = None; prev_right = None
    for idx, it in enumerate(insts):
        s = scales[idx]
        r = [(x * s, y * s) for x, y in cl.side_ports(it["prims"], it["pts"], "right")]
        l = [(x * s, y * s) for x, y in cl.side_ports(it["prims"], it["pts"], "left")]
        b = cl.bbox(it["prims"])
        bl = (b[0] * s, b[1] * s, b[2] * s, b[3] * s)
        if idx == 0:
            P = (0.0, 0.0)
        else:
            pr = match_pairs(prev_outs, l)
            offs = sorted(prev_outs[i][1] - l[j][1] for (i, j) in pr)
            off = offs[len(offs) // 2] if offs else 0.0
            P = (prev_right + gap - bl[0], off)
        outs = [(x + P[0], y + P[1]) for x, y in r]
        lins = [(x + P[0], y + P[1]) for x, y in l]
        places.append({"P": P, "s": s, "outs": outs, "lins": lins,
                       "right": P[0] + bl[1]})
        if idx > 0:
            pr2 = match_pairs(prev_outs, lins)
            n = len(pr2)
            log.append("%s -> %s : 连 %d 条线" % (insts[idx-1]["name"], it["name"], n))
            for (i, j) in pr2:
                a = prev_outs[i]; b2 = lins[j]
                L = ((a[0]-b2[0]) ** 2 + (a[1]-b2[1]) ** 2) ** 0.5
                log.append("  线%d: 长 %.2f" % (i+1, L))
        log.append("放块 %s 于 (%.2f, %.2f) 缩放 x%.4f" % (it["name"], P[0], P[1], s))
        prev_outs = outs; prev_right = places[-1]["right"]

    # === 组装：host = 外框图(若有) 或 第1个块；保留全部段 ===
    use_frame = bool(frame and os.path.exists(frame))
    host_path = frame if use_frame else insts[0]["path"]
    host, order = parse_sections(host_path)
    mspace = model_space_handle(host)
    counter = [max_handle(host) + 1]

    def next_handle():
        v = "%X" % counter[0]
        counter[0] += 1
        return v

    base_ents = group_entities(host.get("ENTITIES", []))
    off = (0.0, 0.0)
    if use_frame:
        cb = _content_bbox(insts, places, scales)
        fb = _records_bbox(base_ents, _blocks_map(host))
        if cb and fb:
            off = ((fb[0] + fb[1]) / 2 - (cb[0] + cb[1]) / 2,
                   (fb[2] + fb[3]) / 2 - (cb[2] + cb[3]) / 2)
        log.append("套用外框图: %s (内容偏移 %.1f, %.1f)" %
                   (os.path.basename(frame), off[0], off[1]))

    content = []
    for idx in range(len(insts)):
        s = scales[idx]; P = places[idx]["P"]
        if (not use_frame) and idx == 0 and abs(s - 1.0) < 1e-9:
            continue     # 实例0 用 base_ents 原样
        tx = P[0] + off[0]; ty = P[1] + off[1]
        for e in insts[idx]["ents"]:
            e2 = scale_entity(e, s) if abs(s - 1.0) > 1e-9 else e
            content.append(clone_entity(e2, tx, ty, next_handle(), mspace))
    ent_out = list(base_ents) + content

    for idx in range(1, len(insts)):
        pr = match_pairs(places[idx-1]["outs"], places[idx]["lins"])
        for (i, j) in pr:
            a = places[idx-1]["outs"][i]; b = places[idx]["lins"][j]
            a = (a[0] + off[0], a[1] + off[1])
            b = (b[0] + off[0], b[1] + off[1])
            ent_out.append([("0", "LINE"), ("5", next_handle()), ("330", mspace or "0"),
                            ("100", "AcDbEntity"), ("8", "WIRE"), ("100", "AcDbLine"),
                            ("10", "%.6f" % a[0]), ("20", "%.6f" % a[1]), ("30", "0"),
                            ("11", "%.6f" % b[0]), ("21", "%.6f" % b[1]), ("31", "0")])
            if show_len:
                L = ((a[0]-b[0]) ** 2 + (a[1]-b[1]) ** 2) ** 0.5
                h = max(4.0, gap * 0.15)
                ent_out.append([("0", "TEXT"), ("5", next_handle()), ("330", mspace or "0"),
                                ("100", "AcDbEntity"), ("8", "TEXT"), ("100", "AcDbText"),
                                ("10", "%.6f" % ((a[0]+b[0])/2)),
                                ("20", "%.6f" % ((a[1]+b[1])/2 + h * 1.3)), ("30", "0"),
                                ("40", "%.4f" % h), ("1", "%.1f" % L), ("50", "0")])

    # 图层 + 合并子块(用外框图时，所有块都算“外部”)
    src = insts if use_frame else insts[1:]
    desired = []
    for it in src:
        for rec in layer_records(it["sec"].get("TABLES", [])):
            nm = layer_name(rec)
            color = "7"
            for c, v in rec:
                if c == "62":
                    color = v
            desired.append((nm, color))
    desired += [("WIRE", "1"), ("TEXT", "7")]
    have = {layer_name(r) for r in layer_records(host.get("TABLES", []))}
    tables = add_layers(list(host.get("TABLES", [])), desired, have, next_handle)
    other_paths = []
    for it in src:
        if it["path"] != host_path and it["path"] not in other_paths:
            other_paths.append(it["path"])
    blocks_body = list(host.get("BLOCKS", []))
    blocks_body, tables = merge_blocks(blocks_body, tables, other_paths,
                                       next_handle, mspace, log)
    header = update_handseed(list(host.get("HEADER", [])), counter[0])
    _LAST_BLOCKS = _blocks_map({"BLOCKS": blocks_body})

    out = ""
    for name in order:
        if name == "HEADER":
            body = header
        elif name == "TABLES":
            body = tables
        elif name == "ENTITIES":
            body = ent_out
        elif name == "BLOCKS":
            body = blocks_body
        else:
            body = host[name]
        out += emit_section(name, body)
    out += "0\nEOF\n"
    return out, log, ent_out


def scale_entity(ent, s):
    """按类型做均匀缩放(正确处理 INSERT/SPLINE/ELLIPSE 等，避免破坏块引用)。"""
    t = ent[0][1] if ent and ent[0][0] == "0" else ""
    xy = ("10", "11", "12", "13", "14", "20", "21", "22", "23", "24")
    if t == "INSERT":
        # INSERT 必须同时缩插入点(10/20)和缩放因子(41/42/43)；
        # 若原本没写 41/42(默认=1)，要显式补 = s，否则块内容不会被放大 -> 错位
        out = []
        has = {"41": False, "42": False, "43": False}
        for c, v in ent:
            if c in xy or c in has:
                try:
                    v = "%.6f" % (float(v) * s)
                except (TypeError, ValueError):
                    pass
                if c in has:
                    has[c] = True
            out.append((c, v))
        add = []
        if not has["41"]:
            add.append(("41", "%.6f" % s))
        if not has["42"]:
            add.append(("42", "%.6f" % s))
        if add:
            pos = len(out)
            for idx, (c, v) in enumerate(out):
                if c == "30":
                    pos = idx + 1
            for idx, (c, v) in enumerate(out):
                if c == "20" and pos == len(out):
                    pos = idx + 1
            out[pos:pos] = add
        return out
    out = []
    for c, v in ent:
        if t in ("TEXT", "MTEXT", "ATTDEF"):
            do = c in xy or c == "40"
        elif t in ("CIRCLE", "ARC"):
            do = c in ("10", "20", "40")
        elif t == "ELLIPSE":
            do = c in ("10", "20", "11", "21")
        elif t == "SPLINE":
            do = c in ("10", "20", "11", "21")
        elif t in ("LWPOLYLINE", "POLYLINE", "VERTEX"):
            do = c in ("10", "20")
        else:
            do = c in xy
        if do:
            try:
                v = "%.6f" % (float(v) * s)
            except (TypeError, ValueError):
                pass
        out.append((c, v))
    return out


def _g1(rec, code):
    for c, v in rec:
        if c == code:
            return v
    return None


def _isnum(v):
    try:
        float(v); return True
    except (TypeError, ValueError):
        return False


def _gf(rec, code, default=0.0):
    v = _g1(rec, code)
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _lw_verts(rec):
    verts = []; x = y = b = None; started = False
    for c, v in rec:
        if c == "10":
            if started and x is not None and y is not None:
                verts.append((x, y, b))
            try: x = float(v)
            except (TypeError, ValueError): x = None
            y = None; b = None; started = True
        elif c == "20":
            try: y = float(v)
            except (TypeError, ValueError): y = None
        elif c == "42":
            try: b = float(v)
            except (TypeError, ValueError): b = None
    if started and x is not None and y is not None:
        verts.append((x, y, b))
    return verts


def _bulge_seg(p1, p2, b, segs=10):
    if not b or abs(b) < 1e-9:
        return [p2]
    theta = 4.0 * math.atan(b)
    dx = p2[0] - p1[0]; dy = p2[1] - p1[1]
    chord = math.hypot(dx, dy)
    if chord < 1e-9:
        return [p2]
    mx = (p1[0] + p2[0]) / 2.0; my = (p1[1] + p2[1]) / 2.0
    t = math.tan(theta / 2.0)
    if abs(t) < 1e-12:
        return [p2]
    d = (chord / 2.0) / t
    nx = -dy / chord; ny = dx / chord
    cx = mx + nx * d; cy = my + ny * d
    r = math.hypot(p1[0] - cx, p1[1] - cy)
    a1 = math.atan2(p1[1] - cy, p1[0] - cx)
    a2 = math.atan2(p2[1] - cy, p2[0] - cx)
    if theta > 0 and a2 < a1: a2 += 2 * math.pi
    if theta < 0 and a2 > a1: a2 -= 2 * math.pi
    out = []
    for k in range(1, segs + 1):
        a = a1 + (a2 - a1) * k / segs
        out.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return out


def _poly_points(verts, closed):
    if not verts:
        return []
    pts = [(verts[0][0], verts[0][1])]
    for i in range(len(verts) - 1):
        pts += _bulge_seg((verts[i][0], verts[i][1]),
                          (verts[i + 1][0], verts[i + 1][1]), verts[i][2])
    if closed and len(verts) > 1:
        pts += _bulge_seg((verts[-1][0], verts[-1][1]),
                          (verts[0][0], verts[0][1]), verts[-1][2])
    return pts


def _de_boor(u, ctrl, p, knots, n):
    k = p
    for i in range(p, n + 1):
        if knots[i] <= u < knots[i + 1]:
            k = i; break
    else:
        k = n
    d = [list(ctrl[j]) for j in range(k - p, k + 1)]
    for rr in range(1, p + 1):
        for j in range(p, rr - 1, -1):
            den = knots[j + 1 + k - rr] - knots[j + k - p]
            a = 0.0 if abs(den) < 1e-12 else (u - knots[j + k - p]) / den
            d[j] = [(1 - a) * d[j - 1][t] + a * d[j][t] for t in (0, 1)]
    return (d[p][0], d[p][1])


def _bspline(ctrl, degree, knots, segs=24):
    n = len(ctrl) - 1
    if n < 1:
        return list(ctrl)
    p = max(1, min(degree, n))
    if len(knots) < n + p + 2:
        knots = [0.0] * (p + 1) + [float(i) for i in range(1, n - p + 1)] + [float(n - p + 1)] * (p + 1)
    u0 = knots[p]; u1 = knots[n + 1]
    if u1 <= u0:
        return list(ctrl)
    return [_de_boor(u0 + (u1 - u0) * s / segs, ctrl, p, knots, n) for s in range(segs + 1)]


def _arc_pts(rec, segs=32):
    cx = _gf(rec, "10"); cy = _gf(rec, "20"); r = _gf(rec, "40")
    a0 = math.radians(_gf(rec, "50")); a1 = math.radians(_gf(rec, "51"))
    if a1 <= a0: a1 += 2 * math.pi
    return [(cx + r * math.cos(a0 + (a1 - a0) * k / segs),
             cy + r * math.sin(a0 + (a1 - a0) * k / segs)) for k in range(segs + 1)]


def _ellipse_pts(rec, segs=64):
    cx = _gf(rec, "10"); cy = _gf(rec, "20")
    mx = _gf(rec, "11"); my = _gf(rec, "21"); ratio = _gf(rec, "40", 1.0)
    a0 = _gf(rec, "41", 0.0); a1 = _gf(rec, "42", 2 * math.pi)
    nx = -my; ny = mx
    if a1 <= a0: a1 += 2 * math.pi
    return [(cx + mx * math.cos(a0 + (a1 - a0) * k / segs) + nx * ratio * math.sin(a0 + (a1 - a0) * k / segs),
             cy + my * math.cos(a0 + (a1 - a0) * k / segs) + ny * ratio * math.sin(a0 + (a1 - a0) * k / segs))
            for k in range(segs + 1)]


def _hatch_points(rec):
    pts = []; x = None
    for c, v in rec:
        if c == "10":
            try: x = float(v)
            except (TypeError, ValueError): x = None
        elif c == "20" and x is not None:
            try: pts.append((x, float(v)))
            except (TypeError, ValueError): pass
            x = None
    return pts


def entities_to_svg(records, width=1000, pad=16):
    """把原始 DXF 实体记录渲染成 SVG 预览（支持 LINE/CIRCLE/ARC/LWPOLYLINE/POLYLINE/SPLINE/ELLIPSE/HATCH/POINT/TEXT）。"""
    prims = []   # ("poly",pts,closed) / ("circle",cx,cy,r)
    i = 0
    while i < len(records):
        rec = records[i]
        t = rec[0][1] if rec and rec[0][0] == "0" else ""
        if t == "LINE":
            prims.append(("poly", [(_gf(rec, "10"), _gf(rec, "20")),
                                   (_gf(rec, "11"), _gf(rec, "21"))], False))
        elif t == "CIRCLE":
            prims.append(("circle", _gf(rec, "10"), _gf(rec, "20"), _gf(rec, "40")))
        elif t == "ARC":
            prims.append(("poly", _arc_pts(rec), False))
        elif t == "LWPOLYLINE":
            v = _lw_verts(rec)
            closed = str(_g1(rec, "70") or "0").strip().endswith("1")
            prims.append(("poly", _poly_points(v, closed), closed))
        elif t == "POLYLINE":
            verts = []; j = i + 1
            while j < len(records) and (records[j][0][1] if records[j] and records[j][0][0] == "0" else "") != "SEQEND":
                if records[j][0][1] == "VERTEX":
                    verts.append((_gf(records[j], "10"), _gf(records[j], "20"), _gf(records[j], "42", 0.0)))
                j += 1
            closed = str(_g1(rec, "70") or "0").strip().endswith("1")
            prims.append(("poly", _poly_points(verts, closed), closed))
            i = j
        elif t == "SPLINE":
            ctrl = []; x = None
            for c, v in rec:
                if c == "10":
                    try: x = float(v)
                    except (TypeError, ValueError): x = None
                elif c == "20" and x is not None:
                    try: ctrl.append((x, float(v)))
                    except (TypeError, ValueError): pass
                    x = None
            deg = int(_gf(rec, "71", 3))
            knots = [float(v) for c, v in rec if c == "40" and _isnum(v)]
            prims.append(("poly", _bspline(ctrl, deg, knots) if len(ctrl) >= 2 else ctrl, False))
        elif t == "ELLIPSE":
            prims.append(("poly", _ellipse_pts(rec), True))
        elif t == "HATCH":
            prims.append(("poly", _hatch_points(rec), False))
        elif t == "POINT":
            x = _gf(rec, "10"); y = _gf(rec, "20")
            prims.append(("poly", [(x - 1, y), (x + 1, y)], False))
            prims.append(("poly", [(x, y - 1), (x, y + 1)], False))
        elif t in ("TEXT", "MTEXT"):
            prims.append(("text", _gf(rec, "10"), _gf(rec, "20"),
                          _g1(rec, "1") or "", _gf(rec, "40", 2.0)))
        i += 1

    xs = []; ys = []
    for p in prims:
        if p[0] == "poly":
            for x, y in p[1]:
                xs.append(x); ys.append(y)
        elif p[0] == "circle":
            xs += [p[1] - p[3], p[1] + p[3]]; ys += [p[2] - p[3], p[2] + p[3]]
        elif p[0] == "text":
            xs.append(p[1]); ys.append(p[2])
    if not xs:
        return "<svg></svg>"
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    sc = (width - 2 * pad) / max(maxx - minx, 1e-6)
    H = int((maxy - miny) * sc) + 2 * pad
    parts = []
    for p in prims:
        if p[0] == "poly":
            pts = " ".join("%.1f,%.1f" % (pad + (x - minx) * sc, H - pad - (y - miny) * sc)
                           for x, y in p[1])
            if pts:
                parts.append('<polyline points="%s" fill="none" stroke="#1c1c1c" stroke-width="1"/>' % pts)
        elif p[0] == "circle":
            parts.append('<circle cx="%.1f" cy="%.1f" r="%.1f" fill="none" stroke="#1c1c1c" stroke-width="1"/>'
                         % (pad + (p[1] - minx) * sc, H - pad - (p[2] - miny) * sc, p[3] * sc))
        elif p[0] == "text":
            sv = str(p[3]).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            if sv:
                parts.append('<text x="%.1f" y="%.1f" font-size="%.1f" fill="#1466c8" '
                             'text-anchor="middle">%s</text>'
                             % (pad + (p[1] - minx) * sc, H - pad - (p[2] - miny) * sc,
                                max(6.0, p[4] * sc), sv))
    return ('<svg viewBox="0 0 %d %d" style="width:100%%;background:#fff;'
            'border:1px solid #e6e8ee;border-radius:10px">' % (width, H)
            + "".join(parts) + "</svg>")


# ---- INSERT 递归解析(预览) ----
_LAST_BLOCKS = {}


def _blocks_map(sec):
    ents = group_entities(sec.get("BLOCKS", []))
    out = {}
    i = 0
    while i < len(ents):
        if ents[i] and ents[i][0][1] == "BLOCK":
            nm = _g1(ents[i], "2")
            members = []
            j = i + 1
            while j < len(ents) and ents[j][0][1] != "ENDBLK":
                members.append(ents[j]); j += 1
            if nm:
                out[nm] = members
            i = j
        i += 1
    return out


def _matmul(P, M):
    a, b, c, d, e, f = P
    A, B, C, D, E, F = M
    return (a * A + c * B, b * A + d * B, a * C + c * D, b * C + d * D,
            a * E + c * F + e, b * E + d * F + f)


def _apply(m, x, y):
    a, b, c, d, e, f = m
    return a * x + c * y + e, b * x + d * y + f


def _tf(pts, m):
    return [_apply(m, x, y) for (x, y) in pts]


def _closed(rec):
    try:
        return (int(_gf(rec, "70", 0)) & 1) == 1
    except (TypeError, ValueError):
        return False


def _prim_list(records, blocks, mtx, depth, out):
    if depth > 8:
        return
    i = 0
    while i < len(records):
        rec = records[i]
        t = rec[0][1] if rec and rec[0][0] == "0" else ""
        # 每条图元都带上“图层 + 实体颜色(62)”。重画到 CAD 时要按原样设回去，
        # 不然线会全挤到 0 层、颜色全丢（“直接画到 CAD”那条路踩过这个坑）。
        lay = _g1(rec, "8") or ""
        try:
            col = int(_gf(rec, "62", 0))
        except (TypeError, ValueError):
            col = 0
        if t == "INSERT":
            nm = _g1(rec, "2")
            px = _gf(rec, "10"); py = _gf(rec, "20")
            sx = _gf(rec, "41", 1.0) or 1.0
            sy = _gf(rec, "42", 1.0) or 1.0
            rr = math.radians(_gf(rec, "50", 0.0))
            T = (1, 0, 0, 1, px, py)
            R = (math.cos(rr), math.sin(rr), -math.sin(rr), math.cos(rr), 0, 0)
            S = (sx, 0, 0, sy, 0, 0)
            child = _matmul(mtx, _matmul(_matmul(T, R), S))
            if nm in blocks:
                _prim_list(blocks[nm], blocks, child, depth + 1, out)
            i += 1
            continue
        if t == "DIMENSION":
            # CAD 原生标注：画它引用的那块（块里就是尺寸线/尺寸界线/箭头/文字）。
            # 预览（界面上的 SVG、导出的 PNG）以前不认 DIMENSION，改完线号标注
            # 之后预览就“少了一块”——这里补上。
            nm = _g1(rec, "2")
            if nm in blocks:
                px = _gf(rec, "12"); py = _gf(rec, "22")      # 块插入点（一般 0,0）
                _prim_list(blocks[nm], blocks, _matmul(mtx, (1, 0, 0, 1, px, py)),
                           depth + 1, out)
            i += 1
            continue
        if t == "LINE":
            out.append(("poly", _tf([(_gf(rec, "10"), _gf(rec, "20")),
                                     (_gf(rec, "11"), _gf(rec, "21"))], mtx), False, lay, col))
        elif t == "CIRCLE":
            cx, cy = _apply(mtx, _gf(rec, "10"), _gf(rec, "20"))
            out.append(("circle", cx, cy, _gf(rec, "40") * math.hypot(mtx[0], mtx[1]), lay, col))
        elif t == "ARC":
            out.append(("poly", _tf(_arc_pts(rec), mtx), False, lay, col))
        elif t == "LWPOLYLINE":
            cl = _closed(rec)
            out.append(("poly", _tf(_poly_points(_lw_verts(rec), cl), mtx), cl, lay, col))
        elif t == "POLYLINE":
            verts = []; j = i + 1
            while j < len(records) and (records[j][0][1] if records[j] and records[j][0][0] == "0" else "") != "SEQEND":
                if records[j][0][1] == "VERTEX":
                    verts.append((_gf(records[j], "10"), _gf(records[j], "20"), _gf(records[j], "42", 0.0)))
                j += 1
            cl = _closed(rec)
            out.append(("poly", _tf(_poly_points(verts, cl), mtx), cl, lay, col))
            i = j
        elif t == "SPLINE":
            ctrl = []; x = None
            for c, v in rec:
                if c == "10":
                    try: x = float(v)
                    except (TypeError, ValueError): x = None
                elif c == "20" and x is not None:
                    try: ctrl.append((x, float(v)))
                    except (TypeError, ValueError): pass
                    x = None
            deg = int(_gf(rec, "71", 3))
            knots = [float(v) for c, v in rec if c == "40" and _isnum(v)]
            pts = _bspline(ctrl, deg, knots) if len(ctrl) >= 2 else ctrl
            out.append(("poly", _tf(pts, mtx), False, lay, col))
        elif t == "ELLIPSE":
            out.append(("poly", _tf(_ellipse_pts(rec), mtx), True, lay, col))
        elif t == "HATCH":
            out.append(("poly", _tf(_hatch_points(rec), mtx), False, lay, col))
        elif t == "MULTILEADER":
            # 多行引线（CBX 块上的 “Combiner Box” 就在它里面）：以前整类没处理，
            # 块里的文字和引线整个丢了（用户反馈的“CBX 块文字丢失”）。
            # 引线顶点画成一条线、文字画成 TEXT；这类引线的坐标常常跑到块外很远，
            # 所以统一挂在 MLEADER 层上，算包围盒时跳过（见 _prims_bbox），
            # 免得把块撑大、把排版带偏。
            verts, txt, tpos, th_m = [], "", None, 0.0
            _pend = None
            for c, v in rec:
                try:
                    if c == "10":
                        _pend = [float(v), None]
                    elif c == "20" and _pend:
                        _pend[1] = float(v)
                        verts.append(_apply(mtx, _pend[0], _pend[1])[:2])
                        _pend = None
                    elif c == "12":
                        tpos = [float(v), None]
                    elif c == "22" and tpos:
                        tpos[1] = float(v)
                    elif c == "304" and v and not str(v).strip().endswith("{"):
                        if len(str(v)) > len(txt):
                            txt = str(v)
                    elif c == "41" and th_m == 0.0:
                        # 41 = 引线文字的“字高”（后面还会出现引线自己的 41，别被它改小）
                        th_m = abs(float(v))
                except (TypeError, ValueError):
                    continue
            base = _prims_bbox(out) or (0.0, 0.0, 0.0, 0.0)
            _lim = max(base[1] - base[0], base[3] - base[2], 1.0) * 3.0
            _cx, _cy = (base[0] + base[1]) / 2.0, (base[2] + base[3]) / 2.0
            keep = [p for p in verts if math.hypot(p[0] - _cx, p[1] - _cy) <= _lim]
            if len(keep) >= 2:
                out.append(("poly", keep, False, "MLEADER", col))
            if txt:
                if tpos is None or tpos[1] is None:
                    tpos = keep[0] if keep else (0.0, 0.0)
                _tx, _ty = _apply(mtx, tpos[0], tpos[1])[:2]
                # 字高别超过块本身的三分之一（引线自带的字高常常是给原图比例写的）
                _bh = max(base[1] - base[0], base[3] - base[2], 1.0)
                out.append(("text", _tx, _ty, txt,
                            max(0.5, min(th_m or _bh * 0.3, _bh * 0.35)),
                            "MLEADER", col))
        elif t == "POINT":
            x, y = _apply(mtx, _gf(rec, "10"), _gf(rec, "20"))
            out.append(("poly", [(x - 1, y), (x + 1, y)], False, lay, col))
            out.append(("poly", [(x, y - 1), (x, y + 1)], False, lay, col))
        elif t in ("TEXT", "MTEXT"):
            x, y = _apply(mtx, _gf(rec, "10"), _gf(rec, "20"))
            out.append(("text", x, y, _g1(rec, "1") or "", _gf(rec, "40", 2.0), lay, col))
        i += 1


def _prim_color(p):
    """取图元的 DXF 颜色号（预览里用来区分不同的块）。"""
    try:
        if p[0] == "text":
            return int(p[6] or 0)
        if p[0] == "circle":
            return int(p[5] or 0)
        return int(p[4] or 0)
    except (IndexError, TypeError, ValueError):
        return 0


_PREV_STROKE = {1: "#c0392b", 2: "#b7791f", 3: "#2e7d32", 4: "#0e8a8a",
                5: "#1466c8", 6: "#8e44ad", 7: "#333333"}


def _svg_from_prims(prims, width=1000, pad=16, min_h=56, min_font=7.0, css_w=None,
                    css_h=None):
    """展平图元 -> SVG（块卡片、生成结果、板子布局预览都用这一套渲染）。"""
    xs = []; ys = []
    for p in prims:
        if p[0] == "poly":
            for x, y in p[1]:
                xs.append(x); ys.append(y)
        elif p[0] == "circle":
            xs += [p[1] - p[3], p[1] + p[3]]; ys += [p[2] - p[3], p[2] + p[3]]
        elif p[0] == "text":
            xs.append(p[1]); ys.append(p[2])
    if not xs:
        return "<svg></svg>"
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    sc = (width - 2 * pad) / max(maxx - minx, 1e-6)
    # 很扁的块（横着的接头、母线之类）按比例画出来只有几个像素高，卡片上看就是
    # 一条糊线 —— 给它一个最小高度，内容垂直居中，卡片里才看得清。
    Hraw = int((maxy - miny) * sc) + 2 * pad
    H = max(Hraw, min_h)
    yoff = (H - Hraw) / 2.0
    def X(x): return pad + (x - minx) * sc
    def Y(y): return yoff + Hraw - pad - (y - miny) * sc
    parts = []
    for p in prims:
        if p[0] == "poly":
            pts = " ".join("%.1f,%.1f" % (X(x), Y(y)) for x, y in p[1])
            if pts:
                # vector-effect=non-scaling-stroke：卡片把 SV-G 缩小时线宽不跟着
                # 缩到 0.1 像素（以前看着又淡又糊，就是缩没了）
                parts.append('<polyline points="%s" fill="none" stroke="%s" '
                             'stroke-width="1.4" vector-effect="non-scaling-stroke" '
                             'stroke-linejoin="round" stroke-linecap="round"/>'
                             % (pts, _PREV_STROKE.get(_prim_color(p), "#1c1c1c")))
        elif p[0] == "circle":
            parts.append('<circle cx="%.1f" cy="%.1f" r="%.1f" fill="none" stroke="%s" '
                         'stroke-width="1.4" vector-effect="non-scaling-stroke"/>'
                         % (X(p[1]), Y(p[2]), max(p[3] * sc, 1.0),
                            _PREV_STROKE.get(_prim_color(p), "#1c1c1c")))
        elif p[0] == "text":
            sv = str(p[3]).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            if sv:
                parts.append('<text x="%.1f" y="%.1f" font-size="%.1f" fill="#1466c8" '
                             'text-anchor="middle" vector-effect="non-scaling-stroke">%s</text>'
                             % (X(p[1]), Y(p[2]), max(min_font, p[4] * sc), sv))
    # 卡片容器给多大就画多大（preserveAspectRatio=meet 保证不变形、不裁切），
    # 线宽用 non-scaling-stroke 保持清晰 —— 预览才和画出来的图对得上。
    if css_h:
        # 固定显示高度、宽度按比例算：整块预览的高度就固定了，
        # 界面不会因为串数/排法不同而忽高忽低（一屏放得下）。
        style = "height:%dpx;width:auto;display:block;margin:0 auto;background:#fff" % int(css_h)
    elif css_w:
        # 画布比容器宽时给个**像素宽**，让外层横向滚动（预览要看序号就得不缩那么狠）
        style = "width:%dpx;height:auto;display:block;background:#fff" % int(css_w)
    else:
        style = "width:100%%;height:100%%;display:block;background:#fff"
    return ('<svg viewBox="0 0 %d %d" preserveAspectRatio="xMidYMid meet" style="%s">'
            % (width, H, style) + "".join(parts) + "</svg>")


def entities_to_svg(records, blocks=None, width=1000, pad=16):
    """把原始 DXF 实体记录渲染成 SVG 预览（解析 INSERT 递归）。"""
    if blocks is None:
        blocks = _LAST_BLOCKS
    prims = []
    _prim_list(records, blocks, (1, 0, 0, 1, 0, 0), 0, prims)
    return _svg_from_prims(prims, width, pad)


if __name__ == "__main__":
    chain = sys.argv[1:] or ["CU - AL", "CU - AL"]     # 欧标模板里自带这个块
    text, log, ents = build_chain_raw(chain, 60, True, True)
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
    os.makedirs(base, exist_ok=True)
    if text:
        p = os.path.join(base, "raw_test.dxf")
        open(p, "w", encoding="latin-1", newline="").write(text)
        print("saved:", p)
    svgp = os.path.join(base, "raw_test.svg")
    open(svgp, "w", encoding="utf-8").write(entities_to_svg(ents))
    print("svg:", svgp)
    for l in log:
        print(l)
