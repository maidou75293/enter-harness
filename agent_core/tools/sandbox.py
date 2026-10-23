"""沙箱：把「命令 / 子进程」关进隔离环境（参数化，无业务依赖）。

后端（iso_mode）：
    bwrap   轻量进程沙箱（默认；共用宿主内核）
    docker  容器（namespace + 独立 rootfs）
    microvm Kata microVM（containerd + Kata V2 shim，独立内核）
    none    不隔离（排障）

调用方通过 mask（遮罩目录）、rw_paths、net_mode/net_allow 注入业务边界；
后端不可用时自动回退 bwrap，并把实际后端回报给调用方。
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import time

DEFAULT_RW = ("/tmp", "/mnt/c")
SANDBOX_MARK = "_IN_SANDBOX"

_PROBE = {"docker": 0.0, "ok": False}
PROBE_TTL = 60.0

CTR_NS = "agentarch"
CTR_RUNTIME = "io.containerd.kata.v2"
CTR_IMAGE = "docker.io/library/ubuntu:24.04"
CTR_BIN = "/usr/bin/ctr"
CTR_SOCK = "/run/containerd/containerd.sock"
KATA_SHIM = "/opt/kata/bin/containerd-shim-kata-v2"
_IMG_READY = {"t": 0.0, "ok": False}


# ---------------------------------------------------------------- 可用性
def bwrap_available() -> bool:
    return bool(shutil.which("bwrap")) and os.environ.get(SANDBOX_MARK) != "1"


def docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    now = time.time()
    if now - _PROBE["docker"] < PROBE_TTL:
        return _PROBE["ok"]
    try:
        p = subprocess.run(["docker", "info"], capture_output=True, timeout=8)
        ok = p.returncode == 0
    except Exception:  # noqa: BLE001
        ok = False
    _PROBE["docker"], _PROBE["ok"] = now, ok
    return ok


def kata_available() -> bool:
    if not (os.path.exists(KATA_SHIM) and os.path.exists(CTR_BIN)):
        return False
    return os.access(CTR_SOCK, os.R_OK | os.W_OK)


def ensure_ctr_image(image: str = CTR_IMAGE) -> bool:
    now = time.time()
    if _IMG_READY["ok"] and now - _IMG_READY["t"] < 600:
        return True
    ok = False
    try:
        chk = subprocess.run([CTR_BIN, "-n", CTR_NS, "images", "ls", "-q"],
                             capture_output=True, timeout=90)
        if image.encode() in (chk.stdout or b""):
            ok = True
        else:
            save = subprocess.run(["docker", "save", image.split("/")[-1]],
                                  capture_output=True, timeout=600)
            imp = subprocess.run([CTR_BIN, "-n", CTR_NS, "images", "import", "-"],
                                 input=save.stdout, capture_output=True, timeout=600)
            ok = imp.returncode == 0
    except Exception:  # noqa: BLE001
        ok = False
    _IMG_READY["t"], _IMG_READY["ok"] = now, ok
    return ok


def list_backends() -> list:
    return [
        {"mode": "bwrap", "label": "bwrap 进程沙箱（轻量，默认）",
         "available": bwrap_available()},
        {"mode": "docker", "label": "Docker 容器（独立 rootfs）",
         "available": docker_available()},
        {"mode": "microvm", "label": "Kata microVM（独立内核，最强）",
         "available": kata_available()},
        {"mode": "none", "label": "不隔离（排障用）", "available": True},
    ]


def resolve_backend(iso_mode: str) -> str:
    m = (iso_mode or "").strip().lower()
    if m == "none":
        return "none"
    if m == "docker" and docker_available():
        return "docker"
    if m == "microvm" and kata_available():
        return "microvm"
    return "bwrap" if bwrap_available() else "none"


# ---------------------------------------------------------------- 环境
def min_env(workdir: str, extra_path: str = "") -> dict:
    """最小环境变量：不继承宿主 env（密钥不经环境变量外泄）。"""
    path = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    if extra_path:
        path = path + ":" + extra_path
    return {
        "PATH": path,
        "HOME": workdir,
        "TERM": "dumb",
        "LANG": "C.UTF-8",
        "TZ": os.environ.get("TZ", "Asia/Shanghai"),
        SANDBOX_MARK: "1",
    }


# ---------------------------------------------------------------- 后端参数
def _microvm_argv(argv, ws, net_mode="all", net_allow=None, image=None, name=None) -> list:
    ensure_ctr_image(image or CTR_IMAGE)
    cname = name or f"aa-{os.getpid()}-{int(time.time() * 1000) % 1000000}"
    parts = [CTR_BIN, "-n", CTR_NS, "run", "--runtime", CTR_RUNTIME, "--rm"]
    parts += ["--mount", f"type=bind,src={ws},dst={ws},options=rbind:rw"]
    parts += ["--cwd", ws, "--env", f"HOME={ws}", "--env", f"{SANDBOX_MARK}=1"]
    if net_mode == "all":
        parts += ["--net-host"]
    elif net_mode == "whitelist":
        try:
            from . import egress
            parts += ["--net-host",
                      "--mount",
                      f"type=bind,src={egress.build_hosts(net_allow or [])},"
                      f"dst=/etc/hosts,options=rbind:ro",
                      "--mount",
                      f"type=bind,src={egress.empty_resolv()},"
                      f"dst=/etc/resolv.conf,options=rbind:ro"]
        except Exception:  # noqa: BLE001
            pass
    parts += [image or CTR_IMAGE, cname]
    return parts + list(argv)


def _bwrap_argv(argv, ws, rw_paths, net_mode, net_allow, mask):
    parts = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc"]
    parts += ["--bind", ws, ws]
    for m in mask:
        if m and os.path.isdir(str(m)) and not ws.startswith(str(m)):
            parts += ["--tmpfs", str(m)]
    for p in rw_paths:
        if p and os.path.isdir(str(p)):
            parts += ["--bind", p, p]
    parts += _net_flags_bwrap(net_mode, net_allow)
    parts += ["--chdir", ws, "--setenv", "HOME", ws]
    return parts + list(argv)


def _net_flags_bwrap(net_mode, net_allow):
    if net_mode == "off":
        return ["--unshare-net"]
    if net_mode == "whitelist":
        try:
            from . import egress
            return ["--bind", str(egress.build_hosts(net_allow or [])), "/etc/hosts",
                    "--bind", str(egress.empty_resolv()), "/etc/resolv.conf"]
        except Exception:  # noqa: BLE001
            return ["--unshare-net"]
    return []


def _docker_argv(argv, ws, runtime=None, net_mode="all", net_allow=None, image=None):
    parts = ["docker", "run", "--rm", "-i"]
    if runtime:
        parts += ["--runtime", runtime]
    parts += ["-v", f"{ws}:{ws}", "-w", ws, "-e", f"HOME={ws}",
              "-e", f"{SANDBOX_MARK}=1"]
    if net_mode == "off":
        parts += ["--network", "none"]
    elif net_mode == "whitelist":
        parts += ["--dns", "127.0.0.1"]
        try:
            from . import egress
            for d in (net_allow or []):
                base = str(d)[2:] if str(d).startswith("*.") else str(d)
                for ip in egress._resolve(base):
                    parts += ["--add-host", f"{base}:{ip}"]
        except Exception:  # noqa: BLE001
            parts += ["--network", "none"]
    parts += [image or "ubuntu:24.04"]
    return parts + list(argv)


# ---------------------------------------------------------------- 对外
def wrap_argv(argv: list, workdir: str, iso_mode: str = "bwrap", rw_paths=DEFAULT_RW,
              net_mode: str = "all", net_allow=None, mask=()) -> tuple:
    backend = resolve_backend(iso_mode)
    ws = str(workdir)
    if backend == "docker":
        return _docker_argv(argv, ws, None, net_mode, net_allow), backend
    if backend == "microvm":
        return _microvm_argv(argv, ws, net_mode, net_allow), backend
    if backend == "bwrap":
        return _bwrap_argv(argv, ws, rw_paths, net_mode, net_allow, mask), backend
    return list(argv), "none"


def wrap_shell(command: str, workdir: str, iso_mode: str = "bwrap", rw_paths=DEFAULT_RW,
               net_mode: str = "all", net_allow=None, mask=()) -> tuple:
    argv, backend = wrap_argv(["/bin/bash", "-lc", command], workdir, iso_mode,
                              rw_paths, net_mode, net_allow, mask)
    return " ".join(shlex.quote(x) for x in argv), backend
