"""Deterministic static analysis of Python files using the `ast` module.

These checks are cheap, reliable, and run before the LLM. Their findings are
posted directly AND passed to the LLM as hints, so the model can focus on
deeper issues instead of re-discovering mechanical ones.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass

from app.models import Finding, Severity

SECRET_NAME_RE = re.compile(r"(password|passwd|secret|api[_-]?key|access[_-]?key|token|private[_-]?key)", re.I)
MAX_FUNC_LINES = 60
MAX_COMPLEXITY = 10
MAX_ARGS = 6


@dataclass
class _Ctx:
    path: str
    findings: list[tuple[Finding, int]]  # (finding, end_line) - range used for function-level checks

    def add(self, line: int, sev: Severity, cat: str, title: str, body: str, end_line: int | None = None):
        f = Finding(path=self.path, line=line, severity=sev, category=cat, title=title, body=body, source="ast")
        self.findings.append((f, end_line or line))


def _call_name(node: ast.Call) -> str:
    """Dotted name of a call target, e.g. 'subprocess.run' or 'eval'."""
    parts: list[str] = []
    cur = node.func
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    return ".".join(reversed(parts))


def cyclomatic_complexity(func: ast.AST) -> int:
    score = 1
    for node in ast.walk(func):
        if isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.IfExp, ast.ExceptHandler,
                             ast.With, ast.AsyncWith, ast.Assert, ast.comprehension)):
            score += 1
        elif isinstance(node, ast.BoolOp):
            score += len(node.values) - 1
        elif isinstance(node, ast.match_case):
            score += 1
    return score


class _Visitor(ast.NodeVisitor):
    def __init__(self, ctx: _Ctx, is_test: bool):
        self.ctx = ctx
        self.is_test = is_test

    # ---- functions ----
    def _check_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef):
        end = getattr(node, "end_lineno", node.lineno)
        for default in node.args.defaults + node.args.kw_defaults:
            if isinstance(default, (ast.List, ast.Dict, ast.Set)) or (
                isinstance(default, ast.Call) and _call_name(default) in {"list", "dict", "set"}
            ):
                self.ctx.add(default.lineno, Severity.medium, "bug", "Mutable default argument",
                             f"`{node.name}` uses a mutable default value. It is created once at definition "
                             "time and shared across calls. Use `None` and create the object inside the function.")
        n_args = len(node.args.args) + len(node.args.kwonlyargs) + len(node.args.posonlyargs)
        if node.args.args and node.args.args[0].arg in {"self", "cls"}:
            n_args -= 1
        if n_args > MAX_ARGS:
            self.ctx.add(node.lineno, Severity.low, "maintainability", "Too many parameters",
                         f"`{node.name}` takes {n_args} parameters (>{MAX_ARGS}). Consider grouping them "
                         "into a dataclass or config object.", end)
        length = end - node.lineno + 1
        if length > MAX_FUNC_LINES:
            self.ctx.add(node.lineno, Severity.low, "maintainability", "Long function",
                         f"`{node.name}` is {length} lines long (>{MAX_FUNC_LINES}). Consider splitting it.", end)
        cc = cyclomatic_complexity(node)
        if cc > MAX_COMPLEXITY:
            self.ctx.add(node.lineno, Severity.medium, "maintainability", "High cyclomatic complexity",
                         f"`{node.name}` has cyclomatic complexity {cc} (>{MAX_COMPLEXITY}), which makes it "
                         "hard to test and reason about.", end)
        self.generic_visit(node)

    visit_FunctionDef = _check_function
    visit_AsyncFunctionDef = _check_function

    # ---- exceptions ----
    def visit_ExceptHandler(self, node: ast.ExceptHandler):
        if node.type is None:
            self.ctx.add(node.lineno, Severity.medium, "error-handling", "Bare `except:`",
                         "A bare `except:` also catches `KeyboardInterrupt` and `SystemExit`. "
                         "Catch a specific exception (or at least `Exception`).")
        elif len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
            self.ctx.add(node.lineno, Severity.medium, "error-handling", "Exception silently swallowed",
                         "This handler does nothing (`pass`), so failures disappear without a trace. "
                         "Log the error or handle it explicitly.")
        self.generic_visit(node)

    # ---- calls ----
    def visit_Call(self, node: ast.Call):
        name = _call_name(node)
        if name in {"eval", "exec"}:
            self.ctx.add(node.lineno, Severity.high, "security", f"Use of `{name}()`",
                         f"`{name}()` executes arbitrary code. If any part of the input is user-controlled "
                         "this is a code-injection vulnerability. Prefer `ast.literal_eval` or explicit parsing.")
        elif name.startswith("subprocess.") or name in {"os.system", "os.popen"}:
            shell = any(k.arg == "shell" and isinstance(k.value, ast.Constant) and k.value.value is True
                        for k in node.keywords)
            if shell or name in {"os.system", "os.popen"}:
                self.ctx.add(node.lineno, Severity.high, "security", "Shell command injection risk",
                             f"`{name}` runs through a shell. Pass an argument list with `shell=False` "
                             "to avoid command injection.")
        elif name in {"pickle.load", "pickle.loads", "marshal.loads"}:
            self.ctx.add(node.lineno, Severity.high, "security", "Unsafe deserialization",
                         f"`{name}` can execute arbitrary code when given untrusted data.")
        elif name == "yaml.load" and not any(k.arg == "Loader" for k in node.keywords) and len(node.args) < 2:
            self.ctx.add(node.lineno, Severity.high, "security", "`yaml.load` without a safe Loader",
                         "Use `yaml.safe_load()` or pass `Loader=yaml.SafeLoader`.")
        elif name == "print" and not self.is_test:
            self.ctx.add(node.lineno, Severity.low, "style", "`print()` left in code",
                         "Use the `logging` module instead of `print` in application code.")
        elif any(k.arg == "verify" and isinstance(k.value, ast.Constant) and k.value.value is False
                 for k in node.keywords):
            self.ctx.add(node.lineno, Severity.high, "security", "TLS verification disabled",
                         "`verify=False` disables certificate checks and allows man-in-the-middle attacks.")
        self.generic_visit(node)

    # ---- assignments: hardcoded secrets ----
    def visit_Assign(self, node: ast.Assign):
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str) and len(node.value.value) >= 8:
            for target in node.targets:
                tname = target.id if isinstance(target, ast.Name) else getattr(target, "attr", "")
                if tname and SECRET_NAME_RE.search(tname) and not self.is_test:
                    self.ctx.add(node.lineno, Severity.critical, "security", "Possible hardcoded secret",
                                 f"`{tname}` is assigned a string literal. Load secrets from environment "
                                 "variables or a secret manager, and rotate this value if it is real.")
        self.generic_visit(node)

    # ---- comparisons ----
    def visit_Compare(self, node: ast.Compare):
        for op, comp in zip(node.ops, node.comparators):
            if isinstance(op, (ast.Eq, ast.NotEq)) and isinstance(comp, ast.Constant) and comp.value is None:
                self.ctx.add(node.lineno, Severity.low, "style", "Comparison to `None` with `==`",
                             "Use `is None` / `is not None` instead of `==` / `!=`.")
        self.generic_visit(node)

    # ---- imports ----
    def visit_ImportFrom(self, node: ast.ImportFrom):
        if any(a.name == "*" for a in node.names):
            self.ctx.add(node.lineno, Severity.low, "style", "Wildcard import",
                         f"`from {node.module} import *` pollutes the namespace and hides where names come from.")
        self.generic_visit(node)


def _unused_imports(tree: ast.Module, ctx: _Ctx):
    imported: dict[str, int] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for a in node.names:
                imported[(a.asname or a.name).split(".")[0]] = node.lineno
        elif isinstance(node, ast.ImportFrom) and node.module != "__future__":
            for a in node.names:
                if a.name != "*":
                    imported[a.asname or a.name] = node.lineno
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    # names referenced in __all__ or string annotations count as used
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            used.update(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", n.value))
    for name, line in imported.items():
        if name not in used:
            ctx.add(line, Severity.low, "maintainability", "Unused import", f"`{name}` is imported but never used.")


def analyze_python(path: str, source: str, changed_lines: set[int] | None = None) -> list[Finding]:
    """Run all checks. If `changed_lines` is given, keep only findings touching changed code."""
    ctx = _Ctx(path=path, findings=[])
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as e:
        return [Finding(path=path, line=e.lineno or 1, severity=Severity.critical, category="bug",
                        title="Syntax error", body=f"This file does not parse: `{e.msg}`.", source="ast")]

    is_test = "test" in path.rsplit("/", 1)[-1].lower() or "/tests/" in f"/{path}"
    _Visitor(ctx, is_test).visit(tree)
    if not path.endswith("__init__.py"):
        _unused_imports(tree, ctx)

    if changed_lines is None:
        return [f for f, _ in ctx.findings]

    kept: list[Finding] = []
    for f, end in ctx.findings:
        start = f.line
        touched = [ln for ln in changed_lines if start <= ln <= end]
        if not touched:
            continue
        if f.line not in changed_lines:  # function-level finding: anchor on first changed line in range
            f.line = min(touched)
        kept.append(f)
    return kept
