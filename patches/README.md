# GCC source patches

These patches are applied by `build.py` to the GCC source tree before
configuration. They are intentionally small and platform/feature scoped.

| Patch | Applied when | Purpose |
| --- | --- | --- |
| `0001-gcc-makefile-libgdiagnostics-static.patch` | libgdiagnostics enabled | Remove the bad `$(LIBS)` prerequisite, filter static flags from the shared library link, and link `sarif-replay` against a static `libgdiagnostics.a`. |
| `0002-gcc-jit-shared-link-flags.patch` | JIT enabled | Do not pass `-static` to the `libgccjit` shared link. |
| `0003-gcc-m2-host-link-libs.patch` | static host, Modula-2, ELF target | Add the C++ runtime libraries to the `m2/pge` and `m2/mc` host links. |
| `0004-gcc-go-split-stack.patch` | x86_64/i386 Linux | Gate gccgo's `-fsplit-stack` on the libc SSP capability macros. |
| `0005-gcc-aarch64-linux-asm-hooks.patch` | aarch64 Linux | Define the missing unaligned assembler hooks. |
| `0006-libgcobol-static-iconv.patch` | static host, COBOL, Linux, static libiconv | Map COBOL's iconv calls to GNU libiconv for static linking. |
| `0007-gnattools-libiconv.patch` | static host, Ada, Linux, static libiconv | Add `-liconv` to the gnattools link libraries. |
| `0008-darwin-static-libgcc-rules.patch` | Darwin, static-only libraries, aarch64 | Add the shared emutls/heap-trampoline object rules required by Darwin's libgcc extra parts. |
| `0009-libgrust-cargo-variable.patch` | Rust enabled | Use the configured `CARGO` path instead of relying on the caller's PATH. |
| `0010-gcc-rust-cargo-variable.patch` | Rust enabled | Use the configured `CARGO` path for the Rust frontend's ffi-polonius build. |

The driver also patches generated Makefiles after `configure`. Those edits
contain absolute build paths and dependency locations, so they are not stored
as portable GCC patches.
