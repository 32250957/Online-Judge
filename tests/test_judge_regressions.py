import base64
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from app import judge


@pytest.mark.parametrize("phase,rc,expected", [("compile", 101, "CE"), ("system", 125, "SE")])
def test_failure_phase_precedes_time_and_memory(tmp_path, monkeypatch, phase, rc, expected):
    monkeypatch.chdir(tmp_path)
    tests = Path("problems/1/tests")
    tests.mkdir(parents=True)
    (tests / "1.in").write_text("")
    (tests / "1.out").write_text("1")
    with patch.object(judge, "_docker_available", return_value=(True, "")), patch.object(
        judge, "_run_test", return_value=(rc, "", "error", 10000, 0, False, False, phase)
    ):
        assert judge.judge_code(1, "code", "cpp", 1, 256)[0] == expected


def test_bounded_runner_drains_both_streams_and_stdin():
    data = b"test" * 50000
    result = judge._bounded_run([sys.executable, "-c",
        "import sys; data=sys.stdin.buffer.read(); sys.stdout.buffer.write(data); sys.stderr.write('err')"], data, 5)
    assert result.stdout == data
    assert result.stderr == b"err"


def test_unbounded_output_is_terminated(monkeypatch):
    monkeypatch.setattr(judge, "MAX_OUTPUT_BYTES", 4096)
    with pytest.raises(judge.OutputLimitExceeded):
        judge._bounded_run([sys.executable, "-c", "while True: print('x'*1000, flush=True)"], b"", 5)


def test_silent_process_is_timed_out():
    with pytest.raises(subprocess.TimeoutExpired):
        judge._bounded_run([sys.executable, "-c", "import time; time.sleep(30)"], b"", 0.1)


@pytest.mark.parametrize("failure", [subprocess.TimeoutExpired("docker", 90), judge.OutputLimitExceeded(), FileNotFoundError()])
def test_docker_cleanup_on_all_failures(failure):
    with patch.object(judge, "_bounded_run", side_effect=failure), patch.object(judge, "_remove_container") as remove:
        outcome = judge._run_test("python", "print(1)", "", 1, 256)
    remove.assert_called_once()
    assert remove.call_args.args[0].startswith("oj-run-")
    assert outcome[-1] in {"system", "output"}


def test_docker_error_is_system_error():
    with patch.object(judge, "_bounded_run", return_value=subprocess.CompletedProcess([], 125, b"", b"daemon error")), patch.object(judge, "_remove_container"):
        assert judge._run_test("cpp", "code", "", 1, 256)[-1] == "system"


def test_python_runner_streams_output_and_emits_metrics():
    environment = dict(os.environ, OJ_CODE_B64=base64.b64encode(b"print(input())").decode(), OJ_TIME_LIMIT="2")
    result = subprocess.run([sys.executable, "-c", judge.PYTHON_RUNNER_CODE], input=b"hello\n", capture_output=True, env=environment, timeout=5)
    assert result.returncode == 0
    assert result.stdout == b"hello\n"
    assert b"__OJ_METRICS__" in result.stderr


@pytest.mark.skipif(os.environ.get("OJ_TEST_DOCKER") != "1", reason="Requires a Docker daemon and prebuilt judge images")
@pytest.mark.parametrize("language,code,expected", [
    ("python", "print(3)", "AC"),
    ("c", '#include <stdio.h>\nint main(){puts("3");}', "AC"),
    ("cpp", '#include <iostream>\nint main(){std::cout<<3;}', "AC"),
    ("java", 'public class Main {public static void main(String[] a){System.out.println(3);}}', "AC"),
    ("cpp", "invalid c++ code", "CE"),
    ("python", "raise ValueError('test')", "RE"),
    ("python", "while True: pass", "TLE"),
    ("python", "while True: print('x'*1000, flush=True)", "RE"),
])
def test_real_judge_containers(tmp_path, monkeypatch, language, code, expected):
    monkeypatch.chdir(tmp_path)
    tests = Path("problems/1/tests")
    tests.mkdir(parents=True)
    (tests / "1.in").write_text("")
    (tests / "1.out").write_text("3")
    assert judge.judge_code(1, code, language, 2, 512)[0] == expected
