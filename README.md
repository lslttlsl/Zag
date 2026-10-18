# The Zag Compiler

**Zag** is a sister language that stands opposite to [Zig](https://ziglang.org): it differs from Zig by a single letter,
with 1:1 syntax parity but simpler rules. This repository is the reference implementation of Zag — a
**source-to-source compiler** written in pure Python: it translates `.zag` into `.zig` and then hands it off to the Zig toolchain for the final build.

```
example.zag ──► Zag compiler (Python) ──► example.zig ──► zig build-exe / zig run / zig test
```

Zero dependencies: all you need is Python 3.8+ and a Zig executable.

## Quick Start

```bash
# Compile (defaults to a same-directory, same-name .zig output)
python -m src.main examples/example.zag

# Compile and run immediately
python -m src.main examples/example.zag --run

# Compile and run tests (test blocks)
python -m src.main examples/example.zag --test

# Specify output / specify zig path / dump AST
python -m src.main examples/example.zag -o out.zig
python -m src.main examples/example.zag --run --zig /path/to/zig
python -m src.main examples/example.zag --ast
```

`--zig` may also be set via the `ZIG` environment variable; otherwise it looks for `zig` on the `PATH`.

The sample `examples/example.zag` covers every core construct in the language specification,
while `examples/mymod.zag` demonstrates module imports (`@import("mymod.zag")` is automatically compiled into `mymod.zig`).

## Language Tour

```zig
const std = @import("std")

pub fn main() void {
    std.print("Hello, Zag")

    // The Four Pillars: Zag's signature variable rules
    const x = 10          // immutable; error if reassigned, also error if unused
    var y = 20            // mutable; error if never mutated after declaration
    y = 21
    const? z = 30         // const-safe: attempts to mutate are silently ignored, program continues
    z = 99                // z is still 30
    var. a = 40           // declaration-safe: unused/unmutated silently ignored, never errors

    if (y > 20) {
        std.print("big")
    } else {
        std.print("small")
    }

    for (var i = 0; i < 3; i++) {
        std.print(i)
    }
}
```

### Full Syntax Reference

| # | Feature | Syntax | Compiles to |
|---|---------|--------|-------------|
| 1 | Entry point | `pub fn main() void` | `pub fn main() void` |
| 2 | Standard library import | `const std = @import("std")` | unchanged |
| 3 | **The Four Pillars** | `const` / `var` / `const?` / `var.` | see table below |
| 4 | The four base types | `int` `float` `bool` `string` | `i32` `f64` `bool` `[]const u8` |
| 5 | Logical keywords | `and` `or` `not` | unchanged (Zig native keywords) |
| 6 | Operators | `+ - * / %` `== != < <= > >=` | unchanged |
| 7 | if statement | `if (c) { } else { }`, dangling else supported | unchanged |
| 8 | Ternary expression | `if (c) "big" else "small"` (if is an expression) | unchanged |
| 9 | Loops | C-style `while` / `for (init; cond; post)` | while + continue expression |
| 10 | Functions | `fn add(a: int, b: int) int` | unchanged |
| 11 | Constants | `const PI = 3.14159` | unchanged |
| 12 | Comments | `//` `///` `/* nested */` | unchanged |
| 13 | Arrays | `[1, 2, 3]` | `[_]i32{ ... }` |
| 14 | Slices | `arr[0..2]` / `arr[1..]` | unchanged |
| 15 | Error unions | `!void` / `MyError!int` / `error{ Bad }` | unchanged |
| 16 | Error values | `error.X` / `try` / `catch \|err\| { }` | unchanged |
| 17 | Optional types | `?int` / `if (o) \|v\| { }` / `orelse` | unchanged |
| 18 | Destructuring | `const [a, b] = x` | `const a = x[0]; const b = x[1];` |
| 19 | Structs | `const P = struct { x: int }` | unchanged |
| 20 | Enums | `const C = enum { red, green }` | unchanged |
| 21 | Unions | `const V = union { i: int }` | unchanged |
| 22 | Pointers | `$ptr = 100` (dereference, replaces Zig's `.*`) | `ptr.* = 100` |
| 23 | Allocator | `std.select("gpa")` + `.allocator(n)` | `std.heap.page_allocator` + `.alloc(u8, n)` |
| 24 | defer | `defer { }` / `defer expr` | unchanged |
| 25 | Compile-time | `comptime { }` | unchanged |
| 26 | Generics | `fn f(comptime T: type, a: T) T` | unchanged |
| 27 | Inline | `inline fn` | unchanged |
| 28 | Builtin functions | `@sizeOf` `@typeName` `@errorName` `@import` … | passthrough unchanged |
| 29 | Module import | `@import("mymod.zag")` | `@import("mymod.zig")` + recursive compile |
| 30 | C interoperability | `export fn` / `extern fn` | unchanged |
| 31 | Conditional compilation | `@import("builtin").os.tag == .windows` | unchanged |
| 32 | Tests | `test "name" { }` | unchanged |

### How The Four Pillars Map at Compile Time

| Declaration | Unused | Mutated | Emission |
|-------------|--------|---------|----------|
| `const x` | **error** (hint: use `const?`) | **error** (hint: use `const?`) | `const x` |
| `var y` | error (hint: use `var.`) | OK | `var y` |
| `const? z` | silent (`_ = &z;`) | silently ignored (`_ = 99;`) | `const z` |
| `var. a` | silent (`_ = &a;`) | OK | degrades to `const` if unmutated |

> Container-level (top-level) variables are not enforced, consistent with Zig; function-local ones are strictly checked and produce Chinese error messages,
> e.g. `zag: error: file.zag:2:5: 不能给常量 'x' 赋值（…请使用 const?）`.

## Project Structure

```
zag/
├── src/
│   ├── __init__.py     # package marker and version
│   ├── lexer.py        # lexical analysis (newline is a soft terminator; const?/var. are single tokens)
│   ├── ast_nodes.py    # all AST nodes (dataclasses)
│   ├── parser.py       # recursive descent + precedence climbing; multi-line expressions and dangling else
│   ├── codegen.py      # Zig code generation + Four Pillars semantics + static type inference
│   └── main.py         # CLI: compile / --run / --test / --ast, recursively compiles imported modules
├── examples/
│   ├── example.zag     # sample covering all core syntax
│   └── mymod.zag       # a sample module that gets imported
├── syntax/
│   └── zag.mtsx        # TextMate syntax highlighting definition
├── .gitignore
├── LICENSE             # MIT
└── README.md
```

## Implementation Notes

- **Newline as statement terminator**: Zag has no semicolons; a newline is a soft statement terminator. The lexer emits `NEWLINE` tokens, and the parser ignores newlines when the previous token belongs to the "obviously incomplete" set (binary operators, `(`, `,`, etc.), enabling multi-line expressions; `else` may also be written on the following line (dangling else).
- **Static type inference** (best effort): it infers literal and function-return types to drive `std.print`'s format specifier choice (strings use `{s}`, everything else uses `{}`) and array literal element types; `pub fn` return types of imported modules are also pre-scanned and registered.
- **Zig 0.17 adaptation**: every emission strategy is verified against the real compiler, including: local `var` must be explicitly typed (comptime_int restriction), unused captures must be elided (`catch`) or placeholder (`if`'s `|_|`, because the payload performs the unwrap), error-union value blocks must use the `blk: { … break :blk …; }` form, i32 indices require `@intCast`, and a `var` whose address is taken must remain `var` (emitting a self-assignment satisfies Zig's "must be mutated" check), etc.

## Known Limitations (v1)

- Type inference is heuristic; in complex scenarios `std.print` may fall back to `{any}`;
- No support for closures, traits/interfaces, or concurrency primitives;
- Generics only support the `comptime T: type` parameter form;
- Array literals must be non-empty (an empty array cannot infer its element type).

## License

MIT, see [LICENSE](LICENSE).
