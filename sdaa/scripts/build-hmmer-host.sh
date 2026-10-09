#!/usr/bin/env bash
# 在宿主机（无需 root）用「下载 RPM + 解包到本地前缀」的方式取得 autotools，
# 然后编译 HMMER 3.4。产物放到项目 downloads/hmmer-prefix。
#
# 这样做的原因：基础镜像内 yum install 在本机环境下不稳定，
# 而 HMMER 自带 configure 不能识别 loongarch64，必须有较新的 autotools。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DL="${ROOT}/downloads"
AT="${DL}/autotools"
SRC="${DL}/hmmer-3.4.tar.gz"
OUT="${DL}/hmmer-prefix"

for t in rpm2cpio cpio; do
    command -v "$t" >/dev/null || { echo "ERROR: 缺少 $t" >&2; exit 1; }
done

echo "==> 1. 下载 autotools RPM（不安装到系统）"
mkdir -p "${AT}"
# * perl 主包：本机只装了 perl-libs，缺少 File::Compare / threads / Thread::Queue
#   等 perl 核心模块，而 autoconf 的 autom4te 需要它们。
# * perl-threads 为 threads 的架构相关部分。
( cd "${AT}" && rm -f ./*.rpm && dnf download autoconf automake libtool m4 perl perl-threads >/dev/null )
ls -1 "${AT}"/*.rpm

echo "==> 2. 解包到本地前缀 ${AT}/prefix"
PREFIX="${AT}/prefix"
rm -rf "${PREFIX}"; mkdir -p "${PREFIX}"
for r in "${AT}"/*.rpm; do
    ( cd "${PREFIX}" && rpm2cpio "$r" | cpio -idmu --quiet )
done
export PATH="${PREFIX}/usr/bin:${PREFIX}/bin:${PATH}"
# autoconf 的 autom4te 脚本把路径写死为 /usr/bin/autom4te，且其 perl 模块在
# …/usr/share/autoconf 下，必须显式指定，否则会去系统路径找不到。
export AUTOM4TE="${PREFIX}/usr/bin/autom4te"
export AUTOCONF="${PREFIX}/usr/bin/autoconf"
export AUTOHEADER="${PREFIX}/usr/bin/autoheader"
# 解包出来的 perl 核心模块 + automake 模块目录
PERL_CORE_SHARE="${PREFIX}/usr/share/perl5"
PERL_CORE_ARCH="$(dirname "$(find "${PREFIX}/usr/lib64/perl5" -maxdepth 2 -name 'threads.pm' 2>/dev/null | head -1)")"
export PERL5LIB="${PERL_CORE_SHARE}:${PERL_CORE_ARCH}:${PREFIX}/usr/share/autoconf:${PREFIX}/usr/share/automake-1.16${PERL5LIB:+:${PERL5LIB}}"

echo "==> 2b. 自检 autoconf 可用（automake 在本机缺 perl 模块，故不使用 autoreconf）"
echo 'AC_INIT([t],[1.0]) AC_PROG_CC AC_OUTPUT' > /tmp/at-selftest.ac
( cd /tmp && autoconf at-selftest.ac ) || { echo "ERROR: autoconf 不可用" >&2; exit 1; }
echo "    autoconf OK: $(autoconf --version | head -1)"

echo "==> 3. 解包 HMMER"
BUILD="$(mktemp -d)"
tar -xzf "${SRC}" -C "${BUILD}"
cd "${BUILD}/hmmer-3.4"

echo "==> 4. 给 config.guess 注入 loongarch64 识别 + 生成 configure"
# HMMER 3.4 自带的 config.guess（2018-03-08）不认识 loongarch64；
# 自带的 config.sub 接受 loongarch64-unknown-linux-gnu。
patch_config_guess() {
    local f="$1"
    awk '
    {
        print
        if ($0 ~ /^timestamp=/) {
            print ""
            print "# --- loongarch64 support injected for ImmuneBuilder SDAA migration ---"
            print "case \"${UNAME_MACHINE}\" in"
            print "    loongarch64|loongarch32)"
            print "        echo \"${UNAME_MACHINE}-unknown-linux-gnu\"; exit ;;"
            print "esac"
            print "# --- end injected block ---"
        }
    }' "$f" > "${f}.new" && mv "${f}.new" "$f"
    chmod +x "$f"
}
patch_config_guess config.guess
[[ -f easel/config.guess ]] && patch_config_guess easel/config.guess
echo "    config.guess -> $(./config.guess)"
echo "    config.sub   -> $(./config.sub "$(./config.guess)")"

autoconf >"${BUILD}/autoconf.log" 2>&1 || { tail -30 "${BUILD}/autoconf.log"; exit 1; }
( cd easel && autoconf >"${BUILD}/autoconf-easel.log" 2>&1 ) || { tail -30 "${BUILD}/autoconf-easel.log"; exit 1; }
test -x ./configure || { echo "ERROR: configure 未生成" >&2; exit 1; }
echo "    configure 已生成: $(ls -la configure | awk '{print $5}') bytes"

echo "==> 5. configure --prefix=${OUT}"
./configure --prefix="${OUT}" \
    --without-gsl --disable-mpi \
    --build=loongarch64-unknown-linux-gnu \
    --host=loongarch64-unknown-linux-gnu >"${BUILD}/configure.log" 2>&1 \
    || { tail -30 "${BUILD}/configure.log"; exit 1; }
tail -5 "${BUILD}/configure.log"

echo "==> 6. make -j$(nproc)"
make -j"$(nproc)" >"${BUILD}/make.log" 2>&1 || { tail -40 "${BUILD}/make.log"; exit 1; }
make install >"${BUILD}/install.log" 2>&1 || { tail -20 "${BUILD}/install.log"; exit 1; }

echo "==> 7. 验证"
"${OUT}/bin/hmmscan" -h | head -3
ls -la "${OUT}/bin"
echo "HMMER_BUILD_OK prefix=${OUT}"
rm -rf "${BUILD}"
