"""Zag 代码生成器：把 AST 翻译为 Zig 源码。

核心职责：
1. **四大金刚语义检查** —— const / var / const? / var. 的使用规则；
2. **类型映射** —— int→i32, float→f64, string→[]const u8 ...；
3. **Zig 惯用法适配** —— std.print→std.debug.print、索引 @intCast、
   var 字面量显式标注类型、catch/if 的值块用 blk: 形式等。

代码生成策略中的所有 Zig 行为均已在 Zig 0.17.0 上实测验证。
"""

from . import ast_nodes as ast


class ZagError(Exception):
    """Zag 语义错误（四大金刚规则违规等）。"""

    def __init__(self, message, line=0, col=0):
        super().__init__(message)
        self.message = message
        self.line = line
        self.col = col

    def __str__(self):
        if self.line:
            return f"{self.line}:{self.col}: {self.message}"
        return self.message


class VarInfo:
    """一个变量在作用域内的使用情况（四大金刚检查的依据）。"""

    __slots__ = ("kind", "mutated", "read", "address_taken")

    def __init__(self, kind):
        self.kind = kind
        self.mutated = False        # 是否有直接赋值（= / += / ++ / $x = ）
        self.read = False           # 是否被读取（含取地址）
        self.address_taken = False  # 是否被 & 取地址（可能被指针间接修改）


