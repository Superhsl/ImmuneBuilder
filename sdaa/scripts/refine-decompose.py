#!/usr/bin/env python
"""精修耗时分解：把 refine_once 拆成各阶段，回答「时间花在哪」。

背景：跨机器数据显示精修是端到端的大头（H100 上占 95%，龙芯 SDAA 上占 71%），
但不知道耗时分布在「平台无关的准备阶段」还是「平台相关的能量最小化」。
线索：H100 的 CUDA refine（4.487s）≈ 龙芯单线程 Reference refine（4.61s），
暗示平台无关部分占大头。本脚本用来证实或推翻这个推断。

实现方式：**不重写 refine 逻辑**，而是对 ImmuneBuilder 真实调用的函数打计时桩，
因此测到的是真实代码路径。计时采用**调用栈感知的独占时间**：
父阶段的时间会扣掉子阶段，避免 addHydrogens ⊃ createSystem 这类嵌套被重复计数。

拆解阶段：
    pdbfixer_init          PDBFixer(input_file)
    pdbfixer_missing       findMissingResidues / findMissingAtoms / addMissingAtoms
    addHydrogens           Modeller.addHydrogens（内部会调用 createSystem）
    createSystem           ForceField.createSystem          ← 平台无关
    simulation_ctor        app.Simulation(...)  ← **Context 创建，平台首次触达**
    minimizeEnergy         simulation.minimizeEnergy()      ← 平台相关核心
    checks                 bond_check/cis_check/stereo_check/clash_check
    chirality_fixer        修 D-型手性
    strained_sidechain     侧链穿插检查与重建

⚠️ 输入必须是真的「未精修」结构，否则精修第一轮就收敛，n=6 的修正路径根本不会跑。
   用 ABodyBuilder2 的 -u 生成的 PDB **仍然是精修过的**（-u 只是关掉侧链键检查）。
   真正未精修的结构要从 Antibody 对象直接取，见下方 gen-unrefined 子命令。

用法（在容器内）：
    # 1) 先产生真正未精修的输入
    python scripts/refine-decompose.py gen-unrefined \
        --case antibody --output /workspace/repro/work/refine_bench/antibody_true_unrefined.pdb

    # 2) 分解
    IMMUNEBUILDER_OPENMM_PLATFORM=CPU REFINE_THREADS=32 \
      python scripts/refine-decompose.py --tag cpu32 --input <上一步输出>

    IMMUNEBUILDER_OPENMM_PLATFORM=SDAA \
      IMMUNEBUILDER_OPENMM_PROPERTIES="UseCpuDirect=true" \
      python scripts/refine-decompose.py --tag sdaa_cd --input <上一步输出>

输出：stdout 表格 + <OUTPUT_ROOT>/<tag>/decompose-<tag>.json
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import shutil
import sys
import time
from pathlib import Path

T = collections.defaultdict(float)        # 独占时间
T_INC = collections.defaultdict(float)    # 含子调用时间
N = collections.defaultdict(int)
_STACK: list = []
_TOTAL_CHILD = [0.0]

SEQUENCES = {
    "antibody": {
        "H": "EVQLVESGGGVVQPGGSLRLSCAASGFTFNSYGMHWVRQAPGKGLEWVAFIRYDGGNKYYADSVKGRFTISRDNSKNTLYLQMKSLRAEDLAEDTAVYYCANLKDSRYSGSYYDYWGQGTLVTVS",
        "L": "VIWMTQSPSSLSASVGDRVTITCQASQDIRFYLNWYQQKPGKAPKLLISDASNMETGVPSRFSGSGSGTDFTFTISSLQPEDIATYYCQQYDNLPFTFGPGTKVDFK",
    },
    "nanobody": {
        "H": "QVQLQESGGGLVQAGGSLRLSCAASGRTFSSYAMGWFRQAPGKEREFVAAISWSGGSTYYADSVKGRFTISRDNAKNTVYLQMNSLKPEDTAVYYCAAAGLGTVVSEWDYDYDYWGQGTQVTVSS",
    },
    "tcr": {
        "A": "AQEVTQIPAALSVPEGENLVLNCSFTDSAIYNLQWFRQDPGKGLTSLLLIQSSQREQTSGRLNASLDKSSGRSTLYIAASQPGDSATYLCAVRPTSGGSYIPTFGRGTSLIVHPY",
        "B": "NAGVTQTPKFQVLKTGQSMTLQCAQDMNHEYMSWYRQDPGMGLRLIHYSVGAGITDQGEVPNGYNVSRSTTEDFPLRLLSAAPSQTSVYFCASSYVGNTGELFFGEGSRLTVL",
    },
}
# 修正上面的 antibody H 链（避免手误）
SEQUENCES["antibody"]["H"] = (
    "EVQLVESGGGVVQPGGSLRLSCAASGFTFNSYGMHWVRQAPGKGLEWVAFIRYDGGNKYYADSVKGRFTISRDNSKNTLYLQMKSLRAEDTAVYYCANLKDSRYSGSYYDYWGQGTLVTVS"
)


def _enter(key):
    _STACK.append((key, time.perf_counter(), _TOTAL_CHILD[0]))


def _exit():
    key, t0, child_at_entry = _STACK.pop()
    elapsed = time.perf_counter() - t0
    child = _TOTAL_CHILD[0] - child_at_entry
    T[key] += elapsed - child          # 独占（扣掉子阶段）
    T_INC[key] += elapsed
    N[key] += 1
    _TOTAL_CHILD[0] += elapsed


def timer(key):
    def deco(fn):
        def wrapper(*a, **k):
            _enter(key)
            try:
                return fn(*a, **k)
            finally:
                _exit()
        return wrapper
    return deco


def install_timers():
    import pdbfixer
    from openmm import app
    from openmm.app.forcefield import ForceField

    import ImmuneBuilder.refine as R

    pdbfixer.PDBFixer.__init__ = timer("pdbfixer_init")(pdbfixer.PDBFixer.__init__)
    for name in ("findMissingResidues", "findMissingAtoms", "addMissingAtoms"):
        setattr(pdbfixer.PDBFixer, name, timer("pdbfixer_missing")(getattr(pdbfixer.PDBFixer, name)))

    app.Modeller.addHydrogens = timer("addHydrogens")(app.Modeller.addHydrogens)

    # createSystem 特殊处理：OpenMM 的 argtracker 会执行
    # `args.checkArgs(self.createSystem)`，用来检查传入的关键字参数是否都被用到。
    # 若把实例属性换成普通 wrapper，self.createSystem 会解析到 wrapper，
    # argtracker 便报 "The argument 'flexibleConstraints' ... was never used"。
    # 规避：调用期间把类属性临时还原成原函数，让内部 self.createSystem 命中原实现。
    _orig_createSystem = ForceField.createSystem

    def timed_createSystem(self, *a, **k):
        _enter("createSystem")
        try:
            ForceField.createSystem = _orig_createSystem
            return _orig_createSystem(self, *a, **k)
        finally:
            ForceField.createSystem = timed_createSystem
            _exit()

    ForceField.createSystem = timed_createSystem

    app.Simulation.__init__ = timer("simulation_ctor")(app.Simulation.__init__)
    app.Simulation.minimizeEnergy = timer("minimizeEnergy")(app.Simulation.minimizeEnergy)

    for name in ("bond_check", "cis_check", "stereo_check", "clash_check"):
        if hasattr(R, name):
            setattr(R, name, timer("checks")(getattr(R, name)))
    if hasattr(R, "chirality_fixer"):
        R.chirality_fixer = timer("chirality_fixer")(R.chirality_fixer)
    for name in ("strained_sidechain_bonds_check", "strained_sidechain_bonds_fixer"):
        if hasattr(R, name):
            setattr(R, name, timer("strained_sidechain")(getattr(R, name)))
    return R


def parse_properties(text):
    props = {}
    for item in (text or "").split(","):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise SystemExit("属性必须是 key=value：%r" % item)
        k, v = item.split("=", 1)
        props[k.strip()] = v.strip()
    return props


def cmd_gen_unrefined(args):
    """生成**真正未精修**的结构（refine 之前的状态）。"""
    from ImmuneBuilder import ABodyBuilder2, NanoBodyBuilder2, TCRBuilder2

    classes = {"antibody": ABodyBuilder2, "nanobody": NanoBodyBuilder2, "tcr": TCRBuilder2}
    if args.case not in classes:
        raise SystemExit("未知用例 %r" % args.case)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    builder = classes[args.case]()
    pred = builder.predict(SEQUENCES[args.case])
    # save_single_unrefined 直接写 refine 之前的坐标，不经过 OpenMM
    pred.save_single_unrefined(str(out), index=0)
    n = sum(1 for line in open(out) if line.startswith("ATOM"))
    print("已写出未精修结构: %s  atoms=%d  device=%s" % (out, n, builder.device))
    print("注意：这是 refine() 的输入，因此跑分解时会真正走完 n 轮修正路径。")
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")

    g = sub.add_parser("gen-unrefined", help="生成真正未精修的输入")
    g.add_argument("--case", default="antibody")
    g.add_argument("--output", required=True)
    g.set_defaults(func=cmd_gen_unrefined)

    ap.add_argument("--tag")
    ap.add_argument("--input")
    ap.add_argument("--output", default=None)
    ap.add_argument("--tries", type=int, default=1)
    ap.add_argument("--n", type=int, default=6)
    ap.add_argument("--no-sidechain-check", action="store_true")
    ap.add_argument("--skip-refine", action="store_true",
                    help="只测 pdbfixer + 一次 minimize，不跑完整 refine 循环")
    args = ap.parse_args()

    if args.cmd == "gen-unrefined":
        return args.func(args)

    if not args.tag or not args.input:
        ap.error("需要 --tag 与 --input（或用 gen-unrefined 子命令）")

    root = Path(os.environ.get("OUTPUT_ROOT", "/workspace/repro/work/cross_machine"))
    tag_dir = root / args.tag
    tag_dir.mkdir(parents=True, exist_ok=True)

    threads = int(os.environ.get("REFINE_THREADS", "-1"))
    props = parse_properties(os.environ.get("IMMUNEBUILDER_OPENMM_PROPERTIES", ""))
    forced_name = os.environ.get("IMMUNEBUILDER_OPENMM_PLATFORM", "").strip()

    src = Path(args.input)
    if not src.is_file():
        raise SystemExit("找不到输入 %s" % src)
    out = Path(args.output) if args.output else (tag_dir / "decompose.pdb")
    shutil.copy(src, out)

    from openmm import Platform

    available = [Platform.getPlatform(i).getName() for i in range(Platform.getNumPlatforms())]
    if forced_name and forced_name.lower() != "auto":
        if forced_name not in available:
            raise SystemExit("平台 %r 不可用；可用：%s" % (forced_name, available))
        plat = Platform.getPlatformByName(forced_name)
    else:
        plat = None

    prop_defaults = {}
    if props:
        if plat is None:
            raise SystemExit("指定了属性就必须指定 IMMUNEBUILDER_OPENMM_PLATFORM（不能是 auto）")
        for k in props:
            try:
                prop_defaults[k] = plat.getPropertyDefaultValue(k)
            except Exception:
                raise SystemExit("平台 %s 不接受属性 %r" % (plat.getName(), k))

    R = install_timers()

    from openmm import app as app_mod

    applied = []
    if plat is not None:
        if hasattr(R, "resolve_openmm_platform"):
            R.resolve_openmm_platform = lambda requested=None, n_threads=-1: plat
        _orig_sim = app_mod.Simulation

        def patched(topology, system, integrator, p=None, plat_props=None):
            merged = dict(plat_props or {})
            merged.update(props)
            target = p if p is not None else plat
            if merged:
                applied.append({"platform": target.getName(), "properties": dict(merged)})
                return _orig_sim(topology, system, integrator, target, merged)
            return _orig_sim(topology, system, integrator, target)

        app_mod.Simulation = patched
        R.app.Simulation = patched

    print("input        :", src)
    print("output       :", out)
    print("platform     :", plat.getName() if plat else "(源码自选)")
    print("properties   :", props, "defaults_before:", prop_defaults)
    print("refine n_thr :", threads, " tries/n:", args.tries, "/", args.n)
    print("profiling...", flush=True)

    t0 = time.perf_counter()
    if args.skip_refine:
        import pdbfixer

        fixer = pdbfixer.PDBFixer(str(out))
        fixer.findMissingResidues()
        fixer.findMissingAtoms()
        fixer.addMissingAtoms()
        R.minimize_energy(fixer.topology, fixer.positions, n_threads=threads)
        ok = None
    else:
        ok = R.refine(str(out), str(out),
                      check_for_strained_bonds=not args.no_sidechain_check,
                      tries=args.tries, n=args.n, n_threads=threads)
    wall = time.perf_counter() - t0

    accounted = sum(T.values())
    print()
    print("== 阶段耗时分解（%s）==" % args.tag)
    print("  %-18s %10s %10s %6s %9s" % ("stage", "exclusive", "inclusive", "calls", "% wall"))
    for key in sorted(T, key=lambda k: -T[k]):
        print("  %-18s %10.3f %10.3f %6d %8.1f%%"
              % (key, T[key], T_INC[key], N[key], 100.0 * T[key] / wall))
    print("  %-18s %10.3f %10s %6s %8.1f%%" % ("[accounted]", accounted, "-", "-",
                                               100.0 * accounted / wall))
    print("  %-18s %10.3f" % ("WALL", wall))
    print()

    prep = T["pdbfixer_init"] + T["pdbfixer_missing"] + T["addHydrogens"] + T["createSystem"]
    plat_specific = T["simulation_ctor"] + T["minimizeEnergy"]
    other = accounted - prep - plat_specific
    print("  -- 平台无关（pdbfixer + addHydrogens + createSystem）= %.3f s (%.1f%%)"
          % (prep, 100.0 * prep / wall))
    print("  -- 平台相关（Context 创建 + minimizeEnergy）        = %.3f s (%.1f%%)   最小化次数=%d"
          % (plat_specific, 100.0 * plat_specific / wall, N["minimizeEnergy"]))
    print("  -- 其它（几何判据 / 手性 / 侧链重建）               = %.3f s (%.1f%%)"
          % (other, 100.0 * other / wall))
    print()

    result = {
        "tag": args.tag,
        "platform": plat.getName() if plat else None,
        "properties": props,
        "properties_defaults_before": prop_defaults,
        "refine_threads": threads,
        "tries": args.tries,
        "n": args.n,
        "skip_refine": args.skip_refine,
        "refine_returned": ok,
        "wall_seconds": round(wall, 3),
        "stages_exclusive_seconds": {k: round(v, 3) for k, v in T.items()},
        "stages_inclusive_seconds": {k: round(v, 3) for k, v in T_INC.items()},
        "stages_calls": dict(N),
        "platform_independent_seconds": round(prep, 3),
        "platform_specific_seconds": round(plat_specific, 3),
        "minimize_calls": N["minimizeEnergy"],
        "simulation_props_applied": applied[:3],
        "atoms": sum(1 for line in open(out) if line.startswith("ATOM")),
        "input": str(src),
    }
    p = tag_dir / ("decompose-%s.json" % args.tag)
    p.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print("DECOMPOSE_RESULT=" + json.dumps(result, sort_keys=True))
    print()
    print("  明细写入: %s" % p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
