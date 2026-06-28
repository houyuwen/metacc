#!/bin/bash
# ==============================================================================
# package.sh - Nuitka 高兼容单层扁平化打包脚本 (v10)
# 所有产物（执行文件、Nuitka 依赖 .so、打包机 libclang、metacc.h）
# 全部平铺放进 release/ 文件夹下。
# 发布结构：tools/metacc/release/
#   metacc          <- Nuitka 编译的可执行文件
#   metacc.h        <- 运行时需要的注释宏头文件
#   libclang.so     <- 打包机 libclang（与 release/ 内其他 .so 共存）
#   *.so            <- Nuitka 依赖的 Python 标准库 .so
# 编译完成后自动清理所有临时过程文件。
# ==============================================================================
set -e

# 确保脚本在其所在目录下绝对对齐执行，并定位项目根目录
SCRIPT_DIR="$(cd "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CDPATH="" cd -- "$SCRIPT_DIR"
# 发布产物目标目录（Nuitka standalone 平铺到 release/）
METACC_DIST_DIR="$SCRIPT_DIR/release"

echo "=============================================================================="
echo "[metacc-pack] Starting Pure Single-Folder Standalone Compilation Flow..."
echo "=============================================================================="

# 清理并初始化干净的输出目录与临时构建目录
rm -rf build_tmp "$METACC_DIST_DIR"
mkdir -p "$METACC_DIST_DIR"

# 激活私有 Python 隔离虚拟开发环境 (如果存在)
if [ -d ".venv" ]; then
    echo "[metacc-pack] Activating virtual environment (.venv)..."
    source .venv/bin/activate
elif [ -d "venv" ]; then
    echo "[metacc-pack] Activating virtual environment (venv)..."
    source venv/bin/activate
fi

# 确保安装了 Nuitka 编译器 (在已激活的 venv 中)
if ! python3 -m nuitka --version &> /dev/null; then
    echo "[metacc-pack] Nuitka compiler missing. Installing via pip..."
    pip install nuitka ordered-set
fi

echo ""
echo "------------------------------------------------------------------------------"
echo "[Step 1/4] Compiling Python script to Standalone Distribution..."
echo "------------------------------------------------------------------------------"

# 执行 Nuitka 独立目录模式编译
python3 -m nuitka \
    --standalone \
    --remove-output \
    --include-package=clang \
    --noinclude-pytest-mode=nofollow \
    --noinclude-setuptools-mode=nofollow \
    --no-deployment-flag=self-execution \
    --output-dir=build_tmp \
    --output-filename=metacc \
    metacc.py

# 整理产物：将独立目录内的所有文件全平铺转移到 release/ 中
echo "[metacc-pack] Moving all standalone artifacts directly to $METACC_DIST_DIR/..."
mv build_tmp/metacc.dist/* "$METACC_DIST_DIR/"

echo "[metacc-pack] Removing duplicated clang/native libclang payloads..."
if [ -d "$METACC_DIST_DIR/clang/native" ]; then
    find "$METACC_DIST_DIR/clang/native" -maxdepth 1 -type f -name 'libclang*' -exec rm -f {} +
    rmdir "$METACC_DIST_DIR/clang/native" 2>/dev/null || true
    rmdir "$METACC_DIST_DIR/clang" 2>/dev/null || true
fi

echo ""
echo "------------------------------------------------------------------------------"
echo "[Step 2/4] Copying metacc.h into release/..."
echo "------------------------------------------------------------------------------"

if [ -f "$SCRIPT_DIR/metacc.h" ]; then
    cp "$SCRIPT_DIR/metacc.h" "$METACC_DIST_DIR/"
    echo "[metacc-pack] metacc.h copied to $METACC_DIST_DIR/"
else
    echo "[metacc-pack] WARNING: metacc.h not found at $SCRIPT_DIR/metacc.h" >&2
fi

echo ""
echo "------------------------------------------------------------------------------"
echo "[Step 3/4] Auto-discovering and capturing host libclang..."
echo "------------------------------------------------------------------------------"

# 利用 Python 脚本精准定位打包机器当前正在使用的 libclang 真实物理路径
HOST_LIBCLANG=$(python3 -c "
import os, pathlib, sys
lib_ext = '.dll' if sys.platform.startswith('win') else ('.dylib' if sys.platform == 'darwin' else '.so')
candidates = []
env_path = os.getenv('METACC_LIBCLANG')
if env_path: candidates.append(pathlib.Path(env_path))
for v in range(14, 23): candidates.append(pathlib.Path(f'/usr/lib/llvm-{v}/lib/libclang{lib_ext}'))
candidates.extend([pathlib.Path(f'/usr/local/lib/libclang{lib_ext}'), pathlib.Path(f'/opt/homebrew/lib/libclang{lib_ext}')])
try:
    import clang
    candidates.append(pathlib.Path(clang.__file__).resolve().parent / 'native' / f'libclang{lib_ext}')
except: pass
found = ''
for c in candidates:
    if c.is_file():
        found = str(c.resolve())
        break
print(found)
")

if [ -n "$HOST_LIBCLANG" ] && [ -f "$HOST_LIBCLANG" ]; then
    echo "[metacc-pack] Found active host libclang: $HOST_LIBCLANG"

    # 复制到 release/（打包产物开箱即用）
    echo "[metacc-pack] Copying libclang to $METACC_DIST_DIR/libclang.so ..."
    cp "$HOST_LIBCLANG" "$METACC_DIST_DIR/libclang.so"
    rm -f "$METACC_DIST_DIR"/libclang-*.so*

    echo ">> [SUCCESS] libclang placed at $METACC_DIST_DIR/libclang.so"
else
    echo "[metacc-pack] WARNING: Could not automatically locate libclang on this machine." >&2
    echo "[metacc-pack] Please manually copy your local libclang to release/libclang.so later if needed." >&2
fi

echo ""
echo "------------------------------------------------------------------------------"
echo "[Step 4/4] Post-build cleaning..."
echo "------------------------------------------------------------------------------"
# 彻底清理打包生成的过程临时文件
echo "[metacc-pack] Cleaning up intermediate build process directories..."
rm -rf build_tmp

echo ""
echo "=============================================================================="
echo "[metacc-pack] PACKAGING COMPLETED SUCCESSFULLY!"
echo ">> 发布目录:       $METACC_DIST_DIR/"
echo ">> 可执行入口:     $METACC_DIST_DIR/metacc"
echo ">> 运行命令:       tools/metacc/release/metacc -c <compile_commands> -p <project_root>"
echo ">> 运行时依赖:     $METACC_DIST_DIR/libclang.so  (all .so files co-located)"
echo "=============================================================================="
