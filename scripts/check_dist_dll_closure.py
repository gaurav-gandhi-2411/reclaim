from __future__ import annotations

# check_dist_dll_closure.py -- build gate: every MSVC runtime DLL a shipped binary imports must
# be shipped at the dist root.
#
# Why this exists (2026-09-23): the first build with the named test-suite allow-list shipped
# winrt's vendored msvcp140.dll (14.29) at the dist root and no msvcp140_1.dll at all.
# onnxruntime_pybind11_state.pyd imports both, so the loader paired the app-local 14.29
# msvcp140 with System32's 14.50 msvcp140_1, and `import onnxruntime` failed inside the frozen
# exe. The previous build had carried a matching 14.50 pair only by accident of Nuitka's DLL
# resolution order. Nothing in the build noticed: the failure surfaced only at runtime, as an AI
# track skipped with "onnxruntime isn't installed", and only packaging/test_packaged_serve.ps1
# caught it. This gate makes the same class of gap fail the build instead: it reads the PE
# import table of every .pyd/.dll/.exe in the dist and fails if any imported VC runtime DLL is
# missing from the dist root.
#
# Why the root and not "anywhere in the dist": a clean Windows machine without the VC++
# redistributable has no System32 fallback, and a copy inside some package's *.libs directory is
# only on the search path after that package's own __init__ has run (delvewheel's
# os.add_dll_directory), so whether it resolves depends on import order.
#
# Scope (house rule 85a): VC runtime DLLs only. OS DLLs (kernel32, api-ms-win-*, ...) are the
# OS's job, and non-runtime third-party DLLs are left to Nuitka's own dependency walk. Version
# consistency across the root runtime DLLs is checked by build_installer.ps1 (FileVersion needs
# the version resource, which this stdlib-only script doesn't parse). The frozen smoke test is
# still the end-to-end backstop.
#
# Usage: python scripts/check_dist_dll_closure.py packaging/build/entry_point.dist
import argparse
import re
import struct
import sys
from pathlib import Path

VC_RUNTIME_RE = re.compile(
    r"^(msvcp140(_1|_2|_atomic_wait|_codecvt_ids)?|vcruntime140(_1|_threads)?|vcomp140|concrt140)"
    r"\.dll$",
    re.IGNORECASE,
)
_BINARY_SUFFIXES = frozenset({".pyd", ".dll", ".exe"})


class PEFormatError(ValueError):
    pass


def pe_imports(data: bytes) -> list[str]:
    """DLL names from a PE file's import directory (PE32 and PE32+). Raises PEFormatError on
    anything that doesn't parse, and the caller treats that as a failure, never as "no imports"."""
    if data[:2] != b"MZ":
        raise PEFormatError("no MZ header")
    (pe_off,) = struct.unpack_from("<I", data, 0x3C)
    if data[pe_off : pe_off + 4] != b"PE\0\0":
        raise PEFormatError("no PE signature")
    coff = pe_off + 4
    (num_sections,) = struct.unpack_from("<H", data, coff + 2)
    (opt_size,) = struct.unpack_from("<H", data, coff + 16)
    opt = coff + 20
    (magic,) = struct.unpack_from("<H", data, opt)
    if magic == 0x10B:
        dd_off = opt + 96
    elif magic == 0x20B:
        dd_off = opt + 112
    else:
        raise PEFormatError(f"unknown optional-header magic {magic:#x}")
    import_rva, import_size = struct.unpack_from("<II", data, dd_off + 8)
    if import_rva == 0 or import_size == 0:
        return []
    sections = []
    sec = opt + opt_size
    for i in range(num_sections):
        base = sec + 40 * i
        vsize, vaddr, rawsize, rawptr = struct.unpack_from("<IIII", data, base + 8)
        sections.append((vaddr, max(vsize, rawsize), rawptr))

    def rva_to_off(rva: int) -> int:
        for vaddr, size, rawptr in sections:
            if vaddr <= rva < vaddr + size:
                return int(rawptr + (rva - vaddr))
        raise PEFormatError(f"RVA {rva:#x} not in any section")

    names: list[str] = []
    desc = rva_to_off(import_rva)
    while True:
        fields = struct.unpack_from("<IIIII", data, desc)
        if not any(fields):
            break
        name_off = rva_to_off(fields[3])
        end = data.index(b"\0", name_off)
        names.append(data[name_off:end].decode("ascii", errors="replace"))
        desc += 20
    return names


def check(dist: Path) -> tuple[list[str], list[str]]:
    """Returns (problems, unparsable). A problem is a binary importing a VC runtime DLL that
    isn't at the dist root."""
    root_names = {p.name.lower() for p in dist.iterdir() if p.is_file()}
    problems: list[str] = []
    unparsable: list[str] = []
    for binary in sorted(dist.rglob("*")):
        if binary.suffix.lower() not in _BINARY_SUFFIXES or not binary.is_file():
            continue
        try:
            imports = pe_imports(binary.read_bytes())
        except (PEFormatError, struct.error, ValueError) as exc:
            unparsable.append(f"{binary.relative_to(dist)}: {exc}")
            continue
        for name in imports:
            if VC_RUNTIME_RE.match(name) and name.lower() not in root_names:
                problems.append(
                    f"{binary.relative_to(dist)} imports {name}, which is not at the dist root"
                )
    return problems, unparsable


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("dist", type=Path)
    args = ap.parse_args(argv)
    if not args.dist.is_dir():
        print(f"FAIL: dist dir not found: {args.dist}", file=sys.stderr)
        return 1
    problems, unparsable = check(args.dist)
    for p in problems:
        print(f"MISSING: {p}", file=sys.stderr)
    for u in unparsable:
        print(f"UNPARSABLE: {u}", file=sys.stderr)
    if problems or unparsable:
        print(
            f"FAIL: {len(problems)} missing VC runtime import(s), {len(unparsable)} unparsable",
            file=sys.stderr,
        )
        return 1
    print("OK: every VC runtime DLL imported by a shipped binary is at the dist root")
    return 0


if __name__ == "__main__":
    sys.exit(main())
