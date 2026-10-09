#!/usr/bin/env bash
# 一次性预热模型权重（官方下载机制，写入宿主 weights/）。
#
# 为什么需要单独一个脚本：
#   正常容器按"权重只读"挂载 /weights:ro。镜像内
#       .../site-packages/ImmuneBuilder/trained_model -> /weights
#   而官方代码在权重缺失时调用 ImmuneBuilder.util.download_file() **写入** /weights，
#   只读挂载下会失败。所以首次用本脚本起一个**临时容器**（weights 以 :rw 挂载），
#   触发官方下载，权重直接落到宿主 weights/ 里；之后正常容器全程只读、可离线运行。
#
# 用法：
#   ./scripts/prewarm-weights-sdaa.sh              # 下载默认需要的 12 份
#   ./scripts/prewarm-weights-sdaa.sh --original   # 额外下载旧版 TCRBuilder2 权重（共 16 份）
#
# 特性：
#   * 只新建并删除**临时**容器（--rm），不碰现有容器、镜像与工作副本
#   * 设备用 CPU（下载与设备无关，避免占用 SDAA 卡）
#   * 幂等：已有的权重会被 are_weights_ready() 判定为就绪，不会重复下载
#   * 结束后打印文件数、总大小与 md5 清单
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

# shellcheck disable=SC1091
set -a; source ./.env.sdaa; set +a

: "${IMAGE_NAME:?}" "${HOST_UID:?}" "${HOST_GID:?}"
: "${OPENMM_INSTALL:?}" "${OPENMM_SITE_PACKAGES:?}"

WITH_ORIGINAL=0
for arg in "$@"; do
    case "${arg}" in
        --original) WITH_ORIGINAL=1 ;;
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
        *) echo "未知参数: ${arg}" >&2; exit 2 ;;
    esac
done

mkdir -p "${ROOT}/weights"

for p in "${OPENMM_INSTALL}" "${OPENMM_SITE_PACKAGES}"; do
    if [[ ! -d "${p}" ]]; then
        echo "ERROR: OpenMM 路径不存在: ${p}" >&2
        exit 1
    fi
done

INNER_PY='
from ImmuneBuilder import ABodyBuilder2, NanoBodyBuilder2, TCRBuilder2
from ImmuneBuilder.util import are_weights_ready
import os, sys

names = ["antibody_model_%d" % i for i in (1, 2, 3, 4)]
names += ["nanobody_model_%d" % i for i in (1, 2, 3, 4)]
names += ["tcr_model_%d" % i for i in (1, 2, 3, 4)]
if os.environ.get("WITH_ORIGINAL") == "1":
    names += ["tcr2_model_%d" % i for i in (1, 2, 3, 4)]

print("构建器中（缺失的权重会按官方机制从 Zenodo 下载）...", flush=True)
ABodyBuilder2()
NanoBodyBuilder2()
TCRBuilder2()
if os.environ.get("WITH_ORIGINAL") == "1":
    TCRBuilder2(use_TCRBuilder2_PLUS_weights=False)

missing = [n for n in names if not are_weights_ready(os.path.join("/weights", n))]
print("WEIGHTS_TOTAL=%d" % len(names))
print("WEIGHTS_MISSING=%s" % (missing if missing else "none"))
sys.exit(1 if missing else 0)
'

echo "==> 临时容器预热权重（官方 Zenodo 下载，weights 以 :rw 挂载）"
echo "    镜像: ${IMAGE_NAME}"
echo "    设备: cpu（下载与设备无关）"
[[ "${WITH_ORIGINAL}" == "1" ]] && echo "    含旧版 TCRBuilder2 权重（共 16 份）"

set +e
docker run --rm \
    --name "immunebuilder-sdaa-prewarm-$$" \
    --init \
    --user "${HOST_UID}:${HOST_GID}" \
    --workdir /tmp \
    -e HOME=/home/immunebuilder \
    -e PYTHONUNBUFFERED=1 \
    -e PYTHONDONTWRITEBYTECODE=1 \
    -e PYTHONPATH=/opt/openmm-py \
    -e IMMUNEBUILDER_DEVICE=cpu \
    -e IMMUNEBUILDER_OPENMM_PLATFORM=Reference \
    -e WITH_ORIGINAL="${WITH_ORIGINAL}" \
    -e LD_LIBRARY_PATH=/opt/openmm/lib:/opt/openmm/lib/plugins:/opt/tecoai/lib64:/usr/local/lib:/usr/local/python/lib \
    -e OPENMM_PLUGIN_DIR=/opt/openmm/lib/plugins \
    -e TMPDIR=/tmp \
    -v "${ROOT}/weights":/weights:rw \
    -v "${OPENMM_INSTALL}":/opt/openmm:ro \
    -v "${OPENMM_SITE_PACKAGES}":/opt/openmm-py:ro \
    "${IMAGE_NAME}" \
    python -c "${INNER_PY}"
rc=$?
set -e

echo
echo "==> weights/ 现状"
echo "    文件数: $(find "${ROOT}/weights" -maxdepth 1 -type f ! -name 'manifest.tsv' ! -name 'weights.sha256' | wc -l)"
echo "    总大小: $(du -sh --apparent-size "${ROOT}/weights" | cut -f1)"
ls -l "${ROOT}/weights" | sed 's/^/    /'

if [[ ${rc} -ne 0 ]]; then
    echo
    echo "ERROR: 仍有权重缺失（见上面的 WEIGHTS_MISSING）。" >&2
    echo "       常见原因：Zenodo 链路慢或中断 —— 见 LOONG_SDAA_REPRO.md 第 7 节。" >&2
    echo "       注意：中断留下的残缺文件会被 are_weights_ready() 当成有效，" >&2
    echo "             需要先删掉尺寸明显偏小的文件再重跑。" >&2
    exit ${rc}
fi

echo
echo "==> 预热完成。之后正常容器可以全程只读运行：./scripts/run-container.sh"
