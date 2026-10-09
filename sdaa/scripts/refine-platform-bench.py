#!/usr/bin/env python
"""精修平台基准测试：CPU / SDAA / SDAA+UseCpuDirect 对比。

为什么需要这个脚本：
  ImmuneBuilder 只暴露"选哪个平台"，**不暴露 OpenMM 的平台属性**
  （如 SDAA 的 ``UseCpuDirect`` / ``DisablePmeStream`` / ``DeterministicForces``）。
  平台属性只有在 **Context 构造时** 通过 ``app.Simulation(..., platformProperties)``
  传才真正生效；用 ``Context.setPlatformProperties()`` 会修改平台的全局状态、
  污染同进程后续调用。

  因此本脚本 monkeypatch ``ImmuneBuilder.refine.app.Simulation``，
  在构造 Simulation 时把请求的属性合入 ``platformProperties``，
  其余完全复用 ImmuneBuilder 自己的 ``refine()``。

已确认的属性（v2 构建）：
  SDAA : UseCpuDirect(default false) / DisablePmeStream(false) / DeterministicForces(false) / Precision(single)
  CPU  : Threads(default 128) / DeterministicForces(false)

用法（在容器内）：
    python scripts/refine-platform-bench.py --platform SDAA
    python scripts/refine-platform-bench.py --platform SDAA --sdda-props UseCpuDirect=true
    python scripts/refine-platform-bench.py --platform CPU --threads 64
    python scripts/refine-platform-bench.py --platform Reference
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import time
from pathlib import Path

from openmm import Platform

import ImmuneBuilder.refine as R
from ImmuneBuilder import refine as _refine_mod


def parse_properties(text):
    props = {}
    for item in (text or "").split(","):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise SystemExit("属性必须是 key=value 形式: %r" % item)
        k, v = item.split("=", 1)
        props[k.strip()] = v.strip()
    return props


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--platform", default="SDAA", help="CPU | SDAA | Reference")
    ap.add_argument("--sdaa-props", default="", help="追加给平台的属性，如 UseCpuDirect=true")
    ap.add_argument("--threads", type=int, default=None, help="传给 refine 的 n_threads（CPU 平台生效）")
    ap.add_argument("--tries", type=int, default=1)
    ap.add_argument("--n", type=int, default=6)
    ap.add_argument("--input", default="/workspace/repro/work/smoke_test_sdaa/sdaa/antibody.pdb")
    ap.add_argument("--output", default=None)
    ap.add_argument("--json", default=None)
    ap.add_argument("--no-sidechain-check", action="store_true")
    args = ap.parse_args()

    props = parse_properties(args.sdaa_props)

    available = [Platform.getPlatform(i).getName() for i in range(Platform.getNumPlatforms())]
    if args.platform not in available:
        raise SystemExit("平台 %r 不可用；当前可用: %s" % (args.platform, available))
    platform = Platform.getPlatformByName(args.platform)

    # 校验属性名是否被该平台支持（提前报错，避免跑到一半才发现）
    supported = {}
    for k in props:
        try:
            supported[k] = platform.getPropertyDefaultValue(k)
        except Exception as exc:
            raise SystemExit("平台 %s 不支持属性 %r: %s" % (args.platform, k, exc))

    # 运行期替换平台解析（不改源码）
    _refine_mod.resolve_openmm_platform = lambda requested=None, n_threads=-1: platform

    # 关键：把属性合入 app.Simulation 的 platformProperties
    orig_simulation = _refine_mod.app.Simulation
    seen_props = []

    def patched_simulation(topology, system, integrator, plat=None, plat_props=None):
        merged = dict(plat_props or {})
        merged.update(props)
        seen_props.append(dict(merged))
        if plat is None:
            return orig_simulation(topology, system, integrator, platform, merged) if merged \
                else orig_simulation(topology, system, integrator)
        return orig_simulation(topology, system, integrator, plat, merged) if merged \
            else orig_simulation(topology, system, integrator, plat)

    _refine_mod.app.Simulation = patched_simulation

    src = Path(args.input)
    if not src.is_file():
        raise SystemExit("找不到输入: %s（先跑一次 smoke test 生成未精修结构）" % src)
    tag = args.platform + ("-" + args.sdaa_props.replace("=", "").replace(",", "_") if props else "")
    out = Path(args.output) if args.output else Path("/workspace/repro/work/refine_bench/%s.pdb" % tag)
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(src, out)

    print("platform        :", platform.getName())
    print("injected props  :", props, "(defaults:", supported, ")")
    print("refine n_threads:", args.threads if args.threads else -1)
    print("tries/n         :", args.tries, "/", args.n)
    print("input / output  :", src, "->", out)
    print("profiling...", flush=True)

    t0 = time.perf_counter()
    error = None
    try:
        ok = R.refine(
            str(out), str(out),
            check_for_strained_bonds=not args.no_sidechain_check,
            tries=args.tries, n=args.n,
            n_threads=args.threads if args.threads else -1,
        )
    except Exception as exc:
        ok = None
        error = "%s: %s" % (type(exc).__name__, exc)
        import traceback

        traceback.print_exc()
    elapsed = time.perf_counter() - t0

    atoms = sum(1 for line in open(out) if line.startswith("ATOM")) if out.is_file() else 0

    result = {
        "platform": platform.getName(),
        "injected_properties": props,
        "property_defaults": supported,
        "simulation_props_seen": seen_props[:3],
        "refine_n_threads": args.threads if args.threads else -1,
        "tries": args.tries,
        "n": args.n,
        "refine_returned": ok,
        "converged": bool(ok),
        "seconds": round(elapsed, 2),
        "atoms": atoms,
        "error": error,
        "available_platforms": available,
    }
    print("REFINE_BENCH_RESULT=" + json.dumps(result, sort_keys=True))
    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
