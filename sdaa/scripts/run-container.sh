#!/usr/bin/env bash
# 启动/复用 ImmuneBuilder SDAA 容器（等价于 compose.sdaa.yaml）
#
# 本服务器无 docker compose 插件，故以 docker run 等价实现。
# 关键点：
#   * 固定宿主机 UID/GID（避免产出 root 属主文件）
#   * 映射 /dev/tcaicard*（SDAA 无 NVIDIA gpus 语义）
#   * OpenMM 只读复用宿主已编译产物（当前为 Openmm-sdaa-v2 的 CPU+SDAA 构建）
#
# 挂载策略（源码只读、权重只读、只有输出目录可写）：
#   ${ROOT}          -> /workspace/repro        :ro   源码/脚本/输入，只读
#   ${ROOT}/work     -> /workspace/repro/work   :rw   唯一可写输出目录
#   ${ROOT}/weights  -> /weights                :ro   模型权重，只读
#   并且把容器工作目录设为 /workspace/repro/work：
#       上游 refine.py 会把 side_chain_fix_tmp_<rand>.pdb 写到**进程当前目录**，
#       项目根一旦只读，就必须让 CWD 落在可写目录，否则精修会失败。
#
# 权重只读的代价：官方首次下载（ImmuneBuilder.util.download_file 写 /weights）在
# 正常容器里跑不了。首次请用 scripts/prewarm-weights-sdaa.sh 预热一次（临时 :rw），
# 之后推理全程只读、可离线。
#
# OpenMM v2 接入的两个要点（详见 SDAA_MIGRATION.md「OpenMM v2 接入」）：
#   1. LD_LIBRARY_PATH 必须同时含 lib 与 lib/plugins
#      —— v2 的 libOpenMMSDAA.so 通过 NEEDED 链接 libOpenMMCPU.so，而后者装在
#         plugins 目录里。只写 lib 会被 ldd 报 not found（实际靠 dlopen 侥幸可用）。
#   2. 换 OpenMM 构建后必须清 SDAA JIT 缓存
#      —— fatbin 是按平台代码 JIT 的，跨构建复用会命中不匹配的内核。
#         这里用 CACHE_STAMP 记录构建路径，不一致时自动清空并告警。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

# shellcheck disable=SC1091
set -a; source ./.env.sdaa; set +a

: "${IMAGE_NAME:?}" "${CONTAINER_NAME:?}" "${HOST_UID:?}" "${HOST_GID:?}"
: "${OPENMM_INSTALL:?}" "${OPENMM_SITE_PACKAGES:?}"

CACHE_DIR="${ROOT}/work/sdaa-cache"
CACHE_STAMP="${CACHE_DIR}/.openmm_build_stamp"
mkdir -p "${CACHE_DIR}"
chmod 0775 "${CACHE_DIR}"
# weights 必须在 docker run 之前存在，否则 docker 会以 root 属主创建它
mkdir -p "${ROOT}/weights"

# 构建来源指纹：任一 OpenMM 挂载源变化即视为换了构建
STAMP_VALUE="${OPENMM_INSTALL}|${OPENMM_SITE_PACKAGES}"
if [[ -f "${CACHE_STAMP}" ]]; then
    if [[ "$(cat "${CACHE_STAMP}")" != "${STAMP_VALUE}" ]]; then
        echo "==> 检测到 OpenMM 构建已更换，清空 SDAA JIT 缓存（避免命中旧 fatbin）"
        find "${CACHE_DIR}" -mindepth 1 ! -name '.openmm_build_stamp' -delete
    fi
else
    echo "==> 首次使用该 OpenMM 构建，清空历史 SDAA JIT 缓存"
    find "${CACHE_DIR}" -mindepth 1 ! -name '.openmm_build_stamp' -delete
fi
printf '%s\n' "${STAMP_VALUE}" > "${CACHE_STAMP}"

for p in "${OPENMM_INSTALL}" "${OPENMM_SITE_PACKAGES}"; do
    if [[ ! -d "${p}" ]]; then
        echo "ERROR: 复用的 OpenMM 路径不存在: ${p}" >&2
        exit 1
    fi
done
# v2 要求 plugins 目录也在 LD_LIBRARY_PATH 中
if [[ ! -f "${OPENMM_INSTALL}/lib/plugins/libOpenMMCPU.so" && ! -f "${OPENMM_INSTALL}/lib/plugins/libOpenMMSDAA.so" ]]; then
    echo "WARNING: ${OPENMM_INSTALL}/lib/plugins 下未找到平台插件，请检查挂载源" >&2
