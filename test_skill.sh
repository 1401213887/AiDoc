#!/bin/bash

# 测试 upload2git skill 的测试脚本

echo "=== upload2git Skill 测试 ==="
echo ""

# 测试 1: 检查脚本文件是否存在
echo "测试 1: 检查脚本文件..."
if [ -f "upload2git.sh" ]; then
    echo "✓ 脚本文件存在"
else
    echo "✗ 脚本文件不存在"
    exit 1
fi

# 测试 2: 检查脚本是否可执行
echo ""
echo "测试 2: 检查脚本权限..."
if [ -x "upload2git.sh" ]; then
    echo "✓ 脚本可执行"
else
    echo "⚠ 脚本不可执行，尝试添加执行权限..."
    chmod +x upload2git.sh
    if [ -x "upload2git.sh" ]; then
        echo "✓ 已添加执行权限"
    else
        echo "✗ 无法添加执行权限"
    fi
fi

# 测试 3: 检查脚本语法
echo ""
echo "测试 3: 检查脚本语法..."
if bash -n upload2git.sh 2>&1; then
    echo "✓ 脚本语法正确"
else
    echo "✗ 脚本语法错误"
    exit 1
fi

# 测试 4: 测试无参数调用（应该显示错误）
echo ""
echo "测试 4: 测试无参数调用..."
OUTPUT=$(./upload2git.sh 2>&1)
if echo "$OUTPUT" | grep -q "错误: 请指定要上传的文件路径"; then
    echo "✓ 无参数时正确显示错误信息"
else
    echo "⚠ 无参数测试结果:"
    echo "$OUTPUT" | head -3
fi

# 测试 5: 测试文件不存在的情况
echo ""
echo "测试 5: 测试文件不存在的情况..."
OUTPUT=$(./upload2git.sh /nonexistent/file.txt 2>&1)
if echo "$OUTPUT" | grep -q "警告: 文件不存在"; then
    echo "✓ 文件不存在时正确显示警告"
else
    echo "⚠ 文件不存在测试结果:"
    echo "$OUTPUT" | head -3
fi

# 测试 6: 检查依赖
echo ""
echo "测试 6: 检查依赖..."
if command -v git >/dev/null 2>&1; then
    GIT_VERSION=$(git --version)
    echo "✓ Git 已安装: $GIT_VERSION"
else
    echo "✗ Git 未安装"
    exit 1
fi

# 测试 7: 检查操作系统检测功能
echo ""
echo "测试 7: 操作系统检测..."
OS_TYPE=$(bash -c 'source upload2git.sh 2>/dev/null; detect_os 2>/dev/null || echo "Unknown"')
if [ -n "$OS_TYPE" ]; then
    echo "✓ 检测到操作系统: $OS_TYPE"
else
    echo "⚠ 无法检测操作系统"
fi

echo ""
echo "=== 测试完成 ==="
echo ""
echo "要测试实际上传功能，请运行:"
echo "  ./upload2git.sh test_skill.txt"
