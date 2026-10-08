#!/usr/bin/env python3
"""
connect_library.py -- 用库里的真实块(POS + FUSE)渲染并连线【保留原始图层】

要点：
  - 展平时保留每个实体所在的层(INSERT 内层为 0 的实体继承插入层)
  - 用到的层全部写入 DXF 的 LAYER 表
  - 自动缩放两块，使“接线点”间距一致；两条线为水平直线

输出：connect_2lines.dxf（被占用自动换名）+ connect_2lines.png
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sld_generate import Dxf
import blockui_server as ui

TARGET_SPAN = 12.0
GAP = 30.0


# ------------------------- 保留图层的展平 -------------------------
def collect(ents, blocks, mtx, depth, parent_layer, out):
    """把实体展平成图元，图元末位带 layer。"""
    if depth > 8:
        return
    i = 0
    while i < len(ents):
        e = ents[i]
        t = e["type"]
        layer = ui._g(e, "8") or "0"
        eff = layer if layer != "0" else (parent_layer or "0")

        if t == "INSERT":
            nm = ui._g(e, "2")
            px, py = ui._f(e, "10"), ui._f(e, "20")
            sx = ui._f(e, "41", 1) or 1
            sy = ui._f(e, "42", 1) or 1
            rot = ui._f(e, "50", 0)
            rr = rot * 3.141592653589793 / 180.0
            S = (sx, 0, 0, sy, 0, 0)
            R = (__import__("math").cos(rr), __import__("math").sin(rr),
                 -__import__("math").sin(rr), __import__("math").cos(rr), 0, 0)
            T = (1, 0, 0, 1, px, py)
            child = ui._matmul(mtx, ui._matmul(ui._matmul(T, R), S))
            if nm in blocks:
                collect(blocks[nm], blocks, child, depth + 1, eff, out)
            i += 1
            continue

        if t == "LINE":
            p1 = ui._apply(mtx, ui._f(e, "10"), ui._f(e, "20"))
            p2 = ui._apply(mtx, ui._f(e, "11"), ui._f(e, "21"))
            out.append(("line", p1[0], p1[1], p2[0], p2[1], eff))
        elif t == "CIRCLE":
            c = ui._apply(mtx, ui._f(e, "10"), ui._f(e, "20"))
            r = ui._f(e, "40") * ((mtx[0] ** 2 + mtx[1] ** 2) ** 0.5)
            out.append(("circle", c[0], c[1], r, eff))
        elif t == "ARC":
            c = ui._apply(mtx, ui._f(e, "10"), ui._f(e, "20"))
            r = ui._f(e, "40")
            a0, a1 = ui._f(e, "50"), ui._f(e, "51")
            if a1 < a0:
                a1 += 360
            pts = []
            for k in range(17):
                aa = (a0 + (a1 - a0) * k / 16.0) * 3.141592653589793 / 180.0
                pts.append(ui._apply(ui._matmul(mtx, (1, 0, 0, 1, 0, 0)),
                                     ui._f(e, "10") + r * __import__("math").cos(aa),
                                     ui._f(e, "20") + r * __import__("math").sin(aa)))
            out.append(("poly", pts, False, eff))
        elif t == "LWPOLYLINE":
            pts = []
            curx = None
            for c2, v in e.get("codes", []):
                if c2 == "10":
                    curx = v
                elif c2 == "20" and curx is not None:
                    pts.append(ui._apply(mtx, curx, v))
                    curx = None
            if pts:
                out.append(("poly", pts, True, eff))
        elif t == "POLYLINE":
            pts = []
            j = i + 1
            while j < len(ents) and ents[j]["type"] != "SEQEND":
                if ents[j]["type"] == "VERTEX":
                    pts.append(ui._apply(mtx, ui._f(ents[j], "10"), ui._f(ents[j], "20")))
                j += 1
            if pts:
                out.append(("poly", pts, True, eff))
            i = j
        elif t in ("TEXT", "MTEXT"):
            p = ui._apply(mtx, ui._f(e, "10"), ui._f(e, "20"))
            val = (ui._g(e, "1") or "").replace("\\P", " ").replace("\\p", " ")
            out.append(("text", p[0], p[1], val, eff))
        elif t == "ATTDEF":
            p = ui._apply(mtx, ui._f(e, "10"), ui._f(e, "20"))
            tag = ui._g(e, "3") or ""
            if tag:
                out.append(("text", p[0], p[1], "[" + tag + "]", eff))
        elif t == "ELLIPSE":
            c = ui._apply(mtx, ui._f(e, "10"), ui._f(e, "20"))
            maj, mj = ui._f(e, "11"), ui._f(e, "21")
            rx = (maj ** 2 + mj ** 2) ** 0.5 if maj else 1
            pts = []
            for k in range(25):
                th = 2 * 3.141592653589793 * k / 24.0
                pts.append((c[0] + rx * __import__("math").cos(th),
                            c[1] + rx * __import__("math").sin(th)))
            out.append(("poly", pts, True, eff))
        i += 1


def flatten(block):
    dxf = os.path.join(ui.BLOCKS_DIR, block + ".dxf")
    ents, blocks_def = ui._read_dxf(dxf)
    out = []
    collect(ents, blocks_def, (1, 0, 0, 1, 0, 0), 0, None, out)
    return out


def bbox(prims):
    xs, ys = [], []
    for p in prims:
        # “多行引线抢救出来的图元”挂在 MLEADER 层上（见 wiring_raw._prim_list），
        # 它们的坐标常常在块外很远（CBX 那个块的引线能甩到块外一百多个单位，
        # 而块自己只有 12×15）。算包围盒时跳过它 —— 和 wiring_raw._prims_bbox
        # 一个口径。以前这里算进去了，块的中线被“撑”到引线那头，于是
        # CBX 摆下来和阵列行不在同一高度（用户口径 2026-10-08）。
        if any(isinstance(x, str) and x.strip().upper() == "MLEADER" for x in p[2:]):
            continue
        if p[0] == "line":
            xs += [p[1], p[3]]; ys += [p[2], p[4]]
        elif p[0] == "circle":
            xs += [p[1]-p[3], p[1]+p[3]]; ys += [p[2]-p[3], p[2]+p[3]]
        elif p[0] == "poly":
            for x, y in p[1]:
                xs.append(x); ys.append(y)
        elif p[0] == "text":
            xs.append(p[1]); ys.append(p[2])
    return (min(xs), max(xs), min(ys), max(ys)) if xs else (0, 0, 0, 0)


def scale_prims(prims, s):
    out = []
    for p in prims:
        if p[0] == "line":
            out.append(("line", p[1]*s, p[2]*s, p[3]*s, p[4]*s, p[5]))
        elif p[0] == "circle":
            out.append(("circle", p[1]*s, p[2]*s, p[3]*s, p[4]))
        elif p[0] == "poly":
            out.append(("poly", [(x*s, y*s) for x, y in p[1]], p[2], p[3]))
        elif p[0] == "text":
            out.append(("text", p[1]*s, p[2]*s, p[3], p[4]))
    return out


def move_prims(prims, dx, dy):
    out = []
    for p in prims:
        if p[0] == "line":
            out.append(("line", p[1]+dx, p[2]+dy, p[3]+dx, p[4]+dy, p[5]))
        elif p[0] == "circle":
            out.append(("circle", p[1]+dx, p[2]+dy, p[3], p[4]))
        elif p[0] == "poly":
            out.append(("poly", [(x+dx, y+dy) for x, y in p[1]], p[2], p[3]))
        elif p[0] == "text":
            out.append(("text", p[1]+dx, p[2]+dy, p[3], p[4]))
    return out


def side_ports(prims, pts, side):
    """取一侧的接点：按 x 把接点归成“竖列”，left = 最左那一列，right = 最右那一列。

    以前是按几何中位数左右切：块中间要是还有别的接点（比如 POS 顶上的 CONNPOS、
    或者标签点），就会被算进某一侧，配对跟着错。按竖列取就稳。
    只有一列（比如公头两个点在同一条竖线上）时，左右都返回这一列。
    """
    if not pts:
        return []
    cols = []
    for p in sorted(pts, key=lambda q: q[0]):
        if cols and abs(p[0] - cols[-1][-1][0]) <= max(0.5, abs(p[0]) * 0.02):
            cols[-1].append(p)
        else:
            cols.append([p])
    sel = list(pts) if len(cols) == 1 else (cols[0] if side == "left" else cols[-1])
    return sorted(sel, key=lambda p: (-p[1], p[0]))


def emit(dxf, prims):
    for p in prims:
        if p[0] == "line":
            dxf.line(p[1], p[2], p[3], p[4], p[5])
        elif p[0] == "circle":
            dxf.circle(p[1], p[2], p[3], p[4])
        elif p[0] == "poly":
            pts, layer = p[1], p[3]
            for i in range(len(pts) - 1):
                dxf.line(pts[i][0], pts[i][1], pts[i+1][0], pts[i+1][1], layer)
        elif p[0] == "text":
            if p[3]:
                dxf.text(p[1], p[2], p[3], 1.6, p[4])


def cross(dxf, x, y, r=1.2, layer="CONN"):
    dxf.line(x - r, y, x + r, y, layer)
    dxf.line(x, y - r, x, y + r, layer)


def write_dxf(dxf, base):
    content = dxf.build({"title": "FUSE + POS"})
    cand = base; i = 0
    while True:
        try:
            with open(cand, "w", encoding="utf-8", newline="") as f:
                f.write(content)
            return cand
        except PermissionError:
            i += 1
            root, ext = os.path.splitext(base)
            cand = "%s_%d%s" % (root, i, ext)


def main():
    fuse = flatten("FUSE")
    zj = flatten("POS")
    fc = [(p["x"], p["y"]) for p in ui.capture_points("FUSE")]
    zc = [(p["x"], p["y"]) for p in ui.capture_points("POS")]
    print("几何: FUSE=%d POS=%d | CONN: FUSE=%d POS=%d"
          % (len(fuse), len(zj), len(fc), len(zc)))
    print("POS 图层数:", len(set(p[-1] for p in zj)))

    fr = side_ports(fuse, fc, "right")
    zl = side_ports(zj, zc, "left")
    span_f = fr[0][1] - fr[-1][1]
    span_z = zl[0][1] - zl[-1][1]
    s_f = TARGET_SPAN / span_f
    s_z = TARGET_SPAN / span_z
    print("缩放: FUSE x%.3f  POS x%.3f" % (s_f, s_z))

    fuse2 = scale_prims(fuse, s_f); zj2 = scale_prims(zj, s_z)
    F = [(x*s_f, y*s_f) for x, y in fc]; Z = [(x*s_z, y*s_z) for x, y in zc]
    fr2 = [(x*s_f, y*s_f) for x, y in fr]; zl2 = [(x*s_z, y*s_z) for x, y in zl]

    # === 解块插入位置 ===
    # 图纸坐标 = 插入点 + 局部坐标；让 B 的接点落在 A 接点右侧 GAP 处(同高度)
    ref_out = fr2[0]     # FUSE 右上接点(缩放后局部坐标)
    ref_in = zl2[0]      # POS 左上接点(缩放后局部坐标)
    P_fuse = (0.0, 0.0)
    P_zj = (ref_out[0] + GAP - ref_in[0], ref_out[1] - ref_in[1])
    print("块插入位置: FUSE=%s  POS=(%.3f, %.3f)" % (P_fuse, P_zj[0], P_zj[1]))
    Fo = P_fuse
    Zo = P_zj

    fuse3 = move_prims(fuse2, *Fo); F = [(x+Fo[0], y+Fo[1]) for x, y in F]
    zj3 = move_prims(zj2, *Zo); Z = [(x+Zo[0], y+Zo[1]) for x, y in Z]
    fr3 = [(x+Fo[0], y+Fo[1]) for x, y in fr2]
    zl3 = [(x+Zo[0], y+Zo[1]) for x, y in zl2]

    dxf = Dxf()
    emit(dxf, fuse3)
    emit(dxf, zj3)
    for (x, y) in F:
        cross(dxf, x, y)
    for (x, y) in Z:
        cross(dxf, x, y)

    n = min(2, len(fr3), len(zl3))
    for i in range(n):
        a = fr3[i]; b = zl3[i]; y = (a[1] + b[1]) / 2.0
        dxf.line(a[0], y, b[0], y, "WIRE")

    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "connect_2lines.dxf")
    out = write_dxf(dxf, base)
    print("saved:", out, "| entities:", len(dxf.ents), "| 图层:", sorted(dxf._used_layers()))

    try:
        from PIL import Image, ImageDraw
        minx, maxx, miny, maxy = dxf.minx, dxf.maxx, dxf.miny, dxf.maxy
        W = 1100
        sc = (W - 40) / max(maxx - minx, 1e-6)
        H = int((maxy - miny) * sc) + 40
        img = Image.new("RGB", (W, H), "white"); d = ImageDraw.Draw(img)
        def P(x, y):
            return (20 + (x - minx) * sc, H - 20 - (y - miny) * sc)
        for e in dxf.ents:
            if e[1] == "LINE":
                d.line([P(float(e[5]), float(e[7])), P(float(e[11]), float(e[13]))],
                       fill="black", width=1)
            elif e[1] == "CIRCLE":
                cx, cy, r = float(e[5]), float(e[7]), float(e[11])
                x0, y0 = P(cx - r, cy - r); x1, y1 = P(cx + r, cy + r)
                d.ellipse([min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)],
                          outline="black")
        for i in range(n):
            a = fr3[i]; b = zl3[i]; y = (a[1] + b[1]) / 2.0
            d.line([P(a[0], y), P(b[0], y)], fill="blue", width=3)
        outp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "connect_2lines.png")
        img.save(outp); print("preview:", outp)
    except Exception as e:
        print("PNG skipped:", e)


if __name__ == "__main__":
    main()

