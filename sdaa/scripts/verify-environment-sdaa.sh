#!/usr/bin/env bash
# 容器内环境自检（设备、SDAA、OpenMM 平台、HMMER、依赖版本）
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

# 读取项目根的 .env.sdaa（容器名、设备/平台配置）；已导出的环境变量优先
if [[ -f ./.env.sdaa ]]; then
    # shellcheck disable=SC1091
    set -a; source ./.env.sdaa; set +a
fi
CONTAINER="${CONTAINER_NAME:-immunebuilder-sdaa}"

if ! docker container inspect "${CONTAINER}" >/dev/null 2>&1; then
    echo "ERROR: 容器 ${CONTAINER} 不存在" >&2
    exit 1
fi

cat > /tmp/verify-sdaa-env.py <<'PY'
import json
import os
import shutil
import subprocess
import sys

report = {"ok": True, "checks": {}}


def check(name, fn):
    try:
        value = fn()
        report["checks"][name] = {"ok": True, "value": value}
    except Exception as exc:
        report["ok"] = False
        report["checks"][name] = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}


import numpy
import openmm
import torch

check("python", lambda: sys.version.split()[0])
check("torch", lambda: torch.__version__)
check("numpy", lambda: numpy.__version__)
check("openmm", lambda: openmm.version.version)
check("openmm_platforms", lambda: [
    openmm.Platform.getPlatform(i).getName() for i in range(openmm.Platform.getNumPlatforms())
])
check("torch_cuda_is_available", lambda: bool(torch.cuda.is_available()))
check("torch_sdaa_version", lambda: __import__("torch_sdaa").__version__)
check("torch_sdaa_is_available", lambda: bool(torch.sdaa.is_available()))
check("torch_sdaa_device_count", lambda: int(torch.sdaa.device_count()))
check("sdaa_device_name", lambda: torch.sdaa.get_device_name(0))


def sdaa_matmul():
    a = torch.randn(64, 64, device="sdaa")
    b = torch.randn(64, 64, device="sdaa")
    return float((a @ b).sum())


check("sdaa_matmul", sdaa_matmul)


def sdaa_svd_det():
    """ImmuneBuilder util.find_alignment_transform 用到 svd/det（Kabsch 对齐）。"""
    a = torch.randn(8, 3, 3, device="sdaa", dtype=torch.float64)
    v, _, w = torch.linalg.svd(a)
    d = torch.linalg.det(v @ w)
    torch.sdaa.synchronize()
    return {"det_shape": list(d.shape), "det_finite": bool(torch.isfinite(d).all())}


check("sdaa_linalg_svd_det", sdaa_svd_det)

check("hmmscan", lambda: subprocess.run(["hmmscan", "-h"], capture_output=True, text=True).stdout.splitlines()[1].strip())
check("anarci_version", lambda: getattr(__import__("anarci"), "__version__", "unknown"))
check("pdbfixer_version", lambda: getattr(__import__("pdbfixer"), "__version__", "unknown"))
check("biopython_version", lambda: __import__("Bio").__version__)


def resolve_device_works():
    from ImmuneBuilder.device_utils import resolve_device
    return str(resolve_device("sdaa"))


check("resolve_device(sdaa)", resolve_device_works)


def resolve_platform_works():
    from ImmuneBuilder.device_utils import resolve_openmm_platform
    p = resolve_openmm_platform()
    return None if p is None else p.getName()


check("resolve_openmm_platform()", resolve_platform_works)
check("env_IMMUNEBUILDER_DEVICE", lambda: os.environ.get("IMMUNEBUILDER_DEVICE"))
check("env_IMMUNEBUILDER_OPENMM_PLATFORM", lambda: os.environ.get("IMMUNEBUILDER_OPENMM_PLATFORM"))
check("env_LD_LIBRARY_PATH", lambda: os.environ.get("LD_LIBRARY_PATH"))
check("env_OPENMM_PLUGIN_DIR", lambda: os.environ.get("OPENMM_PLUGIN_DIR"))

print("VERIFY_ENV_REPORT=" + json.dumps(report, sort_keys=True, indent=2))
sys.exit(0 if report["ok"] else 1)
PY
docker cp /tmp/verify-sdaa-env.py "${CONTAINER}":/tmp/verify-sdaa-env.py >/dev/null
docker exec "${CONTAINER}" python /tmp/verify-sdaa-env.py
