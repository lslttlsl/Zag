"""Zag 抽象语法树（AST）节点定义。

所有节点都带 line/col，用于把语义错误精确定位到源码位置。
命名约定：*Decl 是声明，*Stmt 是语句，其余是表达式。
"""

from dataclasses import dataclass, field
from typing import List, Optional, Tuple


# ============================================================ 类型节点
@dataclass
class TypeRef:
    """具名类型：int / float / bool / string / void / type / 自定义结构体名。"""
    name: str
    line: int = 0
    col: int = 0


@dataclass
class OptionalType:
    """?T —— 可选类型。"""
    inner: object
    line: int = 0
    col: int = 0


@dataclass
class ErrorUnionType:
    """!T 错误联合（前缀）；命名错误集时 error_set 非空（MyError!int）。"""
    inner: object
    error_set: Optional[object] = None
    line: int = 0
    col: int = 0


@dataclass
class ArrayTypeNode:
    """[N]T 或 []T。size 为 None 表示切片。"""
    elem: object
    size: object = None
    line: int = 0
    col: int = 0


# ============================================================ 声明
@dataclass
class Param:
    name: str
    type: object
    comptime: bool = False   # comptime T: type —— 编译期参数（泛型）
    line: int = 0
    col: int = 0


@dataclass
class VarDecl:
    """const / var / const? / var. 声明。

    kind 取值：
        "const"       —— 普通常量，修改/未使用都会报错
        "var"         —— 普通变量，从未被修改会报错
        "const_safe"  —— const? 常量安全，赋值被静默丢弃
        "var_safe"    —— var. 声明安全，未使用/未修改都静默忽略
    """
    kind: str
    name: str
    type: Optional[object]
    value: Optional[object]
    line: int = 0
    col: int = 0
    pub: bool = False


@dataclass
class DestructureDecl:
    """const [a, b] = expr 解构声明。"""
    kind: str
    targets: List[str]
    value: object
    line: int = 0
    col: int = 0
    pub: bool = False


@dataclass
class FnDecl:
    name: str
    params: List[Param]
    ret_type: Optional[object]
    body: Optional[object]          # extern 函数没有函数体
    pub: bool = False
    inline: bool = False
    export: bool = False
    extern: bool = False
    line: int = 0
    col: int = 0


@dataclass
class StructField:
    name: str
    type: object
    line: int = 0
    col: int = 0


@dataclass
class StructDecl:
    name: str
    fields: List[StructField]
    methods: List["FnDecl"] = field(default_factory=list)
    pub: bool = False
    line: int = 0
    col: int = 0


@dataclass
class EnumVariant:
    name: str
    value: Optional[object] = None
    line: int = 0
    col: int = 0


@dataclass
class EnumDecl:
    name: str
    variants: List[EnumVariant]
    methods: List["FnDecl"] = field(default_factory=list)
    pub: bool = False
    line: int = 0
    col: int = 0


@dataclass
class UnionDecl:
    name: str
    fields: List[StructField]
    methods: List["FnDecl"] = field(default_factory=list)
    pub: bool = False
    line: int = 0
    col: int = 0


@dataclass
class TestDecl:
    name: str
    body: object
    line: int = 0
    col: int = 0


# ============================================================ 语句
@dataclass
class Block:
    stmts: List[object]


@dataclass
class ExprStmt:
    expr: object
    line: int = 0
    col: int = 0


@dataclass
class AssignStmt:
    """target op value，op 为 "=" / "+=" / "-=" / "*=" / "/=" / "%="。"""
    target: object
    op: str
    value: object
    line: int = 0
    col: int = 0


@dataclass
class IncDecStmt:
    """target++ / target--。"""
    target: object
    op: str
    line: int = 0
    col: int = 0


@dataclass
class ReturnStmt:
    value: Optional[object]
    line: int = 0
    col: int = 0


@dataclass
class IfStmt:
    cond: object
    then_stmt: object
    else_stmt: Optional[object]
    payload: Optional[str] = None   # if (opt) |val| 的可选值捕获名
    line: int = 0
    col: int = 0


