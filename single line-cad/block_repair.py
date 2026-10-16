#!/usr/bin/env python3
"""block_repair.py -- 块库体检/整理：把 blocklib/blocks/*.dxf 修成 CAD 能直接打开的文件。

为什么需要它（2026-10-09 用户反馈“块库里的图像全部打开无效”）：
  块库里的 .dxf 是从大图上抠出来的，**表没跟着清** —— 每个文件里都留着源图那
  两千多条 BLOCK_RECORD（T3 / L1 / Frame1 / 防尘塞…），可文件里根本没有这些块的
  定义；NEG 少了自己的块记录、CU-AI 连块定义都没有。CAD 打开这种“记录对不上定义”
  的文件，直接判“无效或不完整的 DXF 输入 —— 图形被放弃”。

这个脚本做的事：
  1. 把原文件裁成“空壳”（HEADER + CLASSES + 除 BLOCK_RECORD 外的所有表 +
     只剩 *Model_Space / *Paper_Space 的块表与块定义 + 空 ENTITIES）；
  2. 用 blockpack.pack_into 把**这一块**（含它嵌套的子块）重新并进去 ——
     句柄重排、块记录按需重建、$HANDSEED 同步，全走已经在用的那套逻辑；
  3. 模型空间补一条本块的 INSERT（在 CAD 里打开就能看到这个符号）;
  4. 自检：结构 verify() 通过 + **展平出来的图元和原文件逐字节一致**（图形不动）。

用法:
    python block_repair.py --dry-run      # 只看会改什么，不写文件
    python block_repair.py                # 备份原文件后原地修
"""

import argparse
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import blockpack as bp          # noqa: E402
import wiring_raw as wr         # noqa: E402

KEEP_BLOCKS = (b"*MODEL_SPACE", b"*PAPER_SPACE")


def _raw(data, gl, lo, hi):
    """把 gl[lo:hi] 这些组的原始字节拼起来（不解析、不转码 → 图形一个字节不动）。"""
    if lo >= hi:
        return b""
    return data[gl[lo][0]:gl[hi - 1][3]]


def _sect_span(gl, name):
    """整段（含 SECTION/ENDSEC）的组下标范围，找不到返回 None。"""
    r = bp.section_range(gl, name)
    if r is None:
        return None
    lo, hi = r
    return lo - 2, hi + 1          # +1 把 ENDSEC 那条也带上


def _table_span(gl, name):
    r = bp.table_range(gl, name)
    if r is None:
        return None
    return r                                  # (TABLE 那条组的下标, 记录起, 记录止)


def clean_shell(data):
    """把源文件裁成空壳；返回 (字节, *Model_Space 的块记录句柄)。"""
    gl = list(bp.iter_groups(data))
    out = bytearray()

    head = _sect_span(gl, b"HEADER")
    if head:
        out += _raw(data, gl, *head)
    cls = _sect_span(gl, b"CLASSES")
    if cls:
        out += _raw(data, gl, *cls)

    # ---- TABLES：除了 BLOCK_RECORD 表，其它表原样搬 ----
    tr = _sect_span(gl, b"TABLES")
    if tr is None:
        raise ValueError("没有 TABLES 段")
    tlo, thi = tr
    body_lo, body_hi = tlo + 2, thi - 1       # 去掉 SECTION/2 与 0/ENDSEC
    # TABLES 段里逐张表处理
    ms_handle = None
    keep_br_recs, br_table = [], None
    i = body_lo
    segs = []
    while i < body_hi:
        if gl[i][4] == b"0" and gl[i][5] == b"TABLE":
            j = i + 1
            while j < body_hi and not (gl[j][4] == b"0" and gl[j][5] == b"ENDTAB"):
                j += 1
            tname = None
            for k in range(i, j):
                if gl[k][4] == b"2":
                    tname = gl[k][5]
                    break
            if tname == b"BLOCK_RECORD":
                # table_range 给的是 (表头第一组, 第一条记录, ENDTAB, 表句柄, 70组)
                tr5 = bp.table_range(gl, b"BLOCK_RECORD")
                br_table = (i, j, tr5[0], tr5[1])        # TABLE 记录 / ENDTAB / 表头 / 记录起
                for rec in bp.split_records(gl, i + 1, j):
                    nm = bp.rec_get(rec, b"2")
                    if nm and nm.upper() in KEEP_BLOCKS:
                        keep_br_recs.append(rec)
                        if nm.upper() == b"*MODEL_SPACE":
                            h = bp.rec_get(rec, b"5")
                            ms_handle = h
            else:
                segs.append((i, j + 1))                 # 含 ENDTAB
            i = j + 1
        else:
            i += 1

    out += b"  0\nSECTION\n  2\nTABLES\n"           # 自己拼头，末尾接 ENDSEC
    for lo, hi in segs:
        out += _raw(data, gl, lo, hi)
    if br_table is None:
        raise ValueError("没有 BLOCK_RECORD 表")
    lo, hi, hdr_lo, rec_lo = br_table
    out += _raw(data, gl, lo, hdr_lo)               # 0 TABLE + 2 BLOCK_RECORD
    out += _raw(data, gl, hdr_lo, rec_lo)           # 表头剩下的组（5/330/100/70）
    for rec in keep_br_recs:
        st = rec[0][0]
        en = rec[-1][3]
        out += data[st:en]
    out += _raw(data, gl, hi, hi + 1)               # 0 ENDTAB 那一条
    out += b"  0\nENDSEC\n"

    # ---- BLOCKS：只留 *Model_Space / *Paper_Space ----
    br = _sect_span(gl, b"BLOCKS")
    if br is None:
        raise ValueError("没有 BLOCKS 段")
    blo, bhi = br
    out += b"  0\nSECTION\n  2\nBLOCKS\n"
    idx = blo + 2
    while idx < bhi - 1:
        if gl[idx][4] == b"0" and gl[idx][5] == b"BLOCK":
            j = idx + 1
            while j < bhi - 1 and not (gl[j][4] == b"0" and gl[j][5] == b"ENDBLK"):
                j += 1
            nm = None
            for k in range(idx, j):
                if gl[k][4] == b"2":
                    nm = gl[k][5]
                    break
            if nm and nm.upper() in KEEP_BLOCKS:
                out += data[gl[idx][0]:gl[j][3]]
            idx = j + 1
        else:
            idx += 1
    out += b"  0\nENDSEC\n"
    out += b"  0\nSECTION\n  2\nENTITIES\n  0\nENDSEC\n"
    out += b"  0\nEOF\n"
    return bytes(out), ms_handle