class CodeGen:
    # Zag 类型 → Zig 类型
    TYPE_MAP = {
        "int": "i32",
        "float": "f64",
        "bool": "bool",
        "string": "[]const u8",
        "void": "void",
        "type": "type",
    }

    def __init__(self, filename="<stdin>", source_dir="."):
        self.filename = filename
        self.source_dir = source_dir
        self.out = []
        self.indent = 0
        self.errors = []
        # 类型信息表（用于 std.print 的格式符推断，尽力而为）
        self.fn_returns = {}      # 函数名 -> Zag 返回类型名
        self.struct_fields = {}   # 结构体名 -> {字段名: 类型名}
        self.var_types = {}       # 变量名 -> Zag 类型名
        # 待递归编译的 .zag 导入: [(原始路径, 源文件目录)]
        self.pending_imports = []
        # 当前作用域上下文（表达式生成时需要）
        self._infos = {}
        self._is_local = False

    # ============================================================ 输出辅助
    def emit(self, text):
        self.out.append("    " * self.indent + text)

    def blank(self):
        self.out.append("")

    def _error(self, message, line, col):
        self.errors.append(f"{line}:{col}: {message}")

    # ============================================================ 入口
    def generate(self, program):
        self._collect_decls(program)

        self.emit("// " + "=" * 62)
        self.emit("// 本文件由 Zag 编译器自动生成，请勿手动编辑")
        self.emit(f"// 源文件: {self.filename}")
        self.emit("// " + "=" * 62)
        self.blank()

        global_infos = self._analyze_globals(program)
        for item in program.items:
            self._gen_toplevel(item, global_infos)

        if self.errors:
            details = "\n".join(f"  {e}" for e in self.errors)
            raise ZagError(f"发现 {len(self.errors)} 个语义错误:\n{details}")
        return "\n".join(self.out) + "\n"

    # ============================================================ 预扫描
    def _collect_decls(self, program):
        """收集函数返回类型、结构体字段、顶层变量类型，供格式推断使用。"""
        for item in program.items:
            if isinstance(item, ast.FnDecl):
                if item.ret_type is not None and isinstance(item.ret_type, ast.TypeRef):
                    self.fn_returns[item.name] = item.ret_type.name
            elif isinstance(item, ast.StructDecl):
                self.struct_fields[item.name] = {
                    f.name: (f.type.name if isinstance(f.type, ast.TypeRef) else None)
                    for f in item.fields
                }
            elif isinstance(item, ast.UnionDecl):
                self.struct_fields[item.name] = {
                    f.name: (f.type.name if isinstance(f.type, ast.TypeRef) else None)
                    for f in item.fields
                }
            elif isinstance(item, ast.VarDecl):
                self._record_var_type(item)
                # const m = @import("x.zag") —— 预扫描被导入模块的 pub fn 返回类型
                if isinstance(item.value, ast.ImportExpr):
                    self._collect_import_returns(item.name, item.value.path)

    def _collect_import_returns(self, alias, path):
        """加载被导入的 .zag 模块，登记 '模块.函数' -> 返回类型（尽力而为）。"""
        if not path.endswith(".zag"):
            return
        import os
        full = os.path.join(self.source_dir, path)
        try:
            with open(full, "r", encoding="utf-8") as f:
                src = f.read()
            from .parser import parse as _parse
            prog = _parse(src)
        except Exception:
            return  # 模块缺失或解析失败：交由 Zig 编译阶段报错
        for item in prog.items:
            if isinstance(item, ast.FnDecl) and item.pub and \
                    item.ret_type is not None and isinstance(item.ret_type, ast.TypeRef):
                self.fn_returns[f"{alias}.{item.name}"] = item.ret_type.name
            elif isinstance(item, ast.VarDecl):
                self._record_var_type(item)

    def _record_var_type(self, decl):
        if decl.type is not None and isinstance(decl.type, ast.TypeRef):
            self.var_types[decl.name] = decl.type.name
        elif isinstance(decl.value, (ast.StructDecl, ast.EnumDecl, ast.UnionDecl)):
            # const Point = struct { ... } —— 该名字本身成为类型名
            self.var_types[decl.name] = decl.name
            if not isinstance(decl.value, ast.EnumDecl):
                self.struct_fields[decl.name] = {
                    f.name: (f.type.name if isinstance(f.type, ast.TypeRef) else None)
                    for f in decl.value.fields
                }
        elif decl.value is not None:
            t = self.static_type(decl.value)
            if t:
                self.var_types[decl.name] = t

    # ============================================================ 作用域分析
    def _analyze_globals(self, program):
        """容器级作用域：注册顶层声明，并遍历所有函数体收集引用/赋值。"""
        infos = {}

        def register(name, kind):
            if name not in infos:
                infos[name] = VarInfo(kind)

        for item in program.items:
            if isinstance(item, ast.VarDecl):
                register(item.name, item.kind)
                self._visit_expr(item.value, infos, None)
            elif isinstance(item, ast.DestructureDecl):
                for t in item.targets:
                    register(t, item.kind)
                self._visit_expr(item.value, infos, None)

        # 遍历所有函数体（含结构体方法、测试块），屏蔽局部名避免污染全局
        containers = []
        for item in program.items:
            if isinstance(item, ast.FnDecl) and item.body:
                containers.append(item.body)
            elif isinstance(item, ast.TestDecl):
                containers.append(item.body)
            elif isinstance(item, (ast.StructDecl, ast.EnumDecl, ast.UnionDecl)):
                containers.extend(m.body for m in item.methods if m.body)
            elif isinstance(item, ast.VarDecl) and isinstance(
                    item.value, (ast.StructDecl, ast.EnumDecl, ast.UnionDecl)):
                # const Point = struct { fn method() ... } —— 值内的方法也要分析
                containers.extend(m.body for m in item.value.methods if m.body)
        for body in containers:
            locals_ = self._collect_local_names(body)
            self._visit_stmts(body.stmts, infos, locals_)
        return infos

    def _analyze_scope(self, stmts):
        """函数局部作用域分析。"""
        infos = {}
        self._visit_stmts(stmts, infos, None)
        return infos

    def _collect_local_names(self, body):
        """收集一个函数体内声明的所有局部名（不含嵌套函数）。"""
        names = set()

        def walk(stmts):
            for s in stmts:
                if isinstance(s, ast.VarDecl):
                    names.add(s.name)
                elif isinstance(s, ast.DestructureDecl):
                    names.update(s.targets)
                elif isinstance(s, ast.FnDecl):
                    continue  # 嵌套函数是独立作用域
                elif isinstance(s, ast.Block):
                    walk(s.stmts)
                elif isinstance(s, ast.IfStmt):
                    walk([s.then_stmt] if not isinstance(s.then_stmt, ast.Block) else s.then_stmt.stmts)
                    if s.else_stmt is not None:
                        walk([s.else_stmt] if not isinstance(s.else_stmt, ast.Block) else s.else_stmt.stmts)
                elif isinstance(s, ast.WhileStmt):
                    walk([s.body] if not isinstance(s.body, ast.Block) else s.body.stmts)
                elif isinstance(s, ast.ForStmt):
                    parts = [p for p in (s.init, s.post) if p is not None]
                    walk(parts)
                    walk([s.body] if not isinstance(s.body, ast.Block) else s.body.stmts)
                elif isinstance(s, ast.DeferStmt):
                    walk([s.body] if not isinstance(s.body, ast.Block) else s.body.stmts)
                elif isinstance(s, ast.ComptimeStmt):
                    walk(s.body.stmts)

        walk(body.stmts)
        return names

    # ---- 访问者：标记 mutated / read / address_taken
    def _visit_stmts(self, stmts, infos, skip):
        for s in stmts:
            self._visit_stmt(s, infos, skip)

    def _visit_stmt(self, s, infos, skip):
        if isinstance(s, ast.VarDecl):
            if skip is None or s.name not in skip:
                infos.setdefault(s.name, VarInfo(s.kind))
            self._visit_expr(s.value, infos, skip)
        elif isinstance(s, ast.DestructureDecl):
            for t in s.targets:
                if skip is None or t not in skip:
                    infos.setdefault(t, VarInfo(s.kind))
            self._visit_expr(s.value, infos, skip)
        elif isinstance(s, ast.FnDecl):
            return  # 嵌套函数独立作用域
        elif isinstance(s, ast.ExprStmt):
            self._visit_expr(s.expr, infos, skip)
        elif isinstance(s, ast.AssignStmt):
            if isinstance(s.target, ast.Ident):
                if skip is None or s.target.name not in skip:
                    info = infos.get(s.target.name)
                    if info is not None:
                        info.mutated = True
            else:
                self._visit_expr(s.target, infos, skip)
            self._visit_expr(s.value, infos, skip)
        elif isinstance(s, ast.IncDecStmt):
            if isinstance(s.target, ast.Ident):
                if skip is None or s.target.name not in skip:
                    info = infos.get(s.target.name)
                    if info is not None:
                        info.mutated = True
            else:
                self._visit_expr(s.target, infos, skip)
        elif isinstance(s, ast.ReturnStmt):
            self._visit_expr(s.value, infos, skip)
        elif isinstance(s, ast.IfStmt):
            self._visit_expr(s.cond, infos, skip)
            self._visit_stmt(s.then_stmt, infos, skip)
            if s.else_stmt is not None:
                self._visit_stmt(s.else_stmt, infos, skip)
        elif isinstance(s, ast.WhileStmt):
            self._visit_expr(s.cond, infos, skip)
            self._visit_stmt(s.body, infos, skip)
        elif isinstance(s, ast.ForStmt):
            for part in (s.init, s.post):
                if part is not None:
                    self._visit_stmt(part, infos, skip)
            self._visit_expr(s.cond, infos, skip)
            self._visit_stmt(s.body, infos, skip)
        elif isinstance(s, ast.DeferStmt):
            self._visit_stmt(s.body, infos, skip)
        elif isinstance(s, ast.ComptimeStmt):
            self._visit_stmts(s.body.stmts, infos, skip)
        elif isinstance(s, ast.Block):
            self._visit_stmts(s.stmts, infos, skip)
        # 其余声明无变量引用

    def _visit_expr(self, e, infos, skip):
        if e is None:
            return
        if isinstance(e, ast.Ident):
            if skip is None or e.name not in skip:
                info = infos.get(e.name)
                if info is not None:
                    info.read = True
        elif isinstance(e, ast.AddressOf):
            self._visit_expr(e.operand, infos, skip)
            if isinstance(e.operand, ast.Ident):
                if skip is None or e.operand.name not in skip:
                    info = infos.get(e.operand.name)
                    if info is not None:
                        info.read = True
                        info.address_taken = True
        elif isinstance(e, ast.Deref):
            self._visit_expr(e.operand, infos, skip)
            if isinstance(e.operand, ast.Ident):
                if skip is None or e.operand.name not in skip:
                    info = infos.get(e.operand.name)
                    if info is not None:
                        info.read = True
        elif isinstance(e, ast.Binary):
            self._visit_expr(e.left, infos, skip)
            self._visit_expr(e.right, infos, skip)
        elif isinstance(e, ast.Unary):
            self._visit_expr(e.operand, infos, skip)
        elif isinstance(e, ast.Call):
            self._visit_expr(e.callee, infos, skip)
            for a in e.args:
                self._visit_expr(a, infos, skip)
        elif isinstance(e, ast.Index):
            self._visit_expr(e.base, infos, skip)
            self._visit_expr(e.index, infos, skip)
        elif isinstance(e, ast.SliceExpr):
            self._visit_expr(e.base, infos, skip)
            self._visit_expr(e.start, infos, skip)
            self._visit_expr(e.end, infos, skip)
        elif isinstance(e, ast.Field):
            self._visit_expr(e.base, infos, skip)
        elif isinstance(e, ast.ArrayLit):
            for x in e.elems:
                self._visit_expr(x, infos, skip)
        elif isinstance(e, ast.StructLit):
            for _, v in e.fields:
                self._visit_expr(v, infos, skip)
        elif isinstance(e, ast.IfExpr):
            self._visit_expr(e.cond, infos, skip)
            self._visit_expr(e.then_expr, infos, skip)
            self._visit_expr(e.else_expr, infos, skip)
        elif isinstance(e, ast.ErrorSetLit):
            pass  # 只有错误名，没有变量引用
        elif isinstance(e, ast.TryExpr):
            self._visit_expr(e.expr, infos, skip)
        elif isinstance(e, ast.CatchExpr):
            self._visit_expr(e.expr, infos, skip)
            self._visit_expr(e.payload, infos, skip)
        elif isinstance(e, ast.BuiltinCall):
            for a in e.args:
                self._visit_expr(a, infos, skip)

    # ============================================================ 四大金刚判定
    def _resolve(self, kind, name, info, is_local, line, col):
        """返回 (zig 声明关键字, 是否需要 _ = &x 静默未使用, 是否需要 x = x 自赋值)。"""
        if kind == "const":
            if info.mutated:
                self._error(
                    f"不能给常量 '{name}' 赋值（常量不可变；若希望静默忽略修改，请使用 const?）",
                    line, col)
            if is_local and not info.read:
                self._error(
                    f"常量 '{name}' 声明后从未使用（若希望允许未使用，请使用 const?）",
                    line, col)
            return "const", False, False

        if kind == "const_safe":
            # const? ：赋值在生成时静默丢弃，程序继续运行
            return "const", (is_local and not info.read), False

        # var / var_safe
        if info.mutated:
            return "var", False, False
        if info.address_taken:
            # 取地址后可能被指针间接修改，保守地保留 var
            return "var", False, is_local
        if kind == "var":
            self._error(
                f"var '{name}' 声明后从未被修改（若希望静默忽略，请使用 var.）",
                line, col)
            return "var", False, False
        # var_safe 且从未修改也从未取地址：安全退化为 const
        return "const", (is_local and not info.read), False

    # ============================================================ 顶层声明
    def _gen_toplevel(self, item, global_infos):
        if isinstance(item, ast.FnDecl):
            self._gen_fn(item)
            self.blank()
        elif isinstance(item, ast.StructDecl):
            self._gen_struct(item)
            self.blank()
        elif isinstance(item, ast.EnumDecl):
            self._gen_enum(item)
            self.blank()
        elif isinstance(item, ast.UnionDecl):
            self._gen_union(item)
            self.blank()
        elif isinstance(item, ast.VarDecl):
            self._gen_var_decl(item, global_infos, is_local=False)
            self.blank()
        elif isinstance(item, ast.DestructureDecl):
            self._gen_destructure(item, global_infos, is_local=False)
            self.blank()
        elif isinstance(item, ast.TestDecl):
            self._gen_test(item)
            self.blank()
        elif isinstance(item, ast.ComptimeStmt):
            # 顶层 comptime 块按局部作用域处理
            infos = self._analyze_scope(item.body.stmts)
            saved = (self._infos, self._is_local)
            self._infos, self._is_local = infos, True
            self.emit("comptime {")
            self._gen_body(item.body)
            self.emit("}")
            self._infos, self._is_local = saved
            self.blank()

    def _gen_fn(self, fn):
        prefix = ""
        if fn.pub:
            prefix += "pub "
        if fn.export:
            prefix += "export "
        if fn.extern:
            prefix += "extern "
        if fn.inline:
            prefix += "inline "
        params = ", ".join(
            f"{'comptime ' if p.comptime else ''}{p.name}: {self.zig_type(p.type)}"
            for p in fn.params)
        ret = f" {self.zig_type(fn.ret_type)}" if fn.ret_type is not None else ""

        if fn.extern:
            self.emit(f"{prefix}fn {fn.name}({params}){ret};")
            return

        self.emit(f"{prefix}fn {fn.name}({params}){ret} {{")
        self.indent += 1
        infos = self._analyze_scope(fn.body.stmts)
        saved = (self._infos, self._is_local)
        self._infos, self._is_local = infos, True
        for st in fn.body.stmts:
            self._gen_stmt(st)
        self._infos, self._is_local = saved
        self.indent -= 1
        self.emit("}")

    def _emit_aggregate(self, kw, decl, header):
        """发射 struct/enum/union 体；header 为 'const Name = ' 之类的前缀。"""
        self.emit(header + kw + " {")
        self.indent += 1
        if kw == "enum":
            for v in decl.variants:
                if v.value is not None:
                    self.emit(f"{v.name} = {self.expr(v.value)},")
                else:
                    self.emit(f"{v.name},")
        else:
            for f in decl.fields:
                self.emit(f"{f.name}: {self.zig_type(f.type)},")
        for m in decl.methods:
            self._gen_fn(m)
        self.indent -= 1
        self.emit("};")

    def _gen_struct(self, decl):
        self._emit_aggregate("struct", decl,
                             f"{'pub ' if decl.pub else ''}const {decl.name} = ")

    def _gen_enum(self, decl):
        self._emit_aggregate("enum", decl,
                             f"{'pub ' if decl.pub else ''}const {decl.name} = ")

    def _gen_union(self, decl):
        self._emit_aggregate("union", decl,
                             f"{'pub ' if decl.pub else ''}const {decl.name} = ")

    def _gen_test(self, decl):
        self.emit(f'test "{decl.name}" {{')
        self.indent += 1
        infos = self._analyze_scope(decl.body.stmts)
        saved = (self._infos, self._is_local)
        self._infos, self._is_local = infos, True
        for st in decl.body.stmts:
            self._gen_stmt(st)
        self._infos, self._is_local = saved
        self.indent -= 1
        self.emit("}")

    # ============================================================ 变量声明
    def _gen_var_decl(self, decl, infos, is_local):
        info = infos.get(decl.name) or VarInfo(decl.kind)
        zig_kind, discard, self_assign = self._resolve(
            decl.kind, decl.name, info, is_local, decl.line, decl.col)

        # const Point = struct { ... } —— 聚合类型字面量，多行发射
        if isinstance(decl.value, (ast.StructDecl, ast.EnumDecl, ast.UnionDecl)):
            kw = ("enum" if isinstance(decl.value, ast.EnumDecl)
                  else "union" if isinstance(decl.value, ast.UnionDecl)
                  else "struct")
            pub = "pub " if decl.pub else ""
            self._emit_aggregate(kw, decl.value, f"{pub}{zig_kind} {decl.name} = ")
            self._record_var_type(decl)
            return

        type_str = ""
        if decl.type is not None:
            type_str = ": " + self.zig_type(decl.type)
        elif decl.value is not None:
            ann = self._literal_annotation(decl.value)
            if ann is not None:
                type_str = ": " + ann

        value_str = self.expr(decl.value) if decl.value is not None else "undefined"
        pub = "pub " if decl.pub else ""
        self.emit(f"{pub}{zig_kind} {decl.name}{type_str} = {value_str};")
        if self_assign:
            # 地址被取得后可能经指针间接修改，保守保留 var；
            # Zig 要求 var 至少出现一次直接赋值，故发自赋值
            self.emit(f"{decl.name} = {decl.name};")
        if discard:
            self.emit(f"_ = &{decl.name};")
        self._record_var_type(decl)

    def _gen_destructure(self, decl, infos, is_local):
        for i, target in enumerate(decl.targets):
            info = infos.get(target) or VarInfo(decl.kind)
            zig_kind, discard, _ = self._resolve(
                decl.kind, target, info, is_local, decl.line, decl.col)
            self.emit(f"{zig_kind} {target} = {self.expr(decl.value)}[{i}];")
            if discard:
                self.emit(f"_ = &{target};")

    def _literal_annotation(self, value):
        """字面量初始值的类型标注：Zig 的 var 不能是 comptime_int，必须显式标注。"""
        if isinstance(value, ast.IntLit):
            return "i32"
        if isinstance(value, ast.FloatLit):
            return "f64"
        if isinstance(value, ast.StringLit):
            return "[]const u8"
        if isinstance(value, ast.BoolLit):
            return "bool"
        return None

    # ============================================================ 语句
    def _gen_stmt(self, s):
        if isinstance(s, ast.VarDecl):
            self._gen_var_decl(s, self._infos, self._is_local)
        elif isinstance(s, ast.DestructureDecl):
            self._gen_destructure(s, self._infos, self._is_local)
        elif isinstance(s, ast.FnDecl):
            self._gen_fn(s)
        elif isinstance(s, ast.StructDecl):
            self._gen_struct(s)
        elif isinstance(s, ast.EnumDecl):
            self._gen_enum(s)
        elif isinstance(s, ast.UnionDecl):
            self._gen_union(s)
        elif isinstance(s, ast.TestDecl):
            self._gen_test(s)
        elif isinstance(s, ast.ExprStmt):
            self.emit(self.expr(s.expr) + ";")
        elif isinstance(s, ast.AssignStmt):
            self._gen_assign(s)
        elif isinstance(s, ast.IncDecStmt):
            self._gen_incdec(s)
        elif isinstance(s, ast.ReturnStmt):
            if s.value is not None:
                self.emit(f"return {self.expr(s.value)};")
            else:
                self.emit("return;")
        elif isinstance(s, ast.IfStmt):
            self._gen_if(s)
        elif isinstance(s, ast.WhileStmt):
            prefix = f"{s.label}: " if s.label else ""
            self.emit(f"{prefix}while ({self.expr(s.cond)}) {{")
            self._gen_body(s.body)
            self.emit("}")
        elif isinstance(s, ast.ForStmt):
            self._gen_for(s)
        elif isinstance(s, ast.DeferStmt):
            self.emit("defer {")
            self._gen_body(s.body)
            self.emit("}")
        elif isinstance(s, ast.ComptimeStmt):
            self.emit("comptime {")
            self._gen_body(s.body)
            self.emit("}")
        elif isinstance(s, ast.Block):
            self.emit("{")
            self._gen_body(s)
            self.emit("}")
        elif isinstance(s, ast.BreakStmt):
            self.emit(f"break{(' : ' + s.label) if s.label else ''};")
        elif isinstance(s, ast.ContinueStmt):
            self.emit(f"continue{(' : ' + s.label) if s.label else ''};")
        else:
            raise ZagError(f"代码生成器遇到未知语句: {type(s).__name__}")

    def _gen_body(self, body):
        """body 可以是 Block 或单条语句，统一缩进处理。"""
        self.indent += 1
        if isinstance(body, ast.Block):
            for st in body.stmts:
                self._gen_stmt(st)
        else:
            self._gen_stmt(body)
        self.indent -= 1

    def _gen_assign(self, s):
        # const? 的赋值被静默丢弃（保留右侧副作用）
        if isinstance(s.target, ast.Ident):
            info = self._infos.get(s.target.name)
            if info is not None and info.kind == "const_safe":
                self.emit(f"_ = {self.expr(s.value)};")
                return
        self.emit(f"{self.expr(s.target)} {s.op} {self.expr(s.value)};")

    def _gen_incdec(self, s):
        if isinstance(s.target, ast.Ident):
            info = self._infos.get(s.target.name)
            if info is not None and info.kind == "const_safe":
                sign = "+" if s.op == "++" else "-"
                self.emit(f"_ = {self.expr(s.target)} {sign} 1;")
                return
        op = "+=" if s.op == "++" else "-="
        self.emit(f"{self.expr(s.target)} {op} 1;")

    def _gen_if(self, s):
        # 展开 else-if 链，生成 Zig 惯用的 } else if (...) {
        parts = [s]
        while isinstance(parts[-1].else_stmt, ast.IfStmt):
            parts.append(parts[-1].else_stmt)
        for idx, part in enumerate(parts):
            cond = self.expr(part.cond)
            # if 的 payload 承担解包职责，未使用时用 |_| 占位（Zig 0.17 要求）
            cap = ""
            if getattr(part, "payload", None):
                if (self._name_used(part.then_stmt, part.payload)
                        or self._name_used(part.else_stmt, part.payload)):
                    cap = f" |{part.payload}|"
                else:
                    cap = " |_|"
            if idx == 0:
                self.emit(f"if ({cond}){cap} {{")
            else:
                self.emit(f"}} else if ({cond}){cap} {{")
            self._gen_body(part.then_stmt)
        last = parts[-1]
        if last.else_stmt is None:
            self.emit("}")
        else:
            self.emit("} else {")
            self._gen_body(last.else_stmt)
            self.emit("}")

    def _gen_for(self, s):
        # Zag C 风格 for → Zig while + continue 表达式
        if s.init is not None:
            if isinstance(s.init, ast.VarDecl):
                self._gen_var_decl(s.init, self._infos, self._is_local)
            elif isinstance(s.init, (ast.AssignStmt, ast.IncDecStmt)):
                self._gen_stmt(s.init)
            elif isinstance(s.init, ast.ExprStmt):
                self.emit(self.expr(s.init.expr) + ";")
        cond = self.expr(s.cond) if s.cond is not None else "true"
        header = f"while ({cond})"
        if s.post is not None:
            header += f" : ({self._post_expr(s.post)})"
        if s.label:
            header = f"{s.label}: " + header
        self.emit(header + " {")
        self._gen_body(s.body)
        self.emit("}")

    def _post_expr(self, post):
        if isinstance(post, ast.IncDecStmt):
            op = "+=" if post.op == "++" else "-="
            return f"{self.expr(post.target)} {op} 1"
        if isinstance(post, ast.AssignStmt):
            return f"{self.expr(post.target)} {post.op} {self.expr(post.value)}"
        return self.expr(post.expr)

    # ============================================================ 表达式
    def expr(self, e):
        if e is None:
            return ""
        if isinstance(e, ast.IntLit):
            return e.value
        if isinstance(e, ast.FloatLit):
            return e.value
        if isinstance(e, ast.StringLit):
            return '"' + self._escape(e.value) + '"'
        if isinstance(e, ast.BoolLit):
            return "true" if e.value else "false"
        if isinstance(e, ast.NullLit):
            return "null"
        if isinstance(e, ast.Ident):
            return e.name
        if isinstance(e, ast.Binary):
            return f"({self.expr(e.left)} {e.op} {self.expr(e.right)})"
        if isinstance(e, ast.Unary):
            op = "!" if e.op == "not" else e.op
            return f"({op}{self.expr(e.operand)})"
        if isinstance(e, ast.Call):
            return self._gen_call(e)
        if isinstance(e, ast.Field):
            return f"{self.expr(e.base)}.{e.name}"
        if isinstance(e, ast.Index):
            return f"{self.expr(e.base)}[{self._index_expr(e.index)}]"
        if isinstance(e, ast.SliceExpr):
            start = "" if e.start is None else self._index_expr(e.start)
            end = "" if e.end is None else self._index_expr(e.end)
            return f"{self.expr(e.base)}[{start}..{end}]"
        if isinstance(e, ast.ArrayLit):
            return self._gen_array_lit(e)
        if isinstance(e, ast.StructLit):
            return self._gen_struct_lit(e)
        if isinstance(e, ast.IfExpr):
            then_s = self._branch_value(e.then_expr)
            else_s = self._branch_value(e.else_expr)
            cap = ""
            if e.payload:
                if (self._name_used(e.then_expr, e.payload)
                        or self._name_used(e.else_expr, e.payload)):
                    cap = f" |{e.payload}|"
                else:
                    cap = " |_|"  # payload 承担解包，未使用也要占位
            return f"if ({self.expr(e.cond)}){cap} {then_s} else {else_s}"
        if isinstance(e, ast.TryExpr):
            return f"try {self.expr(e.expr)}"
        if isinstance(e, ast.CatchExpr):
            return self._gen_catch(e)
        if isinstance(e, ast.AddressOf):
            return f"&{self.expr(e.operand)}"
        if isinstance(e, ast.Deref):
            return f"{self.expr(e.operand)}.*"
        if isinstance(e, ast.EnumLit):
            return f".{e.name}"
        if isinstance(e, ast.ErrorSetLit):
            return "error{ " + ", ".join(e.variants) + " }"
        if isinstance(e, ast.BuiltinCall):
            args = ", ".join(self.expr(a) for a in e.args)
            return f"@{e.name}({args})"
        if isinstance(e, ast.ImportExpr):
            return f'@import("{self._import_path(e.path)}")'
        if isinstance(e, (ast.StructDecl, ast.EnumDecl, ast.UnionDecl)):
            return self._aggregate_inline(e)
        if isinstance(e, ast.TypeRef):
            return self.zig_type(e)
        raise ZagError(f"代码生成器遇到未知表达式: {type(e).__name__}")

    def _aggregate_inline(self, e):
        """聚合类型的行内形式（表达式位置使用，如 @sizeOf(struct{...})）。"""
        if e.methods:
            raise ZagError("聚合类型字面量中的方法请改用声明形式: struct Name { ... }")
        if isinstance(e, ast.EnumDecl):
            items = [v.name + (f" = {self.expr(v.value)}" if v.value is not None else "")
                     for v in e.variants]
            return "enum { " + ", ".join(items) + " }"
        fields = ", ".join(f"{f.name}: {self.zig_type(f.type)}" for f in e.fields)
        kw = "union" if isinstance(e, ast.UnionDecl) else "struct"
        return f"{kw} {{ {fields} }}"

    # ---- 特殊调用
    def _gen_call(self, e):
        # std.print(...) → std.debug.print(...)
        if isinstance(e.callee, ast.Field) and isinstance(e.callee.base, ast.Ident):
            if e.callee.base.name == "std" and e.callee.name == "print":
                return self._gen_print(e.args)
            if e.callee.base.name == "std" and e.callee.name == "select":
                return self._gen_select(e.args)
        # allocator.allocator(n) → allocator.alloc(u8, n)
        if isinstance(e.callee, ast.Field) and e.callee.name == "allocator" and len(e.args) == 1:
            return f"{self.expr(e.callee.base)}.alloc(u8, {self.expr(e.args[0])})"
        callee = self.expr(e.callee)
        args = ", ".join(self.expr(a) for a in e.args)
        return f"{callee}({args})"

    def _gen_print(self, args):
        """std.print → std.debug.print，按静态类型选择格式符。"""
        specs, vals = [], []
        for a in args:
            specs.append("{s}" if self.static_type(a) == "string" else "{any}")
            vals.append(self.expr(a))
        fmt = " ".join(specs) + "\\n"
        tuple_str = ".{" + ", ".join(vals) + "}"
        return f'std.debug.print("{fmt}", {tuple_str})'

    def _gen_select(self, args):
        """std.select("gpa") → std.heap.page_allocator（v1 统一映射）。"""
        return "std.heap.page_allocator"

    def _gen_array_lit(self, e):
        elem_type = "i32"
        t = self.static_type(e.elems[0])
        if t is not None:
            elem_type = self.TYPE_MAP.get(t, t)
        elems = ", ".join(self.expr(x) for x in e.elems)
        return f"[_]{elem_type}{{ {elems} }}"

    def _gen_struct_lit(self, e):
        fields = ", ".join(f".{name} = {self.expr(v)}" for name, v in e.fields)
        if e.type_name:
            return f"{e.type_name}{{ {fields} }}"
        return f".{{ {fields} }}"

    def _gen_catch(self, e):
        cap = f"|{e.capture}| " if e.capture else ""
        # Zig 0.17 要求捕获必须被使用；未被引用时自动省略 |err|
        if e.capture and not self._name_used(e.payload, e.capture):
            cap = ""
        if isinstance(e.payload, ast.Block):
            payload = self._block_value(e.payload)
            return f"{self.expr(e.expr)} catch {cap}{payload}"
        if isinstance(e.payload, (ast.ReturnStmt, ast.BreakStmt, ast.ContinueStmt)):
            return f"{self.expr(e.expr)} catch {cap}{self._stmt_text(e.payload)}"
        return f"{self.expr(e.expr)} catch {cap}{self.expr(e.payload)}"

    def _name_used(self, node, name):
        """AST 子树中是否引用了名字 name（用于判断 catch 捕获是否被使用）。"""
        if node is None:
            return False
        if isinstance(node, ast.Ident):
            return node.name == name
        if isinstance(node, ast.Block):
            return any(self._name_used(st, name) for st in node.stmts)
        for field in vars(node).values():
            if isinstance(field, list):
                if any(self._name_used(x, name) for x in field):
                    return True
            elif hasattr(field, "__dataclass_fields__"):
                if self._name_used(field, name):
                    return True
        return False

    def _stmt_text(self, s):
        """把简单语句渲染为单行文本（用于 catch 的语句 payload）。"""
        if isinstance(s, ast.ReturnStmt):
            return f"return {self.expr(s.value)};" if s.value is not None else "return;"
        if isinstance(s, ast.BreakStmt):
            return f"break{(' : ' + s.label) if s.label else ''};"
        if isinstance(s, ast.ContinueStmt):
            return f"continue{(' : ' + s.label) if s.label else ''};"
        raise ZagError(f"不支持的 catch 语句 payload: {type(s).__name__}")

    # ---- 值块（if 表达式分支 / catch payload）
    def _block_value(self, block):
        """把 Block 生成为 Zig 的值块。

        规则（均已在 Zig 0.17.0 实测）：
        - 最后一条是表达式语句 → break :blk <表达式>;（void 操作数也合法）
        - 否则 → break :blk;（裸 break，块类型为 void）
        """
        saved_out, saved_indent = self.out, self.indent
        self.out, self.indent = [], saved_indent + 1
        stmts = block.stmts
        if stmts and isinstance(stmts[-1], ast.ExprStmt):
            for st in stmts[:-1]:
                self._gen_stmt(st)
            self.emit(f"break :blk {self.expr(stmts[-1].expr)};")
        else:
            for st in stmts:
                self._gen_stmt(st)
            self.emit("break :blk;")
        inner = self.out
        self.out, self.indent = saved_out, saved_indent
        pad = "    " * saved_indent
        return "blk: {\n" + "\n".join(inner) + "\n" + pad + "}"

    def _branch_value(self, branch):
        if isinstance(branch, ast.Block):
            return self._block_value(branch)
        return self.expr(branch)

    # ---- 索引（Zig 索引要求 usize，i32 需要 @intCast）
    def _index_expr(self, e):
        if isinstance(e, ast.IntLit):
            return e.value
        return f"@intCast({self.expr(e)})"

    def _import_path(self, path):
        if path.endswith(".zag"):
            self.pending_imports.append((path, self.source_dir))
            return path[:-4] + ".zig"
        return path

    @staticmethod
    def _escape(s):
        return (s.replace("\\", "\\\\")
                 .replace('"', '\\"')
                 .replace("\n", "\\n")
                 .replace("\t", "\\t")
                 .replace("\r", "\\r")
                 .replace("\0", "\\0"))

    # ============================================================ 类型
    def zig_type(self, t):
        if t is None:
            return "void"
        if isinstance(t, ast.TypeRef):
            return self.TYPE_MAP.get(t.name, t.name)
        if isinstance(t, ast.ErrorSetLit):
            return "error{ " + ", ".join(t.variants) + " }"
        if isinstance(t, ast.OptionalType):
            return "?" + self.zig_type(t.inner)
        if isinstance(t, ast.ErrorUnionType):
            if t.error_set is not None:
                return f"{self.zig_type(t.error_set)}!{self.zig_type(t.inner)}"
            return "!" + self.zig_type(t.inner)
        if isinstance(t, ast.ArrayTypeNode):
            size = "" if t.size is None else self.expr(t.size)
            return f"[{size}]{self.zig_type(t.elem)}"
        return "void"

    # ============================================================ 静态类型推断
    def static_type(self, e):
        """尽力而为的类型推断，目前主要服务于 std.print 的格式符选择。"""
        if isinstance(e, ast.StringLit):
            return "string"
        if isinstance(e, ast.IntLit):
            return "int"
        if isinstance(e, ast.FloatLit):
            return "float"
        if isinstance(e, ast.BoolLit):
            return "bool"
        if isinstance(e, ast.Ident):
            return self.var_types.get(e.name)
        if isinstance(e, ast.Binary):
            if e.op in ("==", "!=", "<", "<=", ">", ">=", "and", "or"):
                return "bool"
            lt, rt = self.static_type(e.left), self.static_type(e.right)
            if lt == "float" or rt == "float":
                return "float"
            if lt == "int" and rt == "int":
                return "int"
            return None
        if isinstance(e, ast.Call):
            if isinstance(e.callee, ast.Ident):
                return self.fn_returns.get(e.callee.name)
            if isinstance(e.callee, ast.Field) and isinstance(e.callee.base, ast.Ident):
                # myModule.greet() —— 导入模块的函数
                return self.fn_returns.get(f"{e.callee.base.name}.{e.callee.name}")
            return None
        if isinstance(e, ast.IfExpr):
            # 两个分支静态类型一致时取该类型（如 if (c) "a" else "b" → string）
            lt, rt = self.static_type(e.then_expr), self.static_type(e.else_expr)
            if lt is not None and lt == rt:
                return lt
            return None
        if isinstance(e, ast.Field):
            base_t = self.static_type(e.base)
            if base_t is not None and base_t in self.struct_fields:
                return self.struct_fields[base_t].get(e.name)
            return None
        if isinstance(e, ast.StructLit):
            return e.type_name or None
        if isinstance(e, ast.BuiltinCall):
            # @typeName / @tagName / @errorName 返回字符串
            if e.name in ("typeName", "tagName", "errorName"):
                return "string"
            return None
        return None


def generate_zig(program, filename="<stdin>", source_dir="."):
    """便捷入口：AST -> (Zig 源码, 待编译导入列表)。"""
    cg = CodeGen(filename, source_dir)
    code = cg.generate(program)
    return code, cg.pending_imports
