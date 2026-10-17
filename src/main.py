#!/usr/bin/env python3
"""Zag 编译器命令行入口。

用法（在项目根目录）：
    python -m src.main examples/example.zag                # 编译为 .zig
    python -m src.main examples/example.zag -o out.zig     # 指定输出
    python -m src.main examples/example.zag --run          # 编译并立即运行
    python -m src.main examples/example.zag --test         # 编译并运行测试
    python -m src.main examples/example.zag --ast          # 只打印 AST

也支持直接运行：python src/main.py examples/example.zag
"""

import argparse
import os
import subprocess
import sys
from dataclasses import fields, is_dataclass

try:
    from .lexer import Lexer, LexError
    from .parser import Parser, ParseError
    from .codegen import CodeGen, ZagError
except ImportError:  # 直接以脚本方式运行
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from src.lexer import Lexer, LexError
    from src.parser import Parser, ParseError
    from src.codegen import CodeGen, ZagError


# ============================================================ AST 打印（调试用）
def dump_ast(node, indent=0):
    pad = "  " * indent
    if is_dataclass(node):
        name = type(node).__name__
        simple = []
        children = []
        for f in fields(node):
            v = getattr(node, f.name)
            if isinstance(v, (int, bool, str)) or v is None:
                simple.append(f"{f.name}={v!r}")
            else:
                children.append((f.name, v))
        head = f"{pad}{name}(" + ", ".join(simple) + ")"
        print(head)
        for fname, v in children:
            print(f"{pad}  .{fname}:")
            if isinstance(v, list):
                for item in v:
                    dump_ast(item, indent + 2)
            else:
                dump_ast(v, indent + 2)
    elif isinstance(node, list):
        for item in node:
            dump_ast(item, indent)
    else:
        print(f"{pad}{node!r}")


# ============================================================ 编译
def compile_file(input_path, show_ast=False):
    """编译单个 .zag 文件，返回 (zig 源码, 待编译的 .zag 导入列表)。"""
    with open(input_path, "r", encoding="utf-8") as f:
        source = f.read()

    tokens = Lexer(source).tokenize()
    ast = Parser(tokens).parse_program()

    if show_ast:
        dump_ast(ast)

    source_dir = os.path.dirname(os.path.abspath(input_path))
    codegen = CodeGen(os.path.basename(input_path), source_dir)
    zig_code = codegen.generate(ast)
    return zig_code, codegen.pending_imports


def compile_project(input_path, output_path, show_ast=False):
    """编译入口文件，并递归编译它导入的所有 .zag 模块。"""
    visited = set()

    def _compile(src_path, out_path):
        real = os.path.abspath(src_path)
        if real in visited:
            return
        visited.add(real)

        zig_code, pending = compile_file(src_path, show_ast)

        out_dir = os.path.dirname(os.path.abspath(out_path))
        os.makedirs(out_dir, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(zig_code)
        print(f"[zag] {src_path} -> {out_path}")

        src_dir = os.path.dirname(os.path.abspath(src_path))
        for raw_path, _ in pending:
            dep_src = os.path.join(src_dir, raw_path)
            dep_out = os.path.join(
                out_dir, os.path.splitext(os.path.basename(raw_path))[0] + ".zig")
            if os.path.exists(dep_src):
                _compile(dep_src, dep_out)
            else:
                print(f"[zag] 警告: 找不到导入的模块 {raw_path}", file=sys.stderr)

    _compile(input_path, output_path)


# ============================================================ CLI
def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="zag",
        description="Zag 编译器 —— 把 Zag 语言编译为 Zig 源码")
    ap.add_argument("input", help="Zag 源文件（.zag）")
    ap.add_argument("-o", "--output",
                    help="输出的 .zig 路径（默认与输入同目录同名）")
    ap.add_argument("--run", action="store_true",
                    help="生成后立即用 zig run 运行")
    ap.add_argument("--test", action="store_true",
                    help="生成后用 zig test 运行测试")
    ap.add_argument("--zig", default=os.environ.get("ZIG", "zig"),
                    help="zig 可执行文件路径（默认在 PATH 中查找，也可用环境变量 ZIG）")
    ap.add_argument("--ast", action="store_true",
                    help="打印 AST 后退出（调试用）")
    args = ap.parse_args(argv)

    if not os.path.isfile(args.input):
        print(f"zag: error: 文件不存在: {args.input}", file=sys.stderr)
        return 1

    output = args.output or (os.path.splitext(args.input)[0] + ".zig")

    try:
        compile_project(args.input, output, show_ast=args.ast)
    except (LexError, ParseError, ZagError) as e:
        print(f"zag: error: {args.input}: {e}", file=sys.stderr)
        return 1

    if args.run:
        cmd = [args.zig, "run", output]
        print(f"[zag] 运行: {' '.join(cmd)}")
        return subprocess.run(cmd).returncode
    if args.test:
        cmd = [args.zig, "test", output]
        print(f"[zag] 测试: {' '.join(cmd)}")
        return subprocess.run(cmd).returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