def add_insert(data, block_name, ms_handle):
    """往 ENTITIES 里补一条本块的 INSERT；句柄重新分配，$HANDSEED 同步。"""
    gl = list(bp.iter_groups(data))
    mx = bp._max_handle(gl)
    try:                                  # *Model_Space 的句柄可能比最大句柄还大
        mx = max(mx, int(ms_handle, 16))
    except (TypeError, ValueError):
        pass
    new_h = b"%X" % (mx + 0x20)      # 避开打包时可能用掉的固定句柄
    ent = bp.section_range(gl, b"ENTITIES")
    if ent is None:
        raise ValueError("没有 ENTITIES 段")
    lo, hi = ent
    ins = (b"  0\nINSERT\n  5\n" + new_h + b"\n330\n" + (ms_handle or b"1F") +
           b"\n100\nAcDbEntity\n  8\n0\n100\nAcDbBlockReference\n  2\n" +
           block_name.encode("utf-8") + b"\n 10\n0.0\n 20\n0.0\n 30\n0.0\n")
    at = gl[hi][0]                      # ENTITIES 的 0 ENDSEC 起点
    out = data[:at] + ins + data[at:]
    # $HANDSEED 调大
    gl2 = list(bp.iter_groups(out))
    seed = b"%X" % (mx + 0x21)
    for i in range(len(gl2) - 1):
        if gl2[i][4] == b"9" and gl2[i][5] == b"$HANDSEED":
            g = gl2[i + 1]
            out = out[:g[0]] + b"  1\n" + seed + b"\n" + out[g[3]:]
            break
    return out


def flatten_prims(path_or_bytes):
    """按程序自己那套解析展平出图元（用来比对“图形有没有动”）。"""
    if isinstance(path_or_bytes, bytes):
        text = path_or_bytes.decode("latin-1")
    else:
        text = wr.read_dxf_text(path_or_bytes)
    sec, _o = wr.parse_sections_text(text)
    bmap = wr._blocks_map(sec)
    recs = [e for e in wr.group_entities(sec.get("ENTITIES", [])) if e]
    out = []
    wr._prim_list(recs, bmap, (1, 0, 0, 1, 0, 0), 0, out)
    return out


def repair_one(path, dry=False, backup_dir=None):
    name = os.path.splitext(os.path.basename(path))[0]
    data = open(path, "rb").read()
    gl0 = list(bp.iter_groups(data))
    t0 = _table_span(gl0, b"BLOCK_RECORD")
    n_before = len(bp.split_records(gl0, t0[1], t0[2])) if t0 else 0
    try:
        before = flatten_prims(path)
    except Exception as ex:
        before = None
        print("   ⚠ 原文件展平失败: %s" % ex)
    shell, ms_handle = clean_shell(data)
    packed, info = bp.pack_into(shell, [path])
    packed = add_insert(packed, name, ms_handle)
    probs = bp.verify(packed, [name])
    gl1 = list(bp.iter_groups(packed))
    t1 = _table_span(gl1, b"BLOCK_RECORD")
    n_after = len(bp.split_records(gl1, t1[1], t1[2])) if t1 else 0
    try:
        after = flatten_prims(packed)
    except Exception as ex:
        after = None
        probs = probs + ["整理后展平失败: %s" % ex]
    same = (before == after)
    print("%-12s 块记录 %5d → %-3d  图元 %s → %s  图形%s  verify=%s"
          % (name, n_before, n_after, len(before or []), len(after or []),
             "一致" if same else "★变了★", probs if probs else "OK"))
    if dry or not same or probs:
        return False
    if backup_dir:
        os.makedirs(backup_dir, exist_ok=True)
        dst = os.path.join(backup_dir, os.path.basename(path))
        if not os.path.exists(dst):
            shutil.copy2(path, dst)
    with open(path, "wb") as f:
        f.write(packed)
    return True


def main():
    ap = argparse.ArgumentParser(description="块库 DXF 结构整理（图形不动）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--dir", default="")
    args = ap.parse_args()
    d = args.dir or os.path.join(HERE, "blocklib", "blocks")
    files = sorted(f for f in os.listdir(d) if f.lower().endswith(".dxf"))
    backup = os.path.join(d, "_backup_原样_20261009")
    ok = 0
    for f in files:
        try:
            if repair_one(os.path.join(d, f), args.dry_run, backup):
                ok += 1
        except Exception as ex:
            print("%-12s [失败] %s: %s" % (f, type(ex).__name__, ex))
    print("整理完成: %d/%d%s" % (ok, len(files), "（dry-run，没写文件）" if args.dry_run else ""))


if __name__ == "__main__":
    main()
