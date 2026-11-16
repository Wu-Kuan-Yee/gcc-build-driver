# Portable GCC build driver

`build.py` is a self-contained Python build driver for GCC. It provisions the
third-party libraries GCC needs, configures a full-featured compiler, runs the
bootstrap stages, installs the result, and verifies the produced toolchain.

The script is intentionally self-contained: besides Python, a host C/C++
compiler, GNU make, and the usual POSIX build tools, it fetches and builds its
own dependencies from source. The resulting host compiler is linked against
static libraries by default on Linux and BSD, while macOS and Windows keep
their native system-library linkage.

## Features

- All buildable GCC languages by default, including C, C++, Ada, D, Fortran,
  Go, Modula-2, Objective-C, Objective-C++, Rust, COBOL, and Algol 68.
- Full bootstrap (`stage1 -> stage2 -> stage3`) on native builds, with an
  option to build a single stage for a fast turnaround.
- Shared and static runtime libraries by default, plus Graphite/ISL, LTO,
  zstd-compressed debug info, JIT, libgdiagnostics, OpenMP, and sanitizers.
- Automatic source builds of GMP, MPFR, MPC, ISL, zstd, libxml2, and libiconv
  with checksum verification, resumable downloads, mirror fallback, and an
  offline cache.
- Stage-0 toolchain provisioning for self-hosted front ends: GNAT for Ada,
  GDC for D, and Rust toolchains for gccrs.
- Optional private static glibc and source-built `libstdc++`/`libgcc` for a
  host compiler that does not depend on the distribution's static archives.
- Binary verification with `otool`, `ldd`, and `readelf`; smoke tests for
  every enabled language; optional relocatable packages.
- Cross-platform path handling for macOS, Linux, the BSDs, and MSYS2/MinGW.

## Requirements

- Python 3.8 or newer.
- A working host C and C++ compiler.
- GNU make.
- Common build tools: `tar`, `xz`, `bzip2`, `sed`, `grep`, `awk`, `m4`,
  `bison`, and `flex`.
- Platform SDK/toolchain:
  - macOS: Xcode Command Line Tools.
  - Linux: the distribution's development packages.
  - FreeBSD/OpenBSD/NetBSD: the base toolchain and `gmake`/`bmake` as needed.
  - Windows: MSYS2/MinGW with a POSIX shell and GNU make.

Only the host compiler, Python, make, and system tools are required initially.
The build driver downloads and builds everything else.

## Quick start

```sh
git clone <repository-url> build-gcc
cd build-gcc

./build.py --source /path/to/gcc -j 8
```

If the GCC source tree is placed at the default location used by the script,
`--source` may be omitted. Use `--show-paths` to see the resolved source,
build, dependency, and installation directories before starting.

A fast development build without stage2/stage3:

```sh
./build.py --source /path/to/gcc --no-bootstrap -j 8
```

A fully static host toolchain on Linux:

```sh
./build.py --source /path/to/gcc --bootstrap=yes -j 8
```

A host toolchain that also uses a private static glibc and source-built
`libstdc++`/`libgcc`:

```sh
./build.py --source /path/to/gcc \
  --bootstrap=yes \
  --static-runtime=source \
  -j 8
```

Install into a custom prefix:

```sh
./build.py --source /path/to/gcc \
  --build-dir /data/gcc-build \
  --deps-dir /data/gcc-deps \
  --prefix /data/gcc \
  -j 8
```

## Useful options

| Option | Purpose |
| --- | --- |
| `--source DIR` | GCC source tree. |
| `--work-dir DIR` | Base directory for the default build, deps, and install paths. |
| `--build-dir DIR` | Explicit build directory. |
| `--deps-dir DIR` | Explicit dependency cache. |
| `--prefix DIR` | Installation prefix. |
| `--languages LIST` | Language list or `all` (default). |
| `--bootstrap {auto,yes,no}` | Bootstrap policy. |
| `--static-host` | Static host executables where supported. |
| `--static-runtime {auto,system,source}` | Source of static libc/crt. |
| `--library-linkage {both,static}` | Target runtime libraries; `both` is the default. `static` installs only target static archives. On macOS it also disables shared-only components (JIT, libgdiagnostics, plugins, sanitizers) and uses static libgcc for Ada. |
| `--gdc-seed {auto,modern,legacy,system}` | D front-end bootstrap seed. |
| `--no-gas-base64` | Avoid `.base64` for distribution to older binutils. |
| `--package [FILE]` | Create a relocatable archive. |
| `--doctor` | Print an environment diagnosis. |
| `--show-paths` | Print resolved paths without building. |

Run `./build.py --help` for the complete option list.

## Output

The default layout, relative to the directory containing `build.py`, is:

```text
build/          build tree and build.log
_deps/          downloaded and built third-party dependencies
install/        installed toolchain
```

Override these with `--work-dir`, `--build-dir`, `--deps-dir`, and `--prefix`.
The build log is written to `<build-dir>/build.log` and a machine-readable
summary to `<build-dir>/build-summary.json`.

## Cross-platform notes

- Paths may be absolute or relative; explicit relative paths are resolved
  against the current working directory.
- macOS builds use the selected SDK and preserve the platform's native
  dynamic system libraries. With `--library-linkage=static`, host tools are
  still linked against the macOS system libraries, while target libraries are
  installed as static archives only.
- Linux and BSD builds statically link host executables by default. Pass
  `--no-static-host` to keep them dynamically linked.
- Windows builds are supported through MSYS2/MinGW; use the matching target
  triple and POSIX tools.
- Generated assembly uses `.base64` only when the build assembler supports it.
  Use `--no-gas-base64` when distributing binaries to systems with older
  binutils.

## Verification

The driver checks every installed host executable and reports its linkage,
inventories static and shared libraries, probes optional compiler features,
and runs small programs for each enabled language. A build is considered
successful only when these checks pass.

## Repository layout

```text
build.py        build driver
patches/        GCC source compatibility patches
README.md       this file
.gitignore      generated files and local toolchains
.gitattributes  cross-platform line-ending rules
```

The files under `patches/` are applied conditionally before GCC configure.
They contain only GCC source changes (link rules and platform fixes); the
driver still patches generated Makefiles after configure when absolute build
paths are required.

No submodules or generated binaries are tracked.