fi

if docker container inspect "${CONTAINER_NAME}" >/dev/null 2>&1; then
    state="$(docker container inspect "${CONTAINER_NAME}" --format '{{.State.Status}}')"
    if [[ "${state}" != "running" ]]; then
        echo "==> 启动已存在容器 ${CONTAINER_NAME}（原状态 ${state}）"
        docker start "${CONTAINER_NAME}" >/dev/null
    else
        echo "==> 容器 ${CONTAINER_NAME} 已在运行"
    fi
    # 挂载配置无法在既有容器上更改：发现与当前"只读"策略不一致时只告警，不自动删容器
    repro_rw="$(docker container inspect "${CONTAINER_NAME}" \
        --format '{{range .Mounts}}{{if eq .Destination "/workspace/repro"}}{{.RW}}{{end}}{{end}}')"
    weights_rw="$(docker container inspect "${CONTAINER_NAME}" \
        --format '{{range .Mounts}}{{if eq .Destination "/weights"}}{{.RW}}{{end}}{{end}}')"
    workdir="$(docker container inspect "${CONTAINER_NAME}" --format '{{.Config.WorkingDir}}')"
    if [[ "${repro_rw}" != "false" || "${weights_rw}" != "false" || "${workdir}" != "/workspace/repro/work" ]]; then
        echo "WARNING: 容器 ${CONTAINER_NAME} 是用旧挂载创建的，新的只读策略不会生效。" >&2
        echo "         实际: /workspace/repro rw=${repro_rw}  /weights rw=${weights_rw}  workdir=${workdir}" >&2
        echo "         期望: /workspace/repro rw=false  /weights rw=false  workdir=/workspace/repro/work" >&2
        echo "         如需生效（容器无状态，产物都在宿主 work/ 里，删容器不丢数据）：" >&2
        echo "             docker rm -f ${CONTAINER_NAME} && ./scripts/run-container.sh" >&2
    fi
    docker exec "${CONTAINER_NAME}" true
    echo "==> OK"
    exit 0
fi

echo "==> 创建容器 ${CONTAINER_NAME}"
docker run -d \
    --name "${CONTAINER_NAME}" \
    --init \
    --user "${HOST_UID}:${HOST_GID}" \
    --workdir /workspace/repro/work \
    --shm-size 2gb \
    --device "${SDAA_DEVICE_NODE}:/dev/tcaicard0:rwm" \
    -e HOME=/home/immunebuilder \
    -e PYTHONUNBUFFERED=1 \
    -e PYTHONDONTWRITEBYTECODE=1 \
    -e PYTHONPATH=/opt/openmm-py:/workspace/repro \
    -e SDAA_VISIBLE_DEVICES="${SDAA_VISIBLE_DEVICES}" \
    -e SDAA_HOME=/opt/tecoai \
    -e IMMUNEBUILDER_DEVICE="${IMMUNEBUILDER_DEVICE}" \
    -e IMMUNEBUILDER_OPENMM_PLATFORM="${IMMUNEBUILDER_OPENMM_PLATFORM}" \
    -e IMMUNEBUILDER_OPENMM_PROPERTIES="${IMMUNEBUILDER_OPENMM_PROPERTIES:-}" \
    -e OPENMM_CPU_THREADS="${OPENMM_CPU_THREADS:-}" \
    -e LD_LIBRARY_PATH=/opt/openmm/lib:/opt/openmm/lib/plugins:/opt/tecoai/lib64:/usr/local/lib:/usr/local/python/lib \
    -e OPENMM_PLUGIN_DIR=/opt/openmm/lib/plugins \
    -e XDG_CACHE_HOME=/tmp/.cache \
    -e MPLCONFIGDIR=/tmp/matplotlib \
    -e TMPDIR=/workspace/sdaa-cache \
    -v "${ROOT}":/workspace/repro:ro \
    -v "${ROOT}/work":/workspace/repro/work:rw \
    -v "${ROOT}/weights":/weights:ro \
    -v "${OPENMM_INSTALL}":/opt/openmm:ro \
    -v "${OPENMM_SITE_PACKAGES}":/opt/openmm-py:ro \
    -v "${CACHE_DIR}":/workspace/sdaa-cache \
    "${IMAGE_NAME}" \
    sleep infinity >/dev/null

sleep 1
docker exec "${CONTAINER_NAME}" true
echo "==> OK: ${CONTAINER_NAME} ($(docker container inspect "${CONTAINER_NAME}" --format '{{.State.Status}}'))"
