#!/usr/bin/env python3
"""
cad_draw.py -- 把生成好的 DXF 内容“回放”进 CAD（ZWCAD COM 直画）

思路：布局算法和自检**一行都不动**，仍然先出 DXF；然后把这份 DXF 的模型空间内容
按实体逐个用 COM 画进 CAD。坐标是同一套，所以画出来的位置和 DXF 里完全一致。

画图的顺序（用户口径）：**① 用块放位置 → ② 连线 → ③ 打标注**。
块引用（INSERT）先全部放好位置，线/文字/点跟在后面画，线号标注最后统一建。

块：INSERT 引用的块如果在目标图里没有，就在目标图里现造一个同名块定义，再 INSERT 它。
     **块里的嵌套块原样搬结构**（块里 INSERT 的子块也建出来、按插入点/缩放/旋转引用，
     不再把子块摊平成折线）。块图形优先取 DXF 里的定义；DXF 里没有这个块定义时回
     用户的块库（blocklib/blocks/）拿 —— 否则整块会被跳过（分段串数的图里 CBX 踩过）。

实测过的 ZWCAD 2026 差异：InsertBlock 是 7 个参数（多一个 Zscale），AutoCAD 是 5 个；
其余 AddLine/AddCircle/AddArc/AddText/AddPoint/AddLightWeightPolyline/Blocks.Add/Layers.Add
都和 AutoCAD 一致。
"""

import math
import os
import time

import wiring_raw as wr

try:
    import pythoncom
    from win32com.client import VARIANT
    import win32com.client
    HAVE_COM = True
except ImportError:
    HAVE_COM = False

PROGIDS = ("ZWCAD.Application", "ZWCAD.Application.2026", "AutoCAD.Application")

# 我们自己画的内容只在这几个层上；外框图自带的实体在别的层，别把它们再画一遍。
OUR_LAYERS = ("WIRE", "WIRE_LABEL", "CONN_POS", "CONN_NEG")

# 老版本（“线长→线号”那版之前）留下的“长度标注”在这两层上：
#   TEXT = 链式连线那版把 “26.1” 这种长度写在 TEXT 层；
#   DIM  = 后来用 CAD 原生标注那版，标注实体落在 DIM 层（文字覆盖没设上时显示的就是量出来的长度）。
# 这两层一直不在“我们画的”名单里，所以每次重画都清不掉 —— 用户看到的
# “画出来的图还是没有去长度标注”就是这么留下来的。见 clear_old_length_labels。
LEGACY_LABEL_LAYERS = ("TEXT", "DIM")

# 像“长度标注”的文字：纯数字，可以带引号 / in / mm / m
_LEN_TEXT_RE = r"^-?[0-9]+(?:\.[0-9]+)?\s*(?:\"|in|inch|mm|m)?$"


def pt(x, y):
    return VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, (float(x), float(y), 0.0))


def flat2(seq):
    """把 [(x,y), ...] 变成 COM 要的扁平数组。"""
    vals = []
    for x, y in seq:
        vals.append(float(x)); vals.append(float(y))
    return VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, tuple(vals))


def off_xy(x, y, off):
    """DXF 坐标 → 画到 CAD 的落点。

    off 是“连续画图”时这张图要错开的量 (dx, dy)；第一张在原位，off=(0,0)。
    只挪落点：块定义里的图形坐标一个都不动（块是按插入点放的）。
    """
    if not off:
        return (float(x), float(y))
    return (float(x) + float(off[0]), float(y) + float(off[1]))


def connect(visible=True, log=None):
    log = log if log is not None else []
    if not HAVE_COM:
        log.append("⚠ 画到 CAD 需要 pywin32：装一个 pip install pywin32")
        return None
    pythoncom.CoInitialize()
    last = None
    for pid in PROGIDS:
        try:
            app = win32com.client.Dispatch(pid)
            if visible:
                app.Visible = True
            log.append("已连上 CAD: %s（版本 %s）" % (pid, getattr(app, "Version", "?")))
            return app
        except Exception as ex:
            last = ex
    log.append("⚠ 连不上 CAD（ZWCAD 装了没 / 是不是被权限挡住）: %s" % last)
    return None


def find_doc(app, path, open_if_missing=True):
    want = os.path.basename(path).lower()
    try:
        for d in app.Documents:
            if (d.Name or "").lower() == want:
                return d, False
    except Exception:
        pass
    if not open_if_missing:
        return None, False
    return app.Documents.Open(path), True


def insert_block(space, x, y, name, sx=1.0, sy=1.0, rot=0.0):
    """ZWCAD 7 参数、AutoCAD 5 参数，都试一遍。"""
    last = None
    for args in ((pt(x, y), name, sx, sy, 1.0, rot),
                 (pt(x, y), name, sx, sy, 1.0, rot, ""),
                 (pt(x, y), name, sx, sy, rot)):
        try:
            return space.InsertBlock(*args)
        except Exception as ex:
            last = ex
    raise last


def ensure_layer(doc, name):
    if not name:
        return
    try:
        doc.Layers.Item(name)
    except Exception:
        try:
            doc.Layers.Add(name)
        except Exception:
            pass


def has_block(doc, name):
    try:
        doc.Blocks.Item(name)
        return True
    except Exception:
        return False


def _rename_insert(rec, ren):
    """把一条 INSERT 记录里的块名（第一个 2 组码）换成改名后的名字。"""
    if not rec or rec[0][1] != "INSERT":
        return rec
    out, done = [], False
    for c, v in rec:
        if c == "2" and not done:
            out.append((c, ren.get(v, v)))
            done = True
        else:
            out.append((c, v))
    return out


def lib_block_records(name):
    """从**用户的块库**（blocklib/blocks/<块名>.dxf）读一个块的图形。

    返回 (实体记录, 那份文件自己的块表)，拿不到就返回 (None, None)。

    为什么要有这条兜底（用户口径“COM 端接我的块库”）：
    “画到 CAD”以前所有块都从生成的 DXF 里拿定义，DXF 里缺了谁，CAD 里就整块没有。
    实测分段串数（比如 2+3）生成的图里，CBX 只有 INSERT、没有块定义 —— 于是画到
    CAD 时 CBX 被跳过（看着就是“com 端丢块”）。现在 DXF 里没有就回块库拿，块照样
    放到位置上；块库里也没有才会跳过。
    """
    name = str(name or "").strip()
    if (not name or name.startswith("*")
            or "/" in name or "\\" in name):
        return None, None
    path = os.path.join(wr.ui.BLOCKS_DIR, name + ".dxf")
    if not os.path.exists(path):
        return None, None
    try:
        sec, _o = wr.parse_sections_text(wr.read_dxf_text(path))
        bmap = wr._blocks_map(sec)
        ents = [e for e in wr.group_entities(sec.get("ENTITIES", [])) if e]
        # 块库文件的规律（extract_blocks.py 抽出来的）：模型空间只有一个 INSERT，
        # 真正的图形在它自己的**同名块**里。取那份图形，别把这条自引用当内容。
        if (len(ents) == 1 and ents[0][0][1] == "INSERT"
                and (wr._g1(ents[0], "2") or "") == name):
            ents = [e for e in (bmap.get(name) or []) if e]
            # 子块改名成 <块名>$<原名>：和外框图并块（blockpack）一个口径，
            # 免得和外框图自带的同名块（T3 / L1 / 防尘塞…）串味。
            ren = {n: "%s$%s" % (name, n) for n in bmap
                   if not n.startswith("*") and n != name}
            if ren:
                ents = [_rename_insert(e, ren) for e in ents]
                bmap = {ren.get(n, n): [_rename_insert(e, ren) for e in recs]
                        for n, recs in bmap.items() if not n.startswith("*")}
        # 图层/颜色都在记录里，展平时会带出来
        return (ents or None), bmap
    except Exception:
        return None, None


