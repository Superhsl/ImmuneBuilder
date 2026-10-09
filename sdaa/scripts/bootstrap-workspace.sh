#!/usr/bin/env bash
# 在「项目根」建立 SDAA 复现所需的目录布局（幂等，可重复执行）。
#
# 为什么需要这一层：
#   适配材料放在 ImmuneBuilder 仓库里（ImmuneBuilder/sdaa/），而所有脚本都以
#   「脚本目录的上一级」作为项目根，并假定项目根旁边就有：
#       ImmuneBuilder/   官方源码仓库（含 setup.py）
#       weights/         模型权重（官方机制从 Zenodo 下载到这里）
#       downloads/       tecofft RPM、x86 HMMER + 静态 qemu
#       .env.sdaa        主机相关变量
#   因此项目根必须是**仓库的上一级目录**，再用符号链接把 sdaa/ 的材料挂上来。
#
# 用法：
#   cd <项目根>                                   # 仓库的上一层，例如 ~/ImmuneBuilder-sdaa
#   ./ImmuneBuilder/sdaa/scripts/bootstrap-workspace.sh
#
# 期望的输入布局：
#   <项目根>/ImmuneBuilder/          <- 本仓库（git clone 或拷贝而来）
#
# 幂等：已存在的文件不会被覆盖；已存在的符号链接会校验指向。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
SDAADIR="$(cd "${SCRIPT_DIR}/.." && pwd -P)"
REPO="$(cd "${SDAADIR}/.." && pwd -P)"
ROOT="$(cd "${REPO}/.." && pwd -P)"

if [[ ! -f "${SDAADIR}/Dockerfile.sdaa" || ! -d "${REPO}/ImmuneBuilder" || ! -f "${REPO}/setup.py" ]]; then
    echo "ERROR: 目录结构不符合预期。" >&2
    echo "       期望 <项目根>/ImmuneBuilder/{setup.py,ImmuneBuilder/,sdaa/}，" >&2
    echo "       实际解析: 项目根=${ROOT}  仓库=${REPO}  sdaa=${SDAADIR}" >&2
    exit 1
fi

echo "==> 项目根 : ${ROOT}"
echo "==> 仓库   : ${REPO}"
echo "==> sdaa/  : ${SDAADIR}"

link_one() {
    local rel="$1"                       # 项目根下的相对路径
    local target="ImmuneBuilder/sdaa/${rel}"   # 相对项目根的链接目标
    local path="${ROOT}/${rel}"
    if [[ -L "${path}" ]]; then
        if [[ "$(readlink "${path}")" == "${target}" ]]; then
            echo "    = ${rel} (已存在)"
        else
            echo "    ! ${rel} 已存在但指向 $(readlink "${path}")，跳过"
        fi
    elif [[ -e "${path}" ]]; then
        echo "    ! ${rel} 已存在且不是符号链接，跳过"
    else
        ln -s "${target}" "${path}"
        echo "    + ${rel} -> ${target}"
    fi
}

echo "==> 1. 链接适配材料到项目根"
link_one "scripts"
link_one "patches"
link_one "locks"
link_one "Dockerfile.sdaa"
link_one "compose.sdaa.yaml"
link_one "source.sdaa.lock"

echo "==> 2. 运行期目录"
for d in weights downloads work logs backups inputs; do
    mkdir -p "${ROOT}/${d}"
    echo "    + ${d}/"
done

echo "==> 3. .env.sdaa"
if [[ -f "${ROOT}/.env.sdaa" ]]; then
    echo "    = 已存在，不覆盖"
else
    uid="$(id -u)"; gid="$(id -g)"
    # OpenMM 预编译前缀默认按「项目根的上一层」推断，可用 .env.sdaa 覆盖
    openmm_root="${OPENMM_V2_ROOT:-$(dirname "${ROOT}")/Openmm-sdaa-v2}/work-v2"
    sed -e "s|^HOST_UID=.*|HOST_UID=${uid}|" \
        -e "s|^HOST_GID=.*|HOST_GID=${gid}|" \
        -e "s|<WORKSPACE>/Openmm-sdaa-v2|$(dirname "${ROOT}")/Openmm-sdaa-v2|" \
        "${SDAADIR}/.env.sdaa.example" > "${ROOT}/.env.sdaa"
    echo "    + .env.sdaa  (HOST_UID=${uid} HOST_GID=${gid})"
    echo "      OPENMM_INSTALL=${openmm_root}/install"
    echo "      ! 请确认 OPENMM_INSTALL / OPENMM_SITE_PACKAGES 指向真实的 OpenMM 前缀"
fi

echo "==> 4. .dockerignore（构建上下文 = 项目根）"
if [[ -f "${ROOT}/.dockerignore" ]]; then
    echo "    = 已存在，不覆盖"
else
    cat > "${ROOT}/.dockerignore" <<'EOF'
.git
.env
.env.sdaa
work
logs
backups
weights
downloads/hmmer-3.4.tar.gz
ImmuneBuilder/.git
ImmuneBuilder/data
ImmuneBuilder/notebook
EOF
    echo "    + .dockerignore"
fi

echo "==> 5. 官方示例输入 inputs/antibody.fasta"
if [[ -f "${ROOT}/inputs/antibody.fasta" ]]; then
    echo "    = 已存在，不覆盖"
else
    cat > "${ROOT}/inputs/antibody.fasta" <<'EOF'
>H
EVQLVESGGGVVQPGGSLRLSCAASGFTFNSYGMHWVRQAPGKGLEWVAFIRYDGGNKYYADSVKGRFTISRDNSKNTLYLQMKSLRAEDTAVYYCANLKDSRYSGSYYDYWGQGTLVTVS
>L
VIWMTQSPSSLSASVGDRVTITCQASQDIRFYLNWYQQKPGKAPKLLISDASNMETGVPSRFSGSGSGTDFTFTISSLQPEDIATYYCQQYDNLPFTFGPGTKVDFK
EOF
    echo "    + inputs/antibody.fasta"
fi

echo
echo "==> 完成。目录布局："
( cd "${ROOT}" && ls -la | sed 's/^/    /' )
echo
echo "下一步："
echo "    ./scripts/verify-patch.py                 # 确认源码适配已就位"
echo "    ./scripts/build-image.sh                  # 构建镜像"
echo "    ./scripts/run-container.sh                # 启动容器"
echo "    ./scripts/verify-environment-sdaa.sh      # 环境自检"
