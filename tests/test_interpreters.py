"""解释器探测策略（P1-2）：五套实现收敛成一份。

这条最容易出事的地方**不是**找不到解释器，而是"找不到时怎么办"：以前 5 个组件分别是
抛异常 / 返回 None / 静默退回当前解释器，用户看到的现象从"明确报错"到"配音没声音"不等。
这里把三种策略和"是否信任显式值"都钉住。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from cuesmith import interpreters as ip

REQUIREMENT = ip.Requirement(
    label="faster-whisper",
    module="faster_whisper",
    parameter="--asr-python",
    env_var="CUESMITH_ASR_PYTHON",
    hint="例如 spike/pyvideotrans/.venv/Scripts/python.exe。",
)


def probe_from(mapping: dict[str, bool], probed: list[str]):
    def probe(python: str) -> tuple[bool, str]:
        probed.append(python)
        return (True, "") if mapping.get(python) else (False, "import 失败")

    return probe


# ---------------------------------------------------------------- 候选与探针


def test_ordered_candidates_keeps_priority_and_drops_duplicates() -> None:
    ordered = ip.ordered_candidates(
        ["a", None, "b"],
        ("b", "c"),
        [None, "a"],
    )

    assert ordered == ["a", "b", "c"]


def test_python_in_venv_covers_windows_and_posix_layouts() -> None:
    paths = ip.python_in_venv(Path("/repo/spike/.venv"))

    assert any(path.endswith(str(Path("Scripts") / "python.exe")) for path in paths)
    assert any(path.endswith(str(Path("bin") / "python")) for path in paths)


def test_bool_probe_adapts_existing_check() -> None:
    probe = ip.bool_probe(lambda python: python == "good")

    assert probe("good") == (True, "")
    assert probe("bad")[0] is False


def test_runner_probe_skips_missing_paths_without_spawning() -> None:
    calls: list[list[str]] = []

    def runner(command):
        calls.append(list(command))
        return subprocess.CompletedProcess(command, 0, "", "")

    probe = ip.runner_probe(runner, "faster_whisper")

    ok, detail = probe("definitely/not/a/python.exe")

    assert ok is False
    assert "不存在" in detail
    assert calls == []  # 不给拼错的路径起子进程


def test_runner_probe_reports_last_stderr_line() -> None:
    def runner(command):
        return subprocess.CompletedProcess(
            command, 1, "", "line1\nModuleNotFoundError: No module named 'faster_whisper'\n"
        )

    ok, detail = ip.runner_probe(runner, "faster_whisper")(sys.executable)

    assert ok is False
    assert "ModuleNotFoundError" in detail


def test_runner_probe_success() -> None:
    def runner(command):
        return subprocess.CompletedProcess(command, 0, "", "")

    assert ip.runner_probe(runner, "x")(sys.executable) == (True, "")


# ---------------------------------------------------------------- 报错文案


def test_missing_message_contains_actionable_parts() -> None:
    message = ip.missing_message(
        REQUIREMENT, [("/a/python", "路径不存在"), ("/b/python", "import 失败")]
    )

    assert "faster-whisper" in message
    assert "import faster_whisper" in message
    assert "--asr-python" in message
    assert "CUESMITH_ASR_PYTHON" in message
    assert "spike/pyvideotrans" in message
    assert "/a/python: 路径不存在" in message


def test_missing_message_without_attempts_says_so() -> None:
    message = ip.missing_message(REQUIREMENT, [])

    assert "没有可用的候选解释器" in message


# ---------------------------------------------------------------- 策略


def test_trusted_explicit_wins_without_probing() -> None:
    def probe(python):  # noqa: ANN001, ARG001
        raise AssertionError("信任显式值时不该探测")

    assert (
        ip.resolve(
            REQUIREMENT,
            "explicit-python",
            candidates=["a"],
            probe=probe,
            trust_explicit=True,
        )
        == "explicit-python"
    )


def test_trusted_env_wins_without_probing(monkeypatch) -> None:
    monkeypatch.setenv(REQUIREMENT.env_var, "env-python")

    def probe(python):  # noqa: ANN001, ARG001
        raise AssertionError("信任环境变量时不该探测")

    assert ip.resolve(REQUIREMENT, None, candidates=["a"], probe=probe, trust_env=True) == (
        "env-python"
    )


def test_untrusted_explicit_is_probed_in_priority_order(monkeypatch) -> None:
    monkeypatch.setenv(REQUIREMENT.env_var, "env-python")
    probed: list[str] = []
    probe = probe_from({"env-python": True}, probed)

    found = ip.resolve(
        REQUIREMENT, "explicit-python", candidates=["venv-python"], probe=probe
    )

    assert found == "env-python"
    assert probed == ["explicit-python", "env-python"]  # 显式优先，env 其次，命中即停


def test_candidates_are_deduped_before_probing(monkeypatch) -> None:
    monkeypatch.delenv(REQUIREMENT.env_var, raising=False)
    probed: list[str] = []
    probe = probe_from({}, probed)

    with pytest.raises(RuntimeError):
        ip.resolve(
            REQUIREMENT, "dup", candidates=["dup", "other"], probe=probe, error_cls=RuntimeError
        )

    assert probed == ["dup", "other"]


def test_missing_policy_none_returns_none(monkeypatch) -> None:
    monkeypatch.delenv(REQUIREMENT.env_var, raising=False)

    found = ip.resolve(
        REQUIREMENT,
        None,
        candidates=["a"],
        probe=probe_from({}, []),
        on_missing=ip.MISSING_NONE,
    )

    assert found is None


def test_missing_policy_current_falls_back_to_sys_executable(monkeypatch) -> None:
    monkeypatch.delenv(REQUIREMENT.env_var, raising=False)

    found = ip.resolve(
        REQUIREMENT,
        None,
        candidates=["a"],
        probe=probe_from({}, []),
        on_missing=ip.MISSING_CURRENT,
    )

    assert found == sys.executable


def test_missing_policy_raise_uses_callers_error_class(monkeypatch) -> None:
    monkeypatch.delenv(REQUIREMENT.env_var, raising=False)

    class MyError(RuntimeError):
        pass

    with pytest.raises(MyError) as excinfo:
        ip.resolve(
            REQUIREMENT,
            None,
            candidates=["a"],
            probe=probe_from({}, []),
            error_cls=MyError,
        )

    assert "--asr-python" in str(excinfo.value)