def header_nums(sec, names):
    """从 DXF 表头里取几个系统变量（如 $PDMODE / $PDSIZE）的数值。"""
    want = {n.upper() for n in names}
    out, cur = {}, None
    for c, v in (sec.get("HEADER", []) or []):
        if c == "9":
            cur = str(v).strip().upper()
            continue
        if cur in want:
            try:
                out[cur] = float(v)
            except (TypeError, ValueError):
                pass
            cur = None
    return out


def match_point_style(doc, sec, log=None):
    """把 DXF 表头里的“点怎么显示”抄到 CAD 里。

    DXF 里 $PDMODE/$PDSIZE 决定 POINT 画出来是什么样子；用 COM 画到 CAD 时，
    点显示的是**目标图自己的设置**，两个值不一样时，同一个连接点在 DXF 里是
    一个小点、在 CAD 里却变成十字光标。这里按 DXF 的值对齐，两边看着就一致。
    """
    hv = header_nums(sec, ("$PDMODE", "$PDSIZE", "$PDSTYLE"))
    got = []
    for var, key in (("PDMODE", "$PDMODE"), ("PDSIZE", "$PDSIZE"),
                     ("PDSTYLE", "$PDSTYLE")):
        if key not in hv:
            continue
        try:
            doc.SetVariable(var, hv[key])
            got.append("%s=%g" % (var, hv[key]))
        except Exception:
            pass
    if got and log is not None:
        log.append("连接点(POINT)显示按 DXF 对齐：" + "、".join(got))


def style_obj(o, lay, col):
    """把图元/块引用的图层与颜色设回去。"""
    try:
        if lay:
            o.Layer = lay
    except Exception:
        pass
    try:
        o.color = col if col else 256          # 256 = 随层
    except Exception:
        pass


def draw_prims(space, prims):
    """把展平后的图元画进某个 Block / ModelSpace。返回画了几个。

    图元自带 (图层, 颜色)，画的时候要设回去——否则块里的线全挤到 0 层、颜色全丢。
    """
    n = 0

    for p in prims:
        lay, col = prim_style(p)
        try:
            if p[0] == "poly":
                pts = p[1]
                if len(pts) < 2:
                    continue
                o = space.AddLightWeightPolyline(flat2(pts))
                if p[2]:
                    o.Closed = True
            elif p[0] == "circle":
                o = space.AddCircle(pt(p[1], p[2]), float(p[3]))
            elif p[0] == "text":
                if not str(p[3]).strip():
                    continue
                o = space.AddText(str(p[3]), pt(p[1], p[2]), max(float(p[4]), 0.1))
            else:
                continue
            style_obj(o, lay, col)
            n += 1
        except Exception:
            pass
    return n


def add_solid(space, e, lay_override=None, off=None):
    """画一条 SOLID（标注外观里那两个箭头就是 SOLID）。"""
    _p = lambda c1, c2: pt(*off_xy(wr._gf(e, c1), wr._gf(e, c2), off))
    o = space.AddSolid(_p("10", "20"), _p("11", "21"),
                       _p("12", "22"), _p("13", "23"))
    style_obj(o, lay_override or (wr._g1(e, "8") or ""), int(wr._gf(e, "62", 0)))
    return o


def prim_style(p):
    """取图元的 (图层, 颜色)。

    注意：三种图元的字段位置都不一样 ——
      poly   : (poly, 点表, 闭合, 层, 色)          -> 3/4
      circle : (circle, cx, cy, 半径, 层, 色)      -> 4/5   （p[3] 是半径！）
      text   : (text, x, y, 文字, 字高, 层, 色)    -> 5/6
    取错字段会抛异常，结果就是“块建出来了但是空的”。
    """
    if p[0] == "text":
        return (p[5] if len(p) > 5 else ""), (p[6] if len(p) > 6 else 0)
    if p[0] == "circle":
        return (p[4] if len(p) > 4 else ""), (p[5] if len(p) > 5 else 0)
    return (p[3] if len(p) > 3 else ""), (p[4] if len(p) > 4 else 0)


MAX_NEST = 6           # 块里再套块的最深层数（防自引用/异常文件把递归跑飞）


def blk_fingerprint(recs):
    """一份块定义的内容指纹（用来判断“图里这个块和这次要画的一不一样”）。

    为什么需要它：画到 CAD 慢，慢在**每次都要把几十个块定义清空重画**（Male$4
    这种一个块就 1300 多个图元）。内容和上次一模一样时根本不用重建 —— 存一份
    指纹在 DWG 旁边，指纹没变就跳过。
    """
    try:
        import hashlib
        h = hashlib.sha1()
        for rec in recs or ():
            h.update(repr(rec).encode("utf-8", "replace"))
        return h.hexdigest()[:16]
    except Exception:
        return ""


def block_fp_path(dwg_path):
    return os.path.splitext(os.path.abspath(dwg_path))[0] + ".blocks.json"


def load_block_fp(dwg_path):
    try:
        import json
        with open(block_fp_path(dwg_path), encoding="utf-8") as f:
            st = json.load(f)
        return st if isinstance(st, dict) else {}
    except Exception:
        return {}


