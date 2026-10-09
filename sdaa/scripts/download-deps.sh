#!/usr/bin/env bash
# 下载 / 准备 SDAA 侧构建所需的固定输入。
#
# 产出（全部落在 downloads/，供 Dockerfile COPY）：
#   1. hmmer-3.4.tar.gz          —— HMMER 原生源码（**当前未用于构建**，保留备查，见下方说明）
#   2. tecofft-0.1.0-1.loongarch64.rpm —— libOpenMMSDAA.so 的硬依赖
#   3. hmmer-x86/                —— Debian amd64 的 HMMER 3.4 + 其运行库（经 qemu-x86_64 执行）
#
# 关于 HMMER 的重要决策（详见 SDAA_MIGRATION.md「重大问题记录」）：
#   HMMER 3.4 的 configure 强制要求 SSE / NEON / VMX 之一的向量实现，没有标量回退；
#   而龙芯 LoongArch 提供的是 LSX / LASX，GCC 也不提供 SSE 内联函数兼容层
#   （实测 -msse2 不被识别、emmintrin.h 不存在）。原生移植需要把
#   src/impl_sse/* 与 easel/esl_sse.h 的约 60 个 _mm_* 内联函数重写到 LSX，
#   属于大型工程。
#   因此当前采用「Debian amd64 HMMER + qemu-x86_64 用户态仿真」，
#   复用龙芯 CPU 的 x86 二进制翻译能力（/proc/cpuinfo 含 lbt_x86），
#   以保证 ANARCI 需要的 hmmscan 可用。原生 LSX 移植列为后续优化项。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DL="${ROOT}/downloads"
mkdir -p "${DL}"

DEBIAN_MIRROR="${DEBIAN_MIRROR:-https://mirrors.tuna.tsinghua.edu.cn/debian/pool/main}"
HMMER_VERSION=3.4

# ---------------------------------------------------------------- HMMER 源码
fetch() {
    local url="$1" out="$2"
    echo "  trying: ${url}"
    if curl -fsSL --max-time 180 -o "${out}" "${url}"; then
        echo "  OK -> ${out} ($(stat -c%s "${out}") bytes)"
        return 0
    fi
    echo "  FAILED"
    rm -f "${out}"
    return 1
}

echo "==> 1/3 HMMER ${HMMER_VERSION} 源码（保留）"
TARGET="${DL}/hmmer-3.4.tar.gz"
if [[ -s "${TARGET}" ]]; then
    echo "  already present: ${TARGET}"
else
    TMPXZ="$(mktemp "${DL}/hmmer-XXXXXX.tar.xz")"
    ok=0
    for u in \
        "${DEBIAN_MIRROR}/h/hmmer/hmmer_${HMMER_VERSION}+dfsg.orig.tar.xz" \
        "https://mirrors.aliyun.com/debian/pool/main/h/hmmer/hmmer_${HMMER_VERSION}+dfsg.orig.tar.xz" \
        "https://codeload.github.com/EddyRivasLab/hmmer/tar.gz/refs/tags/hmmer-${HMMER_VERSION}" \
        ; do
        if fetch "$u" "${TMPXZ}"; then ok=1; break; fi
    done
    [[ "${ok}" -eq 1 ]] || { echo "ERROR: 无法获取 HMMER 源码" >&2; exit 1; }
    tmpdir="$(mktemp -d)"
    tar -xf "${TMPXZ}" -C "${tmpdir}"
    top="$(find "${tmpdir}" -maxdepth 1 -mindepth 1 -type d | head -1)"
    [[ -n "${top}" ]] || { echo "ERROR: 解包后未找到顶层目录" >&2; exit 1; }
    if [[ "$(basename "${top}")" != "hmmer-${HMMER_VERSION}" ]]; then
        mv "${top}" "${tmpdir}/hmmer-${HMMER_VERSION}"
    fi
    tar -czf "${TARGET}" -C "${tmpdir}" "hmmer-${HMMER_VERSION}"
    rm -rf "${tmpdir}" "${TMPXZ}"
fi
HMMER_SHA="$(sha256sum "${TARGET}" | awk '{print $1}')"
echo "  sha256 = ${HMMER_SHA}"

ENV_FILE="${ROOT}/.env.sdaa"
if [[ -f "${ENV_FILE}" ]]; then
    if grep -q '^HMMER_SHA256=' "${ENV_FILE}"; then
        sed -i "s|^HMMER_SHA256=.*|HMMER_SHA256=${HMMER_SHA}|" "${ENV_FILE}"
    else
        printf 'HMMER_SHA256=%s\n' "${HMMER_SHA}" >> "${ENV_FILE}"
    fi
fi

# ------------------------------------------------------------- tecofft 0.1.0
echo "==> 2/3 tecofft RPM（必须与所用 OpenMM 构建配套）"
# v2 的 tecofft 与 v1 是两个不同构建，sha256 不同：
#   v2 (当前) 46a2c6213e6bb2c23a17cc2e514ae7635213fdc19bfea3be620a9e4dc1327847
#   v1 (旧)   88a3674c4b4ce451a8ef47cda92a56acd1147ecb2fa15c7a72b38cf72a819e90
OPENMM_V2_ROOT="${OPENMM_V2_ROOT:-$(cd "${ROOT}/.." && pwd)/Openmm-sdaa-v2}"
TECO_SRC="${OPENMM_V2_ROOT}/downloads/tecofft-0.1.0-1.loongarch64.rpm"
TECO_DST="${DL}/tecofft-0.1.0-1.loongarch64.rpm"
TECO_SHA_EXPECT=46a2c6213e6bb2c23a17cc2e514ae7635213fdc19bfea3be620a9e4dc1327847
if [[ -f "${TECO_DST}" ]] && printf '%s  %s\n' "${TECO_SHA_EXPECT}" "${TECO_DST}" | sha256sum -c - >/dev/null 2>&1; then
    echo "  already present and matching: ${TECO_DST}"
