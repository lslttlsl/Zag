"""Zag 语法分析器：把 Token 序列解析为 AST。

采用递归下降 + 优先级爬升。两个关键点：

1. **换行规则**：Zag 没有分号，换行是语句的软终止符。但如果换行前一个
   Token 是二元运算符、左括号、逗号等"明显还没说完"的符号，则换行被忽略
   （支持跨行书写表达式）。
2. **悬空 else**：``else`` 允许写在下一行，由 if 语句解析器主动向后寻找。
"""

from .lexer import tokenize, TYPE_KEYWORDS, LexError
from . import ast_nodes as ast


class ParseError(Exception):
    def __init__(self, message, line, col):
        super().__init__(message)
        self.message = message
        self.line = line
        self.col = col

    def __str__(self):
        return f"{self.line}:{self.col}: {self.message}"


# 这些 Token 之后出现换行时，换行被忽略（表达式未写完，允许跨行）
CONTINUATION_AFTER = frozenset({
    # 二元运算符
    "+", "-", "*", "/", "%", "==", "!=", "<", "<=", ">", ">=",
    "and", "or", "not",
    # 赋值
    "=", "+=", "-=", "*=", "/=", "%=",
    # 明显未写完的结构
    "(", "[", "{", ",", ":", ";", ".", "?", "!", "&", "$", "@",
    ")", "]",
    # 关键字前缀
    "if", "else", "while", "for", "fn", "pub", "inline", "export", "extern",
    "test", "const", "var", "CONSTSAFE", "VARSAFE", "defer", "comptime",
    "struct", "enum", "union", "break", "continue", "try", "catch",
    "orelse",
})

COMPARISON_OPS = frozenset({"==", "!=", "<", "<=", ">", ">="})
COMPOUND_ASSIGN = frozenset({"+=", "-=", "*=", "/=", "%="})


