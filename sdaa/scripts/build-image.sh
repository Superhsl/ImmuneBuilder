#!/usr/bin/env bash
# 构建 ImmuneBuilder SDAA 镜像（基础镜像已用 Image ID 固定）
#
# 注意：本服务器未安装 docker compose 插件，因此使用 docker build。
# compose.sdaa.yaml 仅作为等价声明（若日后安装 compose 可直接使用）。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

# shellcheck disable=SC1091
set -a; source ./.env.sdaa; set +a

: "${BASE_IMAGE:?BASE_IMAGE 未设置}"
: "${BASE_IMAGE_ID:?BASE_IMAGE_ID 未设置}"
: "${IMAGE_NAME:?IMAGE_NAME 未设置}"
: "${HOST_UID:?HOST_UID 未设置}"
: "${HOST_GID:?HOST_GID 未设置}"

# HMMER_SHA256 允许缺失：缺失时从本地 tarball 自动回填 .env.sdaa（避免手工同步漂移）
if [[ -z "${HMMER_SHA256:-}" || "${HMMER_SHA256}" == "UNSET" ]]; then
    tarball="downloads/hmmer-3.4.tar.gz"
    if [[ ! -f "${tarball}" ]]; then
        echo "ERROR: HMMER_SHA256 未设置且 ${tarball} 不存在，请先运行 scripts/download-deps.sh" >&2
        exit 1
    fi
    HMMER_SHA256="$(sha256sum "${tarball}" | awk '{print $1}')"
    if grep -q '^HMMER_SHA256=' ./.env.sdaa; then
        sed -i "s|^HMMER_SHA256=.*|HMMER_SHA256=${HMMER_SHA256}|" ./.env.sdaa
    else
        printf 'HMMER_SHA256=%s\n' "${HMMER_SHA256}" >> ./.env.sdaa
    fi
    echo "==> HMMER_SHA256 由本地 tarball 回填: ${HMMER_SHA256}"
fi

echo "==> 校验基础镜像 ${BASE_IMAGE} 的 Image ID"
if ! docker image inspect "${BASE_IMAGE}" >/dev/null 2>&1; then
    echo "ERROR: 基础镜像不存在: ${BASE_IMAGE}" >&2
    exit 1
fi
actual_id="$(docker image inspect "${BASE_IMAGE}" --format '{{.Id}}')"
if [[ "${actual_id}" != "${BASE_IMAGE_ID}" ]]; then
    printf 'Refusing to build: base image ID mismatch.\nExpected: %s\nActual:   %s\n' \
        "${BASE_IMAGE_ID}" "${actual_id}" >&2
    exit 1
fi
echo "    OK: ${actual_id}"

echo "==> 构建 ${IMAGE_NAME}"
# --pull=false 是必需的：基础镜像只存在于本地，联网拉取会失败
docker build \
    --pull=false \
    --file Dockerfile.sdaa \
    --tag "${IMAGE_NAME}" \
    --build-arg "BASE_IMAGE=${BASE_IMAGE}" \
    --build-arg "HOST_UID=${HOST_UID}" \
    --build-arg "HOST_GID=${HOST_GID}" \
    --build-arg "HMMER_SHA256=${HMMER_SHA256}" \
    --label "org.opencontainers.image.title=ImmuneBuilder SDAA (LoongArch64) runtime" \
    --label "org.opencontainers.image.source=https://github.com/oxpig/ImmuneBuilder" \
    --label "org.opencontainers.image.revision=0df4e2ad82a1aa60f37ea9dae335d1198159ef78" \
    --label "org.opencontainers.image.accelerator=tecorigin-sdaa" \
    --label "org.opencontainers.image.base.name=${BASE_IMAGE}" \
    --label "org.opencontainers.image.base.digest=${BASE_IMAGE_ID}" \
    .

echo "==> 镜像 ID"
docker image inspect "${IMAGE_NAME}" --format '{{.Id}}'
echo "==> 完成"
