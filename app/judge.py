from __future__ import annotations

import base64
import json
import os
import subprocess
import time
from pathlib import Path

DOCKER_CPUS = os.getenv("OJ_DOCKER_CPUS", "1")
DOCKER_PIDS_LIMIT = os.getenv("OJ_DOCKER_PIDS_LIMIT", "64")
PYTHON_IMAGE = os.getenv("OJ_PYTHON_IMAGE", "python:3.11-slim")
C_IMAGE = os.getenv("OJ_C_IMAGE", "gcc:13")
CPP_IMAGE = os.getenv("OJ_CPP_IMAGE", "gcc:13")
JAVA_IMAGE = os.getenv("OJ_JAVA_IMAGE", "eclipse-temurin:17")

SUPPORTED_LANGUAGES = {
    "python": {"label": "Python 3", "image": PYTHON_IMAGE},
    "c": {"label": "C", "image": C_IMAGE},
    "cpp": {"label": "C++17", "image": CPP_IMAGE},
    "java": {"label": "Java 17", "image": JAVA_IMAGE},
}
LANGUAGE_ALIASES = {"py": "python", "python3": "python", "c++": "cpp", "cpp17": "cpp", "cxx": "cpp", "java17": "java"}


def normalize_language(language: str) -> str:
    value = (language or "").strip().lower()
    return LANGUAGE_ALIASES.get(value, value)


def language_label(language: str) -> str:
    language = normalize_language(language)
    return SUPPORTED_LANGUAGES.get(language, {}).get("label", language)


def normalize_output(text: str | None) -> str:
    text = text or ""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines = [line.rstrip() for line in lines]
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


PYTHON_RUNNER_CODE = r'''
import base64, json, os, resource, subprocess, sys, tempfile, time
code = base64.b64decode(os.environ.get("OJ_CODE_B64", "")).decode("utf-8", errors="replace")
time_limit = float(os.environ.get("OJ_TIME_LIMIT", "2"))
input_data = sys.stdin.buffer.read()
with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
    main_path = os.path.join(tmp, "main.py")
    with open(main_path, "w", encoding="utf-8") as f:
        f.write(code)
    started_at = time.perf_counter()
    try:
        completed = subprocess.run([sys.executable, main_path], input=input_data, capture_output=True, timeout=time_limit)
        elapsed_ms = int((time.perf_counter() - started_at) * 1000)
        usage = resource.getrusage(resource.RUSAGE_CHILDREN)
        memory_kb = int(getattr(usage, "ru_maxrss", 0))
        metrics = {"runtime_ms": elapsed_ms, "memory_kb": memory_kb, "timed_out": False}
        sys.stdout.buffer.write(completed.stdout)
        sys.stderr.buffer.write(completed.stderr)
        sys.stderr.write("\n__OJ_METRICS__" + json.dumps(metrics, ensure_ascii=False) + "\n")
        raise SystemExit(completed.returncode)
    except subprocess.TimeoutExpired:
        elapsed_ms = int((time.perf_counter() - started_at) * 1000)
        metrics = {"runtime_ms": elapsed_ms, "memory_kb": 0, "timed_out": True}
        sys.stderr.write("\n__OJ_METRICS__" + json.dumps(metrics, ensure_ascii=False) + "\n")
        raise SystemExit(124)
'''

