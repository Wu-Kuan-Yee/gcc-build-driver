#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build.py -- a portable, self-bootstrapping build driver for GCC.

The script is intentionally self contained: it only needs Python 3.8+, a
system C/C++ compiler, make, and the operating system's own libraries.  All
third-party build dependencies (GMP, MPFR, MPC, ISL and zstd) are downloaded
as source and built as *static* libraries, so the resulting compiler has no
runtime dependency on Homebrew, MacPorts, /usr/local, conda, or any other
package manager.

Features
--------
* Full "all languages" configuration by default.  Languages that need an
  external bootstrap compiler (Ada/GNAT, D/GDC, Rust/cargo) or that are not
  supported for the target are skipped by GCC's configure with a warning.
* Bootstrapped (stage1 -> stage2 -> stage3) builds by default on native
  builds, with an option to disable bootstrapping for a quick build.
* Shared *and* static runtime libraries (libgcc, libstdc++, libgfortran,
  libobjc, libgomp, libatomic, libitm, libquadmath, libssp, libgm2,
  libga68, ...).
* Graphite/ISL, LTO, zstd compressed debug info, JIT, libgdiagnostics,
  OpenMP, sanitizers, plugins and the usual target libraries, all enabled
  whenever the host/target supports them.
* Reproducible dependency cache, SHA-512/SHA-256 verification, resumable
  downloads, multiple mirrors, and offline builds from a tarball directory.
* Post-build verification: runs a smoke test for every built front end,
  inspects binaries with otool/ldd/readelf, and fails if a compiler binary
  links a non-system third-party library.
* Relocatable install and optional binary tarball packaging.
* Cross-platform: macOS (arm64/x86_64), Linux, FreeBSD/OpenBSD/NetBSD,
  and Windows via MSYS2/MinGW (POSIX shell + Python required).

Quick start
-----------
    ./build.py                       # everything, defaults
    ./build.py -j 12                 # explicit parallelism
    ./build.py --no-bootstrap        # fast single-stage build
    ./build.py --deps-only           # just build the static dependencies
    ./build.py --doctor              # print a detailed environment report
    ./build.py --languages=all       # explicit: every buildable language

Use --help for the complete list of options.  The build log is written to
<build-dir>/build.log; a machine readable summary is written to
<build-dir>/build-summary.json.

