#!/usr/bin/env python
"""镜像构建期的依赖自检（由 Dockerfile 的 RUN 调用）。

设计说明：
  `anarci` 与 `pdbfixer` 都用 `--no-deps` 安装，因为它们的依赖
  （biopython / numpy / scipy / openmm）由基础镜像或运行期只读挂载提供。
  这里显式验证依赖确实可导入，避免把问题推迟到运行期。

注意：**openmm 只在运行期可用**（以只读方式挂载到 /opt/openmm-py 与 /opt/openmm），
构建期不存在，因此与 openmm 相关的检查（openmm 本体、pdbfixer）只在可用时才执行。
运行期的完整检查请使用 scripts/verify-environment-sdaa.sh。
"""

import importlib
import sys

import Bio
import anarci
import einops
import numpy
import scipy
import torch

print("python     ", sys.version.split()[0])
print("torch      ", torch.__version__)
print("numpy      ", numpy.__version__)
print("scipy      ", scipy.__version__)
print("einops     ", einops.__version__)
print("biopython  ", Bio.__version__)
print("anarci     ", getattr(anarci, "__version__", "unknown"))

# anarci 的编号 API 必须与 ImmuneBuilder.sequence_checks 的用法一致
from anarci import anarci as anarci_fn  # noqa: F401
from anarci import scheme_short_to_long, validate_sequence  # noqa: F401

print("anarci API : validate_sequence / anarci / scheme_short_to_long OK")

# ---- 以下依赖仅在运行期挂载 OpenMM 后可用 ----
HAS_OPENMM = importlib.util.find_spec("openmm") is not None
print("openmm     ", "available" if HAS_OPENMM else "NOT mounted (expected at build time)")
if HAS_OPENMM:
    import openmm

    print("openmm ver ", openmm.version.version)
    print("platforms  ", [
        openmm.Platform.getPlatform(i).getName()
        for i in range(openmm.Platform.getNumPlatforms())
    ])
    import pdbfixer

    print("pdbfixer   ", pdbfixer.__version__)
else:
    print("pdbfixer   : skipped (imports openmm)")

print("DEPENDENCY_SELFCHECK_OK")
