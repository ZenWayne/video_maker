"""结构性守卫：文件级 `pytestmark = requires_cos` 必须名副其实。

背景：本项目已经三次犯同一个错误——给整个测试文件打上模块级
`pytestmark = requires_cos`，但文件里其实混着不需要真实 COS 的用例（比如一个
纯 404 路径检查、一个断言挂载已删除的用例）。结果是这些本来该在无凭证环境里
天天跑的回归测试被静默 skip 掉，没人会注意到——直到某次审查手动数 skip 数才
发现。靠人记性防不住，所以这里加一道会被 CI 拦下的结构性检查。

判据（与 conftest_cos.py 的既有约定一致）：**带 `cos_prefix` fixture 参数的
测试函数才需要 `@requires_cos`**。文件级 `pytestmark = requires_cos` 只有在
"文件内每一个测试函数都带 cos_prefix 参数"时才是合法用法（例如
test_filmstrip_cache.py——三个用例都要真实 COS + ffmpeg，逐个标反而啰嗦）。
一旦文件里出现哪怕一个不带 cos_prefix 的测试函数，就说明这个文件级标记在
"顺手"过度 gate 别的用例，必须拆成逐个 `@requires_cos`。

本测试是纯 AST 静态扫描，不 import 任何被扫描的模块（避免收集期的循环 import/
副作用），不需要 COS 凭证，也不该被 @requires_cos 装饰——它本身就是防止"过度
gate"的哨兵，标了 gate 就自相矛盾了。
"""
import ast
from pathlib import Path

INTEGRATION_DIR = Path(__file__).resolve().parents[1] / "integration"

# 这些不是"测试文件"，不在扫描范围内：
_SKIP_FILES = {"conftest.py", "conftest_cos.py", "conftest_cos_seed.py"}


def _mark_names(value: ast.expr) -> list[str]:
    """从 `pytestmark = X` 的右值里提取标记名。支持单个名字和 list/tuple。"""
    if isinstance(value, ast.Name):
        return [value.id]
    if isinstance(value, (ast.List, ast.Tuple)):
        return [e.id for e in value.elts if isinstance(e, ast.Name)]
    return []


def _has_module_level_requires_cos_mark(tree: ast.Module) -> bool:
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets):
            continue
        if "requires_cos" in _mark_names(node.value):
            return True
    return False


def _test_functions(tree: ast.Module):
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
            yield node


def _param_names(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    args = fn.args
    names = {a.arg for a in args.args} | {a.arg for a in args.kwonlyargs}
    if args.vararg:
        names.add(args.vararg.arg)
    return names


def test_file_level_requires_cos_mark_implies_every_test_uses_cos_prefix():
    violations = []
    scanned = []

    for path in sorted(INTEGRATION_DIR.glob("*.py")):
        if path.name in _SKIP_FILES:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        scanned.append(path.name)

        if not _has_module_level_requires_cos_mark(tree):
            continue

        for fn in _test_functions(tree):
            if "cos_prefix" not in _param_names(fn):
                violations.append(f"{path.name}::{fn.name}")

    # 扫描面板不能悄悄缩水——至少要扫到目录里现存的 .py 文件数，防止本测试
    # 自己因为 glob/路径写错而"什么都没查"却假装通过。
    assert len(scanned) >= 20, (
        f"只扫到 {len(scanned)} 个文件，看起来 INTEGRATION_DIR 路径不对: "
        f"{INTEGRATION_DIR}"
    )

    assert not violations, (
        "以下测试函数所在文件用了模块级 `pytestmark = requires_cos`，但函数本身"
        "不带 `cos_prefix` 参数——这些用例其实不需要真实 COS，却被文件级标记"
        "连带 gate 掉了（无凭证环境里会被静默 SKIP，没人发现）。"
        "请把文件级 pytestmark 拆成只在真正需要 COS 的测试函数上单独加"
        "`@requires_cos` + `cos_prefix` 参数：\n  " + "\n  ".join(violations)
    )