中文提示：本脚本会自动下载并静态编译所有第三方依赖，默认构建 GCC 支持的
全部语言（需要外部引导编译器的 Ada/D/Rust 会被自动跳过并给出警告），并同时
生成动态库与静态库。常用命令：`./build.py -j 12`；只准备依赖：
`./build.py --deps-only`；快速构建：`./build.py --no-bootstrap`。
"""

from __future__ import print_function

import argparse
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import ssl
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import time
import urllib.error
import urllib.request
from pathlib import Path

SCRIPT_VERSION = "2.0.0"
MIN_PYTHON = (3, 8)
IS_WINDOWS = os.name == "nt"

# ---------------------------------------------------------------------------
# Constants: dependency versions, URLs and checksums.
# ---------------------------------------------------------------------------

GNU_MIRRORS = (
    "https://mirrors.kernel.org/gnu",
    "https://ftpmirror.gnu.org",
    "https://mirrors.ocf.berkeley.edu/gnu",
    "https://gcc.gnu.org/pub/gcc/infrastructure",
)

DEPS = [
    {
        "name": "gmp",
        "version": "6.3.0",
        "archive": "gmp-6.3.0.tar.bz2",
        "urls": [
            "https://mirrors.kernel.org/gnu/gmp/gmp-6.3.0.tar.bz2",
            "https://ftpmirror.gnu.org/gmp/gmp-6.3.0.tar.bz2",
            "https://mirrors.ocf.berkeley.edu/gnu/gmp/gmp-6.3.0.tar.bz2",
            "https://gcc.gnu.org/pub/gcc/infrastructure/gmp-6.3.0.tar.bz2",
        ],
        "sha512": "3b684c9bcb9ede2b7e54d0ba4c9764bfa17c20d4f3000017c553b6f1e135b536949580ff37341680c25dc236cfe0ba1db8cfdfe619ce013656189ef0871b89f8",
        "configure": ["--disable-shared", "--enable-static", "--with-pic", "--enable-cxx"],
        "kind": "autoconf",
        "libs": ["libgmp.a"],
    },
    {
        "name": "mpfr",
        "version": "4.2.2",
        "archive": "mpfr-4.2.2.tar.bz2",
        "urls": [
            "https://mirrors.kernel.org/gnu/mpfr/mpfr-4.2.2.tar.bz2",
            "https://ftpmirror.gnu.org/mpfr/mpfr-4.2.2.tar.bz2",
            "https://mirrors.ocf.berkeley.edu/gnu/mpfr/mpfr-4.2.2.tar.bz2",
            "https://gcc.gnu.org/pub/gcc/infrastructure/mpfr-4.2.2.tar.bz2",
        ],
        "sha512": "0176e50808dcc07afbf5bc3e38bf9b7b21918e5f194aa0bfd860d99b00c470630aef149776c4be814a61c44269c3a5b9a4b0b1c0fcd4c9feb1459d8466452da8",
        "configure": ["--disable-shared", "--enable-static", "--with-pic"],
        "kind": "autoconf",
        "libs": ["libmpfr.a"],
    },
    {
        "name": "mpc",
        "version": "1.3.1",
        "archive": "mpc-1.3.1.tar.gz",
        "urls": [
            "https://mirrors.kernel.org/gnu/mpc/mpc-1.3.1.tar.gz",
            "https://ftpmirror.gnu.org/mpc/mpc-1.3.1.tar.gz",
            "https://mirrors.ocf.berkeley.edu/gnu/mpc/mpc-1.3.1.tar.gz",
            "https://gcc.gnu.org/pub/gcc/infrastructure/mpc-1.3.1.tar.gz",
        ],
        "sha512": "4bab4ef6076f8c5dfdc99d810b51108ced61ea2942ba0c1c932d624360a5473df20d32b300fc76f2ba4aa2a97e1f275c9fd494a1ba9f07c4cb2ad7ceaeb1ae97",
        "configure": ["--disable-shared", "--enable-static", "--with-pic"],
        "kind": "autoconf",
        "libs": ["libmpc.a"],
    },
    {
        "name": "isl",
        "version": "0.25",
        "archive": "isl-0.25.tar.xz",
        "urls": [
            "https://mirrors.kernel.org/debian/pool/main/i/isl/isl_0.25.orig.tar.xz",
            "https://libisl.sourceforge.io/isl-0.25.tar.bz2",
        ],
        # SHA-256 of the Debian upstream tarball above (the ISL 0.24 fallback
        # is deliberately not used for hash verification; if 0.25 cannot be
        # obtained the build stops with a clear message).
        "sha256": "be7b210647ccadf90a2f0b000fca11a4d40546374a850db67adb32fad4b230d9",
        "configure": ["--disable-shared", "--enable-static", "--with-pic"],
        "kind": "autoconf",
        "libs": ["libisl.a"],
    },
    {
        "name": "zstd",
        "version": "1.5.7",
        "archive": "zstd-1.5.7.tar.gz",
        "urls": [
            "https://github.com/facebook/zstd/releases/download/v1.5.7/zstd-1.5.7.tar.gz",
            "https://codeload.github.com/facebook/zstd/tar.gz/refs/tags/v1.5.7",
        ],
        "sha256": "eb33e51f49a15e023950cd7825ca74a4a2b43db8354825ac24fc1b7ee09e6fa3",
        "configure": [],
        "kind": "zstd",
        "libs": ["libzstd.a"],
    },
]

# Libraries that normally exist in both shared and static form.  Used only
# for reporting; absence of a static JIT library, for example, is expected.
LIBXML2_DEP = {
    "name": "libxml2",
    "version": "2.13.5",
    "archive": "libxml2-2.13.5.tar.xz",
    "urls": [
        "https://download.gnome.org/sources/libxml2/2.13/libxml2-2.13.5.tar.xz",
        "https://mirrors.kernel.org/gnome/sources/libxml2/2.13/libxml2-2.13.5.tar.xz",
    ],
    "sha256": "74fc163217a3964257d3be39af943e08861263c4231f9ef5b496b6f6d4c7b2b6",
    "configure": ["--disable-shared", "--enable-static", "--with-pic",
                  "--without-python", "--without-lzma", "--without-zlib",
                  "--without-icu", "--without-readline", "--without-http",
                  "--without-ftp"],
    "kind": "autoconf",
    "libs": ["libxml2.a"],
}

GLIBC_SOURCE = {
    "version": "2.42",
    "archive": "glibc-2.42.tar.xz",
    "url": "https://ftp.gnu.org/gnu/glibc/glibc-2.42.tar.xz",
    "sha256": "d1775e32e4628e64ef930f435b67bb63af7599acb6be2b335b9f19f16509f17f",
}

LIBICONV_DEP = {
    "name": "libiconv",
    "version": "1.18",
    "archive": "libiconv-1.18.tar.gz",
    "urls": [
        "https://ftp.gnu.org/pub/gnu/libiconv/libiconv-1.18.tar.gz",
        "https://mirrors.kernel.org/gnu/libiconv/libiconv-1.18.tar.gz",
    ],
    "sha256": "3b08f5f4f9b4eb82f151a7040bfd6fe6c6fb922efe4b1659c66ea933276965e8",
    "configure": ["--disable-shared", "--enable-static", "--with-pic",
                  "--disable-nls"],
    "kind": "autoconf",
    "libs": ["libiconv.a"],
}

# Open-source stage0 toolchains for self-hosted GCC front ends.  Ada, D and
# Rust are written in their own languages (or need a runtime written in that
# language), so a first compiler cannot be produced from C/C++ sources alone.
# These are the standard, open-source stage0 binaries; GCC itself then builds
# the final Ada/D/Rust front ends and runtimes from source.
STAGE0_GNAT = {
    "aarch64-apple-darwin": {
        "url": "https://github.com/alire-project/GNAT-FSF-builds/releases/download/gnat-16.2.0-pre0/gnat-aarch64-darwin-16.2.0-pre0.tar.gz",
        "sha256": "832e08ade62b4566cb9f7e14d90c06175d543257bbe766a0807c8bf9abc773ca",
        "archive": "gnat-aarch64-darwin-16.2.0-pre0.tar.gz",
    },
    "x86_64-apple-darwin": {
        "url": "https://github.com/alire-project/GNAT-FSF-builds/releases/download/gnat-16.2.0-pre0/gnat-x86_64-darwin-16.2.0-pre0.tar.gz",
        "sha256": "0d41a1cf0f4975e2ee6e1a9606e1b445e9c65da0716eb698aa59e611fb84ab17",
        "archive": "gnat-x86_64-darwin-16.2.0-pre0.tar.gz",
    },
    "x86_64-unknown-linux-gnu": {
        "url": "https://github.com/alire-project/GNAT-FSF-builds/releases/download/gnat-16.1.0-1/gnat-x86_64-linux-16.1.0-1.tar.gz",
        "sha256": "9f74f58a827a2ad40dd84c72a413e75ea52888e0d8f7e252fba4d26762402703",
        "archive": "gnat-x86_64-linux-16.1.0-1.tar.gz",
    },
    "x86_64-pc-linux-gnu": {
        "url": "https://github.com/alire-project/GNAT-FSF-builds/releases/download/gnat-16.1.0-1/gnat-x86_64-linux-16.1.0-1.tar.gz",
        "sha256": "9f74f58a827a2ad40dd84c72a413e75ea52888e0d8f7e252fba4d26762402703",
        "archive": "gnat-x86_64-linux-16.1.0-1.tar.gz",
    },
    "aarch64-unknown-linux-gnu": {
        "url": "https://github.com/alire-project/GNAT-FSF-builds/releases/download/gnat-16.1.0-1/gnat-aarch64-linux-16.1.0-1.tar.gz",
        "sha256": "8e297497f2d6d6845920a363a277b6ef5bc454e48a66d0d606b89d71e31ee623",
        "archive": "gnat-aarch64-linux-16.1.0-1.tar.gz",
    },
    "aarch64-pc-linux-gnu": {
        "url": "https://github.com/alire-project/GNAT-FSF-builds/releases/download/gnat-16.1.0-1/gnat-aarch64-linux-16.1.0-1.tar.gz",
        "sha256": "8e297497f2d6d6845920a363a277b6ef5bc454e48a66d0d606b89d71e31ee623",
        "archive": "gnat-aarch64-linux-16.1.0-1.tar.gz",
    },
}

STAGE0_GNAT_FALLBACK = {
    "x86_64-unknown-linux-gnu": [{
        "url": "https://github.com/alire-project/GNAT-FSF-builds/releases/download/gnat-15.3.0-1/gnat-x86_64-linux-15.3.0-1.tar.gz",
        "sha256": "a6af58cbe96b8ed92b9d9929f89547c1be2ce96b86bb86422026074094429001",
        "archive": "gnat-x86_64-linux-15.3.0-1.tar.gz",
    }],
    "x86_64-pc-linux-gnu": [{
        "url": "https://github.com/alire-project/GNAT-FSF-builds/releases/download/gnat-15.3.0-1/gnat-x86_64-linux-15.3.0-1.tar.gz",
        "sha256": "a6af58cbe96b8ed92b9d9929f89547c1be2ce96b86bb86422026074094429001",
        "archive": "gnat-x86_64-linux-15.3.0-1.tar.gz",
    }],
    "aarch64-unknown-linux-gnu": [{
        "url": "https://github.com/alire-project/GNAT-FSF-builds/releases/download/gnat-15.3.0-1/gnat-aarch64-linux-15.3.0-1.tar.gz",
        "sha256": "322175876bf67b11d91b5cb8dea14da24b2f89f13292887222cf7f47a57f1db7",
        "archive": "gnat-aarch64-linux-15.3.0-1.tar.gz",
    }],
    "aarch64-pc-linux-gnu": [{
        "url": "https://github.com/alire-project/GNAT-FSF-builds/releases/download/gnat-15.3.0-1/gnat-aarch64-linux-15.3.0-1.tar.gz",
        "sha256": "322175876bf67b11d91b5cb8dea14da24b2f89f13292887222cf7f47a57f1db7",
        "archive": "gnat-aarch64-linux-15.3.0-1.tar.gz",
    }],
}

STAGE0_GDC = {
    "aarch64-apple-darwin": {
        "url": "https://github.com/ibara/macOS-GDC/releases/download/14.2/gcc14.tgz",
        "sha256": "dfbda8a21a1376e757328fd3e349c1c88e2acfab79266d649d95ecd1a19f825f",
        "archive": "gdc-14.2-aarch64-apple-darwin.tgz",
    },
}

# Debian gdc-14 packages: a self-contained native GDC frontend (d21) that can
# be extracted without root.  These are binary seeds, like the GNAT/Rust ones;
# GCC then builds the final D frontend and libphobos from source.
STAGE0_GDC_MODERN = {
    "x86_64-pc-linux-gnu": {
        "url": "http://deb.debian.org/debian/pool/main/g/gcc-14/gdc-14-x86-64-linux-gnu_14.2.0-19_amd64.deb",
        "sha256": "f2ff073bbad96e3f4f426feb2f05db6247a096b4f1cb1f9bf1e4937d6600d178",
        "archive": "gdc-14-x86-64-linux-gnu_14.2.0-19_amd64.deb",
        "runtime_url": "http://deb.debian.org/debian/pool/main/g/gcc-14/libgphobos-14-dev_14.2.0-19_amd64.deb",
        "runtime_sha256": "fdbd7f107ed5238d54d10608cb6cae2e773382f26ada9b9b04f4fd3290b01f92",
        "runtime_archive": "libgphobos-14-dev_14.2.0-19_amd64.deb",
    },
    "x86_64-unknown-linux-gnu": {
        "url": "http://deb.debian.org/debian/pool/main/g/gcc-14/gdc-14-x86-64-linux-gnu_14.2.0-19_amd64.deb",
        "sha256": "f2ff073bbad96e3f4f426feb2f05db6247a096b4f1cb1f9bf1e4937d6600d178",
        "archive": "gdc-14-x86-64-linux-gnu_14.2.0-19_amd64.deb",
        "runtime_url": "http://deb.debian.org/debian/pool/main/g/gcc-14/libgphobos-14-dev_14.2.0-19_amd64.deb",
        "runtime_sha256": "fdbd7f107ed5238d54d10608cb6cae2e773382f26ada9b9b04f4fd3290b01f92",
        "runtime_archive": "libgphobos-14-dev_14.2.0-19_amd64.deb",
    },
    "aarch64-unknown-linux-gnu": {
        "url": "http://deb.debian.org/debian/pool/main/g/gcc-14/gdc-14-aarch64-linux-gnu_14.2.0-19_arm64.deb",
        "sha256": "1de55f068663b720ebcb9d214f7f9cae227a3433b7016b134f41b6da2c4394b3",
        "archive": "gdc-14-aarch64-linux-gnu_14.2.0-19_arm64.deb",
        "runtime_url": "http://deb.debian.org/debian/pool/main/g/gcc-14/libgphobos-14-dev_14.2.0-19_arm64.deb",
        "runtime_sha256": "db4b4309bdb729b4e826153f681d1cc48f2117f274f29afa52549e67b17dcf88",
        "runtime_archive": "libgphobos-14-dev_14.2.0-19_arm64.deb",
    },
    "aarch64-pc-linux-gnu": {
        "url": "http://deb.debian.org/debian/pool/main/g/gcc-14/gdc-14-aarch64-linux-gnu_14.2.0-19_arm64.deb",
        "sha256": "1de55f068663b720ebcb9d214f7f9cae227a3433b7016b134f41b6da2c4394b3",
        "archive": "gdc-14-aarch64-linux-gnu_14.2.0-19_arm64.deb",
        "runtime_url": "http://deb.debian.org/debian/pool/main/g/gcc-14/libgphobos-14-dev_14.2.0-19_arm64.deb",
        "runtime_sha256": "db4b4309bdb729b4e826153f681d1cc48f2117f274f29afa52549e67b17dcf88",
        "runtime_archive": "libgphobos-14-dev_14.2.0-19_arm64.deb",
    },
}

STAGE0_GDC_LEGACY = {
    "x86_64-pc-linux-gnu": {
        "url": "https://gdcproject.org/archive/binaries/x86_64-linux-gnu/gdc-5.2.0.tar.xz",
        "sha256": "97a58d830d16ea926c56269a80fc1d4f87ccc8280a15ca2ea4fde3d9a500c95a",
        "archive": "gdc-5.2.0-x86_64-linux-gnu.tar.xz",
    },
    "x86_64-unknown-linux-gnu": {
        "url": "https://gdcproject.org/archive/binaries/x86_64-linux-gnu/gdc-5.2.0.tar.xz",
        "sha256": "97a58d830d16ea926c56269a80fc1d4f87ccc8280a15ca2ea4fde3d9a500c95a",
        "archive": "gdc-5.2.0-x86_64-linux-gnu.tar.xz",
    },
}

RUST_CHANNELS = ("stable", "1.90.0", "1.85.0")

RUST_TARGETS = {
    "aarch64-apple-darwin": "aarch64-apple-darwin",
    "x86_64-apple-darwin": "x86_64-apple-darwin",
    "x86_64-unknown-linux-gnu": "x86_64-unknown-linux-gnu",
    "aarch64-unknown-linux-gnu": "aarch64-unknown-linux-gnu",
    "x86_64-pc-linux-gnu": "x86_64-unknown-linux-gnu",
    "aarch64-unknown-linux-gnu": "aarch64-unknown-linux-gnu",
    "x86_64-unknown-freebsd": "x86_64-unknown-freebsd",
}

DUAL_LIBS = (
    "libgcc", "libstdc++", "libsupc++", "libgfortran", "libobjc", "libobjc-gnu", "libgomp",
    "libatomic", "libitm", "libquadmath", "libssp", "libgm2", "libga68",
    "libasan", "libubsan", "liblsan", "libtsan", "libgdiagnostics",
)

MACHO_SYSTEM_PREFIXES = (
    "/usr/lib/", "/System/Library/", "/Library/Apple/", "/usr/local/lib/system/",
)
ELF_SYSTEM_PREFIXES = (
    "/lib/", "/lib64/", "/usr/lib/", "/usr/lib64/", "/usr/libexec/",
)


# ---------------------------------------------------------------------------
# Logging and command execution.
# ---------------------------------------------------------------------------

class Color(object):
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"


class BuildError(RuntimeError):
    pass


class Logger(object):
    def __init__(self, logfile=None, color="auto", quiet=False, verbose=False):
        self.quiet = quiet
        self.verbose = verbose
        self.fh = None
        self.logfile = Path(logfile) if logfile else None
        if self.logfile:
            self.logfile.parent.mkdir(parents=True, exist_ok=True)
            self.fh = open(str(self.logfile), "a", encoding="utf-8", errors="replace")
            self.fh.write("\n===== build.py %s started %s =====\n" %
                          (SCRIPT_VERSION, time.strftime("%Y-%m-%d %H:%M:%S")))
            self.fh.flush()
        if color == "always":
            self.use_color = True
        elif color == "never":
            self.use_color = False
        else:
            self.use_color = sys.stdout.isatty()

    def close(self):
        if self.fh:
            self.fh.write("===== finished %s =====\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
            self.fh.close()
            self.fh = None

    def _c(self, text, code):
        if self.use_color:
            return code + text + Color.RESET
        return text

    def _emit(self, text, stream=None):
        if not self.quiet:
            print(text, file=stream or sys.stdout, flush=True)
        if self.fh:
            self.fh.write(_strip_ansi(text) + "\n")
            self.fh.flush()

    def info(self, text):
        self._emit(text)

    def step(self, text):
        self._emit("\n" + self._c("==> ", Color.BOLD + Color.CYAN) + self._c(text, Color.BOLD))

    def ok(self, text):
        self._emit(self._c("[ OK ] ", Color.GREEN) + text)

    def warn(self, text):
        self._emit(self._c("[WARN] ", Color.YELLOW) + text, stream=sys.stderr)

    def error(self, text):
        line = self._c("[FAIL] ", Color.RED) + text
        print(line, file=sys.stderr, flush=True)
        if self.fh:
            self.fh.write(_strip_ansi(line) + "\n")
            self.fh.flush()

    def debug(self, text):
        if self.verbose:
            self._emit(self._c("       " + text, Color.DIM))
        elif self.fh:
            self.fh.write(_strip_ansi(text) + "\n")
            self.fh.flush()

    def command(self, cmd):
        line = "       $ " + " ".join(shlex.quote(str(c)) for c in cmd)
        if self.verbose:
            self._emit(self._c(line, Color.DIM))
        elif self.fh:
            self.fh.write(_strip_ansi(line) + "\n")
            self.fh.flush()

    def stream_line(self, line):
        self._emit(line)


def _strip_ansi(text):
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _fmt_duration(seconds):
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return "%dh%02dm%02ds" % (h, m, s)
    if m:
        return "%dm%02ds" % (m, s)
    return "%ds" % s


def _which(names, extra_paths=None):
    for name in names:
        if extra_paths:
            for d in extra_paths:
                candidate = Path(d) / name
                if candidate.is_file() and os.access(str(candidate), os.X_OK):
                    return str(candidate)
        found = shutil.which(name)
        if found:
            return found
    return None


def system_first_path(extra=()):
    """Return PATH with the OS tool directories first, so a package-manager
    copy of gcc/ar/ld cannot leak into the build."""
    items = [str(x) for x in extra if x]
    if not IS_WINDOWS:
        for d in ("/usr/bin", "/bin", "/usr/sbin", "/sbin"):
            if os.path.isdir(d):
                items.append(d)
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if d and d not in items:
            items.append(d)
    return os.pathsep.join(items)


class Runner(object):
    def __init__(self, log, dry_run=False):
        self.log = log
        self.dry_run = dry_run

    def run(self, cmd, cwd=None, env=None, check=True, capture=False, timeout=None):
        cmd = [str(c) for c in cmd]
        self.log.command(cmd)
        if self.dry_run:
            return ""
        try:
            if capture:
                proc = subprocess.run(
                    cmd, cwd=str(cwd) if cwd else None, env=env,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, errors="replace", timeout=timeout,
                )
                for line in (proc.stdout or "").splitlines():
                    self.log.stream_line(line)
                if check and proc.returncode != 0:
                    raise BuildError("command failed with exit code %d: %s" %
                                     (proc.returncode, " ".join(cmd)))
                return proc.stdout or ""
            proc = subprocess.Popen(
                cmd, cwd=str(cwd) if cwd else None, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, errors="replace", bufsize=1,
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                self.log.stream_line(line.rstrip("\n"))
            rc = proc.wait(timeout=timeout)
            if check and rc != 0:
                raise BuildError("command failed with exit code %d: %s" %
                                 (rc, " ".join(cmd)))
            return ""
        except KeyboardInterrupt:
            self.log.error("interrupted")
            raise
        except subprocess.TimeoutExpired:
            raise BuildError("command timed out after %ss: %s" %
                             (timeout, " ".join(cmd)))
        except OSError as exc:
            raise BuildError("could not execute %s: %s" % (" ".join(cmd), exc))


# ---------------------------------------------------------------------------
# Download / extraction helpers.
# ---------------------------------------------------------------------------

class Downloader(object):
    def __init__(self, log, cache_dir, tarball_dir=None, retries=4,
                 insecure=False, offline=False, verify=True, dry_run=False):
        self.log = log
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.tarball_dir = Path(tarball_dir) if tarball_dir else None
        self.retries = retries
        self.insecure = insecure
        self.offline = offline
        self.verify = verify
        self.dry_run = dry_run
        self.ctx = ssl._create_unverified_context() if insecure else ssl.create_default_context()

    def obtain(self, archive, urls, sha512=None, sha256=None):
        """Return a local path to a verified archive."""
        # 1. A user supplied tarball directory always wins (offline builds).
        for d in [self.tarball_dir, self.cache_dir]:
            if d is None:
                continue
            p = d / archive
            if p.is_file():
                if (not self.verify) or self._check_hash(p, sha512, sha256):
                    self.log.ok("using cached %s" % p)
                    return p
                self.log.warn("ignoring %s (checksum mismatch)" % p)
                if d == self.cache_dir:
                    p.unlink()

        if self.offline:
            raise BuildError(
                "offline mode: %s is not present in %s" %
                (archive, self.tarball_dir or self.cache_dir))

        dest = self.cache_dir / archive
        part = self.cache_dir / (archive + ".part")
        errors = []
        for url in urls:
            for attempt in range(1, self.retries + 1):
                try:
                    self.log.info("  downloading %s (try %d/%d)" %
                                  (url, attempt, self.retries))
                    self._http_get(url, part)
                    if self.verify and not self._check_hash(part, sha512, sha256):
                        part.unlink()
                        raise BuildError("checksum mismatch for %s" % url)
                    os.replace(str(part), str(dest))
                    self.log.ok("downloaded %s" % dest.name)
                    return dest
                except KeyboardInterrupt:
                    raise
                except Exception as exc:  # network errors, checksum errors
                    errors.append("%s: %s" % (url, exc))
                    self.log.warn("download failed: %s" % exc)
                    if part.exists() and part.stat().st_size == 0:
                        part.unlink()
                    time.sleep(min(2 ** attempt, 10))
        raise BuildError("could not download %s\n  %s" %
                         (archive, "\n  ".join(errors[-4:])))

    def fetch_text(self, url, timeout=60):
        req = urllib.request.Request(url, headers={
            "User-Agent": "gcc-build.py/%s" % SCRIPT_VERSION})
        with urllib.request.urlopen(req, timeout=timeout, context=self.ctx) as resp:
            return resp.read().decode("utf-8", "replace")

    def _http_get(self, url, part):
        offset = part.stat().st_size if part.exists() else 0
        headers = {"User-Agent": "gcc-build.py/%s" % SCRIPT_VERSION}
        if offset:
            headers["Range"] = "bytes=%d-" % offset
        req = urllib.request.Request(url, headers=headers)
        try:
            resp = urllib.request.urlopen(req, timeout=60, context=self.ctx)
        except urllib.error.HTTPError as exc:
            if exc.code == 416 and offset:
                return
            raise
        with resp:
            status = getattr(resp, "status", 200)
            mode = "ab" if (offset and status == 206) else "wb"
            if mode == "wb":
                offset = 0
            total = offset + int(resp.headers.get("Content-Length") or 0)
            started = time.time()
            last_report = 0.0
            with open(str(part), mode) as out:
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
                    offset += len(chunk)
                    now = time.time()
                    if now - last_report > 2.0:
                        last_report = now
                        elapsed = max(now - started, 0.001)
                        speed = (offset - (total - int(resp.headers.get("Content-Length") or 0))) / elapsed / 1024.0
                        if total:
                            self.log.info("    %5.1f%%  %8.1f KiB/s" %
                                          (100.0 * offset / total, speed))
                        else:
                            self.log.info("    %8.1f KiB received" % (offset / 1024.0))

    @staticmethod
    def _check_hash(path, sha512=None, sha256=None):
        if not sha512 and not sha256:
            return True
        algo = hashlib.sha512() if sha512 else hashlib.sha256()
        expected = sha512 or sha256
        with open(str(path), "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                algo.update(chunk)
        return algo.hexdigest().lower() == expected.lower()


def safe_extract(archive, dest):
    """Extract a tarball, rejecting absolute/.. paths and unsafe links."""
    dest = Path(dest).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(str(archive), "r:*") as tf:
        members = tf.getmembers()
        for m in members:
            name = m.name.replace("\\", "/")
            parts = [p for p in name.split("/") if p not in ("", ".")]
            if name.startswith("/") or ".." in parts:
                raise BuildError("unsafe path in archive: %s" % m.name)
            if m.issym() or m.islnk():
                target = m.linkname.replace("\\", "/")
                if target.startswith("/") or ".." in target.split("/"):
                    raise BuildError("unsafe link in archive: %s -> %s" %
                                     (m.name, m.linkname))
        # Python 3.12+ supports filter=; older versions do not.
        try:
            tf.extractall(str(dest), filter="data")  # type: ignore[call-arg]
        except TypeError:
            tf.extractall(str(dest))
    roots = set()
    for m in members:
        name = m.name.replace("\\", "/").lstrip("./")
        if name and "/" in name:
            roots.add(name.split("/", 1)[0])
        elif name:
            roots.add(name)
    if len(roots) == 1:
        root = dest / roots.pop()
        if root.is_dir():
            return root
    return dest


def extract_deb(archive, dest):
    """Extract the data.tar.* member of a Debian package safely."""
    import io
    import struct
    dest = Path(dest).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    with open(str(archive), "rb") as fh:
        if fh.read(8) != b"!<arch>\n":
            raise BuildError("not a Debian ar archive: %s" % archive)
        payload = None
        while True:
            header = fh.read(60)
            if not header:
                break
            if len(header) < 60:
                raise BuildError("truncated ar archive: %s" % archive)
            name = header[:16].decode("ascii", "replace").strip()
            size = int(header[48:58].decode("ascii", "replace").strip() or 0)
            data = fh.read(size)
            if size % 2:
                fh.read(1)
            if name.startswith("data.tar"):
                payload = data
                break
    if payload is None:
        raise BuildError("Debian package has no data.tar member: %s" % archive)
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:*") as tf:
        for m in tf.getmembers():
            name = m.name.replace("\\", "/")
            parts = [p for p in name.split("/") if p not in ("", ".")]
            if name.startswith("/") or ".." in parts:
                raise BuildError("unsafe path in Debian package: %s" % m.name)
            if m.issym() or m.islnk():
                target = m.linkname.replace("\\", "/")
                if target.startswith("/"):
                    raise BuildError("unsafe link in Debian package: %s" % m.name)
        try:
            tf.extractall(str(dest), filter="data")  # type: ignore[call-arg]
        except TypeError:
            tf.extractall(str(dest))
    return dest


# ---------------------------------------------------------------------------
# Platform / toolchain discovery.
# ---------------------------------------------------------------------------

def _run_capture(cmd, cwd=None, env=None):
    try:
        proc = subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              text=True, errors="replace", timeout=60)
        if proc.returncode == 0:
            return proc.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def detect_triple(source_dir):
    if platform.system() == "Windows":
        machine = platform.machine().lower()
        arch = {"arm64": "aarch64", "amd64": "x86_64"}.get(machine, machine)
        return "%s-w64-mingw32" % arch
    guess = Path(source_dir) / "config.guess"
    if guess.is_file():
        out = _run_capture(["/bin/sh", str(guess)], cwd=source_dir)
        if out and "\n" not in out and " " not in out:
            return out
        out = _run_capture(["sh", str(guess)], cwd=source_dir)
        if out and "\n" not in out and " " not in out:
            return out
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = {"arm64": "aarch64", "amd64": "x86_64", "x86-64": "x86_64"}.get(machine, machine)
    release = platform.release()
    if system == "darwin":
        return "%s-apple-darwin%s" % (arch, release)
    if system == "linux":
        return "%s-unknown-linux-gnu" % arch
    if system in ("freebsd", "openbsd", "netbsd", "dragonfly"):
        return "%s-unknown-%s%s" % (arch, system, release.split(".")[0])
    if system == "windows":
        return "%s-w64-mingw32" % arch
    return "%s-unknown-%s" % (arch, system)


def find_macos_sdk(log):
    if platform.system() != "Darwin":
        return None
    xcrun = shutil.which("xcrun")
    if xcrun:
        out = _run_capture([xcrun, "--show-sdk-path"])
        if out and Path(out).is_dir():
            return out
    for base in (
        "/Applications/Xcode.app/Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs",
        "/Library/Developer/CommandLineTools/SDKs",
    ):
        p = Path(base)
        if p.is_dir():
            for cand in sorted(p.glob("MacOSX*.sdk")):
                if cand.is_dir():
                    return str(cand)
    log.warn("could not locate a macOS SDK; set --sdk explicitly")
    return None


def find_make(log):
    names = ["gmake", "make"] if platform.system() in ("FreeBSD", "OpenBSD", "NetBSD") else ["make", "gmake"]
    for name in names:
        path = shutil.which(name)
        if not path:
            continue
        out = _run_capture([path, "--version"])
        if "GNU Make" in out:
            m = re.search(r"(\d+)\.(\d+)", out)
            if m and (int(m.group(1)), int(m.group(2))) >= (3, 80):
                return path
            log.warn("%s is too old (%s); GCC needs GNU make >= 3.80" %
                     (path, out.splitlines()[0] if out else "unknown"))
    raise BuildError("GNU make >= 3.80 is required but was not found")


def find_compiler(kind, explicit, log):
    names = [explicit] if explicit else []
    if kind == "cc":
        names += ["cc", "clang", "gcc"]
        lang = "c"
    else:
        names += ["c++", "clang++", "g++"]
        lang = "c++"
    for name in names:
        path = shutil.which(name) if name else None
        if not path:
            continue
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / ("t." + ("c" if kind == "cc" else "cc"))
            obj = Path(tmp) / "t.o"
            src.write_text("int main(void){return 0;}\n")
            proc = subprocess.run([path, "-x", lang, "-c", str(src), "-o", str(obj)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True)
            if proc.returncode == 0:
                return path
            log.warn("%s cannot compile a trivial %s program; skipping" % (path, lang))
    raise BuildError("no working %s compiler found; use --cc/--cxx" % lang)


def compiler_banner(path):
    out = _run_capture([path, "--version"])
    return out.splitlines()[0] if out else "unknown"


def tool_paths():
    return {
        "ar": _which(["ar"]), "as": _which(["as"]), "ld": _which(["ld"]),
        "nm": _which(["nm"]), "ranlib": _which(["ranlib"]),
        "strip": _which(["strip"]), "lipo": _which(["lipo"]),
        "otool": _which(["otool"]), "dsymutil": _which(["dsymutil"]),
        "objcopy": _which(["objcopy"]), "objdump": _which(["objdump"]),
        "readelf": _which(["readelf"]),
    }


def make_tool_wrappers(build_dir, triples, log):
    """Create unprefixed/prefixed wrappers for binutils so that GCC's build
    system never picks up a tool from a foreign toolchain or calls xcrun."""
    wrap = Path(build_dir) / "toolwrap"
    wrap.mkdir(parents=True, exist_ok=True)
    tools = tool_paths()
    names = set()
    for triple in triples:
        if not triple:
            continue
        for tool in tools:
            names.add("%s-%s" % (triple, tool))
    for tool, path in tools.items():
        if path:
            names.add(tool)
    created = 0
    for name in sorted(names):
        base = name.split("-")[-1]
        target = tools.get(base)
        if not target:
            continue
        p = wrap / name
        script = "#!/bin/sh\nexec %s \"$@\"\n" % shlex.quote(target)
        try:
            p.write_text(script)
            p.chmod(0o755)
            created += 1
        except OSError as exc:
            log.warn("could not create tool wrapper %s: %s" % (p, exc))
    if created:
        log.debug("created %d tool wrappers in %s" % (created, wrap))
    return wrap


# ---------------------------------------------------------------------------
# Dependency builder.
# ---------------------------------------------------------------------------

class DependencyManager(object):
    def __init__(self, root, prefix, log, runner, cc, cxx, jobs,
                 dep_cflags, downloader, mode="all", deps=None):
        self.root = Path(root)
        self.prefix = Path(prefix)
        self.log = log
        self.runner = runner
        self.cc = cc
        self.cxx = cxx
        self.jobs = jobs
        self.dep_cflags = dep_cflags
        self.downloader = downloader
        self.mode = mode
        self.deps = list(deps if deps is not None else DEPS)
        self.src_dir = self.root / "src"
        self.stamps = self.root / "stamps"
        for p in (self.prefix, self.src_dir, self.stamps, self.root / "logs"):
            p.mkdir(parents=True, exist_ok=True)

    def env(self):
        env = os.environ.copy()
        for key in ("CPATH", "C_INCLUDE_PATH", "CPLUS_INCLUDE_PATH",
                    "OBJC_INCLUDE_PATH", "LIBRARY_PATH", "LD_LIBRARY_PATH",
                    "DYLD_LIBRARY_PATH", "PKG_CONFIG_PATH", "CONDA_PREFIX",
                    "MAKEFLAGS", "MFLAGS"):
            env.pop(key, None)
        env["CC"] = self.cc
        env["CXX"] = self.cxx
        env["CFLAGS"] = self.dep_cflags
        # Never pass a C standard flag to a C++ compiler.  GMP's --enable-cxx
        # configure otherwise probes /usr/bin/c++ -std=gnu11 and fails.
        cxxflags = re.sub(r"-std=(?:gnu|c)(?:11|17|23)",
                          "-std=gnu++14", self.dep_cflags)
        if "-std=" not in cxxflags:
            cxxflags += " -std=gnu++14"
        env["CXXFLAGS"] = cxxflags
        env["LDFLAGS"] = ""
        env["LC_ALL"] = "C"
        env["LANG"] = "C"
        env["PATH"] = system_first_path()
        return env

    def download_all(self):
        archives = []
        for dep in self.deps:
            archives.append(self.downloader.obtain(
                dep["archive"], dep["urls"],
                sha512=dep.get("sha512"), sha256=dep.get("sha256")))
        self.log.ok("downloaded: %s" % ", ".join(str(a) for a in archives))
        return archives

    def ensure_all(self):
        if self.mode == "none":
            self.log.warn("dependency building disabled; GCC will use system libraries if found")
            return self.prefix if self._prefix_has_libs() else None
        built = []
        for dep in self.deps:
            if self._is_built(dep):
                self.log.ok("%s %s already built (%s)" %
                            (dep["name"], dep["version"], self.prefix))
                built.append(dep["name"])
                continue
            self._build_one(dep)
            built.append(dep["name"])
        self.log.ok("static dependencies ready: %s" % ", ".join(built))
        return self.prefix

    def _prefix_has_libs(self):
        return all((self.prefix / "lib" / lib).exists() for dep in self.deps for lib in dep["libs"])

    def _stamp_path(self, dep):
        return self.stamps / ("%s-%s.json" % (dep["name"], dep["version"]))

    def _is_built(self, dep):
        for lib in dep["libs"]:
            if not (self.prefix / "lib" / lib).is_file():
                return False
        header = self.prefix / "include" / ("zstd.h" if dep["name"] == "zstd" else "%s.h" % dep["name"])
        if dep["name"] in ("gmp", "mpfr", "mpc") and not header.is_file():
            return False
        if dep["name"] == "isl" and not (self.prefix / "include" / "isl" / "schedule.h").is_file():
            return False
        if dep["name"] == "libxml2" and not (self.prefix / "include" / "libxml2" / "libxml" / "parser.h").is_file():
            return False
        stamp = self._stamp_path(dep)
        if not stamp.is_file() and not self.runner.dry_run:
            stamp.write_text(json.dumps({
                "name": dep["name"], "version": dep["version"],
                "prefix": str(self.prefix),
                "adopted": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }, indent=2) + "\n")
        return True

    def _build_one(self, dep):
        self.log.step("building %s %s (static, PIC)" % (dep["name"], dep["version"]))
        archive = self.downloader.obtain(
            dep["archive"], dep["urls"],
            sha512=dep.get("sha512"), sha256=dep.get("sha256"))
        src_root = self.src_dir / ("%s-%s" % (dep["name"], dep["version"]))
        if src_root.exists():
            shutil.rmtree(str(src_root))
        self.log.info("  extracting %s" % archive)
        extracted = safe_extract(archive, self.src_dir)
        if extracted == self.src_dir:
            raise BuildError("archive %s has no single top-level directory" % archive)
        if extracted.name != src_root.name:
            if src_root.exists():
                shutil.rmtree(str(src_root))
            extracted.rename(src_root)
        if dep["kind"] == "zstd":
            self._build_zstd(dep, src_root)
        else:
            self._build_autoconf(dep, src_root)
        stamp = {
            "name": dep["name"], "version": dep["version"],
            "prefix": str(self.prefix), "built": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "sha512": dep.get("sha512"), "sha256": dep.get("sha256"),
        }
        self._stamp_path(dep).write_text(json.dumps(stamp, indent=2) + "\n")
        self.log.ok("%s %s built" % (dep["name"], dep["version"]))

    def _build_autoconf(self, dep, src):
        args = ["./configure", "--prefix=%s" % self.prefix] + list(dep["configure"])
        if dep["name"] in ("mpfr", "mpc"):
            args.append("--with-gmp=%s" % self.prefix)
        if dep["name"] == "mpc":
            args.append("--with-mpfr=%s" % self.prefix)
        if dep["name"] == "isl":
            args.append("--with-gmp-prefix=%s" % self.prefix)
        self.runner.run(args, cwd=src, env=self.env())
        self.runner.run([find_make(self.log), "-j%d" % self.jobs], cwd=src, env=self.env())
        self.runner.run([find_make(self.log), "install"], cwd=src, env=self.env())

    def _build_zstd(self, dep, src):
        make = find_make(self.log)
        self.runner.run([make, "-C", "lib", "-j%d" % self.jobs, "libzstd.a"],
                        cwd=src, env=self.env())
        libdir = self.prefix / "lib"
        incdir = self.prefix / "include"
        libdir.mkdir(parents=True, exist_ok=True)
        incdir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src / "lib" / "libzstd.a"), str(libdir / "libzstd.a"))
        for header in ("zstd.h", "zstd_errors.h"):
            shutil.copy2(str(src / "lib" / header), str(incdir / header))


class StaticRuntimeManager(object):
    """Build a private static glibc for linking the shipped host tools.

    The system compiler/linker are used only as build-time tools.  The libc.a,
    crt files and static libs installed here are built from the glibc source
    tarball; GCC's own libstdc++/libgcc are built later by the GCC bootstrap
    and are injected by the static-runtime compiler wrapper.
    """

    def __init__(self, build_root, log, runner, cc, cxx, jobs, downloader):
        self.build_root = Path(build_root)
        self.prefix = self.build_root / "static-runtime"
        self.src_parent = self.build_root / "static-runtime-src"
        self.build = self.build_root / "static-runtime-build"
        self.log = log
        self.runner = runner
        self.cc = cc
        self.cxx = cxx
        self.jobs = jobs
        self.downloader = downloader

    def _ready(self):
        return all((self.prefix / "lib" / name).is_file() for name in
                   ("libc.a", "libm.a", "crt1.o", "crti.o", "crtn.o"))

    def env(self):
        env = os.environ.copy()
        for key in ("CPATH", "C_INCLUDE_PATH", "CPLUS_INCLUDE_PATH",
                    "LIBRARY_PATH", "LD_LIBRARY_PATH", "PKG_CONFIG_PATH",
                    "CONDA_PREFIX", "MAKEFLAGS", "MFLAGS"):
            env.pop(key, None)
        env["CC"] = self.cc
        env["CXX"] = self.cxx
        env["CFLAGS"] = "-O2 -g"
        env["CXXFLAGS"] = "-O2 -g"
        env["LDFLAGS"] = ""
        env["LC_ALL"] = "C"
        env["LANG"] = "C"
        env["PATH"] = system_first_path()
        return env

    def ensure(self):
        if self._ready():
            self.log.ok("private static glibc already built (%s)" % self.prefix)
            return self.prefix
        self.log.step("building private static glibc %s from source" %
                      GLIBC_SOURCE["version"])
        archive = self.downloader.obtain(
            GLIBC_SOURCE["archive"], [GLIBC_SOURCE["url"]],
            sha256=GLIBC_SOURCE["sha256"])
        self.src_parent.mkdir(parents=True, exist_ok=True)
        safe_extract(archive, self.src_parent)
        src = self.src_parent / ("glibc-%s" % GLIBC_SOURCE["version"])
        if not src.is_dir():
            raise BuildError("glibc source was not extracted as %s" % src)
        if self.build.exists():
            shutil.rmtree(str(self.build), ignore_errors=True)
        self.build.mkdir(parents=True, exist_ok=True)
        self.runner.run(
            [str(src / "configure"), "--prefix=%s" % self.prefix,
             "--enable-static", "--enable-shared",
             "--disable-nscd", "--without-selinux", "--disable-werror"],
            cwd=self.build, env=self.env())
        make = find_make(self.log)
        self.runner.run([make, "-j%d" % self.jobs], cwd=self.build,
                        env=self.env())
        self.runner.run([make, "install"], cwd=self.build, env=self.env())
        if not self._ready():
            raise BuildError("private glibc install is incomplete: %s" % self.prefix)
        self.log.ok("private static glibc ready: %s" % self.prefix)
        return self.prefix


# ---------------------------------------------------------------------------
# Stage0 bootstrap toolchains (GNAT / GDC / rustc).
# ---------------------------------------------------------------------------

def _base_triple(triple):
    parts = triple.split("-")
    if len(parts) >= 3:
        # Drop the OS version from ...-darwin27.2.0 / ...-freebsd14.0,
        # while keeping the ABI component of ...-linux-gnu.
        parts[2] = re.sub(r"[0-9]+(\.[0-9]+)*$", "", parts[2])
        return "-".join(parts[:4] if len(parts) >= 4 else parts[:3])
    return triple


class Stage0Manager(object):
    """Locate or fetch the open-source stage0 compilers needed to bootstrap
    GCC's self-hosted front ends.

    A compiler for Ada (GNAT), D (GDC) or Rust (rustc) cannot be created from
    C/C++ sources alone: those languages are self-hosted.  The standard
    solution, used by the projects themselves, is a trusted stage0 binary;
    GCC then builds the final compiler and runtime from source.  The binaries
    fetched here are the official/open-source stage0 distributions.
    """

    def __init__(self, root, log, downloader, host_triple, sdk, args):
        self.root = Path(root)
        self.log = log
        self.downloader = downloader
        self.host = _base_triple(host_triple)
        self.sdk = sdk
        self.args = args
        self.bin = self.root / "bin"
        self.cc = None
        self.cxx = None
        self.env_extra = {}
        self.found = {}
        self.bin.mkdir(parents=True, exist_ok=True)

    # -- helpers ---------------------------------------------------------

    def _wanted(self):
        spec = (self.args.languages or "all").strip()
        if spec == "all":
            return {"ada", "d", "rust"}
        langs = set(x.strip() for x in re.split(r"[,\s]+", spec) if x.strip())
        return langs & {"ada", "d", "rust"}

    def _can_download(self):
        return (self.args.bootstrap_tools in ("auto", "download")
                and not self.args.offline)

    def _find_tool(self, name):
        p = shutil.which(name)
        if p:
            path = Path(p)
            try:
                if path.resolve().parent != self.bin.resolve():
                    return path
            except OSError:
                return path
        return None

    def _provider_tool(self, provider, rel):
        base = self.root / provider
        if not base.is_dir():
            return None
        for p in base.rglob(rel):
            if p.is_file():
                return p
        return None

    def _symlink(self, name, target):
        link = self.bin / name
        try:
            if link.exists() or link.is_symlink():
                link.unlink()
            link.symlink_to(str(target))
        except OSError as exc:
            self.log.warn("could not create stage0 link %s: %s" % (link, exc))

    def _wrapper(self, name, body):
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(0o755)

    def _sdk_min_version(self):
        if platform.system() != "Darwin":
            return None
        ver = _run_capture(["xcrun", "--show-sdk-version"]) if shutil.which("xcrun") else ""
        m = re.match(r"(\d+)", ver or "")
        if m:
            return "%s.0" % m.group(1)
        return None

    # -- providers -------------------------------------------------------

    def ensure(self):
        wanted = self._wanted()
        if "ada" in wanted:
            self._ensure_gnat()
        if "d" in wanted:
            self._ensure_gdc()
        if "rust" in wanted:
            self._ensure_rust()
        return self

    def _specs_for(self, table, fallback=None):
        specs = []
        for src in (table, fallback or {}):
            raw = src.get(self.host)
            if isinstance(raw, list):
                specs.extend(raw)
            elif raw:
                specs.append(raw)
        return specs

    def _install_gnat(self, spec):
        self.log.step("fetching GNAT stage0 (open-source Ada bootstrap)")
        archive = self.downloader.obtain(
            spec["archive"], [spec["url"]], sha256=spec["sha256"])
        dest = self.root / "gnat"
        if dest.exists():
            shutil.rmtree(str(dest))
        dest.mkdir(parents=True, exist_ok=True)
        safe_extract(archive, dest)
        candidates = list(dest.rglob("bin/gnatbind"))
        if not candidates:
            raise BuildError("GNAT archive did not contain bin/gnatbind")
        return candidates[0].parent.parent

    def _ensure_gnat(self):
        provider = (self._provider_tool("gnat", "bin/gnatbind")
                    or (self._provider_tool("gnat", "bin/gnatmake")))
        provider_make = self._provider_tool("gnat", "bin/gnatmake")
        gnat_root = None
        if provider and provider_make:
            gnat_root = provider.parent.parent
        if not gnat_root and self._can_download():
            for spec in self._specs_for(STAGE0_GNAT, STAGE0_GNAT_FALLBACK):
                try:
                    gnat_root = self._install_gnat(spec)
                    break
                except Exception as exc:
                    self.log.warn("GNAT stage0 candidate %s failed: %s" %
                                  (spec.get("archive", "?"), exc))
        if not gnat_root:
            existing = self._find_tool("gnatbind")
            make = self._find_tool("gnatmake")
            if existing and make:
                gnat_root = existing.parent.parent
        if not gnat_root:
            self.log.warn("GNAT not found; Ada will be skipped.  Install GNAT or "
                          "use --stage0-dir/--bootstrap-tools=auto")
            return
        gnat_bin = gnat_root / "bin"
        gnatbind = gnat_bin / "gnatbind"
        gnatmake = gnat_bin / "gnatmake"
        if not gnatbind.is_file() or not gnatmake.is_file():
            self.log.warn("GNAT stage0 is incomplete (%s)" % gnat_root)
            return
        self._symlink("gnatbind", gnatbind)
        self._symlink("gnatls", gnat_bin / "gnatls")
        self._wrapper("gnatmake",
                      'PATH=%s:"$PATH" exec %s "$@"' %
                      (shlex.quote(str(gnat_bin)), shlex.quote(str(gnatmake))))
        self.env_extra["GNATBIND"] = str(gnatbind)
        self.env_extra["GNATMAKE"] = str(self.bin / "gnatmake")
        cc = gnat_bin / "gcc"
        cxx = gnat_bin / "g++"
        if cc.is_file() and not self.args.cc:
            self.cc = str(cc)
            self.cxx = str(cxx) if cxx.is_file() else None
        self.found["gnat"] = str(gnatbind)
        self.log.ok("GNAT stage0 ready: %s" % gnat_root)

    def _gdc_wrapper(self, gdc_bin, b_dirs=None, includes=None):
        wrapper = self.bin / "gdc-stage0"
        sdk_min = self._sdk_min_version()
        extra = ""
        for d in (b_dirs or []):
            extra += " -B%s" % shlex.quote(str(d))
        for d in (includes or []):
            extra += " -I%s" % shlex.quote(str(d))
        # The extracted Debian gdc package does not ship liblto_plugin.so
        # (it lives in the gcc-14 package).  D frontend builds only need
        # object compilation, so do not try to load the LTO plugin.
        extra += " -fno-use-linker-plugin"
        if not self._sdk_min_version():
            # The seed package also omits libgcc_eh/libstdc++ static archives;
            # use the build machine's GCC support directory for those
            # build-time links.
            support_dirs = []
            gcc = shutil.which("gcc")
            if gcc:
                libgcc = _run_capture([gcc, "-print-libgcc-file-name"])
                if libgcc:
                    support_dirs.append(Path(libgcc).parent)
            for base in Path("/usr/lib/gcc").glob("*/*"):
                if (base / "libgcc_eh.a").is_file() or (base / "libgcc.a").is_file():
                    support_dirs.append(base)
            for d in support_dirs:
                extra += " -B%s" % shlex.quote(str(d))
        if self.sdk:
            extra += " -isysroot %s" % shlex.quote(str(self.sdk))
        if sdk_min:
            extra += " -mmacosx-version-min=%s" % sdk_min
        self._wrapper("gdc-stage0", "exec %s%s \"$@\"" %
                      (shlex.quote(str(gdc_bin)), extra))
        return wrapper

    def _gdc_b_dirs(self, gdc_bin, root):
        dirs = []
        try:
            for d21 in Path(root).rglob("d21"):
                if d21.is_file():
                    dirs.append(d21.parent)
            for spec in Path(root).rglob("libgphobos.spec"):
                if spec.is_file():
                    dirs.append(spec.parent)
        except OSError:
            pass
        # Also let the binary find its own libexec if it was relocated.
        dirs.append(Path(gdc_bin).resolve().parent.parent / "libexec" / "gcc")
        out = []
        seen = set()
        for d in dirs:
            key = str(d)
            if key not in seen and Path(d).exists():
                seen.add(key)
                out.append(Path(d))
        return out

    def _gdc_include_dirs(self, root):
        out = []
        seen = set()
        for d in Path(root).rglob("include/d"):
            if d.is_dir():
                key = str(d)
                if key not in seen:
                    seen.add(key)
                    out.append(d)
        return out

    def _install_gdc(self, spec):
        self.log.step("fetching legacy GDC stage0 (open-source D bootstrap)")
        archive = self.downloader.obtain(
            spec["archive"], [spec["url"]], sha256=spec["sha256"])
        dest = self.root / "gdc-legacy"
        if dest.exists():
            shutil.rmtree(str(dest))
        dest.mkdir(parents=True, exist_ok=True)
        safe_extract(archive, dest)
        candidates = list(dest.rglob("bin/gdc"))
        if not candidates:
            raise BuildError("GDC archive did not contain bin/gdc")
        return candidates[0], dest

    def _install_gdc_modern(self, spec):
        self.log.step("fetching modern GDC stage0 (Debian gdc-14 package)")
        archive = self.downloader.obtain(
            spec["archive"], [spec["url"]], sha256=spec["sha256"])
        dest = self.root / "gdc-modern"
        if dest.exists():
            shutil.rmtree(str(dest))
        dest.mkdir(parents=True, exist_ok=True)
        extract_deb(archive, dest)
        if spec.get("runtime_url"):
            rt_archive = self.downloader.obtain(
                spec["runtime_archive"], [spec["runtime_url"]],
                sha256=spec["runtime_sha256"])
            extract_deb(rt_archive, dest)
        bins = []
        for p in dest.rglob("*gdc*"):
            if p.is_file() and os.access(str(p), os.X_OK) and not p.name.endswith((".so", ".spec")):
                bins.append(p)
        if not bins:
            raise BuildError("Debian GDC package did not contain a gdc executable")
        gdc = sorted(bins, key=lambda p: (("gdc-14" not in p.name), len(str(p))))[0]
        return gdc, dest

    def _ensure_gdc(self):
        mode = getattr(self.args, "gdc_seed", "auto")
        provider = (self._provider_tool("gdc", "bin/gdc")
                    or (self.root / "bin" / "gdc"))
        gdc_bin = provider if provider and provider.is_file() else None
        if gdc_bin:
            wrapper = self._gdc_wrapper(gdc_bin, self._gdc_b_dirs(gdc_bin, self.root),
                                        self._gdc_include_dirs(self.root))
            self.found["gdc"] = str(gdc_bin)
            self.env_extra["GDC"] = str(wrapper)
            self.log.ok("GDC stage0 ready (explicit): %s" % gdc_bin)
            return

        if mode != "system" and self._can_download():
            attempts = []
            if mode in ("auto", "modern"):
                attempts.append(("modern", STAGE0_GDC_MODERN, self._install_gdc_modern))
                if self.host.endswith("darwin"):
                    attempts.append(("macos", STAGE0_GDC, self._install_gdc))
            if mode in ("auto", "legacy"):
                attempts.append(("legacy", STAGE0_GDC_LEGACY, self._install_gdc))
            for label, table, installer in attempts:
                for spec in self._specs_for(table):
                    try:
                        gdc_bin, dr = installer(spec)
                        wrapper = self._gdc_wrapper(gdc_bin, self._gdc_b_dirs(gdc_bin, dr),
                                                    self._gdc_include_dirs(dr))
                        self.found["gdc"] = str(gdc_bin)
                        self.env_extra["GDC"] = str(wrapper)
                        self.log.ok("GDC stage0 ready (%s): %s" % (label, gdc_bin))
                        return
                    except Exception as exc:
                        self.log.warn("GDC %s candidate %s failed: %s" %
                                      (label, spec.get("archive", "?"), exc))

        gdc_bin = self._find_tool("gdc")
        if gdc_bin:
            wrapper = self._gdc_wrapper(gdc_bin, self._gdc_b_dirs(gdc_bin, self.root),
                                        self._gdc_include_dirs(self.root))
            self.found["gdc"] = str(gdc_bin)
            self.env_extra["GDC"] = str(wrapper)
            self.log.ok("GDC stage0 ready (system): %s" % gdc_bin)
            return
        self.log.warn("GDC not found; D will be skipped.  Install gdc, use "
                      "--stage0-dir, or --gdc-seed=modern/legacy")

    def _install_rust(self, target, rust_root, channel="stable"):
        self.log.step("fetching Rust stage0 %s (open-source Rust bootstrap)" % channel)
        manifest = self.downloader.fetch_text(
            "https://static.rust-lang.org/dist/channel-rust-%s.toml" % channel)
        rust_root.mkdir(parents=True, exist_ok=True)
        for component in ("rustc", "cargo", "rust-std"):
            header = "pkg.%s.target.%s" % (component, target)
            section = _toml_section(manifest, header)
            if not section:
                raise BuildError("Rust manifest has no %s" % header)
            url = _toml_value(section, "xz_url") or _toml_value(section, "url")
            digest = _toml_value(section, "xz_hash") or _toml_value(section, "hash")
            if not url or not digest:
                raise BuildError("Rust manifest entry %s incomplete" % header)
            archive = self.downloader.obtain(
                os.path.basename(url), [url], sha256=digest)
            work = self.root / "rust-src" / component
            if work.exists():
                shutil.rmtree(str(work))
            work.mkdir(parents=True, exist_ok=True)
            root = safe_extract(archive, work)
            installer = root / "install.sh"
            if not installer.is_file():
                raise BuildError("Rust %s archive has no install.sh" % component)
            env = os.environ.copy()
            env["PATH"] = str(rust_root / "bin") + os.pathsep + env.get("PATH", "")
            self.log.debug("installing Rust component %s" % component)
            proc = subprocess.run(
                ["/bin/sh", str(installer), "--prefix=%s" % rust_root,
                 "--disable-ldconfig"], cwd=str(root), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            if proc.returncode != 0:
                raise BuildError("Rust %s installer failed: %s" %
                                 (component, (proc.stdout or "")[-500:]))
        return rust_root / "bin" / "cargo"

    def _use_rust(self, cargo, rustc, label):
        self.found["rust"] = str(cargo)
        self.env_extra["CARGO"] = str(cargo)
        self.env_extra["RUSTC"] = str(rustc)
        self.log.ok("Rust stage0 ready: %s" % label)

    def _ensure_rust(self):
        provider = (self._provider_tool("rust", "bin/cargo")
                    or (self.root / "bin" / "cargo"))
        provider_rustc = (self._provider_tool("rust", "bin/rustc")
                          or (self.root / "bin" / "rustc"))
        if provider and provider.is_file() and provider_rustc.is_file():
            self._use_rust(provider, provider_rustc, provider.parent)
            return
        rust_root = self.root / "rust"
        target = RUST_TARGETS.get(self.host)
        if self._can_download() and target:
            for channel in RUST_CHANNELS:
                try:
                    cargo = self._install_rust(target, rust_root, channel)
                    self._symlink("cargo", cargo)
                    self._symlink("rustc", rust_root / "bin" / "rustc")
                    self._use_rust(self.bin / "cargo", self.bin / "rustc", rust_root)
                    return
                except Exception as exc:
                    self.log.warn("Rust stage0 %s failed: %s" % (channel, exc))
        elif not target:
            self.log.warn("no automatic Rust stage0 mapping for %s" % self.host)
        cargo = self._find_tool("cargo")
        rustc = self._find_tool("rustc")
        if cargo and rustc:
            self._use_rust(cargo, rustc, cargo.parent)
            return
        self.log.warn("cargo/rustc not found; Rust will be skipped.  Install "
                      "Rust or use --stage0-dir/--bootstrap-tools=auto")

    def apply_env(self, env):
        for key, value in self.env_extra.items():
            env[key] = value
        if self.cc and not self.args.cc:
            env["CC"] = self.cc
        if self.cxx and not self.args.cxx:
            env["CXX"] = self.cxx
        env["PATH"] = os.pathsep.join(
            [str(self.bin)] + [p for p in env.get("PATH", "").split(os.pathsep) if p])


def _toml_section(text, header):
    m = re.search(r"^\[%s\]\s*$(.*?)(?=^\[|\Z)" % re.escape(header),
                  text, re.M | re.S)
    return m.group(1) if m else ""


def _toml_value(section, key):
    m = re.search(r'^%s\s*=\s*"([^"]*)"' % re.escape(key), section, re.M)
    return m.group(1) if m else ""


# ---------------------------------------------------------------------------
# GCC configuration / build.
# ---------------------------------------------------------------------------

class GccBuilder(object):
    def __init__(self, args, paths, log, runner, deps_prefix, available_deps,
                 stage0=None):
        self.args = args
        self.paths = paths
        self.log = log
        self.runner = runner
        self.deps_prefix = deps_prefix
        self.available_deps = available_deps
        self.stage0 = stage0
        self.source = Path(paths["source"])
        self.build_dir = Path(paths["build"])
        self.prefix = Path(paths["prefix"])
        self.make_prog = paths["make"]
        self.jobs = int(args.jobs) if args.jobs else (os.cpu_count() or 4)
        self.cc = paths["cc"]
        self.cxx = paths["cxx"]
        if stage0 and stage0.cc and not args.cc:
            self.cc = stage0.cc
        if stage0 and stage0.cxx and not args.cxx:
            self.cxx = stage0.cxx
        self.triple = paths["triple"]
        self.sdk = paths["sdk"]
        self.native = paths["native"]
        sr = paths.get("static_runtime")
        self.static_runtime = Path(sr) if sr else None

    # ---- configuration -------------------------------------------------

    def gcc_version(self):
        try:
            return (self.source / "gcc" / "BASE-VER").read_text().strip()
        except OSError:
            return "unknown"

    def languages(self):
        spec = (self.args.languages or "all").strip()
        # clang's libc++ headers cannot be combined with GCC's safe-ctype
        # poisoning in the non-bootstrapped JIT build.  A bootstrapped build
        # uses the newly built GCC (and its own libstdc++) instead.
        clang_host = "clang" in compiler_banner(self.cxx).lower()
        if (not self.bootstrap_enabled()) and self.args.jit and clang_host:
            if not getattr(self, "_jit_skip_warned", False):
                self.log.warn("libgccjit needs a bootstrap build (or a GCC host "
                              "compiler) on this platform; excluding JIT from "
                              "this non-bootstrapped build")
                self._jit_skip_warned = True
            if spec == "all":
                return "all,^jit"
            langs = [x.strip() for x in re.split(r"[,\s]+", spec) if x.strip()]
            return ",".join([x for x in langs if x != "jit"])
        if spec == "all":
            if not self.args.jit:
                return "all,^jit"
            return "all"
        if spec in ("default", "none"):
            if spec == "none":
                raise BuildError("--languages=none is not useful; at least C is always built")
            return spec
        langs = [x.strip() for x in re.split(r"[,\s]+", spec) if x.strip()]
        if not self.args.jit:
            langs = [x for x in langs if x not in ("jit",)]
        if "c" not in langs:
            langs.insert(0, "c")
        return ",".join(langs)

    def host_shared(self):
        if self.args.host_shared == "yes":
            return True
        if self.args.host_shared == "no":
            return False
        # Darwin aarch64/x86_64 requires PIC; JIT and libgdiagnostics need it.
        if self.paths["darwin"]:
            return True
        return bool(self.args.libgdiagnostics or (self.args.jit and self.args.languages == "all"))

    def bootstrap_enabled(self):
        if self.args.bootstrap == "yes":
            return True
        if self.args.bootstrap == "no":
            return False
        return self.native

    def checking(self):
        value = (self.args.checking or "release").strip()
        if value == "no":
            return "--enable-checking=no"
        return "--enable-checking=%s" % value

    def werror_enabled(self):
        if self.args.werror is not None:
            return bool(self.args.werror)
        try:
            phase = (self.source / "gcc" / "DEV-PHASE").read_text().strip()
        except OSError:
            phase = ""
        # Experimental development branches enable -Werror automatically;
        # on a new host that regularly turns harmless warnings into failures.
        return phase != "experimental"

    def no_werror_make_override(self):
        if self.werror_enabled():
            return []
        return ["STRICT_WARN=-Wmissing-format-attribute -Wconditionally-supported "
                "-Woverloaded-virtual -pedantic -Wno-long-long -Wno-variadic-macros "
                "-Wno-overlength-strings"]

    def previous_libstdcxx_dirs(self):
        target = self.paths["target_triple"]
        dirs = []
        for base in (self.build_dir / ("prev-" + target),
                     self.build_dir / target):
            for sub in ("libstdc++-v3/src/.libs",
                        "libstdc++-v3/libsupc++/.libs"):
                dirs.append(base / sub)
        return dirs

    def configure_args(self):
        a = self.args
        args = [
            str(self.source / "configure"),
            "--prefix=%s" % self.prefix,
            "--build=%s" % self.paths["build_triple"],
            "--host=%s" % self.paths["host_triple"],
            "--target=%s" % self.paths["target_triple"],
            "--enable-languages=%s" % self.languages(),
            self.checking(),
        ]
        if self.bootstrap_enabled():
            args.append("--enable-bootstrap")
        else:
            args.append("--disable-bootstrap")
        args.append("--enable-werror" if self.werror_enabled() else "--disable-werror")

        if a.shared:
            args.append("--enable-shared")
        else:
            args.append("--disable-shared")
        if a.static:
            args.append("--enable-static")
        else:
            args.append("--disable-static")

        if a.multilib:
            args.append("--enable-multilib")
        else:
            args.append("--disable-multilib")

        if a.lto:
            args.append("--enable-lto")
        else:
            args.append("--disable-lto")

        threads = "win32" if IS_WINDOWS else "posix"
        args.append("--enable-threads=%s" % threads)

        if not IS_WINDOWS:
            args.append("--enable-libstdcxx-threads")
            args.append("--enable-libstdcxx-time=yes")
            if a.libstdcxx_backtrace:
                args.append("--enable-libstdcxx-backtrace=yes")
            args.append("--enable-libgomp")
            args.append("--enable-libatomic")
            args.append("--enable-libssp")
            if "fortran" in self.languages() or self.languages() == "all":
                args.append("--enable-libquadmath")

        if self.host_shared():
            args.append("--enable-host-shared")
        if self.paths["darwin"]:
            args.append("--enable-host-pie")
        if a.libgdiagnostics:
            if self.host_shared():
                args.append("--enable-libgdiagnostics")
            else:
                self.log.warn("libgdiagnostics needs host-shared support; skipping")
        if a.plugins == "yes":
            args.append("--enable-plugin")
        elif a.plugins == "no":
            args.append("--disable-plugin")
        if a.sanitizers == "yes":
            args.append("--enable-libsanitizer")
        elif a.sanitizers == "no":
            args.append("--disable-libsanitizer")
        if a.pgo and a.pgo != "no":
            args.append("--enable-pgo-build=%s" % a.pgo)

        # Dependency locations (static builds).
        dep = self.deps_prefix
        if dep:
            args += [
                "--with-gmp=%s" % dep,
                "--with-mpfr=%s" % dep,
                "--with-mpc=%s" % dep,
            ]
            if a.graphite:
                args.append("--with-isl=%s" % dep)
            else:
                args.append("--without-isl")
            if a.zstd:
                args.append("--with-zstd=%s" % dep)
            else:
                args.append("--without-zstd")
            lang_spec = (a.languages or "all").strip()
            want_cobol = (lang_spec == "all" or
                          "cobol" in [x.strip() for x in re.split(r"[,\s]+", lang_spec)])
            if want_cobol and (Path(dep) / "lib" / "libxml2.a").is_file():
                args.append("--with-target-libxml2-include=%s/include/libxml2" % dep)
                args.append("--with-target-libxml2-lib=%s/lib" % dep)
        for opt, key in (("--with-gmp", "gmp"), ("--with-mpfr", "mpfr"),
                         ("--with-mpc", "mpc"), ("--with-isl", "isl"),
                         ("--with-zstd", "zstd")):
            value = getattr(a, "%s_prefix" % key, None)
            if value:
                args.append("%s=%s" % (opt, value))
            elif key == "isl" and not a.graphite:
                args.append("--without-isl")
            elif key == "zstd" and not a.zstd:
                args.append("--without-zstd")

        if a.explicit_sysroot:
            args.append("--with-sysroot=%s" % a.explicit_sysroot)
        elif self.sdk:
            args.append("--with-sysroot=%s" % self.sdk)
        if self.sdk:
            args.append("--with-build-sysroot=%s" % self.sdk)
        elif a.explicit_sysroot:
            args.append("--with-build-sysroot=%s" % a.explicit_sysroot)

        if a.system_zlib:
            args.append("--with-system-zlib")
        if a.cpu:
            args.append("--with-cpu=%s" % a.cpu)
        if a.stage1_languages:
            args.append("--enable-stage1-languages=%s" % a.stage1_languages)
        if a.static_runtime == "source" and not self.paths["darwin"]:
            args.append("--disable-fixincludes")
        if a.static_host and self.deps_prefix and (
                Path(self.deps_prefix) / "lib" / "libiconv.a").is_file():
            # stage2 link commands use POSTSTAGE1_LIBS, not the stage1 LIBS
            # that already contains -liconv.  Supply it explicitly.
            stage_libs = ("-L%s/lib -liconv -lstdc++ -lsupc++ -lm" %
                          self.deps_prefix)
            boot_libs = stage_libs
            for d in self.previous_libstdcxx_dirs():
                boot_libs += " -L%s" % d
            args.append("--with-stage1-libs=%s" % stage_libs)
            args.append("--with-boot-libs=%s" % boot_libs)
        if a.static_host:
            flags = "-static"
            if not self.paths["darwin"] and not IS_WINDOWS:
                flags += " -static-libstdc++ -static-libgcc"
            if ((a.static_runtime == "source" or self.bootstrap_enabled())
                    and not self.paths["darwin"]):
                # stage1 is a build-time tool.  Keep it dynamic so shared
                # host libraries and the unbuilt private runtime do not get
                # -static; only the post-stage1 (final) links are static.
                boot = flags
                if a.static_runtime == "source" and self.static_runtime:
                    boot += " -B%s/lib -L%s/lib" % (self.static_runtime,
                                                    self.static_runtime)
                boot += " -B%s" % (self.build_dir / "prev-gcc")
                for d in self.previous_libstdcxx_dirs():
                    boot += " -L%s" % d
                args.append("--with-stage1-ldflags=")
                args.append("--with-boot-ldflags=%s" % boot)
                if a.static_runtime == "source" and self.static_runtime:
                    bl = ["-L%s/lib" % self.static_runtime,
                          "-lc", "-lm", "-lpthread", "-ldl", "-lrt",
                          "-lstdc++", "-lsupc++", "-lm"]
                    for d in self.previous_libstdcxx_dirs():
                        bl.append("-L%s" % d)
                    args.append("--with-boot-libs=%s" % " ".join(bl))
            else:
                args.append("--with-stage1-ldflags=%s" % flags)
                args.append("--with-boot-ldflags=%s" % flags)
        if a.pkgversion:
            args.append("--with-pkgversion=%s" % a.pkgversion)
        if a.nls:
            try:
                configure_text = (self.source / "configure").read_text(errors="replace")
            except OSError:
                configure_text = ""
            if "--enable-nls" in configure_text:
                args.append("--enable-nls")
            else:
                self.log.warn("this GCC version has no NLS support; ignoring --nls")
        if a.objc_gc:
            args.append("--enable-objc-gc")
        if a.libstdcxx_debug:
            args.append("--enable-libstdcxx-debug")
        if a.configure_extra:
            args += shlex.split(a.configure_extra)
        return args

    # ---- steps ---------------------------------------------------------

    def build_env(self):
        env = os.environ.copy()
        for key in ("CPATH", "C_INCLUDE_PATH", "CPLUS_INCLUDE_PATH",
                    "OBJC_INCLUDE_PATH", "LIBRARY_PATH", "LD_LIBRARY_PATH",
                    "DYLD_LIBRARY_PATH", "PKG_CONFIG_PATH", "CONDA_PREFIX",
                    "MAKEFLAGS", "MFLAGS", "CFLAGS", "CXXFLAGS", "CPPFLAGS",
                    "LDFLAGS"):
            env.pop(key, None)
        env["LC_ALL"] = "C"
        env["LANG"] = "C"
        env["CC"] = self.cc
        env["CXX"] = self.cxx
        if self.args.cflags:
            env["CFLAGS"] = self.args.cflags
        if self.args.cxxflags:
            env["CXXFLAGS"] = self.args.cxxflags
        if self.args.ldflags:
            env["LDFLAGS"] = self.args.ldflags
        if not self.args.gas_base64:
            # Force the GCC configure probe to fail, so auto-host.h does not
            # define HAVE_GAS_BASE64 and generated assembly stays compatible
            # with older binutils (e.g. Debian 12's as).
            env["gcc_cv_as_base64"] = "no"
        if self.sdk:
            env["SDKROOT"] = self.sdk
            dev = _run_capture(["xcode-select", "-p"])
            if dev:
                env["DEVELOPER_DIR"] = dev
        if self.args.deployment_target:
            env["MACOSX_DEPLOYMENT_TARGET"] = self.args.deployment_target
        # Keep the system tools first; tool wrappers defeat foreign prefixes.
        wrapper = Path(self.build_dir) / "toolwrap"
        path_items = [str(wrapper)]
        if self.deps_prefix:
            path_items.append(str(Path(self.deps_prefix) / "bin"))
        env["PATH"] = system_first_path(path_items)
        if self.stage0:
            self.stage0.apply_env(env)
        return env

    def private_libc_has_tls_get_addr(self):
        if self.args.static_runtime != "source" or not self.static_runtime:
            return False
        libc = Path(self.static_runtime) / "lib" / "libc.a"
        if not libc.is_file():
            return False
        try:
            proc = subprocess.run(["nm", str(libc)],
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL)
            out = (proc.stdout or b"").decode("utf-8", "replace")
            # Only a definition (T/W) counts; x86_64 static libc.a can
            # contain undefined (U) references to this symbol.
            return bool(re.search(r"\s[TtWw]\s+__tls_get_addr\s*$", out,
                                  re.MULTILINE))
        except OSError:
            return False

    def need_d21_shim(self):
        if not self.args.static_host or self.paths["darwin"]:
            return False
        return not self.private_libc_has_tls_get_addr()

    def apply_gcc_patches(self):
        """Apply the GCC source patches shipped in patches/.

        Patches are applied in filename order and only when the corresponding
        feature/platform is enabled.  Existing user changes are not reverted;
        a patch that is already applied is detected and skipped.
        """
        if self.args.no_source_patch:
            return
        patch_dir = Path(__file__).resolve().parent / "patches"
        if not patch_dir.is_dir():
            self.log.warn("GCC patch directory not found: %s" % patch_dir)
            return
        target = self.paths["target_triple"]
        lang_spec = (self.args.languages or "all").strip()
        def lang(name):
            return lang_spec == "all" or name in [
                x.strip() for x in re.split(r"[,\s]+", lang_spec)]
        linux = not self.paths["darwin"] and not IS_WINDOWS
        x86 = target.startswith(("x86_64", "i386", "i686"))
        aarch64 = target.startswith("aarch64")
        wanted = []
        if self.args.libgdiagnostics:
            wanted.append("0001-gcc-makefile-libgdiagnostics-static.patch")
        if self.args.jit:
            wanted.append("0002-gcc-jit-shared-link-flags.patch")
        if self.args.static_host and lang("m2") and linux:
            wanted.append("0003-gcc-m2-host-link-libs.patch")
        if linux and x86:
            wanted.append("0004-gcc-go-split-stack.patch")
        if linux and aarch64:
            wanted.append("0005-gcc-aarch64-linux-asm-hooks.patch")
        if (linux and self.args.static_host and lang("cobol")
                and self.deps_prefix and
                (Path(self.deps_prefix) / "lib" / "libiconv.a").is_file()):
            wanted.append("0006-libgcobol-static-iconv.patch")
        if (linux and self.args.static_host and lang("ada")
                and self.deps_prefix and
                (Path(self.deps_prefix) / "lib" / "libiconv.a").is_file()):
            wanted.append("0007-gnattools-libiconv.patch")
        if (self.paths["darwin"] and self.args.library_linkage == "static"
                and aarch64):
            wanted.append("0008-darwin-static-libgcc-rules.patch")
        git = shutil.which("git")
        for name in wanted:
            patch = patch_dir / name
            if not patch.is_file():
                self.log.warn("missing GCC patch: %s" % patch)
                continue
            if git:
                check = subprocess.run(
                    [git, "-C", str(self.source), "apply", "--check", str(patch)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                already = subprocess.run(
                    [git, "-C", str(self.source), "apply", "--reverse", "--check",
                     str(patch)], stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL)
                if check.returncode == 0:
                    proc = subprocess.run(
                        [git, "-C", str(self.source), "apply", str(patch)],
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                    if proc.returncode != 0:
                        raise BuildError("failed to apply %s: %s" %
                                         (name, proc.stdout.strip()))
                    self.log.ok("applied GCC patch %s" % name)
                elif already.returncode == 0:
                    self.log.ok("GCC patch already applied: %s" % name)
                else:
                    self.log.warn("GCC patch does not apply cleanly: %s" % name)
            else:
                # POSIX patch fallback for hosts without git.
                proc = subprocess.run(
                    ["patch", "-p1", "-d", str(self.source), "-i", str(patch)],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                if proc.returncode != 0:
                    self.log.warn("could not apply %s: %s" %
                                  (name, proc.stdout.strip()))

    def patch_source_makefile(self):
        """GCC 17's libgdiagnostics rule lists $(LIBS) as a prerequisite.
        On Darwin $(LIBS) contains '-liconv', which make tries to build as a
        file.  Apply the one-line upstream fix before configure generates the
        stage Makefiles.  A backup is kept next to the source file."""
        if self.args.no_source_patch:
            self.log.warn("--no-source-patch: libgdiagnostics Makefile may fail")
            return
        path = self.source / "gcc" / "Makefile.in"
        if not path.is_file():
            return
        try:
            text = path.read_text()
        except OSError:
            return
        old = ("$(LIBGDIAGNOSTICS_FILENAME): $(libgdiagnostics_OBJS) $(CPPLIB) "
               "$(EXTRA_GCC_LIBS) $(LIBS) \\\n")
        new = ("$(LIBGDIAGNOSTICS_FILENAME): $(libgdiagnostics_OBJS) $(CPPLIB) "
               "$(EXTRA_GCC_LIBS) \\\n")
        changed = bool(self.args.libgdiagnostics) and old in text
        if changed:
            backup = path.with_name(path.name + ".buildpy-orig")
            if not backup.exists():
                backup.write_text(text)
            path.write_text(text.replace(old, new, 1))
            self.log.ok("applied libgdiagnostics source fix (backup: %s)" % backup.name)
        jit = self.source / "gcc" / "jit" / "Make-lang.in"
        if jit.is_file():
            jt = jit.read_text()
            jold = "JIT_LDFLAGS = $(LDFLAGS)"
            jnew = ("JIT_LDFLAGS = $(filter-out -static -static-libstdc++ "
                    "-static-libgcc,$(LDFLAGS))")
            if jold in jt:
                jbackup = jit.with_name(jit.name + ".buildpy-orig")
                if not jbackup.exists():
                    jbackup.write_text(jt)
                jit.write_text(jt.replace(jold, jnew, 1))
                self.log.ok("applied libgccjit shared-link source fix")
        d = self.source / "gcc" / "d" / "Make-lang.in"
        if d.is_file():
            dt = d.read_text()
            dstatic = ("+$(DLLINKER) $(ALL_LINKERFLAGS) $(LDFLAGS) "
                       "-static-libphobos -o $@ \\\n")
            ddyn = ("+$(DLLINKER) $(ALL_LINKERFLAGS) "
                    "$(filter-out -static -static-libstdc++ -static-libgcc,"
                    "$(LDFLAGS)) -o $@ \\\n")
            changed = False
            if ddyn in dt:
                dt = dt.replace(ddyn, dstatic, 1)
                changed = True
            # The shim is added through LIBS in gcc/Makefile, not the d21
            # recipe; keep the source recipe clean.
            dt = dt.replace(
                "D_ALL_OBJS = $(D21_TLS_SHIM) $(D_FRONTEND_OBJS)",
                "D_ALL_OBJS = $(D_FRONTEND_OBJS)")
            dt = dt.replace(
                "$(D_ALL_OBJS) $(D21_TLS_SHIM) attribs.o",
                "$(D_ALL_OBJS) attribs.o")
            shim_path = str(self.build_dir / "d21-tls-shim.o")
            dt = dt.replace("$(D_ALL_OBJS) %s attribs.o" % shim_path,
                            "$(D_ALL_OBJS) attribs.o")
            if dt != dt:
                changed = True
            if changed:
                db = d.with_name(d.name + ".buildpy-orig")
                if not db.exists():
                    db.write_text(d.read_text())
                d.write_text(dt)
                self.log.ok("d21: restored static link rule")
        m2 = self.source / "gcc" / "m2" / "Make-lang.in"
        if m2.is_file():
            mt = m2.read_text()
            mold = ("+$(LINKER) $(ALL_LINKERFLAGS) $(LDFLAGS) -o $@ "
                    "$(BUILD-PGE-O) -lm")
            mnew = ("+$(LINKER) $(ALL_LINKERFLAGS) $(LDFLAGS) -o $@ "
                    "$(BUILD-PGE-O) -lm -lstdc++ -lsupc++ -lm")
            mold2 = ("+$(LINKER) $(ALL_LINKERFLAGS) $(LDFLAGS) -o $@ "
                     "$(BUILD-PGE-O) -lm -lstdc++ -lm")
            if mold in mt:
                mbackup = m2.with_name(m2.name + ".buildpy-orig")
                if not mbackup.exists():
                    mbackup.write_text(mt)
                m2.write_text(mt.replace(mold, mnew, 1))
                self.log.ok("added libstdc++ to m2/pge link")
            elif mold2 in mt:
                mbackup = m2.with_name(m2.name + ".buildpy-orig")
                if not mbackup.exists():
                    mbackup.write_text(mt)
                m2.write_text(mt.replace(mold2, mnew, 1))
                self.log.ok("added libsupc++ to m2/pge link")
            mt = m2.read_text()
            mc_old = "mcflex.o m2/gm2-libs-boot/RTcodummy.o -lm"
            mc_new = "mcflex.o m2/gm2-libs-boot/RTcodummy.o -lm -lstdc++ -lsupc++ -lm"
            if mc_old in mt:
                m2.write_text(mt.replace(mc_old, mc_new, 1))
                self.log.ok("added libstdc++ to m2/mc link")
        shim_line = "LIBS += %s" % (self.build_dir / "d21-tls-shim.o")
        use_shim = self.need_d21_shim()
        if use_shim and shim_line not in text:
            path.write_text(text + "\n" + shim_line + "\n")
            self.log.ok("added d21 TLS shim to source LIBS")
        elif not use_shim and shim_line in text:
            path.write_text(text.replace(shim_line + "\n", ""))
            self.log.ok("private glibc provides __tls_get_addr; removed shim")

    def patch_source_prereq(self):
        """Remove $(LIBS) from the libgdiagnostics prerequisite list.

        Some GCC Makefile generation paths preserve the original line even
        after patch_source_makefile; apply the edit again as a separate,
        unconditional pass.
        """
        if (not self.args.static_host or self.paths["darwin"]
                or not self.args.libgdiagnostics):
            return
        path = self.source / "gcc" / "Makefile.in"
        if not path.is_file():
            return
        text = path.read_text()
        old = ("$(LIBGDIAGNOSTICS_FILENAME): $(libgdiagnostics_OBJS) $(CPPLIB) "
               "$(EXTRA_GCC_LIBS) $(LIBS) \
")
        new = ("$(LIBGDIAGNOSTICS_FILENAME): $(libgdiagnostics_OBJS) $(CPPLIB) "
               "$(EXTRA_GCC_LIBS) \
")
        if old in text:
            path.write_text(text.replace(old, new, 1))
            self.log.ok("removed $(LIBS) from libgdiagnostics prerequisite")

    def patch_darwin_static_libgcc(self):
        """Darwin's libgcc config keeps libemutls_w.a in EXTRA_PARTS even
        when shared libgcc is disabled, but the shared emutls object rule is
        only emitted for shared builds.  Add the missing rule explicitly."""
        if (not self.paths["darwin"]
                or self.args.library_linkage != "static"):
            return
        path = self.source / "libgcc" / "config" / "t-darwin"
        if not path.is_file():
            return
        text = path.read_text()
        bad = ("\nemutls_s.o: $(srcdir)/emutls.c\n"
               "\t$(gcc_compile) -c -DSHARED $< -o $@\n")
        good = ("\nemutls_s.o: $(srcdir)/emutls.c\n"
                "\t$(gcc_s_compile) -c $< $(vis_hide)\n")
        if bad in text:
            text = text.replace(bad, good)
        elif "emutls_s.o: $(srcdir)/emutls.c" not in text:
            text += good
        else:
            return
        path.write_text(text)
        self.log.ok("added static-build rule for Darwin libgcc emutls_s.o")

    def patch_source_shared_links(self):
        if (not self.args.static_host or self.paths["darwin"]
                or not self.args.libgdiagnostics):
            return
        path = self.source / "gcc" / "Makefile.in"
        if not path.is_file():
            return
        text = path.read_text()
        shared_old = ("+$(LLINKER) $(ALL_LINKERFLAGS) $(LDFLAGS) "
                      "-o $@ -shared \\\n")
        shared_new = ("+$(LLINKER) $(ALL_LINKERFLAGS) "
                      "$(filter-out -static -static-libstdc++ -static-libgcc,"
                      "$(LDFLAGS)) -o $@ -shared \\\n")
        changed = False
        if shared_old in text:
            text = text.replace(shared_old, shared_new, 1)
            changed = True
        target_old = ("sarif-replay: $(SARIF_REPLAY_OBJS) "
                      "$(LIBGDIAGNOSTICS_FILENAME)")
        target_new = "sarif-replay: $(SARIF_REPLAY_OBJS) libgdiagnostics.a"
        link_old = "$(SARIF_REPLAY_OBJS) $(LIBGDIAGNOSTICS_FILENAME) $(LIBS)"
        link_new = "$(SARIF_REPLAY_OBJS) libgdiagnostics.a $(LIBS)"
        if target_old in text and link_old in text:
            text = text.replace(target_old, target_new, 1)
            text = text.replace(link_old, link_new, 1)
            rule = ("libgdiagnostics.a: libgdiagnostics.o\n"
                    "\t$(AR) rc $@ $<\n"
                    "\t$(RANLIB) $@\n\n")
            text = text.replace("sarif-replay: $(SARIF_REPLAY_OBJS) libgdiagnostics.a",
                                rule + "sarif-replay: $(SARIF_REPLAY_OBJS) libgdiagnostics.a", 1)
            changed = True
        if changed:
            backup = path.with_name(path.name + ".buildpy-shared-orig")
            if not backup.exists():
                backup.write_text(path.read_text())
            path.write_text(text)
            self.log.ok("patched gcc/Makefile.in shared-link/static-diagnostics rules")

    def make_static_host_wrappers(self):
        """GCC puts stage1/boot LDFLAGS into every host link, including
        shared libraries.  The wrapper removes -static for -shared links and,
        in --static-runtime=source mode, injects the private glibc plus the
        source-built libstdc++/libgcc once those files exist."""
        if not self.args.static_host or self.paths["darwin"] or IS_WINDOWS:
            return
        wrap = self.build_dir / "static-host-wrap"
        wrap.mkdir(parents=True, exist_ok=True)
        private = self.static_runtime
        target = self.paths["target_triple"]
        std_dirs = [self.build_dir / ("prev-" + target) / "libstdc++-v3",
                    self.build_dir / target / "libstdc++-v3"]
        gcc_dirs = [self.build_dir / "prev-gcc", self.build_dir / "gcc"]
        libstdcxx = [str(d / "src" / ".libs" / "libstdc++.a") for d in std_dirs]
        libsupcxx = [str(d / "libsupc++" / ".libs" / "libsupc++.a")
                     for d in std_dirs]
        libgcc = [str(d / "libgcc.a") for d in gcc_dirs]
        libgcc_eh = [str(d / "libgcc_eh.a") for d in gcc_dirs]
        crtbegin = [str(d / "crtbegin.o") for d in gcc_dirs]
        crtend = [str(d / "crtend.o") for d in gcc_dirs]
        template = (
            "#!/usr/bin/env python3\n"
            "import os\n"
            "import sys\n"
            "real = %(real)r\n"
            "private = %(private)r\n"
            "crt1 = %(crt1)r\n"
            "crti = %(crti)r\n"
            "crtn = %(crtn)r\n"
            "libstdcxx_candidates = %(libstdcxx)r\n"
            "libsupcxx_candidates = %(libsupcxx)r\n"
            "libgcc_candidates = %(libgcc)r\n"
            "libgcc_eh_candidates = %(libgcc_eh)r\n"
            "crtbegin_candidates = %(crtbegin)r\n"
            "crtend_candidates = %(crtend)r\n"
            "private_lib = %(private_lib)r\n"
            "strip = %(strip)r\n"
            "def first(paths):\n"
            "    for p in paths:\n"
            "        if p and os.path.isfile(p):\n"
            "            return p\n"
            "    return None\n"
            "args = sys.argv[1:]\n"
            "if any(a in ('-shared', '-dynamiclib', '-r') for a in args):\n"
            "    args = [a for a in args if a not in strip]\n"
            "    os.execv(real, [real] + args)\n"
            "compile_only = any(a in ('-c', '-S', '-E', '-M', '-MM') for a in args)\n"
            "libstdcxx = first(libstdcxx_candidates)\n"
            "libsupcxx = first(libsupcxx_candidates)\n"
            "libgcc = first(libgcc_candidates)\n"
            "libgcc_eh = first(libgcc_eh_candidates)\n"
            "crtbegin = first(crtbegin_candidates)\n"
            "crtend = first(crtend_candidates)\n"
            "required = (crt1, crti, crtn, crtbegin, crtend, libstdcxx,\n"
            "            libsupcxx, libgcc, libgcc_eh, private_lib + '/libc.a')\n"
            "if private and not compile_only and all(required):\n"
            "    args = [a for a in args if a not in ('-static-libstdc++',\n"
            "                                         '-static-libgcc', '-static')]\n"
            "    pre = ['-nostdlib', '-static', crt1, crti, crtbegin,\n"
            "           '-L' + os.path.dirname(libstdcxx),\n"
            "           '-L' + os.path.dirname(libgcc)]\n"
            "    post = (['-Wl,--start-group', libstdcxx, libsupcxx, libgcc,\n"
            "             libgcc_eh, '-L' + private_lib, '-lc', '-lm', '-lpthread',\n"
            "             '-ldl', '-lrt', '-Wl,--end-group', crtend, crtn])\n"
            "    os.execv(real, [real] + pre + args + post)\n"
            "os.execv(real, [real] + args)\n"
        )
        values = {
            "strip": ("-static", "-static-libstdc++", "-static-libgcc"),
            "private": str(private) if private else "",
            "private_lib": str(Path(private) / "lib") if private else "",
            "crt1": str(Path(private) / "lib" / "crt1.o") if private else "",
            "crti": str(Path(private) / "lib" / "crti.o") if private else "",
            "crtn": str(Path(private) / "lib" / "crtn.o") if private else "",
            "libstdcxx": libstdcxx,
            "libsupcxx": libsupcxx,
            "libgcc": libgcc,
            "libgcc_eh": libgcc_eh,
            "crtbegin": crtbegin,
            "crtend": crtend,
        }
        for name, real in (("cc", self.cc), ("c++", self.cxx)):
            path = wrap / name
            path.write_text(template % dict(values, real=real))
            path.chmod(0o755)
        self.cc = str(wrap / "cc")
        self.cxx = str(wrap / "c++")
        self.log.ok("using static-host compiler wrappers in %s" % wrap)

    def check_static_host_possible(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "t.c"
            out = Path(tmp) / "t"
            src.write_text("int main(void){return 0;}\n")
            proc = subprocess.run(
                [self.cc, "-static", str(src), "-o", str(out)],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            if proc.returncode != 0:
                raise BuildError(
                    "static host linking requested but %s cannot link a static "
                    "program (missing libc.a/libm.a or static libstdc++): %s\n"
                    "Install the static libc development package, or pass "
                    "--no-static-host." % (self.cc, (proc.stdout or "")[-800:]))
            csrc = Path(tmp) / "t.cc"
            cout = Path(tmp) / "tcxx"
            csrc.write_text("#include <iostream>\n"
                            "int main(){std::cout << \"ok\"; return 0;}\n")
            proc = subprocess.run(
                [self.cxx, "-static", str(csrc), "-o", str(cout)],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            if proc.returncode != 0:
                raise BuildError(
                    "static host linking requested but %s cannot link a static "
                    "C++ program (missing libstdc++.a): %s\n"
                    "Install the static libstdc++ development package, or pass "
                    "--no-static-host." % (self.cxx, (proc.stdout or "")[-800:]))

    def patch_x86_gotools(self):
        """On some glibc systems GCC's configure does not define
        TARGET_LIBC_PROVIDES_SSP.  The gccgo driver nevertheless adds
        -fsplit-stack on x86-64 Linux, which makes go1 reject every file and
        breaks gotools.  Gate the driver flag on the same configure result;
        systems that really support split-stack keep it."""
        target = self.paths["target_triple"]
        if self.paths["darwin"]:
            return
        if not (target.startswith("x86_64") or target.startswith("i386")
                or target.startswith("i686")):
            return
        if "linux" not in target and "gnu" not in target:
            return
        path = self.source / "gcc" / "go" / "gospec.cc"
        if not path.is_file():
            return
        text = path.read_text()
        old = ("#ifdef TARGET_CAN_SPLIT_STACK\n"
               "  supports_split_stack = 1;\n"
               "#endif")
        new = ("#if defined(TARGET_CAN_SPLIT_STACK) \\\n"
               "    && defined(TARGET_LIBC_PROVIDES_SSP) \\\n"
               "    && defined(TARGET_THREAD_SPLIT_STACK_OFFSET)\n"
               "  supports_split_stack = 1;\n"
               "#endif")
        if old not in text:
            return
        backup = path.with_name(path.name + ".buildpy-orig")
        if not backup.exists():
            backup.write_text(text)
        path.write_text(text.replace(old, new, 1))
        self.log.ok("gated gccgo -fsplit-stack on TARGET_LIBC_PROVIDES_SSP")

    def patch_aarch64_linux(self):
        """GCC 17's Apple-Silicon branch defines the aligned ASM directives
        for non-Mach-O aarch64 but leaves the unaligned hook macros undefined,
        which breaks a native aarch64-linux build in target-def.h.  Supply the
        standard aliases (the same values upstream GCC uses)."""
        target = self.paths["target_triple"]
        if self.paths["darwin"] or not target.startswith("aarch64"):
            return
        if "linux" not in target and "gnu" not in target:
            return
        path = self.source / "gcc" / "config" / "aarch64" / "aarch64.cc"
        if not path.is_file():
            return
        text = path.read_text()
        old = ('#else\n'
               '# define TARGET_ASM_ALIGNED_DI_OP "\\t.xword\\t"\n'
               '# define TARGET_ASM_ALIGNED_HI_OP "\\t.hword\\t"\n'
               '# define TARGET_ASM_ALIGNED_SI_OP "\\t.word\\t"\n'
               '#endif')
        new = ('#else\n'
               '# define TARGET_ASM_ALIGNED_DI_OP "\\t.xword\\t"\n'
               '# define TARGET_ASM_ALIGNED_HI_OP "\\t.hword\\t"\n'
               '# define TARGET_ASM_ALIGNED_SI_OP "\\t.word\\t"\n'
               '# define TARGET_ASM_UNALIGNED_HI_OP TARGET_ASM_ALIGNED_HI_OP\n'
               '# define TARGET_ASM_UNALIGNED_SI_OP TARGET_ASM_ALIGNED_SI_OP\n'
               '# define TARGET_ASM_UNALIGNED_DI_OP TARGET_ASM_ALIGNED_DI_OP\n'
               '#endif')
        if old not in text:
            return
        backup = path.with_name(path.name + ".buildpy-orig")
        if not backup.exists():
            backup.write_text(text)
        path.write_text(text.replace(old, new, 1))
        self.log.ok("applied aarch64-linux ASM hook compatibility fix")

    def configure(self):
        self.build_dir.mkdir(parents=True, exist_ok=True)
        self.apply_gcc_patches()
        self.patch_source_makefile()
        self.patch_darwin_static_libgcc()
        self.patch_source_shared_links()
        self.patch_source_prereq()
        self.patch_x86_gotools()
        self.patch_aarch64_linux()
        self.patch_static_iconv()
        marker = self.build_dir / ".buildpy-owned"
        if not marker.exists():
            marker.write_text("created by build.py %s\n" % SCRIPT_VERSION)
        config_status = self.build_dir / "config.status"
        args_file = self.build_dir / "config.args"
        new_args = self.configure_args()
        if config_status.exists() and not self.args.reconfigure:
            self.log.ok("reusing existing configuration in %s" % self.build_dir)
            if args_file.is_file():
                old = [ln for ln in args_file.read_text().splitlines() if ln]
                if old != new_args:
                    self.log.warn("configure arguments changed; using the existing "
                                  "configuration (pass --reconfigure to redo it)")
                if (self.args.static_host and not self.paths["darwin"]
                        and not any("--with-stage1-ldflags" in x for x in old)):
                    self.log.warn("existing configuration links host binaries "
                                  "dynamically; static-host was requested. Use a "
                                  "new --build-dir or --reconfigure to rebuild "
                                  "statically.")
            else:
                self.log.warn("existing configuration has no saved arguments; "
                              "pass --reconfigure for a clean configuration")
            self.patch_darwin_makefile()
            self.patch_static_diagnostics()
            self.patch_static_iconv_makefile()
            self.make_static_host_wrappers()
            return
        if self.args.dry_run:
            self.log.info("would run: %s" % " ".join(shlex.quote(x) for x in new_args))
            return
        if (self.args.static_host and not self.paths["darwin"]
                and self.args.static_runtime != "source"):
            self.check_static_host_possible()
        if self.args.static_runtime == "source" and self.static_runtime:
            for name in ("libc.a", "crt1.o", "crti.o", "crtn.o"):
                if not (self.static_runtime / "lib" / name).is_file():
                    raise BuildError("private static runtime is missing %s" % name)
        self.make_static_host_wrappers()
        self.log.step("configuring GCC %s (%s)" % (self.gcc_version(), self.triple))
        self.log.info("  build system : %s" % self.paths["build_triple"])
        self.log.info("  host system  : %s" % self.paths["host_triple"])
        self.log.info("  target       : %s" % self.paths["target_triple"])
        self.log.info("  languages    : %s" % self.languages())
        self.log.info("  prefix       : %s" % self.prefix)
        self.log.info("  bootstrap    : %s" % ("yes" if self.bootstrap_enabled() else "no"))
        self.runner.run(new_args, cwd=self.build_dir, env=self.build_env(),
                        timeout=self.args.command_timeout or None)
        args_file.write_text("\n".join(new_args) + "\n")
        self.patch_darwin_makefile()
        self.patch_static_diagnostics()
        self.patch_static_iconv_makefile()

    def patch_static_diagnostics(self):
        """With -static, the sarif-replay tool cannot link the shared
        libgdiagnostics.  Build a small static archive from libgdiagnostics.o
        (the remaining objects are already in $(LIBS)) and link sarif-replay
        against it."""
        if (not self.args.static_host or self.paths["darwin"]
                or not self.args.libgdiagnostics):
            return
        path = self.build_dir / "gcc" / "Makefile"
        if not path.is_file():
            return
        text = path.read_text()
        target_old = ("sarif-replay: $(SARIF_REPLAY_OBJS) "
                      "$(LIBGDIAGNOSTICS_FILENAME)")
        target_new = ("sarif-replay: $(SARIF_REPLAY_OBJS) libgdiagnostics.a")
        link_old = ("$(SARIF_REPLAY_OBJS) $(LIBGDIAGNOSTICS_FILENAME) $(LIBS)")
        link_new = ("$(SARIF_REPLAY_OBJS) libgdiagnostics.a $(LIBS)")
        shared_old = ("+$(LLINKER) $(ALL_LINKERFLAGS) $(LDFLAGS) "
                      "-o $@ -shared \\\n")
        shared_new = ("+$(LLINKER) $(ALL_LINKERFLAGS) "
                      "$(filter-out -static -static-libstdc++ -static-libgcc,"
                      "$(LDFLAGS)) -o $@ -shared \\\n")
        changed = False
        if shared_old in text:
            text = text.replace(shared_old, shared_new, 1)
            changed = True
        # libgccjit is also a shared host library; strip the executable-only
        # static flags from its link as well.
        jit = self.build_dir / "gcc" / "jit" / "Makefile"
        if not jit.is_file():
            jit = self.build_dir / "gcc" / "Makefile"
        if jit.is_file():
            jt = jit.read_text()
            old = "JIT_LDFLAGS = $(LDFLAGS)"
            new = ("JIT_LDFLAGS = $(filter-out -static -static-libstdc++ "
                   "-static-libgcc,$(LDFLAGS))")
            if old in jt:
                jit.write_text(jt.replace(old, new, 1))
        if target_old not in text or link_old not in text:
            if changed:
                path.write_text(text)
            return
        text = text.replace(target_old, target_new, 1)
        text = text.replace(link_old, link_new, 1)
        changed = True
        rule = ("libgdiagnostics.a: libgdiagnostics.o\n"
                "\t$(AR) rc $@ $<\n"
                "\t$(RANLIB) $@\n\n")
        text = text.replace("sarif-replay: $(SARIF_REPLAY_OBJS) libgdiagnostics.a",
                            rule + "sarif-replay: $(SARIF_REPLAY_OBJS) libgdiagnostics.a", 1)
        path.write_text(text)
        self.log.ok("static libgdiagnostics/sarif-replay link enabled")

    def patch_static_iconv(self):
        """Static glibc iconv loads gconv modules with dlopen, which is not
        available in a static binary.  For a static COBOL frontend, map iconv
        to GNU libiconv (built as a static PIC library) instead."""
        if not self.args.static_host or self.paths["darwin"]:
            return
        path = self.source / "libgcobol" / "charmaps.h"
        if not path.is_file():
            return
        text = path.read_text()
        old = "#include <iconv.h>"
        new = ("#include <iconv.h>\n"
               "#if defined(STATIC_HOST_LIBICONV)\n"
               "#define iconv libiconv\n"
               "#define iconv_open libiconv_open\n"
               "#define iconv_close libiconv_close\n"
               "#endif")
        if "STATIC_HOST_LIBICONV" in text:
            return
        if old not in text:
            return
        backup = path.with_name(path.name + ".buildpy-orig")
        if not backup.exists():
            backup.write_text(text)
        path.write_text(text.replace(old, new, 1))
        self.log.ok("mapped COBOL iconv to static GNU libiconv")

    def patch_static_iconv_makefile(self):
        if not self.args.static_host or self.paths["darwin"]:
            return
        if not self.deps_prefix:
            return
        if not (self.deps_prefix / "lib" / "libiconv.a").is_file():
            self.log.info("libiconv.a not found under %s/lib" % self.deps_prefix)
            return
        spec = (self.args.languages or "all").strip()
        if spec != "all" and "cobol" not in [x.strip() for x in re.split(r"[,\s]+", spec)]:
            self.log.info("cobol not requested; not wiring libiconv")
            return
        path = self.build_dir / "gcc" / "Makefile"
        if not path.is_file():
            # --configure-only does not create gcc/Makefile; make does.
            self.log.debug("no gcc Makefile yet at %s" % path)
            return
        text = path.read_text()
        libline = "LIBICONV = -L%s/lib -liconv" % self.deps_prefix
        text = re.sub(r"(?m)^LIBICONV =.*$", libline, text, count=1)
        cflags = ("CFLAGS-cobol/charmaps.o += "
                  "-DSTATIC_HOST_LIBICONV -I%s/include" % self.deps_prefix)
        if "CFLAGS-cobol/charmaps.o" not in text:
            text += "\n" + cflags + "\n"
        extra_libs = "LIBS += -L%s/lib -liconv" % self.deps_prefix
        if "LIBS += -L%s/lib -liconv" % self.deps_prefix not in text:
            text += "\n" + extra_libs + "\n"
        path.write_text(text)
        # gnattools uses a separate gcc-interface Makefile with TOOLS_LIBS.
        for ada in (self.source / "gcc" / "ada" / "gcc-interface" / "Makefile.in",
                    self.build_dir / "gcc" / "ada" / "gcc-interface" / "Makefile"):
            if not ada.is_file():
                continue
            at = ada.read_text()
            line = "TOOLS_LIBS += -L%s/lib -liconv" % self.deps_prefix
            if line not in at:
                ada.write_text(at + "\n" + line + "\n")
                self.log.ok("added libiconv to gnattools TOOLS_LIBS")
        self.log.ok("static libiconv wired into COBOL frontend")

    def patch_darwin_makefile(self):
        """Work around GCC PR: libgdiagnostics lists $(LIBS), which contains
        '-liconv' on Darwin, as a make prerequisite."""
        candidates = [self.build_dir / "gcc" / "Makefile"]
        candidates += sorted(self.build_dir.glob("*-gcc/Makefile"))
        if not self.args.libgdiagnostics:
            if not self.werror_enabled():
                self.patch_makefile_werror(candidates)
            return
        candidates += sorted(self.build_dir.glob("*-gcc/Makefile"))
        old = ("$(LIBGDIAGNOSTICS_FILENAME): $(libgdiagnostics_OBJS) $(CPPLIB) "
               "$(EXTRA_GCC_LIBS) $(LIBS) \\\n")
        new = ("$(LIBGDIAGNOSTICS_FILENAME): $(libgdiagnostics_objs) $(CPPLIB) "
               "$(EXTRA_GCC_LIBS) \\\n")
        # keep the exact variable spelling
        new = new.replace("libgdiagnostics_objs", "libgdiagnostics_OBJS")
        for path in candidates:
            if not path.is_file():
                continue
            try:
                text = path.read_text()
            except OSError:
                continue
            if old in text:
                path.write_text(text.replace(old, new, 1))
                self.log.ok("applied libgdiagnostics Makefile fix to %s" % path)
        if not self.werror_enabled():
            self.patch_makefile_werror(candidates)

    def patch_makefile_werror(self, candidates):
        old = ("STRICT_WARN = -Wmissing-format-attribute -Wconditionally-supported "
               "-Woverloaded-virtual -pedantic -Wno-long-long -Wno-variadic-macros "
               "-Wno-overlength-strings -Werror\n")
        new = ("STRICT_WARN = -Wmissing-format-attribute -Wconditionally-supported "
               "-Woverloaded-virtual -pedantic -Wno-long-long -Wno-variadic-macros "
               "-Wno-overlength-strings\n")
        for path in candidates:
            if not path.is_file():
                continue
            try:
                text = path.read_text()
            except OSError:
                continue
            if old in text:
                path.write_text(text.replace(old, new, 1))
                self.log.ok("disabled -Werror in %s" % path)

    def prepare_subdirs(self):
        """Create the gcc/ subdirectory Makefile without building it, then
        apply the static libgdiagnostics/libiconv patches.  A fresh top-level
        configure does not create gcc/Makefile until make runs."""
        if (not self.args.static_host or self.paths["darwin"]
                or self.args.dry_run):
            return
        # Always run configure-gcc: source patches (Makefile.in) can make it
        # newer, causing make to regenerate gcc/Makefile and discard the
        # generated-file patches below.  Regenerate first, then patch.
        self.runner.run([self.make_prog, "configure-gcc"],
                        cwd=self.build_dir, env=self.build_env(),
                        timeout=self.args.command_timeout or None)
        self.patch_static_diagnostics()
        self.patch_static_iconv_makefile()
        gcc_mk = self.build_dir / "gcc" / "Makefile"
        if gcc_mk.is_file():
            gt = gcc_mk.read_text()
            oldpr = ("$(LIBGDIAGNOSTICS_FILENAME): $(libgdiagnostics_OBJS) "
                     "$(CPPLIB) $(EXTRA_GCC_LIBS) $(LIBS) \\\n")
            newpr = ("$(LIBGDIAGNOSTICS_FILENAME): $(libgdiagnostics_OBJS) "
                     "$(CPPLIB) $(EXTRA_GCC_LIBS) \\\n")
            if oldpr in gt:
                gt = gt.replace(oldpr, newpr, 1)
                gcc_mk.write_text(gt)
                self.log.ok("removed generated libgdiagnostics LIBS prerequisite")
            dstatic = ("+$(DLLINKER) $(ALL_LINKERFLAGS) $(LDFLAGS) "
                       "-static-libphobos -o $@ \\\n")
            ddyn = ("+$(DLLINKER) $(ALL_LINKERFLAGS) "
                    "$(filter-out -static -static-libstdc++ -static-libgcc,"
                    "$(LDFLAGS)) -o $@ \\\n")
            changed = False
            if ddyn in gt:
                gt = gt.replace(ddyn, dstatic, 1)
                changed = True
            # Build a tiny __tls_get_addr shim for statically linked glibc and
            # add it to the d21 object list.
            shim_src = self.build_dir / "d21-tls-shim.c"
            shim_obj = self.build_dir / "d21-tls-shim.o"
            shim_src.write_text(
                "struct tls_index { unsigned long ti_module; unsigned long ti_offset; };\n"
                "void *__tls_get_addr(struct tls_index *ti) {\n"
                "    return (char *)__builtin_thread_pointer() + ti->ti_offset;\n"
                "}\n")
            proc = subprocess.run([self.cc, "-O2", "-fPIC", "-c", str(shim_src),
                                   "-o", str(shim_obj)],
                                  stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
            if proc.returncode == 0 and shim_obj.is_file():
                shim_line = "LIBS += %s" % shim_obj
                if self.need_d21_shim():
                    if shim_line not in gt:
                        gt += "\n" + shim_line + "\n"
                        changed = True
                elif shim_line in gt:
                    gt = gt.replace(shim_line + "\n", "")
                    changed = True
                self.log.ok("d21: static TLS resolver shim enabled")
            if changed:
                gcc_mk.write_text(gt)
                gt = gcc_mk.read_text()
            target = self.paths["target_triple"]
            std_dirs = [self.build_dir / ("prev-" + target) / "libstdc++-v3",
                        self.build_dir / target / "libstdc++-v3"]
            ldirs = []
            for d in std_dirs:
                for sub in ("src/.libs", "libsupc++/.libs"):
                    p = d / sub
                    if p.is_dir():
                        ldirs.append("-L%s" % p)
            mold = ("+$(LINKER) $(ALL_LINKERFLAGS) $(LDFLAGS) -o $@ "
                    "$(BUILD-PGE-O) -lm")
            mold2 = ("+$(LINKER) $(ALL_LINKERFLAGS) $(LDFLAGS) -o $@ "
                     "$(BUILD-PGE-O) -lm -lstdc++ -lsupc++ -lm")
            mnew = ("+$(LINKER) $(ALL_LINKERFLAGS) $(LDFLAGS) %s -o $@ "
                    "$(BUILD-PGE-O) -lm -lstdc++ -lsupc++ -lm" %
                    " ".join(ldirs))
            if mold in gt:
                gcc_mk.write_text(gt.replace(mold, mnew, 1))
                self.log.ok("added source libstdc++ to generated m2/pge link")
            elif mold2 in gt:
                gcc_mk.write_text(gt.replace(mold2, mnew, 1))
                self.log.ok("added source libstdc++ to generated m2/pge link")
            gt = gcc_mk.read_text()
            mc_old = "mcflex.o m2/gm2-libs-boot/RTcodummy.o -lm"
            mc_new = "mcflex.o m2/gm2-libs-boot/RTcodummy.o -lm -lstdc++ -lsupc++ -lm"
            if mc_old in gt:
                gcc_mk.write_text(gt.replace(mc_old, mc_new, 1))
                self.log.ok("added libstdc++ to generated m2/mc link")
        top = self.build_dir / "Makefile"
        if top.is_file() and self.bootstrap_enabled():
            text = top.read_text()
            if (self.deps_prefix and
                    (Path(self.deps_prefix) / "lib" / "libiconv.a").is_file()):
                libs = ("-L%s/lib -liconv -lstdc++ -lsupc++ -lm" %
                        self.deps_prefix)
                new, n = re.subn(r"(?m)^STAGE1_LIBS =.*$",
                                 "STAGE1_LIBS = %s" % libs, text, count=1)
                if n:
                    top.write_text(new)
                    text = new
                    self.log.ok("added libiconv to STAGE1_LIBS")
            extra = "".join(" -L%s" % d for d in self.previous_libstdcxx_dirs())
            if extra and extra not in text:
                new, n = re.subn(r"(?m)^POSTSTAGE1_LDFLAGS =(.*)$",
                                 lambda m: "POSTSTAGE1_LDFLAGS =%s%s" %
                                 (m.group(1), extra), text, count=1)
                if n:
                    top.write_text(new)
                    self.log.ok("added source libstdc++ dirs to POSTSTAGE1_LDFLAGS")
        if (self.deps_prefix and
                (Path(self.deps_prefix) / "lib" / "libiconv.a").is_file()):
            top = self.build_dir / "Makefile"
            if top.is_file():
                text = top.read_text()
                libs = ("-L%s/lib -liconv -lstdc++ -lsupc++ -lm" %
                        self.deps_prefix)
                for d in self.previous_libstdcxx_dirs():
                    libs += " -L%s" % d
                new, n = re.subn(r"(?m)^POSTSTAGE1_LIBS =.*$",
                                 "POSTSTAGE1_LIBS = %s" % libs, text, count=1)
                if n:
                    top.write_text(new)
                    self.log.ok("added libiconv to POSTSTAGE1_LIBS")

    def make(self):
        if self.args.dry_run:
            self.log.info("would run: %s -j%d" % (self.make_prog, self.jobs))
            return
        self.log.step("building GCC with %d parallel jobs" % self.jobs)
        cmd = [self.make_prog, "-j%d" % self.jobs]
        if self.args.keep_going:
            cmd.append("-k")
        if self.args.make_args:
            cmd += shlex.split(self.args.make_args)
        if self.args.make_target and self.args.make_target != "all":
            cmd.append(self.args.make_target)
        cmd += self.no_werror_make_override()
        started = time.time()
        if (self.args.static_runtime == "source"
                and not self.bootstrap_enabled()):
            # First pass builds stage1 and the target libstdc++/libgcc with
            # the normal build compiler.  Rebuild only the host compiler after
            # those source-built archives exist; the wrapper then links it
            # against them and the private glibc.
            self.log.step("source-runtime first pass (build target libs)")
            self.runner.run(cmd, cwd=self.build_dir, env=self.build_env())
            self.log.step("source-runtime final pass (relink host tools)")
            # Keep crtbegin/crtend and the target libs from the first pass;
            # clean-gcc would delete the very files the wrapper needs.
            for rel in ("gcc/xgcc", "gcc/xg++", "gcc/gcc", "gcc/cc1",
                        "gcc/cc1plus", "gcc/cc1obj", "gcc/cc1objplus",
                        "gcc/f951", "gcc/lto1", "gcc/gnat1", "gcc/d21",
                        "gcc/go1", "gcc/cobol1", "gcc/a681", "gcc/cc1gm2",
                        "gcc/crab1", "gcc/lto-wrapper", "gcc/collect2",
                        "gcc/plugin/gengtype", "gcc/g++-mapper-server",
                        "gcc/gcc-ar", "gcc/gcc-nm", "gcc/gcc-ranlib",
                        "gcc/gcov", "gcc/gcov-dump", "gcc/gcov-tool",
                        "gcc/lto-dump", "gcc/sarif-replay", "gcc/cpp",
                        "gcc/gengtype",
                        "c++tools/g++-mapper-server"):
                try:
                    (self.build_dir / rel).unlink()
                except OSError:
                    pass
            self.runner.run(cmd, cwd=self.build_dir, env=self.build_env())
            try:
                (self.build_dir / "fixincludes" / "full-stamp").unlink()
            except OSError:
                pass
            for subdir, target in (("gcc", "lto-wrapper"),
                                   ("c++tools", "all"),
                                   ("libcc1", "all"),
                                   ("fixincludes", "full-stamp")):
                if (self.build_dir / subdir / "Makefile").is_file():
                    self.runner.run([self.make_prog, "-C", subdir, target],
                                    cwd=self.build_dir, env=self.build_env())
        else:
            self.runner.run(cmd, cwd=self.build_dir, env=self.build_env())
        self.log.ok("build finished in %s" % _fmt_duration(time.time() - started))

    def install(self):
        if self.args.no_install:
            self.log.warn("--no-install given; skipping installation")
            return
        if self.args.dry_run:
            self.log.info("would run: %s install" % self.make_prog)
            return
        self.log.step("installing into %s" % self.prefix)
        self.prefix.mkdir(parents=True, exist_ok=True)
        self.runner.run([self.make_prog, "install"], cwd=self.build_dir,
                        env=self.build_env(), timeout=self.args.command_timeout or None)
        self.write_build_info()
        if (self.args.static_host and not self.paths["darwin"]
                and self.args.libgdiagnostics):
            src = self.build_dir / "gcc" / "libgdiagnostics.a"
            if src.is_file():
                dst = self.prefix / "lib" / "libgdiagnostics.a"
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(src), str(dst))
                self.log.ok("installed static libgdiagnostics.a")
        self.log.ok("installed %s" % self.prefix)

    def configured_languages(self):
        # Top-level Makefile records the languages that configure actually
        # enabled (after dropping unsupported/missing-bootstrap ones).
        try:
            text = (self.build_dir / "Makefile").read_text(errors="replace")
            matches = re.findall(r"--enable-languages=([A-Za-z0-9+_,^.-]+)", text)
            if matches:
                # The BUILD/HOST_CONFIGARGS copy is the fully expanded list;
                # the plain "all" appears earlier in boilerplate.
                return max(matches, key=len)
        except OSError:
            pass
        logf = self.build_dir / "config.log"
        try:
            for line in reversed(logf.read_text(errors="replace").splitlines()):
                if "The following languages will be built:" in line:
                    return line.split(":", 1)[1].strip()
        except OSError:
            pass
        return self.languages()

    def write_build_info(self):
        info_dir = self.prefix / "share" / "gcc-build-info"
        try:
            info_dir.mkdir(parents=True, exist_ok=True)
            lines = [
                "GCC build information",
                "=====================",
                "built by      : build.py %s" % SCRIPT_VERSION,
                "build date    : %s" % time.strftime("%Y-%m-%d %H:%M:%S %z"),
                "GCC version   : %s" % self.gcc_version(),
                "target        : %s" % self.paths["target_triple"],
                "host          : %s" % self.paths["host_triple"],
                "build         : %s" % self.paths["build_triple"],
                "prefix        : %s" % self.prefix,
                "languages     : %s" % self.configured_languages(),
                "bootstrap     : %s" % ("yes" if self.bootstrap_enabled() else "no"),
                "shared libs   : %s" % ("yes" if self.args.shared else "no"),
                "static libs   : %s" % ("yes" if self.args.static else "no"),
                "host linking  : %s" % ("static" if self.args.static_host else "dynamic"),
                "dependencies  : static GMP/MPFR/MPC/ISL/zstd from %s" % self.deps_prefix,
                "",
                "Configure command:",
                " ".join(shlex.quote(x) for x in self.configure_args()),
                "",
                "Environment:",
                "CC=%s" % self.cc,
                "CXX=%s" % self.cxx,
                "SDKROOT=%s" % (self.sdk or ""),
                "",
            ]
            (info_dir / "BUILD-INFO.txt").write_text("\n".join(lines))
            deps = []
            for dep in DEPS:
                stamp = Path(self.paths["deps"]) / "stamps" / ("%s-%s.json" %
                                                               (dep["name"], dep["version"]))
                if stamp.is_file():
                    deps.append(json.loads(stamp.read_text()))
            (info_dir / "DEPENDENCIES.json").write_text(json.dumps(deps, indent=2) + "\n")
        except OSError as exc:
            self.log.warn("could not write build info: %s" % exc)


# ---------------------------------------------------------------------------
# macOS relocation.
# ---------------------------------------------------------------------------

def relocate_macos(prefix, log, runner):
    """Rewrite absolute install names that point into the install prefix to
    @rpath, and add a loader-relative rpath where needed.  This makes the
    whole install tree copyable to a different path or machine."""
    if platform.system() != "Darwin":
        return
    prefix = Path(prefix).resolve()
    install_name_tool = shutil.which("install_name_tool")
    codesign = shutil.which("codesign")
    otool = shutil.which("otool")
    if not install_name_tool or not otool:
        log.warn("install_name_tool/otool not found; skipping macOS relocation")
        return
    log.step("relocating macOS install names under %s" % prefix)
    changed_files = 0
    for p in sorted(prefix.rglob("*")):
        if not p.is_file() or p.is_symlink():
            continue
        if p.suffix in (".a", ".o", ".la", ".h", ".hpp", ".txt", ".json", ".py", ".md"):
            continue
        try:
            with open(str(p), "rb") as fh:
                magic = fh.read(4)
        except OSError:
            continue
        # Mach-O magics: 32/64 bit, little/big endian, fat
        if magic not in (b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe",
                         b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe",
                         b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"):
            continue
        rc, out = _run_tool(otool, ["-L", str(p)])
        if rc != 0:
            continue
        changed = False
        for line in out.splitlines()[1:]:
            dep = line.strip().split(" (")[0].strip()
            if dep.startswith(str(prefix) + os.sep):
                new = "@rpath/" + os.path.basename(dep)
                rc2, _ = _run_tool(install_name_tool, ["-change", dep, new, str(p)])
                if rc2 == 0:
                    changed = True
                    log.debug("  %s: %s -> %s" % (p.name, dep, new))
        rc, out = _run_tool(otool, ["-D", str(p)])
        if rc == 0:
            for line in out.splitlines()[1:]:
                ident = line.strip()
                if ident.startswith(str(prefix) + os.sep):
                    new = "@rpath/" + os.path.basename(ident)
                    rc2, _ = _run_tool(install_name_tool, ["-id", new, str(p)])
                    if rc2 == 0:
                        changed = True
                        log.debug("  %s: id -> %s" % (p.name, new))
        if changed:
            # Add a loader-relative rpath for @rpath dependencies, if absent.
            rc, out = _run_tool(otool, ["-l", str(p)])
            has_rpath = "LC_RPATH" in out
            if not has_rpath:
                if p.parent == prefix / "lib":
                    rpath = "@loader_path"
                elif p.parent == prefix / "bin":
                    rpath = "@loader_path/../lib"
                elif "libexec" in p.parts:
                    rpath = "@loader_path/../../../../lib"
                else:
                    rpath = "@loader_path"
                _run_tool(install_name_tool, ["-add_rpath", rpath, str(p)])
            if codesign:
                rc2, out2 = _run_tool(codesign, ["--force", "--sign", "-", str(p)])
                if rc2 != 0:
                    log.warn("could not re-sign %s: %s" % (p, out2.strip()[:200]))
            changed_files += 1
    if changed_files:
        log.ok("relocated %d Mach-O file(s)" % changed_files)
    else:
        log.ok("all install names were already relocatable")


def _run_tool(path, args):
    try:
        proc = subprocess.run([path] + args, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True,
                              errors="replace", timeout=60)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except (OSError, subprocess.SubprocessError):
        return 1, ""


# ---------------------------------------------------------------------------
# Verification.
# ---------------------------------------------------------------------------

def _is_macho_file(path):
    try:
        with open(str(path), "rb") as fh:
            magic = fh.read(4)
    except OSError:
        return False
    return magic in (b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe",
                     b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe",
                     b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca")


class Verifier(object):
    def __init__(self, args, paths, log, runner, deps_prefix):
        self.args = args
        self.paths = paths
        self.log = log
        self.runner = runner
        self.prefix = Path(paths["prefix"])
        self.deps_prefix = deps_prefix
        self.failures = []
        self.warnings = []

    def _fail(self, msg):
        self.failures.append(msg)
        self.log.error(msg)

    def _warn(self, msg):
        self.warnings.append(msg)
        self.log.warn(msg)

    def _bin(self, name):
        suffix = ".exe" if IS_WINDOWS else ""
        p = self.prefix / "bin" / (name + suffix)
        return p if p.exists() else None

    def _run_binary(self, path, args, timeout=120):
        env = os.environ.copy()
        env.pop("DYLD_LIBRARY_PATH", None)
        env.pop("LD_LIBRARY_PATH", None)
        env["PATH"] = (str(self.prefix / "bin") + os.pathsep +
                       env.get("PATH", ""))
        proc = subprocess.run([str(path)] + args, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True,
                              errors="replace", timeout=timeout, env=env)
        return proc.returncode, proc.stdout or ""

    def check_compilers(self):
        self.log.step("verifying compiler drivers")
        expected = [("gcc", "c")]
        if "c++" in self.args.languages or self.args.languages == "all":
            expected.append(("g++", "c++"))
        if "fortran" in self.args.languages or self.args.languages == "all":
            expected.append(("gfortran", "fortran"))
        for name, _lang in expected:
            path = self._bin(name)
            if not path:
                if name == "gfortran":
                    self._warn("gfortran driver not found")
                else:
                    self._fail("missing compiler driver: %s" % name)
                continue
            rc, out = self._run_binary(path, ["--version"])
            if rc != 0:
                self._fail("%s --version failed (rc=%d)" % (name, rc))
            else:
                self.log.ok("%s: %s" % (name, out.splitlines()[0]))
            rc, out = self._run_binary(path, ["-dumpmachine"])
            if rc == 0:
                self.log.info("       target: %s" % out.strip())

    def check_dependencies(self):
        self.log.step("checking binary dependencies (no third-party dylibs)")
        if not self.prefix.exists():
            self._fail("install prefix does not exist: %s" % self.prefix)
            return
        forbidden = ["/opt/homebrew", "/opt/local", "/sw/lib", "/usr/local/opt"]
        if self.paths["darwin"]:
            forbidden += ["/usr/local/lib", "/usr/local/include", "/opt/local/lib"]
        if self.deps_prefix:
            forbidden.append(str(Path(self.deps_prefix).resolve()))
        forbidden.append(str(Path(self.paths["build"]).resolve()))
        forbidden.append(str(Path(self.paths["source"]).resolve()))
        checked = 0
        for p in sorted(self.prefix.rglob("*")):
            if not p.is_file() or p.is_symlink():
                continue
            try:
                mode = p.stat().st_mode
            except OSError:
                continue
            if p.suffix in (".a", ".o", ".la", ".h", ".txt", ".json", ".md", ".py"):
                continue
            if not (mode & 0o111):
                continue
            deps = self.binary_deps(p)
            if deps is None:
                continue
            checked += 1
            for dep in deps:
                if dep.startswith("@rpath/") or dep.startswith("@loader_path/") or \
                   dep.startswith("@executable_path/"):
                    continue
                if dep.startswith(MACHO_SYSTEM_PREFIXES):
                    continue
                if dep.startswith("/usr/lib/") or dep.startswith("/System/"):
                    continue
                if (not self.paths["darwin"]) and dep.startswith(ELF_SYSTEM_PREFIXES):
                    continue
                if dep.startswith(str(Path(self.prefix).resolve())):
                    continue
                bad = any(dep.startswith(f) for f in forbidden)
                if bad or re.search(r"/(libgmp|libmpfr|libmpc|libisl|libzstd)\.", dep):
                    self._fail("%s depends on third-party library: %s" % (p, dep))
                elif dep.startswith("/"):
                    # A non-system absolute path is a portability bug.
                    self._warn("%s references absolute path: %s" % (p, dep))
        self.log.ok("inspected %d Mach-O/ELF objects" % checked)

    def binary_deps(self, path):
        if IS_WINDOWS:
            return None
        if self.paths["darwin"] and not _is_macho_file(path):
            return None
        if self.paths["darwin"]:
            if not shutil.which("otool"):
                return None
            rc, out = self._run_binary(Path(shutil.which("otool")), ["-L", str(path)])
        else:
            if not shutil.which("ldd"):
                return None
            rc, out = self._run_binary(Path(shutil.which("ldd")), [str(path)])
        if rc != 0:
            return None
        result = []
        for line in out.splitlines()[1:]:
            line = line.strip()
            if not line:
                continue
            if line.startswith("linux-vdso") or line.startswith("linux-gate"):
                continue
            if self.paths["darwin"]:
                dep = line.split(" (")[0].strip()
            else:
                # "libc.so.6 => /lib/... (0x...)"
                if "=>" in line:
                    dep = line.split("=>", 1)[1].strip().split(" (")[0]
                else:
                    dep = line.split(" (")[0].strip()
            if dep and dep != "not":
                result.append(dep)
        return result

    def check_libraries(self):
        self.log.step("inventorying static and shared libraries")
        libdirs = [self.prefix / "lib", self.prefix / "lib64"]
        gccdir = self.prefix / "lib" / "gcc"
        if gccdir.is_dir():
            for d in gccdir.glob("*/*"):
                if d.is_dir():
                    libdirs.append(d)
        static = []
        shared = []
        for d in libdirs:
            if not d.is_dir():
                continue
            for p in d.iterdir():
                if p.is_symlink() or not p.is_file():
                    continue
                if p.suffix == ".a":
                    static.append(p.name)
                elif p.suffix == ".dylib" or ".so" in p.name:
                    shared.append(p.name)
        self.log.info("  static archives : %d" % len(static))
        self.log.info("  shared objects  : %d" % len(shared))
        shared_only = {"libasan", "libubsan", "libgdiagnostics"}
        static_only = {"libsupc++"}
        for stem in DUAL_LIBS:
            has_a = any((n == stem + ".a" or (n.startswith(stem + ".") and n.endswith(".a")))
                        for n in static)
            has_d = any(n.startswith(stem) and (".dylib" in n or ".so" in n)
                        for n in shared)
            if stem == "libobjc":
                # Darwin renames the GNU Objective-C runtime to libobjc-gnu
                # to avoid clashing with the system libobjc.
                has_a = has_a or any(n.startswith("libobjc-gnu") and n.endswith(".a")
                                     for n in static)
                has_d = has_d or any(n.startswith("libobjc-gnu") and
                                     (".dylib" in n or ".so" in n) for n in shared)
            if has_a and not has_d:
                if stem in static_only:
                    self.log.info("  note: %s is static-only in this GCC configuration" % stem)
                else:
                    self._warn("%s has a static library but no shared library" % stem)
            elif has_d and not has_a:
                if stem in shared_only:
                    self.log.info("  note: %s is shared-only on Darwin (upstream design)" % stem)
                else:
                    self._warn("%s has a shared library but no static library" % stem)

    def check_smoke(self):
        self.log.step("running smoke tests")
        gcc = self._bin("gcc")
        gxx = self._bin("g++")
        if not gcc:
            return
        tmp = Path(tempfile.mkdtemp(prefix="gcc-smoke-"))
        try:
            tests = []
            c = tmp / "hello.c"
            c.write_text('#include <stdio.h>\nint main(void){printf("hello-c\\n");return 0;}\n')
            tests.append(("C", gcc, [str(c), "-o", str(tmp / "hello-c")], tmp / "hello-c", "hello-c"))
            if gxx:
                cpp = tmp / "hello.cc"
                cpp.write_text('#include <iostream>\nint main(){std::cout << "hello-c++" << std::endl; return 0;}\n')
                tests.append(("C++", gxx, [str(cpp), "-o", str(tmp / "hello-cpp")], tmp / "hello-cpp", "hello-c++"))
            gf = self._bin("gfortran")
            if gf:
                f = tmp / "hello.f90"
                f.write_text('program hello\n  print *, "hello-fortran"\nend program hello\n')
                tests.append(("Fortran", gf, [str(f), "-o", str(tmp / "hello-f")], tmp / "hello-f", "hello-fortran"))
            objc = self._bin("gcc")
            has_cc1obj = any(self.prefix.rglob("cc1obj")) or any(self.prefix.rglob("cc1obj.exe"))
            if objc and has_cc1obj:
                m = tmp / "hello.m"
                m.write_text('#include <stdio.h>\nint main(void){printf("hello-objc\\n");return 0;}\n')
                tests.append(("ObjC", objc, ["-x", "objective-c", str(m), "-o", str(tmp / "hello-o")], tmp / "hello-o", "hello-objc"))
            gnatmake = self._bin("gnatmake")
            if gnatmake:
                adb = tmp / "hello.adb"
                adb.write_text('with Ada.Text_IO; procedure Hello is begin '
                               'Ada.Text_IO.Put_Line("hello-ada"); end Hello;\n')
                tests.append(("Ada", gnatmake, [str(adb), "-o", str(tmp / "hello-ada")],
                              tmp / "hello-ada", "hello-ada"))
            gdc = self._bin("gdc")
            if gdc:
                dsrc = tmp / "hello.d"
                dsrc.write_text('import std.stdio; void main(){writeln("hello-d");}\n')
                tests.append(("D", gdc, [str(dsrc), "-o", str(tmp / "hello-d")],
                              tmp / "hello-d", "hello-d"))
            for label, compiler, compile_args, exe, expected in tests:
                try:
                    rc, out = self._run_binary(compiler, compile_args, timeout=180)
                    if rc != 0:
                        self._fail("%s smoke test failed to compile:\n%s" % (label, out[-2000:]))
                        continue
                    rc, out = self._run_binary(exe, [], timeout=60)
                    if rc != 0 or expected not in out:
                        self._fail("%s smoke test failed to run (rc=%d): %s" % (label, rc, out))
                    else:
                        self.log.ok("%s smoke test: %s" % (label, expected))
                except Exception as exc:
                    self._fail("%s smoke test error: %s" % (label, exc))

            # Optional feature probes (warnings only).
            feature_src = tmp / "feature.c"
            feature_src.write_text("int main(void){return 0;}\n")
            probes = [
                ("LTO", [str(gcc), "-flto", str(feature_src), "-o", str(tmp / "lto")]),
                ("OpenMP", [str(gcc), "-fopenmp", str(feature_src), "-o", str(tmp / "omp")]),
                ("Graphite", [str(gcc), "-O2", "-fgraphite-identity", "-c", str(feature_src), "-o", str(tmp / "g.o")]),
                ("zstd debug", [str(gcc), "-g", "-gz=zstd", "-c", str(feature_src), "-o", str(tmp / "z.o")]),
                ("ASan", [str(gcc), "-fsanitize=address", str(feature_src), "-o", str(tmp / "asan")]),
                ("UBSan", [str(gcc), "-fsanitize=undefined", str(feature_src), "-o", str(tmp / "ubsan")]),
            ]
            for label, cmd in probes:
                if label == "zstd debug" and self.paths["darwin"]:
                    self.log.info("  note: -gz=zstd is an ELF-assembler feature; "
                                  "zstd is used for LTO compression on Darwin")
                    continue
                try:
                    rc, out = self._run_binary(Path(cmd[0]), cmd[1:], timeout=180)
                    if rc == 0:
                        self.log.ok("feature probe %s: works" % label)
                    else:
                        self._warn("feature probe %s: not available" % label)
                except Exception as exc:
                    self._warn("feature probe %s: %s" % (label, exc))
        finally:
            shutil.rmtree(str(tmp), ignore_errors=True)

    def check_relocatable(self):
        if not self.paths["darwin"] or not shutil.which("otool"):
            return
        self.log.step("checking macOS install names")
        bad = 0
        for p in sorted(self.prefix.rglob("*")):
            if not p.is_file() or p.suffix == ".a":
                continue
            deps = self.binary_deps(p)
            if not deps:
                continue
            for dep in deps:
                if dep.startswith(str(self.prefix)):
                    self._warn("%s has absolute install name %s" % (p, dep))
                    bad += 1
        if bad == 0:
            self.log.ok("all Mach-O install names are relocatable (@rpath/@loader_path)")
        else:
            self._warn("%d absolute install names found; the install tree is still "
                       "relocatable if the whole prefix is copied" % bad)

    def check_static_host(self):
        if not self.args.static_host or self.paths["darwin"]:
            return
        self.log.step("checking host executables are statically linked")
        ldd = shutil.which("ldd")
        if not ldd:
            self.log.warn("ldd not found; cannot verify static host binaries")
            return
        checked = 0
        for root in (self.prefix / "bin", self.prefix / "libexec"):
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*")):
                if not path.is_file() or path.is_symlink():
                    continue
                if path.suffix in (".so", ".dylib", ".la", ".a"):
                    continue
                try:
                    if path.read_bytes()[:4] != b"\x7fELF":
                        continue
                except OSError:
                    continue
                if not (path.stat().st_mode & 0o111):
                    continue
                proc = subprocess.run([ldd, str(path)], stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT, text=True)
                checked += 1
                if proc.returncode == 0:
                    if path.name == "d21" and self.args.allow_dynamic_d21:
                        self._warn("%s is dynamically linked (GDC seed "
                                   "libgphobos limitation)" % path)
                    else:
                        self._fail("%s is dynamically linked: %s" %
                                   (path, (proc.stdout or "").strip().splitlines()[:3]))
        if checked == 0:
            self._fail("static-host requested but no ELF host executables found")
        else:
            self.log.ok("%d host executables are statically linked" % checked)

    def run(self):
        self.check_compilers()
        self.check_static_host()
        self.check_dependencies()
        self.check_libraries()
        if not self.args.no_smoke:
            self.check_smoke()
        self.check_relocatable()
        if self.failures:
            self.log.error("%d verification failure(s)" % len(self.failures))
            for f in self.failures:
                self.log.error("  - " + f)
            return False
        self.log.ok("verification passed (%d warning(s))" % len(self.warnings))
        return True


# ---------------------------------------------------------------------------
# Packaging.
# ---------------------------------------------------------------------------

def package_install(prefix, output, log, runner):
    output = Path(output)
    if output.is_dir():
        output = output / ("gcc-%s.tar.gz" % time.strftime("%Y%m%d"))
    output.parent.mkdir(parents=True, exist_ok=True)
    log.step("packaging %s -> %s" % (prefix, output))
    if output.suffix in (".gz", ".xz", ".bz2"):
        tar = shutil.which("tar")
        if tar:
            cmd = [tar, "-czf", str(output), "-C", str(prefix), "."]
            if output.suffix == ".xz":
                cmd = [tar, "-cJf", str(output), "-C", str(prefix), "."]
            runner.run(cmd)
        else:
            with tarfile.open(str(output), "w:gz") as tf:
                tf.add(str(prefix), arcname=".")
    elif output.suffix == ".zip":
        shutil.make_archive(str(output.with_suffix("")), "zip", str(prefix))
    else:
        raise BuildError("unsupported package suffix: %s" % output.suffix)
    digest = hashlib.sha256()
    with open(str(output), "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    sidecar = output.with_name(output.name + ".sha256")
    sidecar.write_text("%s  %s\n" % (digest.hexdigest(), output.name))
    log.ok("package: %s (%s)" % (output, _human_size(output.stat().st_size)))
    log.ok("sha256:  %s" % digest.hexdigest())
    return output


def _human_size(n):
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024.0:
            return "%.1f %s" % (n, unit)
        n /= 1024.0
    return "%.1f PiB" % n


# ---------------------------------------------------------------------------
# Doctor / language listing.
# ---------------------------------------------------------------------------

def doctor(paths, args, log):
    log.step("environment report")
    log.info("build.py        : %s (Python %s)" % (SCRIPT_VERSION, platform.python_version()))
    log.info("OS              : %s %s (%s)" % (platform.system(), platform.release(), platform.machine()))
    log.info("build triple    : %s" % paths["build_triple"])
    log.info("host triple     : %s" % paths["host_triple"])
    log.info("target triple   : %s" % paths["target_triple"])
    log.info("source          : %s" % paths["source"])
    log.info("build dir       : %s" % paths["build"])
    log.info("install prefix  : %s" % paths["prefix"])
    log.info("deps dir        : %s" % paths["deps"])
    log.info("C compiler      : %s (%s)" % (paths["cc"], compiler_banner(paths["cc"])))
    log.info("C++ compiler    : %s (%s)" % (paths["cxx"], compiler_banner(paths["cxx"])))
    log.info("make            : %s" % paths["make"])
    if paths["sdk"]:
        log.info("macOS SDK       : %s" % paths["sdk"])
    log.info("CPUs            : %s" % (os.cpu_count() or "unknown"))
    try:
        total, used, free = shutil.disk_usage(str(paths["prefix"].parent))
        log.info("disk free       : %s of %s" % (_human_size(free), _human_size(total)))
    except OSError:
        pass
    for tool in ("bison", "flex", "m4", "gawk", "tar", "curl", "git"):
        p = shutil.which(tool)
        log.info("%-15s : %s" % (tool, p or "not found"))
    # dependency status
    log.info("dependencies:")
    for dep in DEPS:
        libs = [Path(paths["deps"]) / "prefix" / "lib" / lib for lib in dep["libs"]]
        ok = all(p.is_file() for p in libs)
        log.info("  %-5s %-7s %s" % (dep["name"], dep["version"], "ready" if ok else "not built"))
    # language bootstrap availability
    log.info("language bootstrap tools (stage0):")
    stage0_dir = (Path(args.stage0_dir) if args.stage0_dir
                  else Path(paths["deps"]) / "stage0")
    for tool, purpose in (("gnatbind", "Ada"), ("gdc", "D"), ("cargo", "Rust")):
        found = shutil.which(tool)
        if not found and stage0_dir.is_dir():
            hits = list(stage0_dir.rglob(tool))
            if hits:
                found = str(hits[0])
        log.info("  %-9s (%s): %s" % (tool, purpose, found or "not found"))
    log.info("  stage0 dir: %s" % stage0_dir)


def list_languages(source, log):
    log.step("languages known to this GCC source tree")
    gcc_dir = Path(source) / "gcc"
    rows = []
    for cfg in sorted(gcc_dir.glob("*/config-lang.in")):
        text = cfg.read_text(errors="replace")
        def field(name):
            m = re.search(r"^%s=\"?([^\"\n]+)\"?" % re.escape(name), text, re.M)
            return m.group(1).strip() if m else ""
        lang = field("language") or cfg.parent.name
        targets = field("target_libs") or "-"
        default = field("build_by_default") or "yes"
        rows.append((lang, targets, default))
    for lang, targets, default in rows:
        log.info("  %-10s target-libs=%-40s default=%s" % (lang, targets, default))
    log.info("")
    log.info("Ada/D/Rust are self-hosted.  build.py can auto-fetch open-source")
    log.info("stage0 GNAT/GDC/rustc (see --bootstrap-tools) and then build the GCC")
    log.info("front ends from source.  COBOL/Go are target-dependent and are left")
    log.info("to GCC configure; no platform-specific disabling is hard-coded here.")


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------

def add_bool(group, name, dest, default, help_text):
    group.add_argument("--" + name, dest=dest, action="store_true", help=help_text)
    group.add_argument("--no-" + name, dest=dest, action="store_false",
                       help="disable: " + help_text)
    group.set_defaults(**{dest: default})


def build_parser():
    epilog = textwrap.dedent("""\
        examples:
          ./build.py                             full native build, all features
          ./build.py -j 16 --prefix=/opt/gcc     choose parallelism and prefix
          ./build.py --no-bootstrap              one stage, much faster
          ./build.py --deps-only                 fetch and build static deps only
          ./build.py --doctor                    show environment and readiness
          ./build.py --languages=all             all languages buildable on host
          ./build.py --languages=c,c++,fortran   explicit language set
          ./build.py --offline --tarball-dir=./tarballs
          ./build.py --package                   install then make a .tar.gz
          ./build.py --clean --reconfigure       wipe build dir and configure afresh

        notes:
          * build/install/_deps default to the directory containing build.py,
            not the current shell directory.  Use --work-dir DIR to move all
            three under a chosen base, or --build-dir/--deps-dir/--prefix for
            individual directories.  --show-paths only prints them.
          * Default languages=all.  Ada/D/Rust stage0 is fetched automatically
            when possible; if it cannot be obtained, GCC's configure skips the
            language with a warning.  COBOL/Go depend only on target support.
          * GMP/MPFR/MPC/ISL/zstd are always built as static PIC libraries in
            <deps-dir>; compiler binaries therefore only link system libraries.
          * Both shared and static runtime libraries are enabled by default.
          * Ada, D and Rust are self-hosted (or need a runtime written in the
            language).  build.py fetches an official open-source GNAT/GDC/rustc
            stage0 seed where one exists for this host triple; otherwise it
            uses the system tool or --stage0-dir, and reports a skip if neither
            is available.  Use --bootstrap-tools=none for a no-download build.
          * On a clang host without stage0, --no-bootstrap skips libgccjit:
            clang's libc++ headers conflict with GCC's safe-ctype poisoning.
            The default bootstrap build includes JIT and uses the newly built
            GCC.
          * The build log is <build-dir>/build.log; summary is
            <build-dir>/build-summary.json.

        中文说明:
          * 默认构建全部可用语言并同时生成动态库和静态库。
          * 所有第三方依赖自动下载源码并静态编译，最终编译器不依赖 Homebrew。
          * Ada/D/Rust 的 stage0 只在有官方种子的 host triple 上自动获取；没有种子时
    使用系统工具或 --stage0-dir，都没有才明确提示并由 GCC 跳过该语言。
        """)
    p = argparse.ArgumentParser(
        prog="build.py",
        description="Portable, self-bootstrapping GCC build driver.",
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--version", action="version", version="build.py %s" % SCRIPT_VERSION)

    g = p.add_argument_group("paths")
    g.add_argument("--source", metavar="DIR", help="GCC source tree (auto-detected)")
    g.add_argument("--work-dir", "--output-dir", dest="work_dir", metavar="DIR",
                   help="base directory for the default build/, _deps/ and install/ "
                        "directories, relative to the current directory "
                        "(default: the directory containing build.py)")
    g.add_argument("--build-dir", metavar="DIR",
                   help="build directory (default: <work-dir>/build)")
    g.add_argument("--prefix", metavar="DIR",
                   help="install prefix (default: <work-dir>/install)")
    g.add_argument("--deps-dir", metavar="DIR",
                   help="dependency cache (default: <work-dir>/_deps)")
    g.add_argument("--tarball-dir", metavar="DIR", help="pre-downloaded dependency tarballs")
    g.add_argument("--log-file", metavar="FILE", help="log file (default: <build-dir>/build.log)")

    g = p.add_argument_group("build")
    g.add_argument("-j", "--jobs", type=int, default=0, metavar="N",
                   help="parallel jobs (default: all CPUs)")
    g.add_argument("--languages", metavar="LIST", default="all",
                   help="comma-separated languages or 'all' (default: all)")
    g.add_argument("--bootstrap", choices=("auto", "yes", "no"), default="auto",
                   help="bootstrap stage1/2/3 (default: auto = yes for native builds)")
    g.add_argument("--no-bootstrap", "--disable-bootstrap", dest="bootstrap",
                   action="store_const", const="no",
                   help="alias for --bootstrap=no (fast single-stage build)")
    g.add_argument("--enable-bootstrap", dest="bootstrap", action="store_const",
                   const="yes", help="alias for --bootstrap=yes")
    g.add_argument("--checking", default="release", metavar="KIND",
                   help="--enable-checking value (default: release)")
    g.add_argument("--multilib", action="store_true", help="enable multilib (default: no)")
    g.add_argument("--stage1-languages", metavar="LIST", help="languages for bootstrap stage 1")
    g.add_argument("--pgo", choices=("no", "lto"), default="no", help="PGO build mode")
    g.add_argument("--make-target", default="all", metavar="TARGET")
    g.add_argument("--make-args", default="", metavar="ARGS", help="extra arguments for make")
    g.add_argument("--configure-extra", default="", metavar="ARGS",
                   help="extra arguments appended to configure")
    g.add_argument("--reconfigure", action="store_true",
                   help="run configure even if config.status already exists")
    g.add_argument("--no-source-patch", action="store_true",
                   help="do not apply the known Darwin libgdiagnostics Makefile fix")
    g.add_argument("--keep-going", action="store_true", help="pass -k to make")
    g.add_argument("--werror", dest="werror", action="store_true", default=None,
                   help="treat warnings as errors in bootstrap stages 2/3")
    g.add_argument("--no-werror", dest="werror", action="store_false",
                   help="disable -Werror in bootstrap stages (recommended for experimental branches)")
    g.add_argument("--no-install", action="store_true", help="build but do not install")
    g.add_argument("--no-smoke", action="store_true", help="skip runtime smoke tests")
    g.add_argument("--no-relocate", action="store_true",
                   help="do not rewrite absolute macOS install names after install")
    g.add_argument("--command-timeout", type=int, default=0, metavar="SECONDS",
                   help="timeout for configure/install (0 = unlimited)")

    g = p.add_argument_group("features (all enabled by default when supported)")
    add_bool(g, "shared", "shared", True, "build shared runtime libraries")
    add_bool(g, "gas-base64", "gas_base64", True,
             "let GCC use the .base64 assembler directive when the build "
             "assembler supports it; disable for distributing to older binutils")
    g.add_argument("--library-linkage", choices=("both", "static"),
                   default="both",
                   help="both (default): install shared and static target "
                        "libraries; static: install static target libraries "
                        "only and disable shared-only components "
                        "(JIT, libgdiagnostics, plugins)")
    add_bool(g, "static-host", "static_host",
             platform.system() in ("Linux", "FreeBSD", "OpenBSD", "NetBSD", "DragonFly"),
             "statically link host executables (default on open-source OSes)")
    add_bool(g, "allow-dynamic-d21", "allow_dynamic_d21", False,
             "allow the GDC frontend d21 to be dynamically linked when the "
             "stage0 libgphobos seed cannot be linked statically")
    g.add_argument("--static-runtime", choices=("auto", "system", "source"),
                   default="auto",
                   help="source of static libc/crt for --static-host: "
                        "auto (use system static libs, fall back to source glibc), "
                        "system (require system static libs), "
                        "source (always build private static glibc from source)")
    add_bool(g, "static", "static", True, "build static runtime libraries")
    add_bool(g, "graphite", "graphite", True, "enable Graphite/ISL loop optimizations")
    add_bool(g, "lto", "lto", True, "enable link-time optimization")
    add_bool(g, "jit", "jit", True, "build libgccjit when languages=all")
    add_bool(g, "libgdiagnostics", "libgdiagnostics", True,
             "build libgdiagnostics shared library")
    add_bool(g, "libstdcxx-backtrace", "libstdcxx_backtrace", True,
             "build std::stacktrace support (libbacktrace)")
    add_bool(g, "zstd", "zstd", True, "use static zstd for compressed debug sections")
    add_bool(g, "system-zlib", "system_zlib", False, "use system zlib instead of in-tree zlib")
    g.add_argument("--sanitizers", choices=("auto", "yes", "no"), default="auto")
    g.add_argument("--plugins", choices=("auto", "yes", "no"), default="auto")
    g.add_argument("--host-shared", choices=("auto", "yes", "no"), default="auto",
                   help="build host code as PIC/shared (auto = forced on Darwin)")
    add_bool(g, "nls", "nls", False, "enable native language support (if available)")
    add_bool(g, "objc-gc", "objc_gc", False, "enable Boehm GC for Objective-C")
    add_bool(g, "libstdcxx-debug", "libstdcxx_debug", False, "build debug libstdc++")

    g = p.add_argument_group("dependencies")
    g.add_argument("--deps", choices=("auto", "all", "none"), default="all",
                   help="build third-party dependencies (default: all)")
    g.add_argument("--skip-deps", action="store_true", help="alias for --deps=none")
    g.add_argument("--deps-only", action="store_true", help="build dependencies and stop")
    g.add_argument("--download-only", action="store_true", help="download dependencies and stop")
    g.add_argument("--rebuild-deps", action="store_true", help="force rebuilding dependencies")
    g.add_argument("--offline", action="store_true", help="never access the network")
    g.add_argument("--insecure", action="store_true", help="do not verify TLS certificates")
    g.add_argument("--no-verify-downloads", action="store_true", help="skip checksum checks")
    g.add_argument("--retries", type=int, default=4, help="download retries per URL")
    g.add_argument("--dep-cflags", default="-O2 -fPIC -std=gnu11", help="CFLAGS for dependency builds")
    g.add_argument("--bootstrap-tools", choices=("auto", "download", "none"),
                   default="auto",
                   help="fetch open-source GNAT/GDC/Rust stage0 tools when needed "
                        "(default: auto)")
    g.add_argument("--gdc-seed", choices=("auto", "modern", "legacy", "system"),
                   default="auto",
                   help="GDC seed selection: auto (modern then legacy then system), "
                        "modern (Debian gdc-14 package), legacy (GDC 5.2 archive), "
                        "system (use installed gdc). --stage0-dir always wins")
    g.add_argument("--no-bootstrap-tools", dest="bootstrap_tools",
                   action="store_const", const="none",
                   help="never fetch stage0 tools; use only what is on PATH")
    g.add_argument("--stage0-dir", metavar="DIR",
                   help="directory with open-source stage0 tools "
                        "(default: <deps-dir>/stage0)")
    g.add_argument("--gmp-prefix", metavar="DIR")
    g.add_argument("--mpfr-prefix", metavar="DIR")
    g.add_argument("--mpc-prefix", metavar="DIR")
    g.add_argument("--isl-prefix", metavar="DIR")
    g.add_argument("--zstd-prefix", metavar="DIR")

    g = p.add_argument_group("platform / toolchain")
    g.add_argument("--build", dest="build_triple", metavar="TRIPLE")
    g.add_argument("--host", dest="host_triple", metavar="TRIPLE")
    g.add_argument("--target", dest="target_triple", metavar="TRIPLE")
    g.add_argument("--cc", metavar="PATH", help="C compiler (default: system cc)")
    g.add_argument("--cxx", metavar="PATH", help="C++ compiler (default: system c++)")
    g.add_argument("--cflags", default="", metavar="FLAGS")
    g.add_argument("--cxxflags", default="", metavar="FLAGS")
    g.add_argument("--ldflags", default="", metavar="FLAGS")
    g.add_argument("--sdk", metavar="DIR", help="macOS SDK path (default: xcrun)")
    g.add_argument("--sysroot", dest="explicit_sysroot", metavar="DIR")
    g.add_argument("--deployment-target", metavar="VERSION",
                   help="macOS MACOSX_DEPLOYMENT_TARGET")
    g.add_argument("--cpu", metavar="CPU", help="--with-cpu value")
    g.add_argument("--pkgversion", metavar="STRING", help="--with-pkgversion value")

    g = p.add_argument_group("actions")
    g.add_argument("--clean", action="store_true", help="remove the build directory first")
    g.add_argument("--distclean", action="store_true", help="remove build, deps and install")
    g.add_argument("--force", action="store_true", help="allow removing non-empty directories")
    g.add_argument("--doctor", action="store_true", help="print environment report and exit")
    g.add_argument("--show-paths", action="store_true",
                   help="print resolved source/work/build/prefix/deps paths and exit")
    g.add_argument("--list-languages", action="store_true", help="list front ends and exit")
    g.add_argument("--check-only", action="store_true", help="only verify an existing install")
    g.add_argument("--relocate-only", action="store_true",
                   help="only rewrite macOS install names in an existing install")
    g.add_argument("--configure-only", action="store_true", help="configure and stop")
    g.add_argument("--package", nargs="?", const="", metavar="FILE",
                   help="create a binary tarball after install")
    g.add_argument("--package-only", action="store_true", help="only package an existing install")
    g.add_argument("--dry-run", action="store_true", help="show planned actions, do nothing")
    g.add_argument("--quiet", action="store_true")
    g.add_argument("--verbose", action="store_true")
    g.add_argument("--color", choices=("auto", "always", "never"), default="auto")
    return p


# ---------------------------------------------------------------------------
# Main.
# ---------------------------------------------------------------------------

def static_link_works(cc, cxx, log):
    """Return True only if both a static C and a static C++ program can link."""
    with tempfile.TemporaryDirectory(prefix="gcc-static-probe-") as tmp:
        tmp = Path(tmp)
        c = tmp / "t.c"
        c.write_text("int main(void){return 0;}\n")
        if subprocess.run([cc, "-static", str(c), "-o", str(tmp / "tc")],
                          stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode != 0:
            log.info("system static C link: unavailable")
            return False
        ccpp = tmp / "t.cc"
        ccpp.write_text("#include <iostream>\n"
                        "int main(){std::cout << 0; return 0;}\n")
        if subprocess.run([cxx, "-static", str(ccpp), "-o", str(tmp / "tcxx")],
                          stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode != 0:
            log.info("system static C++ link: unavailable")
            return False
        log.info("system static C/C++ links: available")
        return True


def resolve_root(args):
    script_dir = Path(__file__).resolve().parent
    candidates = [Path(args.source)] if args.source else []
    candidates += [script_dir / "gcc-darwin-arm64", script_dir,
                   script_dir.parent / "gcc-darwin-arm64"]
    for cand in candidates:
        cand = cand.resolve()
        for probe in (cand, cand / "gcc-darwin-arm64"):
            if (probe / "configure").is_file() and (probe / "gcc" / "BASE-VER").is_file():
                return probe
    raise BuildError("GCC source tree not found; use --source DIR")


def safe_rmtree(path, log, force=False):
    path = Path(path).resolve()
    if not path.exists():
        return
    if path == Path("/") or len(path.parts) < 3:
        raise BuildError("refusing to remove %s" % path)
    marker = path / ".buildpy-owned"
    if not marker.exists() and not force:
        raise BuildError("refusing to remove %s (not created by build.py); use --force" % path)
    log.warn("removing %s" % path)
    shutil.rmtree(str(path))


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.library_linkage == "static":
        args.shared = False
        args.static = True
        args.jit = False
        args.libgdiagnostics = False
        args.plugins = "no"
    log = Logger(args.log_file, color=args.color, quiet=args.quiet, verbose=args.verbose)
    started = time.time()
    try:
        script_dir = Path(__file__).resolve().parent
        source = resolve_root(args)
        root = script_dir
        work_root = Path(args.work_dir).resolve() if args.work_dir else root
        paths = {
            "root": root,
            "work": work_root,
            "source": source,
            "build": Path(args.build_dir).resolve() if args.build_dir
                     else work_root / "build",
            "prefix": Path(args.prefix).resolve() if args.prefix
                      else work_root / "install",
            "deps": Path(args.deps_dir).resolve() if args.deps_dir
                    else work_root / "_deps",
            "darwin": platform.system() == "Darwin",
        }
        if args.show_paths:
            log.info("source     : %s" % paths["source"])
            log.info("script dir : %s" % paths["root"])
            log.info("work dir   : %s" % paths["work"])
            log.info("build dir  : %s" % paths["build"])
            log.info("prefix     : %s" % paths["prefix"])
            log.info("deps dir   : %s" % paths["deps"])
            return 0
        if not args.log_file:
            paths["build"].mkdir(parents=True, exist_ok=True)
            log.close()
            log = Logger(paths["build"] / "build.log", color=args.color,
                         quiet=args.quiet, verbose=args.verbose)
        paths["sdk"] = Path(args.sdk).resolve() if args.sdk else find_macos_sdk(log)
        if args.sdk and not Path(args.sdk).is_dir():
            raise BuildError("--sdk path does not exist: %s" % args.sdk)

        triple = detect_triple(source)
        paths["build_triple"] = args.build_triple or triple
        paths["host_triple"] = args.host_triple or triple
        paths["target_triple"] = args.target_triple or paths["host_triple"]
        paths["native"] = (paths["build_triple"] == paths["host_triple"] == paths["target_triple"])
        paths["triple"] = paths["target_triple"]
        paths["make"] = find_make(log)
        paths["cc"] = find_compiler("cc", args.cc, log)
        paths["cxx"] = find_compiler("cxx", args.cxx, log)
        if args.jobs and args.jobs > 0:
            jobs = args.jobs
        else:
            jobs = os.cpu_count() or 4
        paths["jobs"] = jobs

        log.info(log._c("build.py %s" % SCRIPT_VERSION, Color.BOLD))
        log.info("  source : %s" % source)
        log.info("  build  : %s" % paths["build"])
        log.info("  prefix : %s" % paths["prefix"])
        log.info("  target : %s" % paths["target_triple"])
        log.info("  jobs   : %d" % jobs)
        if args.library_linkage == "static":
            log.info("  libs   : static only (JIT/libgdiagnostics/plugins disabled)")

        if args.doctor:
            doctor(paths, args, log)
            return 0
        if args.list_languages:
            list_languages(source, log)
            return 0

        if args.distclean:
            for key in ("build", "deps", "prefix"):
                safe_rmtree(paths[key], log, force=args.force)
            log.ok("distclean complete")
            return 0
        if args.clean:
            safe_rmtree(paths["build"], log, force=args.force)

        if args.relocate_only:
            relocate_macos(paths["prefix"], log, Runner(log, args.dry_run))
            return 0
        if args.check_only:
            verifier = Verifier(args, paths, log, Runner(log, args.dry_run), None)
            return 0 if verifier.run() else 1
        if args.package_only:
            package_install(paths["prefix"], args.package or
                            (paths["work"] / ("gcc-%s-%s.tar.gz" %
                                              (source.name, time.strftime("%Y%m%d")))),
                            log, Runner(log, args.dry_run))
            return 0

        # ---- dry run ----
        if args.dry_run:
            plan_dep = paths["deps"] / "prefix"
            if not (plan_dep / "lib" / "libgmp.a").is_file():
                plan_dep = None
            plan = GccBuilder(args, paths, log, Runner(log, True), plan_dep, [])
            log.step("dry-run plan")
            log.info("  configure: %s" % " ".join(shlex.quote(x) for x in plan.configure_args()))
            log.info("  build    : %s -j%d" % (paths["make"], jobs))
            log.info("  install  : %s" % paths["prefix"])
            log.info("no files were changed")
            return 0

        # ---- dependencies ----
        deps_mode = "none" if args.skip_deps else args.deps
        if args.rebuild_deps:
            safe_rmtree(paths["deps"], log, force=True)
        lang_spec = (args.languages or "all").strip()
        want_cobol = lang_spec == "all" or "cobol" in [x.strip() for x in re.split(r"[,\s]+", lang_spec)]
        deps_list = list(DEPS)
        if want_cobol:
            deps_list.append(LIBXML2_DEP)
            if args.static_host and platform.system() != "Darwin":
                deps_list.append(LIBICONV_DEP)
        downloader = Downloader(
            log, paths["deps"] / "downloads",
            tarball_dir=args.tarball_dir,
            retries=max(1, args.retries), insecure=args.insecure,
            offline=args.offline, verify=not args.no_verify_downloads,
            dry_run=args.dry_run,
        )
        dep_prefix = None
        if deps_mode != "none":
            dm = DependencyManager(
                paths["deps"], paths["deps"] / "prefix", log,
                Runner(log, args.dry_run), paths["cc"], paths["cxx"], jobs,
                args.dep_cflags, downloader, mode=deps_mode, deps=deps_list)
            if args.download_only:
                dm.download_all()
                log.ok("download-only requested; stopping")
                return 0
            dep_prefix = dm.ensure_all()
        else:
            log.warn("third-party dependency build disabled (--deps=none)")

        if args.download_only:
            log.ok("download-only requested; stopping")
            return 0
        if args.deps_only:
            log.ok("deps-only requested; stopping")
            return 0
        if args.dry_run:
            log.info("dry-run: planned build is complete; stopping")
            return 0

        # ---- optional private static runtime (glibc) ----
        paths["static_runtime"] = None
        if args.static_host and not paths["darwin"]:
            if args.static_runtime == "source":
                log.info("source static runtime: bootstrap allowed; "
                         "stage1 remains dynamic and final stages use the "
                         "private glibc")
            if args.static_runtime == "auto":
                # Probe system static C and C++ links.  If either is missing,
                # switch to the source-built glibc runtime.
                if not static_link_works(paths["cc"], paths["cxx"], log):
                    log.warn("system static libc/libstdc++ not usable; "
                             "switching to source-built static glibc")
                    args.static_runtime = "source"
            if args.static_runtime == "source":
                if platform.system() != "Linux":
                    raise BuildError("--static-runtime=source currently supports "
                                     "glibc Linux hosts only")
                rt = StaticRuntimeManager(paths["build"], log, Runner(log),
                                          paths["cc"], paths["cxx"], jobs,
                                          downloader)
                paths["static_runtime"] = str(rt.ensure())

        # ---- stage0 compilers for Ada / D / Rust ----
        stage0 = None
        need_config = ((not (paths["build"] / "config.status").exists())
                       or args.reconfigure)
        if (args.bootstrap_tools != "none" and need_config
                and not args.dry_run):
            stage0 = Stage0Manager(
                Path(args.stage0_dir).resolve() if args.stage0_dir
                else paths["deps"] / "stage0",
                log, downloader, paths["host_triple"], paths["sdk"], args)
            stage0.ensure()
            if stage0.cc and not args.cc:
                paths["cc"] = stage0.cc
            if stage0.cxx and not args.cxx:
                paths["cxx"] = stage0.cxx

        # ---- GCC ----
        builder = GccBuilder(args, paths, log, Runner(log), dep_prefix,
                             [d["name"] for d in deps_list], stage0=stage0)
        # tool wrappers must exist before configure
        make_tool_wrappers(paths["build"], [paths["build_triple"],
                                            paths["host_triple"],
                                            paths["target_triple"]], log)
        builder.configure()
        if args.configure_only:
            log.ok("configure-only requested; stopping")
            return 0
        builder.prepare_subdirs()
        builder.make()
        builder.install()
        if paths["darwin"] and not args.no_relocate and not args.no_install:
            relocate_macos(paths["prefix"], log, Runner(log))

        # ---- verify ----
        if args.no_install:
            log.warn("--no-install: skipping verification and packaging")
            verifier = Verifier(args, paths, log, Runner(log), dep_prefix)
            ok = True
        else:
            verifier = Verifier(args, paths, log, Runner(log), dep_prefix)
            ok = verifier.run()

        # ---- summary ----
        summary = {
            "script_version": SCRIPT_VERSION,
            "gcc_version": builder.gcc_version(),
            "target": paths["target_triple"],
            "host": paths["host_triple"],
            "build": paths["build_triple"],
            "prefix": str(paths["prefix"]),
            "languages": builder.configured_languages(),
            "bootstrap": builder.bootstrap_enabled(),
            "shared": args.shared,
            "static": args.static,
            "jobs": jobs,
            "verification": "passed" if ok else "failed",
            "warnings": verifier.warnings,
            "failures": verifier.failures,
            "elapsed_seconds": int(time.time() - started),
            "finished": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        try:
            (paths["build"] / "build-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        except OSError:
            pass

        # ---- package ----
        if args.package is not None:
            if args.no_install:
                raise BuildError("--package cannot be used together with --no-install")
            name = args.package
            if not name:
                name = str(paths["work"] / ("gcc-%s-%s-%s.tar.gz" %
                                            (builder.gcc_version(),
                                             paths["target_triple"],
                                             time.strftime("%Y%m%d"))))
            package_install(paths["prefix"], name, log, Runner(log))

        log.step("done in %s" % _fmt_duration(time.time() - started))
        log.info("  compiler : %s/bin/gcc" % paths["prefix"])
        log.info("  log      : %s" % log.logfile)
        if not ok:
            log.error("build completed but verification reported problems")
            return 1
        log.ok("GCC %s is ready at %s" % (builder.gcc_version(), paths["prefix"]))
        return 0
    except BuildError as exc:
        log.error(str(exc))
        if log.logfile:
            log.error("see log: %s" % log.logfile)
        return 1
    except KeyboardInterrupt:
        log.error("interrupted by user")
        return 130
    finally:
        log.close()


if __name__ == "__main__":
    sys.exit(main())
