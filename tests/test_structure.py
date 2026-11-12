"""结构约定的机器校验（对应 docs/代码规范.md）。

这里检查的不是"风格口味"，而是**包结构、依赖方向、边界**这类走样了就会静默出错的约定：

1. 依赖边必须与规范里的分层表一致（新增依赖要同时改规范与这张表，让它出现在 diff 里）；
2. 不许有环；L0 不许依赖内部模块；worker 只许依赖 worker_protocol；
3. 主包不许在模块顶层 import 重依赖（numpy/torch/faster_whisper/sherpa_onnx/gradio…）；
4. 库模块不许 `print` 到标准输出；不许裸 `except`；`except Exception` 必须写清为什么安全；
5. 每个模块要有 `__all__`（worker 脚本与入口除外）；
6. 函数超过 150 行必须在豁免名单里（名单分"声明式不拆"和"欠债待拆"两类）；
7. `# type: ignore` 总数不许超过基线（治欠债时请同步下调）。

失败信息都写了改法：要么改代码，要么改规范与这里 —— 两者必须同时出现在同一个 diff 里。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "submatrix"
MODULES = {path.stem: path for path in sorted(PKG.glob("*.py"))}
WORKER_MODULES = {name for name in MODULES if name.endswith("_worker")}

# ---------------------------------------------------------------- 分层（规范 §1.1）

LAYERS = {
    "L0": {
        "srt", "textutil", "langs", "glossary", "media", "interpreters",
        "worker_protocol", "cost", "history",
    },
    "L1": {
        "validate", "clean", "condense", "export", "report", "merge", "probe",
        "asr", "diarize", "translate", "burn", "mark", "tts", "localtts", "bgm",
    },
    "L2": {"dub", "pipeline", "layout", "matrix", "voices", "cli", "gui"},
    "worker": WORKER_MODULES,
    # 包本身的两个入口：__init__ 只放版本号（等于 L0），__main__ 只转发给 cli
    "entry": {"__init__", "__main__"},
}

# 当前实际的依赖边（规范 §1.1 的表格就是照这个写的）。
# 新增一条边时：先问"是不是分层画错了"，然后同时更新 docs/代码规范.md §1.1 与这里。
EXPECTED_EDGES = {
    "__init__": set(),
    "__main__": {"cli"},
    "asr": {"interpreters", "report", "srt", "worker_protocol"},
    "asr_worker": {"worker_protocol"},
    "bgm": {"interpreters", "media"},
    "burn": {"export", "media", "report", "srt"},
    "clean": {"srt"},
    "cli": {
        "asr", "burn", "cost", "diarize", "dub", "glossary", "gui", "history",
        "mark", "matrix", "merge", "pipeline", "probe", "srt", "translate", "validate",
    },
    "condense": {"textutil"},
    "cost": set(),
    "diarize": {"interpreters", "media", "report", "srt", "worker_protocol"},
    "diarize_worker": {"worker_protocol"},
    "dub": {"bgm", "localtts", "media", "report", "srt", "tts"},
    "export": {"srt", "textutil"},
    "glossary": set(),
    "gui": {"cost", "history", "validate", "voices"},
    "history": set(),
    "interpreters": set(),
    "langs": set(),
    "layout": {"langs", "srt", "validate"},
    "localtts": {"interpreters", "worker_protocol"},
    "mark": {"media", "probe", "report"},
    "matrix": {"export", "layout", "report", "srt", "validate"},
    "media": set(),
    "merge": {"report", "srt"},
    "pipeline": {
        "asr", "burn", "clean", "cost", "diarize", "dub", "glossary", "localtts",
        "mark", "matrix", "merge", "probe", "report", "srt", "translate", "tts",
        "validate",
    },
    "probe": {"media", "report", "srt"},
    "report": {"srt", "validate"},
    "srt": set(),
    "textutil": set(),
    "translate": {"clean", "condense", "glossary", "langs", "report", "srt", "textutil"},
    "tts": {"interpreters"},
    "tts_worker": {"worker_protocol"},
    "validate": {"langs", "srt", "textutil"},
    "voices": {"pipeline", "tts"},
    "worker_protocol": set(),
}

# 模块顶层不许出现的重依赖（必须留在 *_worker.py 里；gui 的 gradio 是函数内延迟 import）
HEAVY_PACKAGES = {
    "gradio", "numpy", "torch", "faster_whisper", "sherpa_onnx", "demucs",
    "pyannote", "onnxruntime", "soundfile", "librosa",
}

# 库模块不许 print 到 stdout；CLI 入口例外（它就是负责打印的）
PRINT_EXEMPT = {"cli"}

# 没有 __all__ 的模块：入口与 worker 脚本
ALL_EXEMPT = {"__init__", "__main__"} | WORKER_MODULES

# >150 行的函数（规范 §2.3 / §10.2）：
#   声明式 = 行数来自"字段/控件/参数有多少"，拆开只让人跳来跳去；
#   欠债   = 与重构前的 run_pipeline 同构，等下一批拆。
LENGTH_OVERRIDES = {
    # 声明式
    "gui:build_app": "Gradio 控件装配",
    "dub:_build_report": "报告字典字面量",
    "cli:_add_pipeline_args": "参数表",
    # 欠债待拆
    "diarize:run_diarize": "待拆：_DiarizeSession + 阶段函数",
    "mark:run_mark": "待拆",
    "burn:run_burn": "待拆",
    "asr:run_asr": "待拆",
    "pipeline:_stage_diarize": "待拆",
    "diarize_worker:main": "待拆",
}
MAX_LINES = 150

# 规范 §3：报告 JSON 的 union-attr 欠债。治一处就下调一处，不许只增长。
TYPE_IGNORE_BUDGET = 57


# ---------------------------------------------------------------- 工具


def source(name: str) -> str:
    return MODULES[name].read_text(encoding="utf-8")


def tree(name: str) -> ast.Module:
    return ast.parse(source(name))


def internal_deps(name: str) -> set[str]:
    """本包内的相对 import（`from .x import y` / `from . import x`）。"""

    deps: set[str] = set()
    for node in ast.walk(tree(name)):
        if isinstance(node, ast.ImportFrom) and node.level == 1:
            if node.module:
                deps.add(node.module.split(".")[0])
            else:
                deps.update(alias.name for alias in node.names)
    deps.discard("__version__")  # 包版本号，不是模块依赖
    return deps


def all_modules_present() -> None:
    missing = set(EXPECTED_EDGES) ^ set(MODULES)
    assert not missing, (
        f"EXPECTED_EDGES 与 submatrix/ 下的模块对不上：{sorted(missing)}"
        "（新增/删除模块时同步更新本表与 docs/代码规范.md §1.1）"
    )


# ---------------------------------------------------------------- 分层与依赖


def test_every_module_is_in_a_layer() -> None:
    all_modules_present()
    placed = set().union(*LAYERS.values())
    assert placed == set(MODULES), f"未分层的模块：{sorted(set(MODULES) - placed)}"


def test_dependency_edges_are_declared() -> None:
    """新增依赖必须是有意识的：改代码就要改这张表，让它在 diff 里出现。"""

    all_modules_present()
    problems = []
    for name in sorted(MODULES):
        actual = internal_deps(name)
        expected = EXPECTED_EDGES[name]
        extra = actual - expected
        gone = expected - actual
        if extra:
            problems.append(f"{name} 新增了依赖 {sorted(extra)}")
        if gone:
            problems.append(f"{name} 少了依赖 {sorted(gone)}（顺手删掉表里的？）")
    assert not problems, (
        "依赖边与声明不一致，改法二选一：\n"
        "  1) 依赖是误加的 → 去掉 import；\n"
        "  2) 依赖是合理的 → 更新 EXPECTED_EDGES 与 docs/代码规范.md §1.1 的分层表，"
        "并在提交信息里说明理由。\n"
        + "\n".join(f"- {item}" for item in problems)
    )


def test_no_import_cycles() -> None:
    edges = {name: internal_deps(name) for name in MODULES}
    cycles: list[list[str]] = []

    def walk(node: str, stack: list[str]) -> None:
        for nxt in edges.get(node, ()):
            if nxt in stack:
                cycles.append(stack[stack.index(nxt) :] + [nxt])
            elif nxt in edges and len(stack) < 40:
                walk(nxt, stack + [nxt])

    for name in edges:
        walk(name, [name])
    assert not cycles, "存在循环依赖：" + ", ".join(" -> ".join(cycle) for cycle in cycles[:5])


def test_layers_only_depend_downwards() -> None:
    problems = []
    for name in sorted(MODULES):
        deps = internal_deps(name)
        if name in LAYERS["L0"] and deps:
            problems.append(f"{name} 在 L0 却依赖 {sorted(deps)}")
        if name in LAYERS["worker"] and not deps <= {"worker_protocol"}:
            problems.append(f"{name} 是 worker，只许依赖 worker_protocol，实际 {sorted(deps)}")
        if name in LAYERS["L1"] and not deps <= LAYERS["L0"] | LAYERS["L1"]:
            problems.append(f"{name} 在 L1 却依赖上层 {sorted(deps - LAYERS['L0'] - LAYERS['L1'])}")
        if name in LAYERS["entry"] and name != "__init__" and not deps <= {"cli"}:
            problems.append(f"{name} 是入口，只许依赖 cli，实际 {sorted(deps)}")
    assert not problems, "分层被打破：\n" + "\n".join(f"- {item}" for item in problems)


# ---------------------------------------------------------------- 边界


def test_main_package_has_no_module_level_heavy_imports() -> None:
    problems = []
    for name in sorted(MODULES):
        if name in WORKER_MODULES:
            continue
        for node in tree(name).body:  # 只看模块顶层
            if isinstance(node, ast.Import):
                problems += [f"{name}: import {alias.name}" for alias in node.names
                             if alias.name.split(".")[0] in HEAVY_PACKAGES]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                if node.module.split(".")[0] in HEAVY_PACKAGES:
                    problems.append(f"{name}: from {node.module} import …")
    assert not problems, (
        "重依赖必须在 *_worker.py 里 import（主包零第三方运行时依赖，见规范 §1.2）：\n"
        + "\n".join(f"- {item}" for item in problems)
    )


def test_library_modules_do_not_print_to_stdout() -> None:
    problems = []
    for name in sorted(MODULES):
        if name in PRINT_EXEMPT:
            continue
        for node in ast.walk(tree(name)):
            if not isinstance(node, ast.Call):
                continue
            if not (isinstance(node.func, ast.Name) and node.func.id == "print"):
                continue
            to_stderr = any(kw.arg == "file" for kw in node.keywords)
            if not to_stderr:
                problems.append(f"{name}:{node.lineno}")
    assert not problems, (
        "库模块不许往标准输出打印（入口 cli 与 stdlib 的 stderr 日志除外）：\n"
        + "\n".join(f"- {item}" for item in problems)
    )


# ---------------------------------------------------------------- 代码形状


def test_no_bare_except_and_justified_broad_except() -> None:
    problems = []
    for name in sorted(MODULES):
        text = source(name)
        lines = text.splitlines()
        for node in ast.walk(tree(name)):
            if not isinstance(node, ast.ExceptHandler):
                continue
            handler_line = lines[node.lineno - 1]
            if node.type is None:
                problems.append(f"{name}:{node.lineno} 裸 except")
            elif isinstance(node.type, ast.Name) and node.type.id == "Exception":
                if "#" not in handler_line:
                    problems.append(
                        f"{name}:{node.lineno} except Exception 没写为什么安全（行内注释）"
                    )
    assert not problems, "异常处理不合规：\n" + "\n".join(f"- {item}" for item in problems)


def test_every_module_declares_all() -> None:
    problems = []
    for name in sorted(MODULES):
        if name in ALL_EXEMPT:
            continue
        declared = any(
            isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "__all__"
                    for target in node.targets)
            for node in tree(name).body
        )
        if not declared:
            problems.append(name)
    assert not problems, (
        "以下模块缺 __all__（列对外契约，不是列全部函数）：" + "、".join(problems)
    )


def test_long_functions_are_declared() -> None:
    problems = []
    for name in sorted(MODULES):
        for node in ast.walk(tree(name)):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            size = (node.end_lineno or node.lineno) - node.lineno + 1
            if size <= MAX_LINES:
                continue
            key = f"{name}:{node.name}"
            if key not in LENGTH_OVERRIDES:
                problems.append(f"{key}（{size} 行）")
    assert not problems, (
        f"函数超过 {MAX_LINES} 行且不在豁免名单里。改法：拆成状态对象 + 阶段函数"
        "（规范 §2）；确实不该拆（声明式字面量/参数表/UI）就登记到 LENGTH_OVERRIDES"
        " 与 docs/代码规范.md §10.2：\n" + "\n".join(f"- {item}" for item in problems)
    )


def test_type_ignore_budget_does_not_grow() -> None:
    total = sum(len(re.findall(r"#\s*type:\s*ignore", source(name))) for name in MODULES)
    assert total <= TYPE_IGNORE_BUDGET, (
        f"# type: ignore 增加到了 {total} 处（基线 {TYPE_IGNORE_BUDGET}）。"
        "规范 §3：报告 JSON 边界应当用 TypedDict/小类型收窄，不要靠压制告警过关。"
    )


if __name__ == "__main__":  # 直接跑也能看：python tests/test_structure.py
    raise SystemExit(pytest.main([__file__, "-q"]))