RUN_WITH_METRICS_SH = r'''
start_ns=$(date +%s%N)
run_err="/tmp/oj_run_stderr.$$"
memory_kb=0
timed_out=false

# Prefer GNU time because it reads ru_maxrss and can catch even very short C/C++ programs.
# If it is unavailable in the language image, fall back to lightweight /proc polling.
if command -v /usr/bin/time >/dev/null 2>&1; then
  set +e
  if [ -n "${OJ_INPUT_FILE:-}" ] && [ -r "$OJ_INPUT_FILE" ]; then
    timeout "${OJ_TIME_LIMIT}s" /usr/bin/time -f "\n__OJ_MEMORY_KB__%M" "$@" < "$OJ_INPUT_FILE" 2>"$run_err"
  else
    timeout "${OJ_TIME_LIMIT}s" /usr/bin/time -f "\n__OJ_MEMORY_KB__%M" "$@" 2>"$run_err"
  fi
  rc=$?
  set -e
  if grep -a "__OJ_MEMORY_KB__" "$run_err" >/dev/null 2>&1; then
    memory_kb=$(grep -a "__OJ_MEMORY_KB__" "$run_err" | tail -n 1 | sed 's/.*__OJ_MEMORY_KB__//' | tr -dc '0-9')
    if [ -z "$memory_kb" ]; then memory_kb=0; fi
  fi
  grep -a -v "__OJ_MEMORY_KB__" "$run_err" >&2 || true
else
  limit_ms=$(awk "BEGIN { printf \"%d\", ${OJ_TIME_LIMIT} * 1000 }")
  set +e
  if [ -n "${OJ_INPUT_FILE:-}" ] && [ -r "$OJ_INPUT_FILE" ]; then
    "$@" < "$OJ_INPUT_FILE" 2>"$run_err" &
  else
    "$@" 2>"$run_err" &
  fi
  child=$!
  rc=0
  while kill -0 "$child" 2>/dev/null; do
    if [ -r "/proc/$child/status" ]; then
      current_kb=$(awk '/VmHWM:/ {h=$2} /VmRSS:/ {r=$2} END {if (h>0) print h; else if (r>0) print r; else print 0}' "/proc/$child/status" 2>/dev/null)
      if [ -n "$current_kb" ] && [ "$current_kb" -gt "$memory_kb" ] 2>/dev/null; then
        memory_kb=$current_kb
      fi
    fi
    now_ns=$(date +%s%N)
    elapsed_ms=$(( (now_ns - start_ns) / 1000000 ))
    if [ "$elapsed_ms" -gt "$limit_ms" ]; then
      timed_out=true
      kill -TERM "$child" 2>/dev/null || true
      sleep 0.05
      kill -KILL "$child" 2>/dev/null || true
      wait "$child" 2>/dev/null
      rc=124
      break
    fi
    sleep 0.002
  done
  if [ "$timed_out" = false ]; then
    wait "$child"
    rc=$?
  fi
  set -e
  cat "$run_err" >&2 || true
fi

end_ns=$(date +%s%N)
elapsed_ms=$(( (end_ns - start_ns) / 1000000 ))
if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ] || [ "$rc" -eq 143 ]; then
  timed_out=true
fi
rm -f "$run_err" 2>/dev/null || true
printf '
__OJ_METRICS__{"runtime_ms":%s,"memory_kb":%s,"timed_out":%s}
' "$elapsed_ms" "$memory_kb" "$timed_out" >&2
exit "$rc"
'''

LANGUAGE_SCRIPTS = {
    "c": r'''
set -eu
OJ_INPUT_FILE=/tmp/oj_input
cat > "$OJ_INPUT_FILE"
export OJ_INPUT_FILE
printf "%s" "$OJ_CODE_B64" | base64 -d > /tmp/main.c
if ! gcc -std=c11 -O2 -pipe /tmp/main.c -o /tmp/main 2>/tmp/compile.err; then
  echo "__OJ_PHASE__compile" >&2
  cat /tmp/compile.err >&2
  exit 101
fi
set -- /tmp/main
''' + RUN_WITH_METRICS_SH + r'''
''',
    "cpp": r'''
set -eu
OJ_INPUT_FILE=/tmp/oj_input
cat > "$OJ_INPUT_FILE"
export OJ_INPUT_FILE
printf "%s" "$OJ_CODE_B64" | base64 -d > /tmp/main.cpp
if ! g++ -std=c++17 -O2 -pipe /tmp/main.cpp -o /tmp/main 2>/tmp/compile.err; then
  echo "__OJ_PHASE__compile" >&2
  cat /tmp/compile.err >&2
  exit 101
fi
set -- /tmp/main
''' + RUN_WITH_METRICS_SH + r'''
''',
    "java": r'''
set -eu
OJ_INPUT_FILE=/tmp/oj_input
cat > "$OJ_INPUT_FILE"
export OJ_INPUT_FILE
printf "%s" "$OJ_CODE_B64" | base64 -d > /tmp/Main.java
if ! javac /tmp/Main.java 2>/tmp/compile.err; then
  echo "__OJ_PHASE__compile" >&2
  cat /tmp/compile.err >&2
  exit 101
fi
set -- java -cp /tmp Main
''' + RUN_WITH_METRICS_SH + r'''
''',
}


