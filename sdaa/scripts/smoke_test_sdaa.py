#!/usr/bin/env python
"""ImmuneBuilder SDAA 冒烟测试。

对齐 CUDA 基线 `scripts/smoke_test.py` 的验收口径，但补齐：
  * 官方三个推理入口（ABodyBuilder2 / NanoBodyBuilder2 / TCRBuilder2）
  * 可选 --seed 固定随机种子（CPU/SDAA 对比用）
  * 记录实际设备、OpenMM 平台、耗时、SDAA 显存峰值、输出哈希

环境变量：
  EXPECT_DEVICE             期望设备类型（cpu / sdaa / cuda），不匹配则失败
  OUTPUT_DIR                输出目录（默认 /workspace/repro/work/smoke_test_sdaa）
  REFINE_THREADS            传给 antibody.save(n_threads=...)；<=0 表示由 OpenMM 自行选择
  IMMUNEBUILDER_DEVICE      设备选择（见 device_utils）
  IMMUNEBUILDER_OPENMM_PLATFORM  OpenMM 平台选择（auto/Reference/SDAA/...）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sys
import time
from pathlib import Path

# --- 官方 README 示例序列（与 CUDA 基线一致） ---
ANTIBODY = {
    "H": "EVQLVESGGGVVQPGGSLRLSCAASGFTFNSYGMHWVRQAPGKGLEWVAFIRYDGGNKYYADSVKGRFTISRDNSKNTLYLQMKSLRAEDTAVYYCANLKDSRYSGSYYDYWGQGTLVTVS",
    "L": "VIWMTQSPSSLSASVGDRVTITCQASQDIRFYLNWYQQKPGKAPKLLISDASNMETGVPSRFSGSGSGTDFTFTISSLQPEDIATYYCQQYDNLPFTFGPGTKVDFK",
}
NANOBODY = {
    "H": "QVQLQESGGGLVQAGGSLRLSCAASGRTFSSYAMGWFRQAPGKEREFVAAISWSGGSTYYADSVKGRFTISRDNAKNTVYLQMNSLKPEDTAVYYCAAAGLGTVVSEWDYDYDYWGQGTQVTVSS",
}
TCR = {
    "A": "AQEVTQIPAALSVPEGENLVLNCSFTDSAIYNLQWFRQDPGKGLTSLLLIQSSQREQTSGRLNASLDKSSGRSTLYIAASQPGDSATYLCAVRPTSGGSYIPTFGRGTSLIVHPY",
    "B": "NAGVTQTPKFQVLKTGQSMTLQCAQDMNHEYMSWYRQDPGMGLRLIHYSVGAGITDQGEVPNGYNVSRSTTEDFPLRLLSAAPSQTSVYFCASSYVGNTGELFFGEGSRLTVL",
}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def atom_count(path: Path) -> int:
    return sum(1 for line in open(path) if line.startswith("ATOM"))


def collect_env():
    import numpy
    import openmm
    import torch

    info = {
        "python": sys.version.split()[0],
        "platform_machine": platform.machine(),
        "numpy": numpy.__version__,
        "torch": torch.__version__,
        "torch_version_cuda": torch.version.cuda,
        "torch_cuda_is_available": bool(torch.cuda.is_available()),
        "openmm": openmm.version.version,
        "openmm_platforms": [
            openmm.Platform.getPlatform(i).getName()
            for i in range(openmm.Platform.getNumPlatforms())
        ],
    }
    try:
        import torch_sdaa

        info["torch_sdaa"] = torch_sdaa.__version__
        info["torch_sdaa_is_available"] = bool(torch.sdaa.is_available())
        info["torch_sdaa_device_count"] = int(torch.sdaa.device_count())
    except Exception as exc:  # pragma: no cover
        info["torch_sdaa"] = "unavailable: %s" % exc
    try:
        import anarci

        info["anarci"] = getattr(anarci, "__version__", "unknown")
    except Exception as exc:  # pragma: no cover
        info["anarci"] = "unavailable: %s" % exc
    try:
        import pdbfixer

        info["pdbfixer"] = getattr(pdbfixer, "__version__", "unknown")
    except Exception as exc:  # pragma: no cover
        info["pdbfixer"] = "unavailable: %s" % exc
    return info


def run_case(name, builder_factory, sequences, outdir: Path, n_threads: int, seed):
    from ImmuneBuilder.device_utils import seed_all

    if seed is not None:
        seed_all(seed)

    started = time.perf_counter()
    builder = builder_factory()
    t_model = time.perf_counter()
    pred = builder.predict(sequences)
    t_predict = time.perf_counter()
    outfile = outdir / ("%s.pdb" % name)
    pred.save(str(outfile), n_threads=n_threads)
    t_save = time.perf_counter()

    return {
        "case": name,
        "device": str(builder.device),
        "output": str(outfile),
        "sha256": sha256_file(outfile),
        "atoms": atom_count(outfile),
        "seconds": {
            "build_models": round(t_model - started, 3),
            "predict": round(t_predict - t_model, 3),
            "save_and_refine": round(t_save - t_predict, 3),
            "total": round(t_save - started, 3),
        },
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default="antibody,nanobody,tcr",
                    help="逗号分隔：antibody,nanobody,tcr")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--summary", default=None, help="汇总 JSON 输出路径")
    args = ap.parse_args()

    outdir = Path(os.environ.get("OUTPUT_DIR", "/workspace/repro/work/smoke_test_sdaa"))
    outdir.mkdir(parents=True, exist_ok=True)
    n_threads = int(os.environ.get("REFINE_THREADS", "-1"))
    expect_device = os.environ.get("EXPECT_DEVICE", "").strip()

    from ImmuneBuilder import ABodyBuilder2, NanoBodyBuilder2, TCRBuilder2

    env = collect_env()
    print("IMMUNEBUILDER_ENV=" + json.dumps(env, sort_keys=True), flush=True)

    factories = {
        "antibody": (lambda: ABodyBuilder2(), ANTIBODY),
        "nanobody": (lambda: NanoBodyBuilder2(), NANOBODY),
        "tcr": (lambda: TCRBuilder2(), TCR),
    }

    results = []
    failures = []
    for name in [c.strip() for c in args.cases.split(",") if c.strip()]:
        if name not in factories:
            raise SystemExit("未知用例: %s（可选 %s）" % (name, ",".join(factories)))
        factory, seqs = factories[name]
        print("=== case %s ===" % name, flush=True)
        try:
            res = run_case(name, factory, seqs, outdir, n_threads, args.seed)
        except Exception as exc:
            import traceback

            traceback.print_exc()
            failures.append({"case": name, "error": "%s: %s" % (type(exc).__name__, exc)})
            continue
        if expect_device and not res["device"].startswith(expect_device):
            failures.append({
                "case": name,
                "error": "设备不匹配：期望 %s，实际 %s" % (expect_device, res["device"]),
            })
        if res["atoms"] < 500:
            failures.append({"case": name, "error": "输出不完整：仅 %d 个 ATOM" % res["atoms"]})
        results.append(res)
        print(json.dumps(res, sort_keys=True), flush=True)

    summary = {
        "environment": env,
        "expect_device": expect_device or None,
        "refine_threads": n_threads,
        "seed": args.seed,
        "results": results,
        "failures": failures,
    }
    if env.get("torch_sdaa_is_available"):
        import torch

        summary["sdaa_peak_bytes"] = int(torch.sdaa.max_memory_allocated())

    print("IMMUNEBUILDER_SMOKE_SUMMARY=" + json.dumps(summary, sort_keys=True), flush=True)
    if args.summary:
        Path(args.summary).write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")

    if failures:
        print("SMOKE_FAILED", flush=True)
        return 1
    print("SMOKE_PASSED", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