class Parser:
    def __init__(self, tokens):
        self.toks = tokens
        self.i = 0
        self.last_type = None
        # 已声明的自定义类型名（结构体/枚举/联合），用于 @sizeOf(Point) 这类歧义
        self.known_types = set()

    # ============================================================ Token 流
    def _raw(self, offset=0):
        j = self.i + offset
        if j < len(self.toks):
            return self.toks[j]
        return self.toks[-1]  # EOF

    def peek(self):
        """返回下一个有效 Token；换行是否被忽略取决于前一个 Token。"""
        t = self._raw()
        if t.type == "NEWLINE" and self.last_type in CONTINUATION_AFTER:
            j = self.i
            while self.toks[j].type == "NEWLINE":
                j += 1
            return self.toks[j]
        return t

    def peek_any(self):
        """跳过所有换行后返回（仅用于 else 这类语法糖判断，不改变续行状态）。"""
        j = self.i
        while self.toks[j].type == "NEWLINE":
            j += 1
        return self.toks[j]

    def advance(self):
        t = self.peek()
        # 只消费 peek() 越过的那些换行；若 peek 返回的就是换行本身，则不能跳
        while self._raw().type == "NEWLINE" and self._raw() is not t:
            self.i += 1
        self.i += 1
        self.last_type = t.type
        return t

    def check(self, *types):
        return self.peek().type in types

    def match(self, *types):
        if self.check(*types):
            return self.advance()
        return None

    def match_any(self, *types):
        """跳过换行后若匹配则消费（用于悬空 else）。"""
        if self.peek_any().type in types:
            while self._raw().type == "NEWLINE":
                self.i += 1
            t = self.advance()
            return t
        return None

    def expect(self, type_, what=None):
        t = self.peek()
        if t.type != type_:
            got = what or t.type
            raise ParseError(f"期望 {type_}，得到 {got}", t.line, t.col)
        return self.advance()

    def skip_newlines(self):
        while self.peek().type == "NEWLINE":
            self.advance()

    def _require_fn(self, prefix):
        """只检查 fn 是否存在（不消费），给出友好错误；由 parse_fn 负责消费。"""
        if not self.check("fn"):
            t = self.peek()
            raise ParseError(f"{prefix} 之后需要 fn，得到 {t.type}", t.line, t.col)

    def _take_label(self):
        """若接下来的有效 Token 是 'IDENT :' 形式，消费并返回标签名（循环标签）。"""
        j = self.i
        while self.toks[j].type == "NEWLINE":
            j += 1
        if self.toks[j].type == "IDENT" and self.toks[j + 1].type == ":":
            label = self.toks[j].value
            self.advance()  # IDENT
            self.advance()  # :
            return label
        return None

    # ============================================================ 程序
    def parse_program(self):
        items = []
        self.skip_newlines()
        while not self.check("EOF"):
            items.append(self.parse_toplevel())
            self.skip_newlines()
        return ast.Program(items)

    def parse_toplevel(self):
        t = self.peek()
        if t.type == "pub":
            self.advance()
            return self.parse_decl_after_pub()
        if t.type == "inline":
            self.advance()
            return self.parse_fn(inline=True)
        if t.type == "export":
            self.advance()
            self._require_fn("export")
            return self.parse_fn(export=True)
        if t.type == "extern":
            self.advance()
            self._require_fn("extern")
            return self.parse_fn(extern=True)
        if t.type == "fn":
            return self.parse_fn()
        if t.type == "test":
            return self.parse_test()
        if t.type in ("const", "CONSTSAFE", "var", "VARSAFE"):
            return self.parse_var_decl()
        if t.type == "comptime":
            return self.parse_comptime()
        if t.type in ("struct", "enum", "union"):
            _, name, fields, methods = self.parse_struct_body(t.type)
            if t.type == "struct":
                return ast.StructDecl(name, fields, methods, False, t.line, t.col)
            if t.type == "enum":
                return ast.EnumDecl(name, fields, methods, False, t.line, t.col)
            return ast.UnionDecl(name, fields, methods, False, t.line, t.col)
        raise ParseError(f"顶层只支持声明，得到 {t.type}", t.line, t.col)

    def parse_decl_after_pub(self):
        t = self.peek()
        if t.type == "fn":
            return self.parse_fn(pub=True)
        if t.type == "inline":
            self.advance()
            self._require_fn("pub inline")
            return self.parse_fn(pub=True, inline=True)
        if t.type == "export":
            self.advance()
            self._require_fn("pub export")
            return self.parse_fn(pub=True, export=True)
        if t.type in ("const", "CONSTSAFE", "var", "VARSAFE"):
            decl = self.parse_var_decl()
            decl.pub = True
            return decl
        raise ParseError(f"pub 之后需要声明，得到 {t.type}", t.line, t.col)

    # ============================================================ 声明
    def parse_fn(self, pub=False, inline=False, export=False, extern=False):
        t = self.expect("fn")
        name = self.expect("IDENT", "函数名").value
        self.expect("(")
        params = []
        if not self.check(")"):
            params.append(self.parse_param())
            while self.match(","):
                if self.check(")"):
                    break
                params.append(self.parse_param())
        self.expect(")")

        ret_type = None
        if not self.check("{"):
            ret_type = self.parse_type()

        body = None
        if extern:
            self.match(";")
        else:
            body = self.parse_block()
        return ast.FnDecl(name, params, ret_type, body, pub, inline, export, extern, t.line, t.col)

    def parse_param(self):
        t = self.peek()
        comptime = False
        if t.type == "comptime":
            self.advance()
            comptime = True
            t = self.peek()
        name = self.expect("IDENT", "参数名").value
        self.expect(":")
        ptype = self.parse_type()
        return ast.Param(name, ptype, comptime, t.line, t.col)

    def parse_var_decl(self):
        t = self.advance()  # const / var / const? / var.
        kind = {"const": "const", "var": "var",
                "CONSTSAFE": "const_safe", "VARSAFE": "var_safe"}[t.type]

        # 解构声明: const [a, b] = expr
        if self.check("["):
            self.advance()
            targets = [self.expect("IDENT", "解构目标名").value]
            while self.match(","):
                if self.check("]"):
                    break
                targets.append(self.expect("IDENT", "解构目标名").value)
            self.expect("]")
            self.expect("=")
            value = self.parse_expr()
            return ast.DestructureDecl(kind, targets, value, t.line, t.col)

        name = self.expect("IDENT", "变量名").value
        vtype = None
        if self.match(":"):
            vtype = self.parse_type()
        value = None
        if self.match("="):
            value = self.parse_expr()
        decl = ast.VarDecl(kind, name, vtype, value, t.line, t.col)
        decl.pub = False
        return decl

    def parse_type(self):
        # 前缀修饰：?T 可选类型、!T 错误联合（Zag 书写习惯，可叠加如 !?int）
        if self.check("?", "!"):
            t = self.advance()
            inner = self.parse_type()
            if t.type == "?":
                return ast.OptionalType(inner, t.line, t.col)
            return ast.ErrorUnionType(inner, None, t.line, t.col)

        t = self.peek()
        if t.type in TYPE_KEYWORDS:
            self.advance()
            base = ast.TypeRef(t.type, t.line, t.col)
        elif t.type == "IDENT":
            # 行内错误集合类型: error{ Bad }!void
            if t.value == "error" and self._raw(1).type == "{":
                return self.parse_error_set()
            self.advance()
            base = ast.TypeRef(t.value, t.line, t.col)
        elif t.type == "[":
            self.advance()
            size = None
            if not self.check("]"):
                size = self.parse_expr()
            self.expect("]")
            elem = self.parse_type()
            base = ast.ArrayTypeNode(elem, size, t.line, t.col)
        else:
            raise ParseError(f"期望类型，得到 {t.type}", t.line, t.col)

        # Zig 风格命名错误联合：MyError!int（错误集在前，载荷在后）
        if self.check("!"):
            op = self.advance()
            inner = self.parse_type()
            return ast.ErrorUnionType(inner, base, op.line, op.col)

        # 后缀 ? 也接受（int? 与 ?int 等价）
        while self.check("?"):
            op = self.advance()
            base = ast.OptionalType(base, op.line, op.col)
        return base

    def parse_test(self):
        t = self.expect("test")
        name = self.expect("STRING", "测试名称字符串").value
        body = self.parse_block()
        return ast.TestDecl(name, body, t.line, t.col)

    def parse_struct_body(self, keyword, named=True):
        """struct/enum/union 共用的声明体解析。named=False 时为匿名聚合（const X = struct {...}）。"""
        t = self.expect(keyword)
        name = None
        if named:
            name = self.expect("IDENT", f"{keyword} 名称").value
            self.known_types.add(name)
        self.expect("{")
        fields, methods = [], []
        self.skip_newlines()
        while not self.check("}"):
            if self.check("fn"):
                methods.append(self.parse_fn())
            elif keyword == "enum":
                vt = self.expect("IDENT", "枚举成员名")
                value = None
                if self.match("="):
                    value = self.parse_expr()
                fields.append(ast.EnumVariant(vt.value, value, vt.line, vt.col))
            else:
                ft = self.expect("IDENT", "字段名")
                self.expect(":")
                ftype = self.parse_type()
                fields.append(ast.StructField(ft.value, ftype, ft.line, ft.col))
            self.match(",")
            self.skip_newlines()
        self.expect("}")
        return t, name, fields, methods

    # ============================================================ 语句
    def parse_block(self):
        t = self.expect("{")
        stmts = []
        self.skip_newlines()
        while not self.check("}"):
            if self.check("EOF"):
                raise ParseError("代码块未闭合", t.line, t.col)
            stmts.append(self.parse_statement())
            self.skip_newlines()
        self.expect("}")
        return ast.Block(stmts)

    def parse_statement(self):
        t = self.peek()
        tt = t.type

        if tt in ("const", "CONSTSAFE", "var", "VARSAFE"):
            return self.parse_var_decl()
        if tt == "fn":
            return self.parse_fn()
        if tt == "struct":
            _, name, fields, methods = self.parse_struct_body("struct")
            return ast.StructDecl(name, fields, methods, False, t.line, t.col)
        if tt == "enum":
            _, name, fields, methods = self.parse_struct_body("enum")
            return ast.EnumDecl(name, fields, methods, False, t.line, t.col)
        if tt == "union":
            _, name, fields, methods = self.parse_struct_body("union")
            return ast.UnionDecl(name, fields, methods, False, t.line, t.col)
        if tt == "if":
            return self.parse_if_stmt()
        if tt == "while":
            return self.parse_while()
        if tt == "for":
            return self.parse_for()
        if tt == "return":
            return self.parse_return()
        if tt == "defer":
            return self.parse_defer()
        if tt == "comptime":
            return self.parse_comptime()
        if tt == "break":
            self.advance()
            label = None
            self.match(":")  # 兼容 break :label 与 break label 两种写法
            if self.check("IDENT"):
                label = self.advance().value
            return ast.BreakStmt(label, t.line, t.col)
        if tt == "continue":
            self.advance()
            label = None
            self.match(":")
            if self.check("IDENT"):
                label = self.advance().value
            return ast.ContinueStmt(label, t.line, t.col)
        if tt == "{":
            return self.parse_block()

        # 循环标签: outer: while/for（IDENT 后紧跟 ':' 与循环关键字）
        if tt == "IDENT":
            j = self.i
            while self.toks[j].type == "NEWLINE":  # 对齐 peek() 跳过的新行
                j += 1
            j += 1
            while self.toks[j].type == "NEWLINE":
                j += 1
            if self.toks[j].type == ":":
                j += 1
                while self.toks[j].type == "NEWLINE":
                    j += 1
                if self.toks[j].type == "while":
                    return self.parse_while()
                if self.toks[j].type == "for":
                    return self.parse_for()

        # 表达式语句 / 赋值 / 自增自减
        expr = self.parse_expr()
        nt = self.peek()
        if nt.type == "=":
            self.advance()
            value = self.parse_expr()
            return ast.AssignStmt(expr, "=", value, t.line, t.col)
        if nt.type in COMPOUND_ASSIGN:
            self.advance()
            value = self.parse_expr()
            return ast.AssignStmt(expr, nt.type, value, t.line, t.col)
        if nt.type == "++":
            self.advance()
            return ast.IncDecStmt(expr, "++", t.line, t.col)
        if nt.type == "--":
            self.advance()
            return ast.IncDecStmt(expr, "--", t.line, t.col)
        return ast.ExprStmt(expr, t.line, t.col)

    def parse_statement_or_block(self):
        if self.check("{"):
            return self.parse_block()
        return self.parse_statement()

    def parse_if_stmt(self):
        t = self.expect("if")
        self.expect("(")
        cond = self.parse_expr()
        self.expect(")")
        # 可选值捕获: if (opt) |val| { ... }
        payload = None
        if self.check("|"):
            self.advance()
            payload = self.expect("IDENT", "可选值捕获名").value
            self.expect("|")
        then_stmt = self.parse_statement_or_block()
        else_stmt = None
        # 悬空 else：允许 else 写在下一行
        if self.match_any("else"):
            if self.check("if"):
                else_stmt = self.parse_if_stmt()
            else:
                else_stmt = self.parse_statement_or_block()
        return ast.IfStmt(cond, then_stmt, else_stmt, payload, t.line, t.col)

    def parse_while(self):
        label = self._take_label()
        t = self.expect("while")
        self.expect("(")
        cond = self.parse_expr()
        self.expect(")")
        body = self.parse_statement_or_block()
        return ast.WhileStmt(cond, body, label, t.line, t.col)

    def parse_for(self):
        label = self._take_label()
        t = self.expect("for")
        self.expect("(")
        init = None
        if not self.check(";"):
            if self.check("const", "CONSTSAFE", "var", "VARSAFE"):
                init = self.parse_var_decl()
            else:
                e = self.parse_expr()
                nt = self.peek()
                if nt.type == "=":
                    self.advance()
                    init = ast.AssignStmt(e, "=", self.parse_expr(), t.line, t.col)
                elif nt.type in COMPOUND_ASSIGN:
                    self.advance()
                    init = ast.AssignStmt(e, nt.type, self.parse_expr(), t.line, t.col)
                elif nt.type == "++":
                    self.advance()
                    init = ast.IncDecStmt(e, "++", t.line, t.col)
                elif nt.type == "--":
                    self.advance()
                    init = ast.IncDecStmt(e, "--", t.line, t.col)
                else:
                    init = ast.ExprStmt(e, t.line, t.col)
        self.expect(";")
        cond = None
        if not self.check(";"):
            cond = self.parse_expr()
        self.expect(";")
        post = None
        if not self.check(")"):
            if self.check("const", "CONSTSAFE", "var", "VARSAFE"):
                post = self.parse_var_decl()
            else:
                e = self.parse_expr()
                nt = self.peek()
                if nt.type == "=":
                    self.advance()
                    post = ast.AssignStmt(e, "=", self.parse_expr(), t.line, t.col)
                elif nt.type in COMPOUND_ASSIGN:
                    self.advance()
                    post = ast.AssignStmt(e, nt.type, self.parse_expr(), t.line, t.col)
                elif nt.type == "++":
                    self.advance()
                    post = ast.IncDecStmt(e, "++", t.line, t.col)
                elif nt.type == "--":
                    self.advance()
                    post = ast.IncDecStmt(e, "--", t.line, t.col)
                else:
                    post = ast.ExprStmt(e, t.line, t.col)
        self.expect(")")
        body = self.parse_statement_or_block()
        return ast.ForStmt(init, cond, post, body, label, t.line, t.col)

    def parse_return(self):
        t = self.expect("return")
        value = None
        if not self.check("NEWLINE", "}", "EOF", ";"):
            value = self.parse_expr()
        return ast.ReturnStmt(value, t.line, t.col)

    def parse_defer(self):
        t = self.expect("defer")
        body = self.parse_statement_or_block()
        return ast.DeferStmt(body, t.line, t.col)

    def parse_comptime(self):
        t = self.expect("comptime")
        body = self.parse_block()
        return ast.ComptimeStmt(body, t.line, t.col)

    # ============================================================ 表达式
    def parse_expr(self):
        return self.parse_or()

    def parse_or(self):
        left = self.parse_and()
        while self.check("or"):
            self.advance()
            right = self.parse_and()
            left = ast.Binary("or", left, right, left.line, left.col)
        return left

    def parse_and(self):
        left = self.parse_comparison()
        while self.check("and"):
            self.advance()
            right = self.parse_comparison()
            left = ast.Binary("and", left, right, left.line, left.col)
        return left

    def parse_comparison(self):
        left = self.parse_orelse()
        while self.check(*COMPARISON_OPS):
            op = self.advance()
            right = self.parse_orelse()
            left = ast.Binary(op.type, left, right, op.line, op.col)
        return left

    def parse_orelse(self):
        # orelse 与 Zig 同优先级：比比较松、比加减紧
        left = self.parse_additive()
        while self.check("orelse"):
            op = self.advance()
            right = self.parse_additive()
            left = ast.Binary("orelse", left, right, op.line, op.col)
        return left

    def parse_additive(self):
        left = self.parse_multiplicative()
        while self.check("+", "-"):
            op = self.advance()
            right = self.parse_multiplicative()
            left = ast.Binary(op.type, left, right, op.line, op.col)
        return left

    def parse_multiplicative(self):
        left = self.parse_unary()
        while self.check("*", "/", "%"):
            op = self.advance()
            right = self.parse_unary()
            left = ast.Binary(op.type, left, right, op.line, op.col)
        return left

    def parse_unary(self):
        t = self.peek()
        if t.type == "-":
            self.advance()
            return ast.Unary("-", self.parse_unary(), t.line, t.col)
        if t.type == "!":
            self.advance()
            return ast.Unary("!", self.parse_unary(), t.line, t.col)
        if t.type == "not":
            self.advance()
            return ast.Unary("not", self.parse_unary(), t.line, t.col)
        if t.type == "&":
            self.advance()
            return ast.AddressOf(self.parse_unary(), t.line, t.col)
        if t.type == "$":
            self.advance()
            return ast.Deref(self.parse_unary(), t.line, t.col)
        if t.type == "try":
            self.advance()
            return ast.TryExpr(self.parse_unary(), t.line, t.col)
        return self.parse_postfix()

    def parse_postfix(self):
        expr = self.parse_primary()
        while True:
            t = self.peek()
            if t.type == "(":
                expr = self.parse_call(expr)
            elif t.type == "[":
                expr = self.parse_index_or_slice(expr)
            elif t.type == ".":
                self.advance()
                name = self.expect("IDENT", "字段名").value
                expr = ast.Field(expr, name, t.line, t.col)
            elif t.type == "catch":
                expr = self.parse_catch(expr)
            else:
                break
        return expr

    def parse_call(self, callee):
        self.expect("(")
        args = []
        if not self.check(")"):
            args.append(self.parse_expr())
            while self.match(","):
                if self.check(")"):
                    break
                args.append(self.parse_expr())
        self.expect(")")
        return ast.Call(callee, args, callee.line, callee.col)

    def parse_index_or_slice(self, base):
        t = self.expect("[")
        # [..end]
        if self.check(".."):
            self.advance()
            end = self.parse_expr()
            self.expect("]")
            return ast.SliceExpr(base, None, end, t.line, t.col)
        start = self.parse_expr()
        if self.check(".."):
            self.advance()
            end = None
            if not self.check("]"):
                end = self.parse_expr()
            self.expect("]")
            return ast.SliceExpr(base, start, end, t.line, t.col)
        self.expect("]")
        return ast.Index(base, start, t.line, t.col)

    def parse_catch(self, expr):
        t = self.expect("catch")
        capture = None
        if self.check("|"):
            self.advance()
            capture = self.expect("IDENT", "错误捕获名").value
            self.expect("|")
        if self.check("{"):
            payload = self.parse_block()
        elif self.check("return", "break", "continue"):
            # catch |err| return / break / continue
            payload = self.parse_statement()
        else:
            payload = self.parse_expr()
        return ast.CatchExpr(expr, capture, payload, t.line, t.col)

    def parse_primary(self):
        t = self.peek()
        tt = t.type

        if tt == "INT":
            self.advance()
            return ast.IntLit(t.value, t.line, t.col)
        if tt == "FLOAT":
            self.advance()
            return ast.FloatLit(t.value, t.line, t.col)
        if tt == "STRING":
            self.advance()
            return ast.StringLit(t.value, t.line, t.col)
        if tt == "true":
            self.advance()
            return ast.BoolLit(True, t.line, t.col)
        if tt == "false":
            self.advance()
            return ast.BoolLit(False, t.line, t.col)
        if tt == "null":
            self.advance()
            return ast.NullLit(t.line, t.col)
        if tt in ("struct", "enum", "union"):
            # 匿名聚合类型字面量: const Point = struct { x: int }
            _, _, fields, methods = self.parse_struct_body(tt, named=False)
            if tt == "struct":
                return ast.StructDecl("", fields, methods, False, t.line, t.col)
            if tt == "enum":
                return ast.EnumDecl("", fields, methods, False, t.line, t.col)
            return ast.UnionDecl("", fields, methods, False, t.line, t.col)
        if tt in TYPE_KEYWORDS:
            # 类型关键字出现在表达式位置：作为类型实参传递，如 maxOf(int, 3, 7)
            self.advance()
            return ast.TypeRef(t.type, t.line, t.col)
        if tt == "IDENT":
            # 错误集合: error{ Bad, Worse }
            if t.value == "error" and self._raw(1).type == "{":
                return self.parse_error_set()
            # 结构体字面量: Name{ .field = value }
            if self._raw(1).type == "{":
                return self.parse_struct_lit()
            self.advance()
            return ast.Ident(t.value, t.line, t.col)
        if tt == "(":
            self.advance()
            e = self.parse_expr()
            self.expect(")")
            return e
        if tt == "[":
            return self.parse_array_lit()
        if tt == ".":
            # .red 枚举字面量，或 .{ } 匿名结构体字面量
            self.advance()
            if self.check("{"):
                fields = self.parse_struct_fields()
                return ast.StructLit("", fields, t.line, t.col)
            name = self.expect("IDENT", "枚举成员名").value
            return ast.EnumLit(name, t.line, t.col)
        if tt == "if":
            return self.parse_if_expr()
        if tt == "@":
            return self.parse_builtin()
        raise ParseError(f"意外的 Token {t.value!r}", t.line, t.col)

    def parse_error_set(self):
        """error{ Bad, Worse } —— 错误集合字面量（Zag 与 Zig 同形）。"""
        t = self.expect("IDENT", "error")
        self.expect("{")
        variants = []
        self.skip_newlines()
        while not self.check("}"):
            variants.append(self.expect("IDENT", "错误名").value)
            self.match(",")
            self.skip_newlines()
        self.expect("}")
        return ast.ErrorSetLit(variants, t.line, t.col)

    def parse_struct_lit(self):
        t = self.expect("IDENT", "结构体名")
        fields = self.parse_struct_fields()
        return ast.StructLit(t.value, fields, t.line, t.col)

    def parse_struct_fields(self):
        self.expect("{")
        fields = []
        self.skip_newlines()
        while not self.check("}"):
            self.expect(".")
            fname = self.expect("IDENT", "字段名").value
            self.expect("=")
            fvalue = self.parse_expr()
            fields.append((fname, fvalue))
            self.match(",")
            self.skip_newlines()
        self.expect("}")
        return fields

    def parse_array_lit(self):
        t = self.expect("[")
        elems = []
        self.skip_newlines()
        if not self.check("]"):
            elems.append(self.parse_expr())
            while self.match(","):
                if self.check("]"):
                    break
                elems.append(self.parse_expr())
                self.skip_newlines()
        self.expect("]")
        if not elems:
            raise ParseError("空数组字面量无法推导元素类型", t.line, t.col)
        return ast.ArrayLit(elems, t.line, t.col)

    def parse_if_expr(self):
        t = self.expect("if")
        self.expect("(")
        cond = self.parse_expr()
        self.expect(")")
        # 可选值捕获: if (opt) |val| expr else expr
        payload = None
        if self.check("|"):
            self.advance()
            payload = self.expect("IDENT", "可选值捕获名").value
            self.expect("|")
        # 分支可以是表达式，也可以是 { 块 }（块作为值表达式）
        if self.check("{"):
            then_expr = self.parse_block()
        else:
            then_expr = self.parse_expr()
        self.expect("else")
        if self.check("{"):
            else_expr = self.parse_block()
        else:
            else_expr = self.parse_expr()
        return ast.IfExpr(cond, then_expr, else_expr, payload, t.line, t.col)

    def parse_builtin(self):
        t = self.expect("@")
        name = self.expect("IDENT", "内建函数名").value
        self.expect("(")
        args = []
        if not self.check(")"):
            args.append(self.parse_builtin_arg())
            while self.match(","):
                if self.check(")"):
                    break
                args.append(self.parse_builtin_arg())
        self.expect(")")
        if name == "import":
            if len(args) != 1 or not isinstance(args[0], ast.StringLit):
                raise ParseError("@import 需要一个字符串参数", t.line, t.col)
            return ast.ImportExpr(args[0].value, t.line, t.col)
        return ast.BuiltinCall(name, args, t.line, t.col)

    def parse_builtin_arg(self):
        """内建函数参数可以是类型（@sizeOf(int)）或值（@TypeOf(x)）。"""
        t = self.peek()
        if t.type in TYPE_KEYWORDS:
            self.advance()
            return ast.TypeRef(t.type, t.line, t.col)
        if t.type == "IDENT":
            # 已知类型名 + 后面是 ) 或 , → 视为类型参数
            nxt = self._raw(1).type
            if t.value in self.known_types and nxt in (")", ","):
                self.advance()
                return ast.TypeRef(t.value, t.line, t.col)
        return self.parse_expr()


def parse(source):
    """便捷入口：源码 -> AST。"""
    tokens = tokenize(source)
    return Parser(tokens).parse_program()