def _split_metrics(stderr_text: str) -> tuple[str, int, int, bool]:
    marker = "__OJ_METRICS__"
    runtime_ms = 0
    memory_kb = 0
    timed_out = False
    if marker not in stderr_text:
        return stderr_text, runtime_ms, memory_kb, timed_out
    before, after = stderr_text.rsplit(marker, 1)
    line = after.strip().splitlines()[0] if after.strip() else "{}"
    try:
        metrics = json.loads(line)
        runtime_ms = int(metrics.get("runtime_ms", 0) or 0)
        memory_kb = int(metrics.get("memory_kb", 0) or 0)
        timed_out = bool(metrics.get("timed_out", False))
    except Exception:
        pass
    return before.strip(), runtime_ms, memory_kb, timed_out


def _docker_available() -> tuple[bool, str]:
    try:
        completed = subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"], capture_output=True, timeout=5)
    except FileNotFoundError as exc:
        return False, f"Docker CLI를 찾을 수 없습니다: {exc}"
    except Exception as exc:
        return False, f"Docker 상태를 확인할 수 없습니다: {exc}"
    if completed.returncode != 0:
        msg = (completed.stderr or b"").decode("utf-8", errors="replace").strip()
        return False, msg or "Docker daemon에 연결할 수 없습니다. docker.sock 마운트와 Docker Desktop 실행 상태를 확인하세요."
    return True, ""


def _base_docker_command(image: str, memory_limit: int) -> list[str]:
    return [
        "docker", "run", "--rm", "-i",
        "--network", "none",
        "--memory", f"{int(memory_limit)}m",
        "--cpus", DOCKER_CPUS,
        "--pids-limit", DOCKER_PIDS_LIMIT,
        "--read-only",
        "--tmpfs", "/tmp:rw,exec,nosuid,size=128m",
    ]


def _run_test(language: str, code: str, input_data: str, time_limit: int, memory_limit: int) -> tuple[int, str, str, int, int, bool, str]:
    language = normalize_language(language)
    encoded_code = base64.b64encode(code.encode("utf-8")).decode("ascii")
    started = time.perf_counter()
    if language == "python":
        command = _base_docker_command(PYTHON_IMAGE, memory_limit) + [
            "-e", f"OJ_TIME_LIMIT={float(time_limit)}",
            "-e", f"OJ_CODE_B64={encoded_code}",
            PYTHON_IMAGE,
            "python", "-c", PYTHON_RUNNER_CODE,
        ]
    elif language in LANGUAGE_SCRIPTS:
        image = SUPPORTED_LANGUAGES[language]["image"]
        command = _base_docker_command(image, memory_limit) + [
            "-e", f"OJ_TIME_LIMIT={float(time_limit)}",
            "-e", f"OJ_CODE_B64={encoded_code}",
            image,
            "sh", "-lc", LANGUAGE_SCRIPTS[language],
        ]
    else:
        return 127, "", f"지원하지 않는 언어입니다: {language}", 0, 0, False, "system"
    try:
        # 컴파일 언어는 컨테이너 시작/컴파일 시간이 문제 시간 제한에 섞이면 안 된다.
        # 실제 프로그램 실행은 컨테이너 내부 timeout으로 제한하고, 바깥 timeout은 Docker hang 방지용으로 넉넉히 둔다.
        completed = subprocess.run(command, input=input_data.encode("utf-8"), capture_output=True, timeout=max(float(time_limit) + 30, 90))
    except FileNotFoundError as exc:
        return 127, "", f"Docker 명령을 찾을 수 없습니다. worker 컨테이너에 Docker CLI가 설치되어 있는지 확인하세요.\n{exc}", 0, 0, False, "system"
    except subprocess.TimeoutExpired:
        return 125, "", "채점 컨테이너 준비 또는 실행이 비정상적으로 오래 걸렸습니다. Docker 이미지 다운로드/실행 상태를 확인하세요.", 0, 0, False, "system"
    stdout_text = (completed.stdout or b"").decode("utf-8", errors="replace")
    stderr_text = (completed.stderr or b"").decode("utf-8", errors="replace")
    user_stderr, runtime_ms, memory_kb, timed_out = _split_metrics(stderr_text)
    if runtime_ms <= 0 and language != "python":
        # 메트릭 파싱에 실패한 경우에만 보조값으로 Docker 전체 시간을 사용한다.
        # 정상 경로에서는 컴파일/컨테이너 시작 시간이 제외된 실제 실행 시간만 기록된다.
        runtime_ms = int((time.perf_counter() - started) * 1000)
        timed_out = completed.returncode == 124
    phase = "compile" if "__OJ_PHASE__compile" in user_stderr or completed.returncode == 101 else "run"
    user_stderr = user_stderr.replace("__OJ_PHASE__compile", "").strip()
    return completed.returncode, stdout_text, user_stderr, runtime_ms, memory_kb, timed_out, phase


