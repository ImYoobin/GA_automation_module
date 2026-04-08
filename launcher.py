"""Launcher for packaged Google automation app."""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

HOST = "127.0.0.1"
DEFAULT_PORT = 8501
MAX_PORT_OFFSET = 10


def _resolve_runtime_root() -> Path:
    if getattr(sys, "frozen", False):
        candidate = Path(sys.executable).resolve().parent
    else:
        candidate = Path(__file__).resolve().parent

    app_main = candidate / "app" / "main.py"
    if app_main.exists():
        return candidate

    if candidate.name.lower() == "_internal":
        parent = candidate.parent
        if (parent / "app" / "main.py").exists():
            return parent

    return candidate


def _is_port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.6)
        return sock.connect_ex((host, port)) == 0


def _wait_port(host: str, port: int, timeout_sec: int) -> bool:
    deadline = time.time() + max(1, int(timeout_sec))
    while time.time() < deadline:
        if _is_port_open(host, port):
            return True
        time.sleep(0.25)
    return False


def _attach_kill_on_close_job(process: subprocess.Popen[bytes], logger: logging.Logger) -> int | None:
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
    except Exception as exc:  # noqa: BLE001
        logger.warning("job_object_init_failed reason=%s", exc)
        return None

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    JobObjectExtendedLimitInformation = 9

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, wintypes.INT, wintypes.LPVOID, wintypes.DWORD]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL

    job_handle = int(kernel32.CreateJobObjectW(None, None) or 0)
    if job_handle <= 0:
        logger.warning("job_object_create_failed last_error=%s", ctypes.get_last_error())
        return None

    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = bool(
        kernel32.SetInformationJobObject(
            wintypes.HANDLE(job_handle),
            JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
    )
    if not ok:
        logger.warning("job_object_setinfo_failed last_error=%s", ctypes.get_last_error())
        kernel32.CloseHandle(wintypes.HANDLE(job_handle))
        return None

    assigned = bool(
        kernel32.AssignProcessToJobObject(
            wintypes.HANDLE(job_handle),
            wintypes.HANDLE(int(process._handle)),
        )
    )
    if not assigned:
        logger.warning("job_object_assign_failed pid=%s last_error=%s", process.pid, ctypes.get_last_error())
        kernel32.CloseHandle(wintypes.HANDLE(job_handle))
        return None
    logger.info("job_object_attached pid=%s", process.pid)
    return job_handle


def _close_job_handle(job_handle: int | None) -> None:
    if os.name != "nt" or not job_handle:
        return
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle(wintypes.HANDLE(int(job_handle)))
    except Exception:  # noqa: BLE001
        return


def _build_streamlit_command(base_dir: Path, *, port: int) -> list[str]:
    runtime_python = base_dir / "_internal" / "python_runtime" / "python.exe"
    python_executable = runtime_python if runtime_python.exists() else Path(sys.executable)
    app_path = base_dir / "app" / "main.py"
    if not app_path.exists():
        raise FileNotFoundError(f"Missing app entrypoint: {app_path}")
    return [
        str(python_executable),
        "-m",
        "streamlit",
        "run",
        str(app_path),
        "--server.headless=true",
        f"--server.address={HOST}",
        f"--server.port={port}",
        "--global.developmentMode=false",
        "--browser.serverAddress=localhost",
        f"--browser.serverPort={port}",
    ]


def _parse_pid_file(pid_file: Path) -> int:
    if not pid_file.exists():
        return 0
    try:
        return int(pid_file.read_text(encoding="utf-8").strip())
    except Exception:  # noqa: BLE001
        return 0


def _is_process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _terminate_process_tree(pid: int, logger: logging.Logger) -> bool:
    if pid <= 0:
        return False
    try:
        proc = subprocess.run(  # noqa: S603
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            logger.warning("terminated_streamlit pid=%s", pid)
            return True
        logger.warning(
            "terminate_streamlit_failed pid=%s returncode=%s stderr=%s",
            pid,
            proc.returncode,
            (proc.stderr or "").strip(),
        )
        return False
    except Exception as exc:  # noqa: BLE001
        logger.warning("terminate_streamlit_exception pid=%s error=%s", pid, exc)
        return False


def _cleanup_stale_pid(pid_file: Path, logger: logging.Logger) -> None:
    stale_pid = _parse_pid_file(pid_file)
    if stale_pid <= 0:
        return
    if not _is_process_alive(stale_pid):
        pid_file.unlink(missing_ok=True)
        logger.info("stale_pid_file_removed pid=%s", stale_pid)
        return

    _terminate_process_tree(stale_pid, logger=logger)
    for _ in range(20):
        if not _is_process_alive(stale_pid):
            break
        time.sleep(0.2)
    pid_file.unlink(missing_ok=True)


def _resolve_launch_port(host: str, preferred_port: int, logger: logging.Logger) -> int:
    if not _is_port_open(host, preferred_port):
        return preferred_port
    for offset in range(1, MAX_PORT_OFFSET + 1):
        candidate = preferred_port + offset
        if not _is_port_open(host, candidate):
            logger.warning(
                "preferred_port_in_use host=%s preferred_port=%s fallback_port=%s",
                host,
                preferred_port,
                candidate,
            )
            return candidate
    raise RuntimeError(
        f"All candidate ports are occupied ({preferred_port}..{preferred_port + MAX_PORT_OFFSET}). "
        "Close previous app instances and retry."
    )


def main() -> int:
    base_dir = _resolve_runtime_root()
    logs_dir = base_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    pid_file = logs_dir / "streamlit.pid"
    launcher_log = logs_dir / "launcher.log"
    streamlit_stdout = logs_dir / "streamlit_stdout.log"
    streamlit_stderr = logs_dir / "streamlit_stderr.log"

    logger = logging.getLogger("google_launcher")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

    file_handler = logging.FileHandler(launcher_log, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    logger.info("launcher_started base_dir=%s", base_dir)
    _cleanup_stale_pid(pid_file, logger=logger)
    launch_port = _resolve_launch_port(HOST, DEFAULT_PORT, logger=logger)
    browser_url = f"http://localhost:{launch_port}"
    command = _build_streamlit_command(base_dir, port=launch_port)
    logger.info("streamlit_cmd=%s", command)

    with streamlit_stdout.open("a", encoding="utf-8") as stdout_file, streamlit_stderr.open(
        "a", encoding="utf-8"
    ) as stderr_file:
        process = subprocess.Popen(  # noqa: S603
            command,
            cwd=str(base_dir),
            stdout=stdout_file,
            stderr=stderr_file,
        )
        logger.info("streamlit_started pid=%s", process.pid)
        pid_file.write_text(str(process.pid), encoding="utf-8")
        job_handle = _attach_kill_on_close_job(process, logger=logger)

        ready = _wait_port(HOST, launch_port, timeout_sec=90)
        if ready:
            logger.info("server_port_open host=%s port=%s", HOST, launch_port)
            try:
                if webbrowser.open(browser_url):
                    logger.info("browser_opened url=%s", browser_url)
                else:
                    logger.warning("browser_open_failed url=%s", browser_url)
            except Exception as exc:  # noqa: BLE001
                logger.warning("browser_open_exception url=%s error=%s", browser_url, exc)
        else:
            logger.warning("streamlit_not_ready host=%s port=%s timeout_sec=90", HOST, launch_port)

        try:
            return int(process.wait())
        except KeyboardInterrupt:
            logger.warning("launcher_keyboard_interrupt")
            _terminate_process_tree(process.pid, logger=logger)
            return 130
        finally:
            if process.poll() is None:
                _terminate_process_tree(process.pid, logger=logger)
            _close_job_handle(job_handle)
            pid_file.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