def save_block_fp(dwg_path, st):
    try:
        import json
        with open(block_fp_path(dwg_path), "w", encoding="utf-8") as f:
            json.dump(st or {}, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def refill_block(doc, name, recs, bmap, log, depth=0):
    """把一个已经存在的块定义**清空重画**（不删块定义本身）。

    为什么不“删掉重建”：图上已经有这个块的上百个引用，把块定义删除有可能把
    那些引用一起弄空（连续画图时前面几张全靠它们）。把定义里的图元清掉、再按
    这次的 DXF 画一遍，引用一直指着同一个块名，稳；顺带把老版本画进去的
    “点十字”之类的东西一起清掉。

    返回画了几个图元；清不空（老 CAD 不让删）就返回 None，让调用方按老办法来。
    """
    try:
        blk = doc.Blocks.Item(name)
    except Exception:
        return None
    try:
        guard = int(blk.Count) + 50
    except Exception:
        return None
    n_del = 0
    while guard > 0:
        guard -= 1
        try:
            if int(blk.Count) <= 0:
                break
        except Exception:
            break
        try:
            blk.Item(0).Delete()
            n_del += 1
        except Exception:
            break
    try:
        if int(blk.Count) > 0:
            return None                    # 没清干净：别在旧图形上再画一遍
    except Exception:
        return None
    n = draw_block_body(blk, recs, bmap, doc, log, depth, name)
    n += add_dims(blk, doc, dims_in_records(recs), log)
    return n


def draw_block_body(space, recs, bmap, doc, log, depth=0, parent=""):
    """把一份块定义的实体画进 space（可以是模型空间，也可以是块定义）。

    **嵌套块原样搬结构**：块里遇到 INSERT，就先把那个子块也建出来，再用 InsertBlock
    按它的插入点/缩放/旋转引用它 —— 不再把子块摊平成折线。其余图元（线/多段线/圆/
    弧/样条/文字/点…）仍然按原样转成实体画进去，图层和颜色都带回来。

    返回画了几个图元。
    """
    n = 0
    i = 0
    while i < len(recs):
        e = recs[i]
        if not e:
            i += 1
            continue
        t = e[0][1]
        nm = wr._g1(e, "2") or ""
        # 匿名块（*D… 是标注那类块）不建块引用，照旧展平进来，免得建出非法块名
        if t == "INSERT" and nm and not nm.startswith("*"):
            if nm == parent:              # 自己引用自己：CAD 里是非法块，别建
                log.append("⚠ 块 %s 里有对自身的引用，跳过（不然 CAD 会报循环引用）" % nm)
                i += 1
                continue
            # 嵌套子块也和父块一起“清空重画”：老版本把点展平成的十字就藏在
            # 这些子块里（比如 POS$POS / PV-POS$PV-POS），只重画父块的话子块
            # 还是旧图形，十字照样看得见。清空重画不删块定义，引用不受影响。
            ok, _np = ensure_block(doc, nm, bmap, log, refresh=True, depth=depth + 1)
            if not ok:
                i += 1
                continue
            try:
                o = insert_block(space, wr._gf(e, "10"), wr._gf(e, "20"), nm,
                                 wr._gf(e, "41", 1.0) or 1.0,
                                 wr._gf(e, "42", 1.0) or 1.0,
                                 math.radians(wr._gf(e, "50", 0.0)))
                style_obj(o, wr._g1(e, "8") or "", int(wr._gf(e, "62", 0)))
                n += 1
            except Exception as ex:
                log.append("⚠ 块里的嵌套块 %s 画失败: %s" % (nm, ex))
            i += 1
            continue
        # 老式 POLYLINE 的顶点跟在后面的 VERTEX 记录里（末尾 SEQEND），
        # 要连着一起转，不然这条多段线会整条丢掉。
        if t == "POINT":
            # 连接点(POINT)只是**算位置**用的（块库里那些你标的接点）。
            # 以前它跟着 _prim_list 展平，会变成两根交叉的短线 —— CAD 里看到
            # 的“点变成一个十字”就是这么来的。画到 CAD 不需要它，直接跳过；
            # 需要看点的时候在界面上勾“画连接点(POINT)”，那走的是模型空间的
            # POINT 实体（按图里的 $PDMODE 显示，不是十字）。
            i += 1
            continue
        if t == "SOLID":                 # 标注外观的箭头（块里也可能有：*D 标注块）
            try:
                add_solid(space, e)
                n += 1
            except Exception as ex:
                log.append("⚠ 画 SOLID 失败: %s" % ex)
            i += 1
            continue
        chunk = [e]
        if t == "POLYLINE":
            j = i + 1
            while (j < len(recs)
                   and (recs[j][0][1] if recs[j] and recs[j][0][0] == "0" else "") != "SEQEND"):
                j += 1
            chunk = recs[i:j + 1]
            i = j + 1
        else:
            i += 1
        pr = []
        try:
            wr._prim_list(chunk, bmap, (1, 0, 0, 1, 0, 0), 0, pr)
        except Exception:
            pr = []
        n += draw_prims(space, pr)
    return n


def ensure_block(doc, name, bmap, log, refresh=True, depth=0, fp=None,
                 verified=None):
    """目标图里没有这个块，就现造一个（**保留嵌套块结构**）。返回 (是否可用, 画了几个图元)。

    refresh=True（画到 CAD 的默认）：图里已经有同名块时**删掉重建**。
    为什么必须重建：画到 CAD 是“按这次生成的 DXF 重画一遍”，图里那个同名块往往是
    上一次生成留下的、内容已经和这次不一样了。以前这里是“已存在就跳过”，于是
    批量连画几张时，第二张以后的图直接沿用了第一次的块内容 —— 这就是
    “单独画一张正常、连着画几张就不对”的根子。

    块图形优先用 DXF 里的定义（这张图是什么样就画成什么样）；DXF 里没有这个块的
    定义时，回**用户的块库**拿（见 lib_block_records）。

    块里的嵌套块原样搬结构（见 draw_block_body）；depth 是嵌套层数，超过 MAX_NEST
    就当画不了，免得异常文件互相引用把递归跑飞。
    """
    if not name:
        return False, 0
    if depth > MAX_NEST:
        log.append("⚠ 块 %s 嵌套超过 %d 层，按展平跳过" % (name, MAX_NEST))
        return False, 0
    recs = bmap.get(name)
    src = "DXF"
    if not recs:
        recs, _lib_map = lib_block_records(name)
        if recs and _lib_map:
            bmap = _lib_map               # 用块库那份的块表解析它自己的子块
            src = "块库"
    if not recs:
        log.append("⚠ 块 %s 在图里、DXF 里、块库里都没有定义，跳过" % name)
        return False, 0
    # 这个块（含嵌套子块）要用到的图层，先在图上建出来，不然颜色/线型会丢
    lays = set()
    for x in recs:
        if x and (wr._g1(x, "8") or ""):
            lays.add(wr._g1(x, "8"))
    lays.add("MLEADER")     # 多行引线抢救出来的文字/引线挂在这一层（见 wiring_raw._prim_list）
    _my_fp = blk_fingerprint(recs)
    if has_block(doc, name):
        # 内容指纹没变（这张图上一次就是这么画的）→ 什么都不用做，直接跳过。
        # 这是“画到 CAD 很慢”的主因：以前每张图都要把几十个块定义清空重画。
        if fp is not None and _my_fp and fp.get(name) == _my_fp:
            if verified is not None:
                verified.add(name)
            return True, 0
        if refresh and not str(name).startswith("*"):
            # 清空重画（不是删掉重建）：图里已有的引用不会受牵连，内容又保证
            # 和这次的 DXF 一模一样 —— 老版本留下的“点十字”也在这一步被清掉。
            n = refill_block(doc, name, recs, bmap, log, depth)
            if n is not None:
                if fp is not None and _my_fp:
                    fp[name] = _my_fp
                if verified is not None:
                    verified.add(name)
                log.append("块 %s 已存在：按这次的图清空重画了 %d 个图元（引用不断）"
                           % (name, n))
                return True, n
            log.append("⚠ 块 %s 重画不了（里面清不空）：沿用图里已有的那份，"
                       "位置对但内容可能是旧的" % name)
            return True, 0
        else:
            # 不重建时：块已存在就直接用。但如果它是**空的**（上一次画到一半失败
            # 留下的），后面每次都会“因为已存在而跳过”，那个块就永远是空的、
            # INSERT 什么都不显示 —— 所以空块还是要把内容补上。
            try:
                blk = doc.Blocks.Item(name)
                if blk.Count == 0 and recs:
                    for lay in sorted(lays):
                        ensure_layer(doc, lay)
                    n = draw_block_body(blk, recs, bmap, doc, log, depth, name)
                    n += add_dims(blk, doc, dims_in_records(recs), log)
                    log.append("块 %s 已存在但是空的，补画了 %d 个图元（嵌套块原样搬）" % (name, n))
                    if fp is not None and _my_fp:
                        fp[name] = _my_fp
                    if verified is not None:
                        verified.add(name)
                    return True, n
            except Exception as ex:
                log.append("⚠ 检查已有块 %s 失败: %s" % (name, ex))
            return True, 0
    try:
        blk = doc.Blocks.Add(pt(0, 0), name)
    except Exception as ex:
        log.append("⚠ 建块 %s 失败: %s" % (name, ex))
        return False, 0
    for lay in sorted(lays):
        ensure_layer(doc, lay)
    n = draw_block_body(blk, recs, bmap, doc, log, depth, name)
    n += add_dims(blk, doc, dims_in_records(recs), log)   # 块里的线号标注
    log.append("现造块定义 %s（图形来自%s，%d 个图元，嵌套块原样搬）" % (name, src, n))
    if fp is not None and _my_fp:
        fp[name] = _my_fp
    if verified is not None:
        verified.add(name)
    return True, n


def dims_in_records(recs):
    """从一组记录里挑出我们的线号标注（DIMENSION 实体），返回
    [(界线点1, 界线点2, 尺寸线位置, 文字位置, 文字), ...]。

    模型空间（replay）和块定义里（合图时每张图是一个块）都用这一套，
    保证 CAD 里建的标注和 DXF 里写的是同一批点。
    """
    out = []
    for e in recs:
        if not e or e[0] != ("0", "DIMENSION"):
            continue
        if (wr._g1(e, "8") or "").upper() != "WIRE_LABEL":
            continue
        out.append(((wr._gf(e, "13"), wr._gf(e, "23")),
                    (wr._gf(e, "14"), wr._gf(e, "24")),
                    (wr._gf(e, "10"), wr._gf(e, "20")),
                    (wr._gf(e, "11"), wr._gf(e, "21")),
                    (wr._g1(e, "1") or "").strip(),
                    int(wr._gf(e, "62", 0) or 0)))     # 颜色（总长标注是红色 1）
    return out


def add_dims(space, doc, dims, log, off=None):
    """按 (界线点1, 界线点2, 尺寸线位置, 文字位置, 文字) 建 CAD 原生对齐标注。

    space 可以是模型空间，也可以是块定义（合图时每张图是一个块，标注在块里）。
    返回建了几个。文字用的是**线号**（和 DXF 里一致），不是量出来的长度。
    off：连续画图时这张图的错开量（标注落点跟着一起挪）。
    """
    n = 0
    for (_d) in dims:
        o1, o2, dpos, tpos, txt = _d[:5]
        _col = _d[5] if len(_d) > 5 else 5        # 默认蓝色；总长标注传 1 = 红色
        try:
            d = space.AddDimAligned(pt(*off_xy(o1[0], o1[1], off)),
                                    pt(*off_xy(o2[0], o2[1], off)),
                                    pt(*off_xy(dpos[0], dpos[1], off)))
            # 样式：按用户图纸的格式用 Voltage（注释性 1:1、文字样式 Voltage）
            for _st in ("Voltage", "ISO-25", "Standard"):
                try:
                    d.StyleName = _st
                    break
                except Exception:
                    pass
            for _attr, _val in (("TextHeight", 7.0), ("TextGap", 0.1),
                                ("TextStyle", "Voltage"), ("TextColor", _col)):
                try:
                    setattr(d, _attr, _val)
                except Exception:
                    pass
            # 尺寸线、尺寸界线都画成蓝色（用户口径）：整条标注实体设颜色 5
            try:
                d.color = _col if _col else 5
            except Exception:
                pass
            _ok = False
            if txt:
                try:
                    d.TextOverride = txt      # 尺寸线上写线号
                    _ok = True
                except Exception:
                    _ok = False
            if txt and not _ok:               # 改不了文字就单独补一个文字，别丢线号
                try:
                    o = space.AddText(txt, pt(*off_xy(tpos[0], tpos[1], off)), 8.0)
                    o.Layer = "WIRE_LABEL"
                except Exception:
                    pass
            # 箭头大小固定 3（用户口径）—— 以前这里是“小于 3 才兜底改成 6.6”，
            # 结果每次都把箭头放大到 6.6，用户看到的箭头一直没变。
            try:
                d.ArrowheadSize = 3.0
            except Exception:
                pass
            ensure_layer(doc, "DIM")
            try:
                d.Layer = "DIM"
            except Exception:
                pass
            n += 1
        except Exception as ex:
            log.append("⚠ 加线号标注失败: %s" % ex)
    return n


def replay(doc, dxf_path, log, only_blocks=None, only_layers=OUR_LAYERS,
           progress=None, sheet_names=None, refresh_blocks=True, offset=None,
           no_refresh_blocks=(), fresh=None, block_fp=None):
    """把 dxf_path 里“我们生成的那部分”画进 doc。返回统计。

    only_blocks：只回放块名在这里面的 INSERT（外框图自己的块不重画）。
    only_layers：只回放这些层上的线/文字/点。
    refresh_blocks：同名块已存在时删掉重建（DXF 才是准的，见 ensure_block）。

    offset：连续画图时这一张的整体错开量 (dx, dy) —— 只有**落点**挪，块定义不动。
    no_refresh_blocks：这些块**不删重建**（外框图自带的 Frame1 / SLD_NOTES：
      我们只是照着它的位置再放一份，没道理把界面里的那份删掉重造）。
    fresh：可选，一个 set；这次真（重）建过的块名会写进去（收“点十字”时跳过它们）。
    block_fp：可选，{块名: 内容指纹}。指纹和图里一致就不再重建那个块（画到 CAD
      很慢的主因就是这个），画完把新的指纹回填进去。
    """
    def pg(pct, stage):
        if progress:
            try:
                progress(pct, stage)
            except Exception:
                pass

    off = (float(offset[0]), float(offset[1])) if offset else (0.0, 0.0)

    def P(x, y):
        return pt(*off_xy(x, y, off))

    _norefresh = {str(x).upper() for x in (no_refresh_blocks or ())}
    LAY = tuple(x.upper() for x in only_layers) if only_layers else None
    sec, _o = wr.parse_sections_text(wr.read_dxf_text(dxf_path))
    bmap = wr._blocks_map(sec)
    recs = wr.group_entities(sec.get("ENTITIES", []))
    ms = doc.ModelSpace
    stat = {"INSERT": 0, "LINE": 0, "LWPOLYLINE": 0, "TEXT": 0, "POINT": 0,
            "ARC": 0, "CIRCLE": 0, "SOLID": 0, "skip": 0, "blk_prim": 0,
            "DIM": 0, "LEADER": 0}
    # 线号标注：**DXF 里是什么点就用什么点**。
    # 以前这里按“离标注最近的那根线 / 离端点最近的 CONN-Label 点”重新算一遍，
    # 于是同一根线跑两次可能挑到不同块的点、甚至挑到隔壁行/隔壁张的线 —— CAD 里
    # 看着就是标注点乱跳。现在只读 DXF（wiring_raw 已经算好：落点=连线中点+
    # 统一偏移，界线两端=这根线自己的接点），画到 CAD 和 DXF 一字不差。
    our_dims = dims_in_records(recs)        # 注意：必须在下面挑掉 DIMENSION 之前算
    # ---- 画图按用户口径分三步：① 用块放位置 → ② 连线 → ③ 打标注 ----
    # ① 所有 INSERT 排到前面先放位置（块定义第一次用到时现造，见 ensure_block：
    #    DXF 里没有这个块的定义就回块库拿，不会整块丢）；
    # ② 直线/多段线/文字/点跟在后面画；
    # ③ DIMENSION 不在这里画，全部留到最后统一建（函数末尾 add_dims）。
    # 只改**画的顺序**，坐标一律用 DXF 里的，不重算。
    recs_draw = [e for e in recs if e and e[0][1] != "DIMENSION"]
    recs_draw.sort(key=lambda e: 0 if e[0][1] == "INSERT" else 1)
    made = set()
    _sheets = set(sheet_names or [])
    _n_sheet, _t_sheet, _t_all = 0, 0.0, time.time()
    _n_ins = sum(1 for e in recs_draw if e[0][1] == "INSERT")
    pg(94, "① 用块放位置：%d 个块引用 → ② 连线 → ③ 打标注（共 %d 个实体）"
       % (_n_ins, len(recs_draw)))
    _n_done = 0
    for e in recs_draw:
        if not e:
            continue
        t = e[0][1]
        lay = wr._g1(e, "8") or "0"
        if t == "INSERT":
            if only_blocks is not None and (wr._g1(e, "2") or "") not in only_blocks:
                continue
        elif LAY is not None and lay.upper() not in LAY:
            continue
        try:
            if t == "INSERT":
                nm = wr._g1(e, "2") or ""
                _t0 = time.time() if nm in _sheets else 0.0
                if nm not in made:
                    ok, np = ensure_block(doc, nm, bmap, log,
                                          refresh=(refresh_blocks
                                                   and nm.upper() not in _norefresh),
                                          fp=block_fp, verified=fresh)
                    stat["blk_prim"] += np
                    made.add(nm)
                    if not ok:
                        stat["skip"] += 1
                        continue
                # 关键：DXF 里的 50 组码是**角度**，而 COM 的 InsertBlock 要**弧度**。
                # 以前把角度直接当弧度传进去（180 变成 180 弧度 = 10313°），
                # 于是“负极那一行/公头母头”这些带旋转的块在 CAD 里全是乱转的。
                o = insert_block(ms, wr._gf(e, "10") + off[0], wr._gf(e, "20") + off[1], nm,
                                 wr._gf(e, "41", 1.0) or 1.0,
                                 wr._gf(e, "42", 1.0) or 1.0,
                                 math.radians(wr._gf(e, "50", 0.0)))
                o.Layer = lay
                stat["INSERT"] += 1
                if _t0:                       # 这一张画完了：报一下用时
                    _n_sheet += 1
                    _el = time.time() - _t0
                    _t_sheet += _el
                    log.append("画到 CAD：第 %d/%d 张 %s 画完，用时 %.1f 秒"
                               % (_n_sheet, len(_sheets), nm, _el))
                    pg(94 + min(3, _n_sheet), "第 %d/%d 张 %s 画完（%.1f 秒）"
                       % (_n_sheet, len(_sheets), nm, _el))
            elif t == "LINE":
                o = ms.AddLine(P(wr._gf(e, "10"), wr._gf(e, "20")),
                               P(wr._gf(e, "11"), wr._gf(e, "21")))
                o.Layer = lay
                _c = int(wr._gf(e, "62", 0))
                if _c:
                    try:
                        o.color = _c          # 正极红(1) / 负极白(7)，跟 DXF 里一致
                    except Exception:
                        pass
                stat["LINE"] += 1
            elif t == "LWPOLYLINE":
                v = [(a[0] + off[0], a[1] + off[1]) for a in wr._lw_verts(e)]
                if len(v) < 2:
                    continue
                o = ms.AddLightWeightPolyline(flat2(v))
                if (int(wr._gf(e, "70", 0)) & 1) == 1:
                    o.Closed = True
                o.Layer = lay
                _c = int(wr._gf(e, "62", 0))
                if _c:
                    try:
                        o.color = _c
                    except Exception:
                        pass
                stat["LWPOLYLINE"] += 1
            elif t == "POINT":
                o = ms.AddPoint(P(wr._gf(e, "10"), wr._gf(e, "20")))
                o.Layer = lay
                stat["POINT"] += 1
            elif t in ("TEXT", "MTEXT"):
                txt = (wr._g1(e, "1") or "").strip()
                if not txt:
                    continue
                # 对齐方式两边不一样，别混：
                #   TEXT ：72/73 是**对齐方式**，对齐点看 11/21；
                #   MTEXT：71 是附着点（10/20 就是那个点），**11/21 是方向向量不是位置**。
                # 以前把 MTEXT 的 11/21 当位置用，文字被挪到 (1,0) 附近 ——
                # 用户看到的“长度数值丢了”就是这么来的。
                _x, _y = wr._gf(e, "10"), wr._gf(e, "20")
                _align = 0
                if t == "TEXT":
                    _ha = int(wr._gf(e, "72", 0) or 0)
                    _va = int(wr._gf(e, "73", 0) or 0)
                    if _ha or _va:
                        _x, _y = wr._gf(e, "11", _x), wr._gf(e, "21", _y)
                        _align = 10 if (_ha == 1 and _va == 2) else (1 if _ha == 1 else 0)
                else:
                    _att = int(wr._gf(e, "71", 1) or 1)
                    if _att == 5:                  # 正中：长度数值就在标注线正上方
                        _align = 10
                    elif _att in (2, 8):           # 上中 / 下中
                        _align = 1
                o = ms.AddText(txt, P(_x, _y), max(wr._gf(e, "40", 2.5), 0.1))
                if _align:
                    try:
                        o.Alignment = _align       # 10 = 正中，1 = 水平居中
                        o.TextAlignmentPoint = P(_x, _y)
                    except Exception:
                        pass
                o.Layer = lay
                stat["TEXT"] += 1
            elif t == "ARC":
                o = ms.AddArc(P(wr._gf(e, "10"), wr._gf(e, "20")), wr._gf(e, "40"),
                              wr._gf(e, "50"), wr._gf(e, "51"))
                o.Layer = lay
                stat["ARC"] += 1
            elif t == "CIRCLE":
                o = ms.AddCircle(P(wr._gf(e, "10"), wr._gf(e, "20")), wr._gf(e, "40"))
                o.Layer = lay
                stat["CIRCLE"] += 1
            elif t == "SOLID":
                # 标注外观的箭头是 SOLID（以前整类没处理，画到 CAD 就只剩尺寸线没有箭头）
                add_solid(ms, e, lay, off)
                stat["SOLID"] += 1
            else:
                stat["skip"] += 1
        except Exception as ex:
            stat["skip"] += 1
            log.append("⚠ 画 %s 失败: %s" % (t, ex))
        _n_done += 1
        if _n_done == 1 or (_n_done % 20 == 0):
            _step = "① 用块放位置" if _n_done <= _n_ins else "② 连线/文字"
            pg(94 + min(3, _n_done * 3 // max(1, len(recs_draw))),
               "%s %d/%d…" % (_step, _n_done, len(recs_draw)))

    # ---- 线号标注：按 DXF 里的点建 CAD 原生标注（坐标一模一样，不再重算）----
    # 只有当这张图里**真的有点**时才去动 PDMODE/PDSIZE（图里没点就别改用户的设置）
    if any(e[0][1] == "POINT" for e in recs_draw):
        match_point_style(doc, sec, log)
    if our_dims:
        pg(97, "正在加线号标注（%d 个）…" % len(our_dims))
        stat["DIM"] += add_dims(ms, doc, our_dims, log, off)
    if _n_sheet:                      # 合图：把每张的用时汇总一下
        log.append("画到 CAD：%d 张图纸，纯画图用时 %.1f 秒（含标注共 %.1f 秒）"
                   % (_n_sheet, _t_sheet, time.time() - _t_all))
    return stat


def ours_in_model(doc, blocks, layers):
    """模型空间里“我们画的”实体：我们那几层上的东西 + 我们这几个块的 INSERT。"""
    lay = set(x.upper() for x in (layers or ()))
    blk = set(blocks or ())
    ms = doc.ModelSpace
    out = []
    for i in range(ms.Count):
        try:
            e = ms.Item(i)
            nm = e.ObjectName
        except Exception:
            continue
        try:
            if nm == "AcDbBlockReference":
                if not blk or (e.Name in blk):
                    out.append(e)
            elif (e.Layer or "").upper() in lay:
                out.append(e)
        except Exception:
            continue
    return out


def count_ours(doc, blocks, layers):
    """模型空间里还剩几个“我们画的”东西。

    连续画图用它判断“这张图上到底有没有上一轮画的内容”—— 一张干净的图
    （或者用户在 CAD 里把程序画的都删了）就当从头排，别把新图甩到老远。
    注意这里块名单**空集 = 不算我们画的**（clear_ours 那份是“空 = 所有块”，
    两处口径不一样，别混）。
    """
    lay = set(x.upper() for x in (layers or ()))
    blk = set(blocks or ())
    try:
        ms = doc.ModelSpace
        n = 0
        for i in range(ms.Count):
            try:
                e = ms.Item(i)
                nm = e.ObjectName
            except Exception:
                continue
            try:
                if nm == "AcDbBlockReference":
                    if blk and (e.Name in blk):
                        n += 1
                elif (e.Layer or "").upper() in lay:
                    n += 1
            except Exception:
                continue
        return n
    except Exception:
        return 0


def looks_like_length(txt):
    """这条文字像不像老的“长度标注”：纯数字，可以带 " / in / mm / m。"""
    t = str(txt or "").strip()
    if not t:
        return False
    try:
        import re as _re
        return bool(_re.match(_LEN_TEXT_RE, t, _re.IGNORECASE))
    except Exception:
        return False


def clear_old_length_labels(doc, log=None):
    """删掉老版本留在图上的“长度标注”（现在图上只写线号）。

    老版本的两种写法都在这儿收掉：
      1) 链式连线那版：长度是 TEXT 层上的纯数字（"26.1"）；
      2) CAD 原生标注那版：标注实体在 DIM 层，文字覆盖没设上时，CAD 显示的
         就是量出来的长度。
    只认“像长度”的：TEXT/MTEXT 内容是纯数字、或者 DIM 层的标注文字是空的/纯数字；
    带 # 的线号标注、块引用、连线和别层的东西一律不碰。

    返回删了几个。
    """
    kill = []
    try:
        ms = doc.ModelSpace
    except Exception:
        return 0
    for i in range(ms.Count):
        try:
            e = ms.Item(i)
            nm = e.ObjectName or ""
        except Exception:
            continue
        try:
            lay = (e.Layer or "").upper()
        except Exception:
            lay = ""
        try:
            if nm in ("AcDbText", "AcDbMText"):
                # 只认 **TEXT 层**（链式那版把长度写在这一层）。
                # 不能按“纯数字文字”去删我们那几层 —— 现在图上标的长度就是数字，
                # 那样会把这次刚画上去的长度标注一起清掉。
                if lay in ("TEXT",) and looks_like_length(getattr(e, "TextString", "")):
                    kill.append(e)
            elif "Dimension" in nm:
                ov = ""
                for attr in ("TextOverride", "TextString"):
                    try:
                        ov = getattr(e, attr) or ""
                    except Exception:
                        ov = ""
                    if str(ov).strip():
                        break
                # DIM 层上**没写文字**的标注 = 老版本那种“显示量出来长度”的标注；
                # 现在我们的长度标注都带文字（覆盖值），不会被误删。
                if lay in LEGACY_LABEL_LAYERS and not str(ov).strip():
                    kill.append(e)
        except Exception:
            continue
    for e in kill:
        try:
            e.Delete()
        except Exception:
            try:
                e.Erase()
            except Exception:
                pass
    if kill and log is not None:
        log.append("清掉老版本留下的长度标注 %d 个（图上现在只写线号，不写长度）"
                   % len(kill))
    return len(kill)


def _segment_of(ent):
    """把一条 LINE / 两点的 LWPOLYLINE 读成 ((x1,y1),(x2,y2))；读不出返回 None。"""
    try:
        nm = ent.ObjectName or ""
    except Exception:
        return None
    try:
        if nm == "AcDbLine":
            a, b = ent.StartPoint, ent.EndPoint
            return ((float(a[0]), float(a[1])), (float(b[0]), float(b[1])))
        if nm == "AcDbPolyline":
            vals = [float(v) for v in ent.Coordinates]
            if len(vals) == 4:
                return ((vals[0], vals[1]), (vals[2], vals[3]))
    except Exception:
        return None
    return None


def lib_block_name_set(extra=()):
    """我们块库里的块名（大写）。"""
    out = {str(x).upper() for x in (extra or ()) if x}
    try:
        for f in os.listdir(wr.ui.BLOCKS_DIR):
            if f.lower().endswith(".dxf"):
                out.add(os.path.splitext(f)[0].upper())
    except Exception:
        pass
    return out


def purge_point_crosses(doc, ours=None, skip=None, log=None):
    """清掉老版本画进块定义里的“点十字”。

    老版本把块里的连接点(POINT)跟着图元一起展平，一个点变成**两根互相垂直、
    长 2、同心**的短线 —— CAD 里看就是一个个十字（用户：我之前标的点都变成
    十字符号）。新版本已经不画点了（见 draw_block_body），这里负责把**图里
    已经有的**十字收掉：只认这个特征（垂直 + 同心 + 长 2），别的图形一律不动。

    ours：只扫这些块（含它们的子块 <名>$…）；skip：这次刚重建过的块不用扫。
    返回删了几条线。
    """
    names = {str(x).upper() for x in (ours or ())}
    fresh = {str(x).upper() for x in (skip or ())}
    if not names:
        return 0
    try:
        n_blk = int(doc.Blocks.Count)
    except Exception:
        return 0
    killed = 0
    for bi in range(n_blk):
        try:
            blk = doc.Blocks.Item(bi)
            name = str(blk.Name or "")
        except Exception:
            continue
        up = name.upper()
        if not up or up.startswith("*"):
            continue
        if up in fresh:
            continue
        if not (up in names or any(up.startswith(n + "$") for n in names)):
            continue
        try:
            n_ent = int(blk.Count)
        except Exception:
            continue
        segs = []
        for i in range(n_ent):
            try:
                ent = blk.Item(i)
            except Exception:
                continue
            seg = _segment_of(ent)
            if seg is None:
                continue
            (x1, y1), (x2, y2) = seg
            L = math.hypot(x2 - x1, y2 - y1)
            if abs(L - 2.0) > 0.02:                 # 老版本这里正好是 1+1
                continue
            if min(abs(x2 - x1), abs(y2 - y1)) > 0.02:   # 必须是横的或竖的
                continue
            segs.append((ent, ((x1 + x2) / 2.0, (y1 + y2) / 2.0),
                         abs(x2 - x1) > 0.02))
        drop = set()
        for i, (e1, c1, horiz1) in enumerate(segs):
            if i in drop:
                continue
            for j in range(i + 1, len(segs)):
                if j in drop:
                    continue
                e2, c2, horiz2 = segs[j]
                if horiz1 == horiz2:                # 一横一竖才算十字
                    continue
                if abs(c1[0] - c2[0]) > 0.02 or abs(c1[1] - c2[1]) > 0.02:
                    continue
                drop.add(i)
                drop.add(j)
                break
        for idx in sorted(drop):
            try:
                segs[idx][0].Delete()
                killed += 1
            except Exception:
                pass
    if killed and log is not None:
        log.append("清掉块里老版本留下的“点十字” %d 条线（连接点现在不画成十字了）"
                   % killed)
    return killed


def clear_ours(doc, blocks, layers, log=None):
    """删掉上一次程序画的内容：我们那几层上的实体 + 这几个块的 INSERT。

    只碰这两类，手工层(HAND_*)和别人画的东西不动。返回删了几个。
    （连续画图时**不用**它：用户口径是“不要清前一个图”，见 draw_dxf_into_cad
      的 auto_place。）
    """
    try:
        kill = ours_in_model(doc, blocks, layers)
    except Exception:
        kill = []
    for e in kill:
        try:
            e.Delete()
        except Exception:
            pass
    return len(kill)


# ------------------------------------------------------------------ 连续画图落点 ----
# 用户口径：连续画图时**不清**前面那张，都画在同一个 DWG 里，只是位置错开 ——
# 先从上往下排（一列 per_col 张），一列排满了往右挪一列，接着往下排。
# 落点记在 DWG 旁边的 .placed.json 里（同一张图跨几次生成 / 关掉窗口再开也接着排）；
# 图上要是已经没有我们画的东西了（新副本、或者人在 CAD 里删光了），就从头排。

PLACE_GAP = (60.0, 60.0)        # 图与图之间的净空（用户口径：挨近点就行，界面上不再调）
PLACE_PER_COL = 2               # 一列排几张（界面“每行放”那个格）


def place_file_of(dwg_path):
    """这张 DWG 的“落点记忆”文件（和 DWG 放一起，一个图一个）。"""
    return os.path.splitext(os.path.abspath(dwg_path))[0] + ".placed.json"


def load_places(dwg_path):
    """读出已经排过的那几张的落点；读不了就当没排过。"""
    try:
        import json
        with open(place_file_of(dwg_path), encoding="utf-8") as f:
            st = json.load(f)
        if isinstance(st, dict) and isinstance(st.get("sheets"), list):
            return st
    except Exception:
        pass
    return {"sheets": []}


def save_places(dwg_path, st):
    try:
        import json
        with open(place_file_of(dwg_path), "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def dxf_rect_of(dxf_path):
    """这张图生成之后的真实范围 (x0, x1, y0, y1)（含外框图那两个块的引用）。

    和拼图排格子用的是同一把尺子（wiring_raw._records_bbox），这样
    “CAD 里连着画”和“拼成一张图纸”排出来的间距是一个口径。
    """
    try:
        sec, _o = wr.parse_sections_text(wr.read_dxf_text(dxf_path))
        recs = wr.group_entities(sec.get("ENTITIES", []))
        r = wr._records_bbox(recs, wr._blocks_map(sec))
        if r and (r[1] - r[0]) >= 0 and (r[3] - r[2]) >= 0:
            return tuple(float(x) for x in r)
    except Exception:
        pass
    return None


def frame_block_names(dxf_path, ours):
    """DXF 里**不是我们画的**那些块名 = 外框图自带的（Frame1 / SLD_NOTES…）。

    连续画图时第二张起要连外框一起画（不然图在外面没框），这些块不删重建。
    """
    try:
        sec, _o = wr.parse_sections_text(wr.read_dxf_text(dxf_path))
        recs = wr.group_entities(sec.get("ENTITIES", []))
        names = {wr._g1(e, "2") for e in recs
                 if e and e[0][1] == "INSERT" and not (wr._g1(e, "2") or "").startswith("*")}
        return names - set(ours or ())
    except Exception:
        return set()


def plan_place(st, rect, gap=PLACE_GAP, per_col=PLACE_PER_COL):
    """算这一张该错开多少：一列 per_col 张，从上往下；排满了往右一列。

    st：load_places 那份（sheets = [{"off": [dx,dy], "rect": [x0,x1,y0,y1]}, ...]，
        按画的先后）；rect：这张图的真实范围 (x0, x1, y0, y1)。
    返回 ((dx, dy), 挪完之后的 (x0, x1, y0, y1))。
    """
    x0, x1, y0, y1 = rect
    gx = float(gap[0]) if gap else 0.0
    gy = float(gap[1]) if gap else 0.0
    sheets = st.get("sheets") or []
    if not sheets:
        dx = dy = 0.0
    else:
        last = sheets[-1]
        dx_last = float(last["off"][0])
        n_col = 0                       # 最后一列已经排了几张
        for s in reversed(sheets):
            if abs(float(s["off"][0]) - dx_last) > 1e-6:
                break
            n_col += 1
        if n_col < max(1, int(per_col or 1)):
            # 接着这一列往下排：这一张的上边 = 上一张的下边 - 净空
            dx = dx_last
            dy = (float(last["rect"][2]) - gy) - y1
        else:
            # 这一列排满了：往右挪一列，和第一张对齐上边
            dx = max(float(s["rect"][1]) for s in sheets) + gx - x0
            dy = float(sheets[0]["rect"][3]) - y1
    return (dx, dy), (x0 + dx, x1 + dx, y0 + dy, y1 + dy)


def draw_dxf_into_cad(dxf_path, dwg_path, log=None, use_original=False,
                      copy_dir=None, visible=True, only_blocks=None,
                      clear_first=True, progress=None, sheet_names=None,
                      auto_place=False, place_gap=PLACE_GAP,
                      place_per_col=PLACE_PER_COL, no_refresh_blocks=(),
                      only_layers=OUR_LAYERS):
    """把 dxf_path 的内容画进 dwg_path（默认画在副本上，不动原文件）。

    auto_place=True（连续画图，用户口径）：
      **不清**前面已经画的图，接着往同一张 DWG 里画，只是把这一张错开 ----
      一列 per_col 张、从上往下排，一列排满了往右挪一列。
      第一张画在原位（外框图本来就在那儿，所以不重复画外框）；第二张起连
      外框图一起画到新位置，图才是一张完整的图。落点记在 DWG 旁边的
      .placed.json 里，跨几次生成也接着排。
    """
    log = log if log is not None else []
    _t_start = time.time()

    def pg(pct, stage):
        if progress:
            try:
                progress(pct, stage)
            except Exception:
                pass

    if not HAVE_COM:
        log.append("⚠ 画到 CAD 需要 pywin32：pip install pywin32")
        return False
    if not os.path.exists(dwg_path):
        log.append("⚠ 找不到 DWG: %s（画到 CAD 需要 DWG，不能是 DXF）" % dwg_path)
        return False
    pg(86, "正在连接 CAD（ZWCAD / AutoCAD）…（没开的话会自动启动，可能要等十几秒）")
    app = connect(visible=visible, log=log)
    if app is None:
        return False
    pg(88, "已连上 CAD: %s" % getattr(app, "Version", "?"))
    # CAD 正忙着（有命令在跑、或弹了个对话框）时，COM 调用会一直等下去。
    # 先看一眼，忙就先不画，免得界面卡在“生成中…”。
    try:
        active = app.ActiveDocument.GetVariable("CMDACTIVE")
        if int(active) != 0:
            log.append("⚠ ZWCAD 里还有命令在跑（CMDACTIVE=%s）：回到命令提示符、"
                       "把弹窗关掉，再点一次生成。" % active)
            return False
    except Exception:
        pass
    target = os.path.abspath(dwg_path)
    pg(89, "准备目标图：%s" % os.path.basename(dwg_path))
    if not use_original:
        import shutil
        d = copy_dir or os.path.dirname(os.path.abspath(dxf_path))
        os.makedirs(d, exist_ok=True)
        cand = os.path.join(d, "_画到CAD_" + os.path.basename(dwg_path))
        doc0, _ = find_doc(app, cand, open_if_missing=False)
        if doc0 is None:                       # 副本没开着才拷，开着就被 CAD 锁着
            if os.path.exists(cand):
                # 上次那张副本还在（可能已经存过盘）：**接着用**，别拿外框图盖掉它
                # —— 连续画图的口径是“前面的图不清”，重开一次就把它抹了说不过去。
                # 想从头来：把这个文件删掉（或者改个名字），下次会自动重做一份。
                log.append("接着上次那张副本画：%s（要重新来就把它删掉）"
                           % os.path.basename(cand))
            else:
                try:
                    shutil.copyfile(dwg_path, cand)
                except Exception as ex:
                    log.append("⚠ 复制副本失败(%s)，直接画在原文件上" % ex)
                    cand = target
        target = cand
    doc, opened = find_doc(app, target)
    log.append("画到: %s%s" % (doc.Name, "（脚本刚打开的）" if opened else "（本来就开着）"))
    for lay in ("WIRE", "WIRE_LABEL", "CONN_POS", "CONN_NEG", "0"):
        ensure_layer(doc, lay)

    # ---- 先把老版本留下的“长度标注”清掉（TEXT 层的数字、DIM 层的原生标注）----
    # 用户口径：图上只写线号，不写长度。老图上的这两种残留不在“我们那几层”里，
    # 以前每次重画都清不到，所以一直留在图上。放在算落点之前：只剩残留的图
    # 会被当成“空的”，新图画回原位。
    try:
        pg(91, "清掉老版本留下的长度标注…")
    except Exception:
        pass
    clear_old_length_labels(doc, log)

    # ---- 连续画图：算这一张错开到哪儿（不清图）----
    off = (0.0, 0.0)
    draw_frame = False
    if auto_place:
        rect = dxf_rect_of(dxf_path)
        if rect is None:
            log.append("⚠ 量不出这张图的范围，按原位画（可能会和上一张叠在一起）")
            rect = (0.0, 0.0, 0.0, 0.0)
        st = load_places(target)
        if count_ours(doc, only_blocks, OUR_LAYERS) <= 0 and st.get("sheets"):
            # 图上已经没有我们画的东西了（新副本 / 人在 CAD 里删光了）→ 从头排
            st = {"sheets": []}
            log.append("清点了一下：这张图上没有程序画过的内容，落点从头排")
        off, rect2 = plan_place(st, rect, place_gap, place_per_col)
        st["sheets"] = list(st.get("sheets") or []) + [{"off": [off[0], off[1]],
                                                        "rect": list(rect2)}]
        # 只有“画在原位、而且图上本来就是我们第一张”时才不画外框；
        # 错开出去的那些要把外框图一起画上（块定义照用图里已有的，不删重建）。
        draw_frame = len(st["sheets"]) > 1
        save_places(target, st)
        log.append("连续画图：这一张是第 %d 张，%s（不清前面的图）"
                   % (len(st["sheets"]),
                      "画在原位" if not draw_frame else
                      "错开到 (%.0f, %.0f) —— 先从上往下、排满往右一列" % off))
    elif clear_first:
        pg(92, "清掉上次程序画的连线/标注…")
        n = clear_ours(doc, only_blocks, OUR_LAYERS, log)
        if n:
            log.append("先清掉上一次程序画的内容 %d 个（只删 %s 层和这几个块的插入）"
                       % (n, "/".join(OUR_LAYERS)))

    _norefresh = set(no_refresh_blocks or ())
    _blocks = only_blocks
    _layers = only_layers
    if auto_place and draw_frame:
        # 错开出去的这张要连外框图一起画：这时回放 DXF 里的**全部** INSERT
        # （我们的块 + 外框图自带的）+ 外框图自带的那几层东西（图签上的文字、
        # 表格…；US 模板就有），外框图那两个块用图里现成的定义。
        _norefresh |= frame_block_names(dxf_path, only_blocks)
        _blocks = None
        _layers = None
    _fresh = set()
    # 块定义的内容指纹（存在 DWG 旁边）：指纹没变就不重建，画到 CAD 才快得起来
    _block_fp = load_block_fp(target)
    stat = replay(doc, dxf_path, log, only_blocks=_blocks, only_layers=_layers, progress=pg,
                  sheet_names=sheet_names, offset=off,
                  no_refresh_blocks=_norefresh, fresh=_fresh, block_fp=_block_fp)
    save_block_fp(target, _block_fp)
    # 这次（重）建过的块是干净的；剩下那些老块里可能还有“点十字”，收一遍
    purge_point_crosses(doc, ours=lib_block_name_set(only_blocks or ()),
                        skip=_fresh, log=log)
    pg(98, "实体画完，正在加长度标注/线号，并缩放到范围")
    try:
        app.ZoomExtents()
    except Exception:
        pass
    log.append("画进 CAD（①用块放位置 → ②连线 → ③打标注）: "
               "INSERT %d、直线 %d、多段线 %d、文字 %d、点 %d、实心箭头 %d（跳过的 %d），"
               "现造块定义用了 %d 个图元，线号标注 %d 个"
               % (stat["INSERT"], stat["LINE"], stat["LWPOLYLINE"], stat["TEXT"],
                  stat["POINT"], stat["SOLID"], stat["skip"],
                  stat["blk_prim"], stat["DIM"]))
    log.append("画到 CAD 总共用时 %.1f 秒" % (time.time() - _t_start))
    if auto_place:
        log.append("连续画图：前面的图没删，这一张接着它排。想从头来：在 CAD 里把程序画的"
                   "删掉，或者把输出文件夹里那张 _画到CAD_*.dwg 删掉（下次会重做一份）。")
    log.append("没有自动保存，你在 CAD 里看过再决定存不存。")
    pg(100, "画到 CAD 完成（还没保存，你在 CAD 里确认后自己存）")
    return True