@dataclass
class WhileStmt:
    cond: object
    body: object
    label: Optional[str] = None    # outer: while (...) —— 循环标签
    line: int = 0
    col: int = 0


@dataclass
class ForStmt:
    init: Optional[object]          # VarDecl / AssignStmt / ExprStmt / None
    cond: Optional[object]
    post: Optional[object]          # IncDecStmt / AssignStmt / None
    body: object
    label: Optional[str] = None     # outer: for (...) —— 循环标签
    line: int = 0
    col: int = 0


@dataclass
class DeferStmt:
    body: object
    line: int = 0
    col: int = 0


@dataclass
class ComptimeStmt:
    body: Block
    line: int = 0
    col: int = 0


@dataclass
class BreakStmt:
    label: Optional[str] = None
    line: int = 0
    col: int = 0


@dataclass
class ContinueStmt:
    label: Optional[str] = None
    line: int = 0
    col: int = 0


# ============================================================ 表达式
@dataclass
class IntLit:
    value: str
    line: int = 0
    col: int = 0


@dataclass
class FloatLit:
    value: str
    line: int = 0
    col: int = 0


@dataclass
class StringLit:
    value: str
    line: int = 0
    col: int = 0


@dataclass
class BoolLit:
    value: bool
    line: int = 0
    col: int = 0


@dataclass
class NullLit:
    line: int = 0
    col: int = 0


@dataclass
class Ident:
    name: str
    line: int = 0
    col: int = 0


@dataclass
class Binary:
    op: str
    left: object
    right: object
    line: int = 0
    col: int = 0


@dataclass
class Unary:
    op: str                        # "-" / "!" / "not"
    operand: object
    line: int = 0
    col: int = 0


@dataclass
class Call:
    callee: object
    args: List[object]
    line: int = 0
    col: int = 0


@dataclass
class Index:
    base: object
    index: object
    line: int = 0
    col: int = 0


@dataclass
class SliceExpr:
    base: object
    start: Optional[object]        # None 表示 [..end]
    end: Optional[object]          # None 表示 [start..]
    line: int = 0
    col: int = 0


@dataclass
class Field:
    base: object
    name: str
    line: int = 0
    col: int = 0


@dataclass
class ArrayLit:
    elems: List[object]
    line: int = 0
    col: int = 0


@dataclass
class StructLit:
    type_name: str
    fields: List[Tuple[str, object]]
    line: int = 0
    col: int = 0


@dataclass
class IfExpr:
    cond: object
    then_expr: object
    else_expr: object
    payload: Optional[str] = None   # if (opt) |val| expr else expr
    line: int = 0
    col: int = 0


@dataclass
class TryExpr:
    expr: object
    line: int = 0
    col: int = 0


@dataclass
class CatchExpr:
    expr: object
    capture: Optional[str]         # |err| 中的错误名
    payload: object                # 块或表达式
    line: int = 0
    col: int = 0


@dataclass
class AddressOf:
    """&expr —— 取地址。"""
    operand: object
    line: int = 0
    col: int = 0


@dataclass
class Deref:
    """$expr —— 解引用（Zag 特色，取代 Zig 的 .*）。"""
    operand: object
    line: int = 0
    col: int = 0


@dataclass
class EnumLit:
    """.name —— 枚举字面量。"""
    name: str
    line: int = 0
    col: int = 0


@dataclass
class ErrorSetLit:
    """error{ Bad, Worse } —— 错误集合（可作表达式，也可作类型）。"""
    variants: List[str]
    line: int = 0
    col: int = 0


@dataclass
class BuiltinCall:
    """@sizeOf(T) / @typeName(T) 等编译器内建函数。"""
    name: str
    args: List[object]
    line: int = 0
    col: int = 0


@dataclass
class ImportExpr:
    """@import("std") / @import("mod.zag")。"""
    path: str
    line: int = 0
    col: int = 0


@dataclass
class Program:
    items: List[object]
