#!/usr/bin/env python
"""通用推理基准：在任意机器上测量 ImmuneBuilder 的推理性能并留档供精度对比。

设计目标：
  1. **不依赖 device_utils.py 补丁** —— 未打补丁的 CUDA 基线上也能直接运行。
  2. 支持注入 OpenMM 平台属性（SDAA 的 UseCpuDirect / Precision 等），
     并把「实际传给 Simulation 的属性」写进结果，避免出现"sdaa_cd 那种跑了但没生效"的情况。

用法（在容器内）：
    # CUDA 机器：走源码自带的设备选择
    python scripts/inference-bench.py --tag cuda --runs 3

    # 龙芯：显式设备 + 平台 + 平台属性
    IMMUNEBUILDER_DEVICE=sdaa IMMUNEBUILDER_OPENMM_PLATFORM=SDAA \
    IMMUNEBUILDER_OPENMM_PROPERTIES="UseCpuDirect=true" \
    python scripts/inference-bench.py --tag sdaa_cd --runs 3

环境变量：
    OUTPUT_ROOT                  输出根目录，默认 /workspace/repro/work/cross_machine
    IMMUNEBUILDER_DEVICE         cuda | cpu | sdaa（可选；需要源码支持，即已打补丁）
    IMMUNEBUILDER_OPENMM_PLATFORM  强制精修平台，如 SDAA / CPU / Reference / CUDA
    IMMUNEBUILDER_OPENMM_PROPERTIES  逗号分隔的 key=value，注入平台属性
                                     例：UseCpuDirect=true,Precision=mixed
                                     指定时必须同时指定 OPENMM_PLATFORM
    REFINE_THREADS               传给 save(n_threads=...)，默认 -1
    SKIP_REFINE                  设为 1 等价于 --skip-refine

输出：
    <OUTPUT_ROOT>/<tag>/run<N>/<case>.pdb
    <OUTPUT_ROOT>/<tag>/inference-<tag>.jsonl   每行一个 run 的完整结果
    stdout 打印 BENCH_RESULT=<json>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import random
import socket
import sys
import time
from pathlib import Path

SEQUENCES = {
    "antibody": {
        "H": "EVQLVESGGGVVQPGGSLRLSCAASGFTFNSYGMHWVRQAPGKGLEWVAFIRYDGGNKYYADSVKGRFTISRDNSKNTLYLQMKSLRAEDTAVYYCANLKDSRYSGSYYDYWGQGTLVTVS",
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

SEED = 20260921

# 已知可用的平台属性（用于在属性名写错时给出提示，不是白名单）
KNOWN_PROPERTIES = {
    "SDAA": ["Precision", "DeviceIndex", "UseCpuPme", "UseCpuDirect",
             "DisablePmeStream", "DeterministicForces", "TempDirectory"],
    "CPU": ["Threads", "DeterministicForces"],
    "CUDA": ["Precision", "DeviceIndex", "DeterministicForces", "CudaPrecision"],
    "Reference": [],
}


def parse_properties(text):
    props = {}
    for item in (text or "").split(","):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise SystemExit("IMMUNEBUILDER_OPENMM_PROPERTIES 项必须是 key=value：%r" % item)
        k, v = item.split("=", 1)
        props[k.strip()] = v.strip()
    return props


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def collect_env():
    info = {
        "hostname": socket.gethostname(),
        "machine": platform.machine(),
        "python": sys.version.split()[0],
    }
    for mod, key in (("torch", "torch"), ("numpy", "numpy"), ("openmm", "openmm"),
                     ("Bio", "biopython"), ("anarci", "anarci"), ("torch_sdaa", "torch_sdaa")):
        try:
            m = __import__(mod)
            info[key] = getattr(m, "__version__", getattr(getattr(m, "version", None), "version", "unknown"))
        except Exception as exc:
            info[key] = "unavailable: %s" % type(exc).__name__
    try:
        import torch

        info["torch_cuda"] = torch.version.cuda
        info["cuda_available"] = bool(torch.cuda.is_available())
        info["cuda_device"] = (
            torch.cuda.get_device_name(0) if torch.cuda.is_available() else None
        )
    except Exception:
        pass
    try:
        import torch

        info["sdaa_available"] = bool(torch.sdaa.is_available())
        info["sdaa_device_count"] = int(torch.sdaa.device_count())
    except Exception:
        pass
    try:
        from openmm import Platform

        info["openmm_platforms"] = [
            Platform.getPlatform(i).getName() for i in range(Platform.getNumPlatforms())
        ]
    except Exception:
        pass
    return info


def seed_everything(seed=SEED):
    random.seed(seed)
    try:
        import numpy as np

        np.random.seed(seed)
    except Exception:
        pass
    import torch

    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)
    try:
        import torch_sdaa  # noqa: F401

        if torch.sdaa.is_available() and hasattr(torch.sdaa, "manual_seed_all"):
            torch.sdaa.manual_seed_all(seed)
    except Exception:
        pass


def resolve_forced_platform(name):
    from openmm import Platform

    available = [Platform.getPlatform(i).getName() for i in range(Platform.getNumPlatforms())]
    if name not in available:
        raise SystemExit("平台 %r 不可用；当前可用：%s" % (name, available))
    return Platform.getPlatformByName(name)


def validate_properties(plat, props):
    """逐个校验属性是否被该平台接受；不通过则立刻报错（而不是跑到一半失败）。"""
    defaults = {}
    for k in props:
        try:
            defaults[k] = plat.getPropertyDefaultValue(k)
        except Exception:
            hint = KNOWN_PROPERTIES.get(plat.getName())
            raise SystemExit(
                "平台 %s 不接受属性 %r。该平台已知属性：%s"
                % (plat.getName(), k, hint if hint else "（未知，请查 OpenMM 文档）")
            )
    return defaults


def install_property_injection(props, forced_platform, applied_log):
    """把平台属性注入到 ImmuneBuilder.refine 的 app.Simulation 调用。

    为什么必须 monkeypatch app.Simulation：
      平台属性只有在 **Context 构造时** 通过 app.Simulation(..., platformProperties)
      传才生效。用 Context.setPlatformProperties() 会改平台全局状态、污染后续调用。

    注意：`refine.py` 里是 `from openmm import app` 后调用 `app.Simulation(...)`，
    因此这里改的是 openmm.app 模块属性（全局）。脚本退出即失效，一次性运行可接受。
    """
    import ImmuneBuilder.refine as _refine_mod

    orig = _refine_mod.app.Simulation

    def patched(topology, system, integrator, plat=None, plat_props=None):
        merged = dict(plat_props or {})
        merged.update(props)
        target = plat if plat is not None else forced_platform
        if merged:
            applied_log.append({"platform": target.getName() if target is not None else None,
                                "properties": dict(merged)})
            if target is None:
                return orig(topology, system, integrator, plat_props=merged)
            return orig(topology, system, integrator, target, merged)
        return orig(topology, system, integrator, plat)

    _refine_mod.app.Simulation = patched
    return orig


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True, help="本次运行的标签，如 cuda / cpu32 / sdaa")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--cases", default="antibody,nanobody,tcr")
    ap.add_argument("--skip-refine", action="store_true")
    ap.add_argument("--threads", type=int, default=None, help="传给 save(n_threads=...)")
    args = ap.parse_args()

    root = Path(os.environ.get("OUTPUT_ROOT", "/workspace/repro/work/cross_machine"))
    tag_dir = root / args.tag
    tag_dir.mkdir(parents=True, exist_ok=True)
    jsonl = tag_dir / ("inference-%s.jsonl" % args.tag)

    threads = args.threads
    if threads is None:
        threads = int(os.environ.get("REFINE_THREADS", "-1"))
    skip_refine = args.skip_refine or os.environ.get("SKIP_REFINE") == "1"

    props = parse_properties(os.environ.get("IMMUNEBUILDER_OPENMM_PROPERTIES", ""))
    forced_name = os.environ.get("IMMUNEBUILDER_OPENMM_PLATFORM", "").strip()

    forced_platform = None
    prop_defaults = {}
    if props:
        # 有属性就必须有明确平台，否则属性可能被套到错误的平台上
        if not forced_name or forced_name.lower() == "auto":
            raise SystemExit(
                "指定了 IMMUNEBUILDER_OPENMM_PROPERTIES=%r 时必须同时指定 "
                "IMMUNEBUILDER_OPENMM_PLATFORM（不能是 auto），否则属性可能被应用到"
                "非预期平台。例如：IMMUNEBUILDER_OPENMM_PLATFORM=SDAA" % props
            )
        forced_platform = resolve_forced_platform(forced_name)
        prop_defaults = validate_properties(forced_platform, props)
        print("PLATFORM_PROPERTIES=%s (平台 %s，注入前默认值 %s)"
              % (json.dumps(props), forced_platform.getName(), json.dumps(prop_defaults)), flush=True)

    applied_log = []
    if props:
        install_property_injection(props, forced_platform, applied_log)

    from ImmuneBuilder import ABodyBuilder2, NanoBodyBuilder2, TCRBuilder2

    classes = {"antibody": ABodyBuilder2, "nanobody": NanoBodyBuilder2, "tcr": TCRBuilder2}
    cases = [c.strip() for c in args.cases.split(",") if c.strip()]

    env = collect_env()
    print("BENCH_ENV=" + json.dumps(env, sort_keys=True), flush=True)

    for run in range(1, args.runs + 1):
        run_dir = tag_dir / ("run%d" % run)
        run_dir.mkdir(parents=True, exist_ok=True)
        results = []
        for case in cases:
            if case not in classes:
                raise SystemExit("未知用例 %r" % case)
            seed_everything(SEED)
            t0 = time.perf_counter()
            builder = classes[case]()
            t1 = time.perf_counter()
            pred = builder.predict(SEQUENCES[case])
            t2 = time.perf_counter()
            outfile = run_dir / ("%s.pdb" % case)
            pred.save(str(outfile), n_threads=threads)
            t3 = time.perf_counter()
            results.append({
                "case": case,
                "device": str(builder.device),
                "load_seconds": round(t1 - t0, 3),
                "predict_seconds": round(t2 - t1, 3),
                "refine_seconds": round(t3 - t2, 3) if not skip_refine else None,
                "total_seconds": round(t3 - t0, 3),
                "atoms": sum(1 for line in open(outfile) if line.startswith("ATOM")),
                "sha256": file_sha256(outfile),
            })
            print("  run%d %-9s device=%-5s predict=%7.3fs total=%7.3fs atoms=%d"
                  % (run, case, results[-1]["device"], results[-1]["predict_seconds"],
                     results[-1]["total_seconds"], results[-1]["atoms"]), flush=True)

        record = {
            "run": run,
            "tag": args.tag,
            "seed": SEED,
            "refine_threads": threads,
            "skip_refine": skip_refine,
            # 平台属性相关：这三项是"sdaa_cd 那种跑了但没生效"的防呆记录
            "openmm_properties_requested": props,
            "openmm_properties_defaults_before": prop_defaults,
            "openmm_platform_forced": forced_name or None,
            "simulation_props_applied": applied_log[-3:],
            "simulation_construction_count": len(applied_log),
            "environment": env,
            "results": results,
        }
        with open(jsonl, "a") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
        print("BENCH_RESULT=" + json.dumps(record, sort_keys=True), flush=True)

    print()
    print("==> 汇总（%s，%d 轮）" % (args.tag, args.runs))
    if props:
        print("    平台属性: %s -> 实际传入 Simulation 次数 %d"
              % (json.dumps(props), len(applied_log)))
        if not applied_log:
            print("    !! 警告：属性从未被传入任何 Simulation —— 本臂等同于未开属性 !!")
    print("  %-10s %-8s %-10s %-10s %-10s" % ("case", "device", "predict中位", "refine中位", "total中位"))
    records = [json.loads(l) for l in open(jsonl) if l.strip()]
    for case in cases:
        vals = {}
        dev = "?"
        for r in records:
            for item in r["results"]:
                if item["case"] == case:
                    vals.setdefault("predict", []).append(item["predict_seconds"])
                    if item["refine_seconds"] is not None:
                        vals.setdefault("refine", []).append(item["refine_seconds"])
                    vals.setdefault("total", []).append(item["total_seconds"])
                    dev = item["device"]
        def med(xs):
            xs = sorted(xs)
            n = len(xs)
            return xs[n // 2] if n % 2 else 0.5 * (xs[n // 2 - 1] + xs[n // 2])
        print("  %-10s %-8s %-10.3f %-10s %-10.3f" % (
            case, dev, med(vals["predict"]),
            ("%.3f" % med(vals["refine"])) if "refine" in vals else "-",
            med(vals["total"])))
    print()
    print("  明细: %s" % jsonl)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
