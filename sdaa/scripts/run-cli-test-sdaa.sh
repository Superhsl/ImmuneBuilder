#!/usr/bin/env bash
# 官方 CLI 入口端到端验收（ABodyBuilder2 / NanoBodyBuilder2 / TCRBuilder2）
#
# 与 run-smoke-test-sdaa.sh 的区别：
#   后者验证 Python API（`ABodyBuilder2().predict()`），本脚本验证**官方安装的
#   命令行入口**（console_scripts），即上游 README 里给用户的实际用法。
#
# 用法：
#   ./scripts/run-cli-test-sdaa.sh              # 默认 CPU 设备 + Reference 精修（最快）
#   ./scripts/run-cli-test-sdaa.sh sdaa         # SDAA 设备 + Reference 精修
#   ./scripts/run-cli-test-sdaa.sh sdaa SDAA    # SDAA 设备 + SDAA 精修
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

# 读取项目根的 .env.sdaa（容器名等）；已导出的环境变量优先
if [[ -f ./.env.sdaa ]]; then
    # shellcheck disable=SC1091
    set -a; source ./.env.sdaa; set +a
fi

DEVICE="${1:-cpu}"
REFINE_PLATFORM="${2:-Reference}"
CONTAINER="${CONTAINER_NAME:-immunebuilder-sdaa}"

case "${DEVICE}" in
    cpu|sdaa|cuda) ;;
    *) echo "用法: $0 [cpu|sdaa|cuda] [Reference|SDAA|auto]" >&2; exit 2 ;;
esac

if ! docker container inspect "${CONTAINER}" >/dev/null 2>&1; then
    echo "ERROR: 容器 ${CONTAINER} 不存在，请先运行 scripts/build-image.sh 与 scripts/run-container.sh" >&2
    exit 1
fi

OUTDIR="work/cli_test/${DEVICE}"
mkdir -p "${OUTDIR}"

# 官方 README / 输入示例序列
cat > "${OUTDIR}/ab.fasta" <<'FA'
>H
EVQLVESGGGVVQPGGSLRLSCAASGFTFNSYGMHWVRQAPGKGLEWVAFIRYDGGNKYYADSVKGRFTISRDNSKNTLYLQMKSLRAEDTAVYYCANLKDSRYSGSYYDYWGQGTLVTVS
>L
VIWMTQSPSSLSASVGDRVTITCQASQDIRFYLNWYQQKPGKAPKLLISDASNMETGVPSRFSGSGSGTDFTFTISSLQPEDIATYYCQQYDNLPFTFGPGTKVDFK
FA
cat > "${OUTDIR}/nb.fasta" <<'FA'
>H
QVQLQESGGGLVQAGGSLRLSCAASGRTFSSYAMGWFRQAPGKEREFVAAISWSGGSTYYADSVKGRFTISRDNAKNTVYLQMNSLKPEDTAVYYCAAAGLGTVVSEWDYDYDYWGQGTQVTVSS
FA
cat > "${OUTDIR}/tcr.fasta" <<'FA'
>B
NAGVTQTPKFQVLKTGQSMTLQCAQDMNHEYMSWYRQDPGMGLRLIHYSVGAGITDQGEVPNGYNVSRSTTEDFPLRLLSAAPSQTSVYFCASSYVGNTGELFFGEGSRLTVL
>A
AQEVTQIPAALSVPEGENLVLNCSFTDSAIYNLQWFRQDPGKGLTSLLLIQSSQREQTSGRLNASLDKSSGRSTLYIAASQPGDSATYLCAVRPTSGGSYIPTFGRGTSLIVHPY
FA

INNER="/tmp/cli_test_${DEVICE}"
docker exec "${CONTAINER}" rm -rf "${INNER}" >/dev/null 2>&1 || true
docker exec "${CONTAINER}" mkdir -p "${INNER}" >/dev/null
for f in ab.fasta nb.fasta tcr.fasta; do
    docker cp "${OUTDIR}/${f}" "${CONTAINER}:${INNER}/${f}" >/dev/null
done

cat > "${OUTDIR}/run.sh" <<INNEREOF
#!/usr/bin/env bash
# 在容器内执行
set -u
cd "${INNER}"
run() {
  name="\$1"; shift
  echo "===== \$name ====="
  t0=\$(date +%s)
  "\$@" > "\$name.out" 2> "\$name.err"
  rc=\$?
  t1=\$(date +%s)
  atoms=\$(grep -c '^ATOM' "\$name.pdb" 2>/dev/null || echo 0)
  printf '%s rc=%s elapsed=%ss atoms=%s\n' "\$name" "\$rc" "\$((t1-t0))" "\$atoms"
}
run ABodyBuilder2    ABodyBuilder2    -f ab.fasta  -o ABodyBuilder2.pdb  -v
run NanoBodyBuilder2 NanoBodyBuilder2 -f nb.fasta  -o NanoBodyBuilder2.pdb -v
run TCRBuilder2      TCRBuilder2      -f tcr.fasta -o TCRBuilder2.pdb -v
echo "===== 精修失败检查 ====="
if grep -l "FAILED TO REFINE" *.out 2>/dev/null; then
  echo "REFINE_FAILED"
else
  echo "NO_REFINE_FAILURE"
fi
echo "===== 头部标记 ====="
for f in ABodyBuilder2 NanoBodyBuilder2 TCRBuilder2; do head -1 "\$f.pdb"; done
echo "CLI_TEST_DONE"
INNEREOF
chmod +x "${OUTDIR}/run.sh"
docker cp "${OUTDIR}/run.sh" "${CONTAINER}:${INNER}/run.sh" >/dev/null

echo "==> 官方 CLI 验收 device=${DEVICE} refine_platform=${REFINE_PLATFORM}"
echo "    注意：完整精修较慢（antibody 数分钟量级），本命令可能需要较长时间"
docker exec \
    -e "IMMUNEBUILDER_DEVICE=${DEVICE}" \
    -e "IMMUNEBUILDER_OPENMM_PLATFORM=${REFINE_PLATFORM}" \
    "${CONTAINER}" bash "${INNER}/run.sh" 2>&1 | tee "${OUTDIR}/cli.log"

# 回收到宿主机，便于留档
for f in ABodyBuilder2.pdb NanoBodyBuilder2.pdb TCRBuilder2.pdb; do
    docker cp "${CONTAINER}:${INNER}/${f}" "${OUTDIR}/${f}" >/dev/null 2>&1 || true
done

echo
echo "==> 判定"
grep -q 'CLI_TEST_DONE' "${OUTDIR}/cli.log" || { echo "CLI 测试未正常结束" >&2; exit 1; }
if grep -q 'REFINE_FAILED' "${OUTDIR}/cli.log"; then
    echo "存在精修失败，详见 ${OUTDIR}/cli.log" >&2
    exit 1
fi
echo "官方三个 CLI 入口全部通过：${OUTDIR}"