def judge_code(problem_id: int, code: str, language: str, time_limit: int, memory_limit: int) -> tuple[str, str, int, int]:
    language = normalize_language(language)
    if language not in SUPPORTED_LANGUAGES:
        return "SE", f"지원하지 않는 언어입니다: {language}", 0, 0
    ok, docker_error = _docker_available()
    if not ok:
        return "SE", docker_error, 0, 0
    problem_path = Path("problems") / str(problem_id) / "tests"
    if not problem_path.exists():
        return "SE", "테스트 케이스 폴더를 찾을 수 없습니다.", 0, 0
    input_files = sorted(problem_path.glob("*.in"), key=lambda p: int(p.stem) if p.stem.isdigit() else p.stem)
    if not input_files:
        return "SE", "테스트 케이스가 없습니다.", 0, 0
    total_runtime_ms = 0
    peak_memory_kb = 0
    for index, input_path in enumerate(input_files, start=1):
        output_path = input_path.with_suffix(".out")
        if not output_path.exists():
            return "SE", f"{input_path.name}에 대응하는 출력 파일이 없습니다.", total_runtime_ms, peak_memory_kb
        input_data = input_path.read_text(encoding="utf-8")
        expected_output = output_path.read_text(encoding="utf-8")
        rc, stdout_text, stderr_text, runtime_ms, memory_kb, timed_out, phase = _run_test(language, code, input_data, time_limit, memory_limit)
        total_runtime_ms += runtime_ms
        peak_memory_kb = max(peak_memory_kb, memory_kb)
        if timed_out or rc == 124 or runtime_ms > int(float(time_limit) * 1000):
            return "TLE", f"{index}번 테스트에서 시간 초과가 발생했습니다.", total_runtime_ms, peak_memory_kb
        if phase == "system":
            return "SE", stderr_text.strip() or "채점 시스템 오류가 발생했습니다.", total_runtime_ms, peak_memory_kb
        if phase == "compile":
            return "CE", f"컴파일 에러가 발생했습니다.\n{stderr_text}", total_runtime_ms, peak_memory_kb
        if rc != 0:
            error = stderr_text.strip() or f"return code: {rc}"
            return "RE", f"{index}번 테스트에서 런타임 에러가 발생했습니다.\n{error}", total_runtime_ms, peak_memory_kb
        actual = normalize_output(stdout_text)
        expected = normalize_output(expected_output)
        if actual != expected:
            return "WA", f"{index}번 테스트에서 오답입니다.\n\n기대 출력:\n{expected}\n\n실제 출력:\n{actual}", total_runtime_ms, peak_memory_kb
    return "AC", "모든 테스트를 통과했습니다.", total_runtime_ms, peak_memory_kb


def judge_python(problem_id: int, code: str, time_limit: int, memory_limit: int) -> tuple[str, str, int, int]:
    return judge_code(problem_id, code, "python", time_limit, memory_limit)