elif [[ -f "${TECO_SRC}" ]]; then
    cp -f "${TECO_SRC}" "${TECO_DST}"
else
    echo "ERROR: 缺少 ${TECO_SRC}" >&2; exit 1
fi
printf '%s  %s\n' "${TECO_SHA_EXPECT}" "${TECO_DST}" | sha256sum -c -

# ----------------------------------------------- x86 HMMER + amd64 运行库
echo "==> 3/3 x86_64 HMMER + amd64 运行库（经 qemu-x86_64 执行）"
XR="${DL}/hmmer-x86"
DEBS="${DL}/x86-debs"
mkdir -p "${DEBS}" "${XR}/sysroot"

pick() { curl -s --max-time 90 "${DEBIAN_MIRROR}/$1/" | grep -o -E "$2" | sort -u | tail -1; }
LIBC=$(pick g/glibc 'libc6_[0-9][^"]*_amd64\.deb')
GOMP=$(pick g/gcc-14 'libgomp1_[0-9][^"]*_amd64\.deb')
STDC=$(pick g/gcc-14 'libstdc\+\+6_[0-9][^"]*_amd64\.deb')
HMMER_DEB=$(pick h/hmmer 'hmmer_[0-9][^"]*_amd64\.deb')
for v in LIBC GOMP STDC HMMER_DEB; do
    eval "val=\${$v}"
    [[ -n "${val}" ]] || { echo "ERROR: 无法确定 $v 的包名" >&2; exit 1; }
    echo "  $v = ${val}"
done

deb_dl() {  # $1=pool-subdir $2=filename ; 幂等
    local out="${DEBS}/$2"
    if [[ -s "${out}" ]]; then echo "  cached $2"; return 0; fi
    curl -fsSL --max-time 300 -o "${out}" "${DEBIAN_MIRROR}/$1/$2" && echo "  OK $2 ($(stat -c%s "${out}") bytes)"
}
deb_dl g/glibc "${LIBC}"
deb_dl g/gcc-14 "${GOMP}"
deb_dl g/gcc-14 "${STDC}"
deb_dl h/hmmer "${HMMER_DEB}"

# 解包（幂等：先清空）
rm -rf "${XR}/sysroot" "${XR}/usr" "${XR}/bin"
mkdir -p "${XR}/sysroot"
for f in "${LIBC}" "${GOMP}" "${STDC}"; do
    ( cd "${XR}/sysroot" && ar x "${DEBS}/${f}" && tar -xf data.tar.* && rm -f data.tar.* control.tar.* debian-binary )
done
( cd "${XR}" && ar x "${DEBS}/${HMMER_DEB}" && tar -xf data.tar.* && rm -f data.tar.* control.tar.* debian-binary )
# Debian 的 /lib64 -> usr/lib64、/lib -> usr/lib，qemu 按 QEMU_LD_PREFIX 查找时需要这两个链接
ln -sfn usr/lib64 "${XR}/sysroot/lib64"
ln -sfn usr/lib   "${XR}/sysroot/lib"

# qemu-x86_64：**必须使用宿主 /usr/bin/qemu-x86_64（7.2.0，静态链接）**
# 原因（实测）：Loongnix os 源里的 qemu-user 8.2.0 在同样 sysroot 下会报
#   "hmmscan: symbol lookup error: undefined symbol: fopen, version GLIBC_2.2.5"，
# 而宿主这份静态链接的 7.2.0 可以正常工作。见 SDAA_MIGRATION.md 重大问题记录。
QEMU_SRC=/usr/bin/qemu-x86_64
mkdir -p "${XR}/qemu"
if [[ -x "${QEMU_SRC}" ]]; then
    cp -f "${QEMU_SRC}" "${XR}/qemu/qemu-x86_64"
    chmod 0755 "${XR}/qemu/qemu-x86_64"
    echo "  qemu-x86_64 <- ${QEMU_SRC} ($("${XR}/qemu/qemu-x86_64" --version | head -1))"
    file "${XR}/qemu/qemu-x86_64" | sed 's/^/  /'
else
    echo "ERROR: 找不到 ${QEMU_SRC}（需要宿主的静态 qemu-x86_64）" >&2
    exit 1
fi

# 冒烟自检：用该 qemu 跑一次 amd64 hmmscan
if QEMU_LD_PREFIX="${XR}/sysroot" "${XR}/qemu/qemu-x86_64" "${XR}/usr/bin/hmmscan" -h >/dev/null 2>&1; then
    echo "  amd64 hmmscan via qemu: OK"
else
    echo "ERROR: amd64 hmmscan 在 qemu 下无法运行" >&2
    exit 1
fi

# 保留每个 .deb 的来源与校验值，作为可复现证据
{
    echo "# ImmuneBuilder SDAA: x86_64 HMMER 依赖来源（Debian mirror）"
    echo "# mirror=${DEBIAN_MIRROR}"
    echo "# generated=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    for f in "${LIBC}" "${GOMP}" "${STDC}" "${HMMER_DEB}"; do
        printf '%s  %s\n' "$(sha256sum "${DEBS}/${f}" | awk '{print $1}')" "${f}"
    done
} > "${XR}/SOURCES.sha256"
cat "${XR}/SOURCES.sha256"

echo "==> 完成"
ls -la "${DL}"
