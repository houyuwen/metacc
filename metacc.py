#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: Apache-2.0
"""metacc.py -- METACC_TABLE / METACC_TABLE_ITEM static-array code generator.

Collects scattered METACC_TABLE_ITEM annotations across translation units and
emits a sorted const array + count for each METACC_TABLE declaration.

Backed by libclang.  Supports C integer-suffix stripping for sort keys and
transitive-include scanning so that tables declared in nested headers are
discovered reliably.

Copyright (c) 2026 houyuwen.
"""
import argparse
import concurrent.futures
import hashlib
import json
import os
import pathlib
import re
import shlex
import subprocess
import sys
from datetime import datetime

# ---------------------------------------------------------------------------
# libclang bindings (official pip package or system clang)
# ---------------------------------------------------------------------------
try:
    from libclang import cindex as clang
except ModuleNotFoundError:
    try:
        from clang import cindex as clang
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError(
            "No module named 'libclang' or 'clang'.\n"
            "Please install the Python libclang bindings in the metacc virtualenv:\n"
            "    pip install libclang>=18.0.0"
        ) from exc

# ---------------------------------------------------------------------------
# Explicitly set libclang native library path for Nuitka-packaged and
# source-parity scenarios.  Prefer the bundled copy in the script directory
# (or release/ when running the standalone binary) so that the exact same
# libclang version is used regardless of how metacc is invoked.
# ---------------------------------------------------------------------------
def _configure_libclang_path() -> None:
    """Point clang.cindex at the libclang.so shipped alongside this script."""
    import ctypes.util

    _here = pathlib.Path(__file__).resolve().parent
    _candidates: list[pathlib.Path] = [
        _here,
        _here / "release" / "metacc",  # Nuitka-packaged output
    ]
    _venv_native = _here / "venv" / "lib"
    if _venv_native.exists():
        for _p in _venv_native.glob("python3.*/site-packages/clang/native"):
            _candidates.append(_p)

    _names = (
        "libclang.so", "libclang-18.so.18", "libclang.so.18",
        "libclang_native.so",
    )
    for _d in _candidates:
        for _n in _names:
            _cand = (_d / _n).resolve()
            if _cand.is_file():
                clang.Config.set_library_file(str(_cand))
                return

    # Fallback: let the system ctypes finder resolve it.
    _sys_lib = ctypes.util.find_library("clang")
    if _sys_lib:
        clang.Config.set_library_file(_sys_lib)


_configure_libclang_path()
del _configure_libclang_path

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------
CACHE_VERSION = 9  # bumped: structs removed, transitive-include scanning added

METACC_MACROS = {"METACC_TABLE", "METACC_TABLE_ITEM"}
METACC_TEXT_RE = re.compile(r"\b(?:METACC_TABLE|METACC_TABLE_ITEM)\b")
PROJECT_INCLUDE_RE = re.compile(
    r'^\s*#\s*include\s*["<]([^">]+)[">]', re.MULTILINE
)


# ===========================================================================
# path / project-root helpers
# ===========================================================================

def _metacc_dir() -> pathlib.Path:
    """Directory containing the metacc package root (metacc.h location).

    The metacc root is always the directory that directly contains this file,
    whether running from source (``tools/metacc/metacc.py``) or from a
    Nuitka-packaged binary (``<any>/metacc/metacc``).
    """
    return pathlib.Path(__file__).resolve().parent


METACC_DIR = _metacc_dir()
METACC_H_NAME = "metacc.h"


def ensure_metacc_header_path() -> pathlib.Path:
    candidate = (METACC_DIR / METACC_H_NAME).resolve()
    if not candidate.exists():
        raise FileNotFoundError(
            f"[metacc] error: Core release header missing at: {candidate}. "
            f"Please check deployment."
        )
    return candidate


def resolve_project_root_arg(
    project_root_arg: str,
    cwd: pathlib.Path | None = None,
) -> tuple[pathlib.Path, pathlib.Path, list[str]]:
    raw = pathlib.Path(project_root_arg)
    cwd = (cwd or pathlib.Path.cwd()).resolve()
    warnings: list[str] = []

    if raw.is_absolute():
        raw_root = raw.resolve()
    else:
        anchors = [cwd, METACC_DIR.parent, METACC_DIR]
        resolved = None
        for anchor in anchors:
            candidate = (anchor / raw).resolve()
            if candidate.exists():
                resolved = candidate
                if anchor is not anchors[0]:
                    name = (
                        "metacc dir"
                        if anchor == METACC_DIR
                        else "metacc package parent"
                    )
                    warnings.append(
                        f"[metacc] warning: --project-root "
                        f"'{project_root_arg}' resolved relative to "
                        f"{name}: {candidate}"
                    )
                break
        raw_root = (
            resolved if resolved is not None else (cwd / raw).resolve()
        )

    if raw_root.exists() and raw_root.is_file():
        parent = raw_root.parent
        warnings.append(
            f"[metacc] warning: --project-root points to a file "
            f"({raw_root}); using parent dir: {parent}"
        )
        return parent, raw_root, warnings

    if project_root_arg in (".", "./metacc") or raw_root == METACC_DIR:
        candidate_parent = METACC_DIR.parent
        if (candidate_parent / "CMakeLists.txt").exists() or (
            candidate_parent / "compile_commands.json"
        ).exists():
            warnings.append(
                f"[metacc] warning: --project-root '{project_root_arg}' "
                f"interpreted as project root: {candidate_parent}"
            )
            return candidate_parent, candidate_parent, warnings

    return raw_root, raw_root, warnings


def resolve_existing_input_path(
    path_arg: str, anchors: list[pathlib.Path]
) -> pathlib.Path:
    path = pathlib.Path(path_arg)
    if path.is_absolute():
        return path.resolve()
    for anchor in anchors:
        candidate = (anchor / path).resolve()
        if candidate.exists():
            return candidate
    return path.resolve()


def resolve_output_path(
    path_arg: str, project_root: pathlib.Path
) -> pathlib.Path:
    path = pathlib.Path(path_arg)
    if path.is_absolute():
        return path.resolve()
    return (project_root / path).resolve()


def default_cache_dir_for(project_root: pathlib.Path) -> pathlib.Path:
    return (project_root / "build" / ".metacc" / ".cache").resolve()


def default_generated_root_for(project_root: pathlib.Path) -> pathlib.Path:
    return (project_root / "build" / "metacc_files").resolve()


def resolve_project_include_path(
    include_name: str,
    including_file: pathlib.Path,
    project_root: pathlib.Path,
) -> pathlib.Path | None:
    """Resolve a #include path relative to the including file or project root."""
    candidates = [
        (including_file.parent / include_name).resolve(),
        (project_root / include_name).resolve(),
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


# ===========================================================================
# text I/O
# ===========================================================================

def read_text_maybe(path: pathlib.Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


# ===========================================================================
# macro-argument parser
# ===========================================================================

def split_top_level_commas(s: str) -> list:
    """Split s on commas that are not nested inside (), [] or {}."""
    out, cur = [], []
    depth_p = depth_b = depth_c = 0
    in_quote = False
    quote_char = None
    escape = False

    for ch in s:
        if escape:
            cur.append(ch)
            escape = False
            continue
        if ch == "\\":
            cur.append(ch)
            escape = True
            continue
        if in_quote:
            cur.append(ch)
            if ch == quote_char:
                in_quote = False
                quote_char = None
            continue
        if ch in ('"', "'"):
            in_quote = True
            quote_char = ch
            cur.append(ch)
            continue
        if ch == "," and not depth_p and not depth_b and not depth_c:
            out.append("".join(cur).strip())
            cur = []
            continue
        if ch == "(":
            depth_p += 1
        elif ch == ")" and depth_p:
            depth_p -= 1
        elif ch == "[":
            depth_b += 1
        elif ch == "]" and depth_b:
            depth_b -= 1
        elif ch == "{":
            depth_c += 1
        elif ch == "}" and depth_c:
            depth_c -= 1
        cur.append(ch)

    if cur:
        out.append("".join(cur).strip())
    return [part for part in out if part]


def format_table_item_initializer(payload: str) -> str:
    """Emit a single array element initializer.

    Struct / aggregate rows keep brace wrapping; scalar rows (e.g. function
    pointers) must not, otherwise GCC warns about braces around scalar init.
    """
    if len(split_top_level_commas(payload)) == 1:
        return f"    {payload.strip()},"
    return f"    {{{payload}}},"


def parse_kv_args(args: list) -> dict:
    result: dict = {}
    for arg in args:
        arg = arg.strip()
        if "=" in arg:
            k, _, v = arg.partition("=")
            result[k.strip()] = v.strip()
        elif arg:
            result[arg] = True
    return result


def macro_args(cursor) -> list:
    """Extract comma-separated top-level arguments from a MACRO_INSTANTIATION."""
    tokens = list(cursor.get_tokens())
    collecting = False
    depth = 0
    raw_parts: list[str] = []
    prev_end_col = None

    for tok in tokens:
        sp = tok.spelling
        loc = tok.extent.start

        if not collecting:
            if sp == "(":
                collecting = True
                depth = 1
                prev_end_col = tok.extent.end.column
            continue

        if sp == "(":
            depth += 1
            raw_parts.append(sp)
        elif sp == ")":
            depth -= 1
            if depth == 0:
                break
            raw_parts.append(sp)
        else:
            if prev_end_col is not None and loc.column > prev_end_col:
                raw_parts.append(" ")
            raw_parts.append(sp)

        prev_end_col = tok.extent.end.column

    return split_top_level_commas("".join(raw_parts))


def normalize_function_token(token: str) -> str | None:
    t = token.strip()
    if not t:
        return None
    if t.startswith("&"):
        t = t[1:].strip()
    while t.startswith("(") and t.endswith(")") and len(t) >= 2:
        t = t[1:-1].strip()
    return t or None


# ===========================================================================
# pre-filter: quick text scan before libclang parse
# ===========================================================================

def source_tree_might_contain_metacc_text(
    src_path: pathlib.Path, project_root: pathlib.Path
) -> tuple[bool, list[pathlib.Path]]:
    """Return (True, deps) if *src_path* or any transitively-included header
    contains a METACC_TABLE / METACC_TABLE_ITEM macro reference.

    FIXED: now recursively follows transitive #include chains so that a table
    declared in a nested header (a.c -> b.h -> c.h) is not silently skipped.
    """
    src_path = src_path.resolve()
    project_root = project_root.resolve()

    text = read_text_maybe(src_path)
    if text is None:
        return False, []

    if "METACC_" in text and METACC_TEXT_RE.search(text):
        return True, [src_path]

    deps: list[pathlib.Path] = [src_path]
    visited: set[pathlib.Path] = {src_path}
    queue: list[pathlib.Path] = [src_path]

    while queue:
        current = queue.pop(0)
        current_text = read_text_maybe(current)
        if current_text is None:
            continue
        for include_name in PROJECT_INCLUDE_RE.findall(current_text):
            ip = resolve_project_include_path(
                include_name, current, project_root
            )
            if ip and ip.exists() and ip not in visited:
                visited.add(ip)
                deps.append(ip)
                inc_text = read_text_maybe(ip)
                if (
                    inc_text
                    and "METACC_" in inc_text
                    and METACC_TEXT_RE.search(inc_text)
                ):
                    return True, deps
                queue.append(ip)

    return False, deps


# ===========================================================================
# libclang AST collectors  (only enums + non-static functions are kept;
#                           struct collection was removed with other features)
# ===========================================================================

def collect_enums(tu) -> dict:
    enums: dict = {}

    def _extract_enum_cursor(cur) -> dict:
        return {
            c.spelling: c.enum_value
            for c in cur.get_children()
            if c.kind == clang.CursorKind.ENUM_CONSTANT_DECL
        }

    for cursor in tu.cursor.walk_preorder():
        if cursor.kind == clang.CursorKind.TYPEDEF_DECL:
            for child in cursor.get_children():
                if child.kind == clang.CursorKind.ENUM_DECL:
                    vals = _extract_enum_cursor(child)
                    if vals:
                        enums[cursor.spelling] = vals
        elif (
            cursor.kind == clang.CursorKind.ENUM_DECL and cursor.spelling
        ):
            vals = _extract_enum_cursor(cursor)
            if vals:
                enums[cursor.spelling] = vals
    return enums


def collect_nonstatic_functions(tu) -> dict:
    """Collect prototypes of non-static functions defined in *tu*'s main file.

    FIXED: use os.path.samefile to compare main-file identity robustly
    (handles symlinks and path-format mismatches between tu.spelling and
    loc.file.name).
    """
    main_file = tu.spelling
    protos: dict = {}
    for cursor in tu.cursor.get_children():
        if cursor.kind != clang.CursorKind.FUNCTION_DECL:
            continue
        loc = cursor.location
        if loc.file is None:
            continue
        try:
            if not os.path.samefile(loc.file.name, main_file):
                continue
        except OSError:
            # If either path is inaccessible, fall back to string equality.
            if loc.file.name != main_file:
                continue
        if cursor.storage_class == clang.StorageClass.STATIC:
            continue

        ret = cursor.result_type.spelling
        args_list = []
        for p in cursor.get_arguments():
            args_list.append({
                "type": p.type.spelling,
                "name": p.spelling or f"arg{len(args_list)}",
            })
        params = (
            ", ".join(
                p.type.spelling + (" " + p.spelling if p.spelling else "")
                for p in cursor.get_arguments()
            )
            or "void"
        )
        protos[cursor.spelling] = {
            "decl": f"{ret} {cursor.spelling}({params});",
            "args": args_list,
            "return_type": ret,
        }
    return protos


def collect_function_protos(model, tokens: list[str]) -> list:
    protos = []
    for token in tokens:
        name = normalize_function_token(token)
        if name and name in model.protos:
            meta = model.protos[name]
            decl = meta["decl"] if isinstance(meta, dict) else meta
            if decl not in protos:
                protos.append(decl)
    return protos


def collect_annotations_from_tu(tu, src_path: pathlib.Path) -> list:
    """Walk the AST and extract every METACC_TABLE / METACC_TABLE_ITEM
    macro instantiation that belongs to a project file (not system headers).

    FIXED: current_func is now explicitly reset to None when the walk enters
    a top-level non-function declaration (struct / enum / union), so that a
    stale function pointer cannot be confused with file-scope annotations.
    """
    annotations: list = []
    main = str(src_path.resolve())
    project_files = {main}
    for inc in tu.get_includes():
        inc_path = pathlib.Path(inc.include.name).resolve()
        if not str(inc_path).startswith(("/usr", "/lib")):
            project_files.add(str(inc_path))

    current_func = None
    for cursor in tu.cursor.walk_preorder():
        # -- track enclosing function ----------------------------------
        if cursor.kind == clang.CursorKind.FUNCTION_DECL:
            current_func = cursor
        elif cursor.kind in (
            clang.CursorKind.STRUCT_DECL,
            clang.CursorKind.ENUM_DECL,
            clang.CursorKind.UNION_DECL,
        ):
            # Top-level declaration that is *not* a function – reset.
            if (
                cursor.semantic_parent is not None
                and cursor.semantic_parent.kind
                == clang.CursorKind.TRANSLATION_UNIT
            ):
                current_func = None

        # -- only care about macro instantiations -----------------------
        if cursor.kind != clang.CursorKind.MACRO_INSTANTIATION:
            continue

        kind = cursor.spelling
        if kind not in METACC_MACROS:
            continue

        resolved_args = macro_args(cursor)
        loc = cursor.location
        if loc.file is None:
            continue
        file_path = pathlib.Path(loc.file.name).resolve()
        if str(file_path) not in project_files:
            continue

        func_name = None
        func_start = None
        func_end = None
        if (
            current_func is not None
            and current_func.extent.start.file is not None
            and current_func.extent.start.file.name == loc.file.name
        ):
            if (
                current_func.extent.start.line
                <= loc.line
                <= current_func.extent.end.line
            ):
                func_name = current_func.spelling
                func_start = current_func.extent.start.line
                func_end = current_func.extent.end.line

        annotations.append({
            "kind": kind,
            "args": resolved_args,
            "file": str(file_path),
            "line": loc.line,
            "func": func_name,
            "func_start": func_start,
            "func_end": func_end,
        })
    return annotations


# ===========================================================================
# compiler-flag helpers
# ===========================================================================

_COMPILER_SYSTEM_INCLUDES: dict = {}


def compiler_system_include_dirs(compiler: str) -> list:
    if not compiler:
        return []
    cached = _COMPILER_SYSTEM_INCLUDES.get(compiler)
    if cached is not None:
        return cached
    try:
        proc = subprocess.run(
            [compiler, "-E", "-x", "c", "-", "-v"],
            input="",
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError:
        _COMPILER_SYSTEM_INCLUDES[compiler] = []
        return []
    includes = []
    capture = False
    for line in proc.stderr.splitlines():
        if "search starts here:" in line:
            capture = True
            continue
        if not capture:
            continue
        if "End of search list." in line:
            break
        path = line.strip()
        if not path or path.startswith("("):
            continue
        if path not in includes:
            includes.append(path)
    _COMPILER_SYSTEM_INCLUDES[compiler] = includes
    return includes


def infer_clang_target(compiler: str, raw_args: list[str]) -> str | None:
    for arg in raw_args:
        if arg.startswith("-march="):
            march = arg.split("=", 1)[1].strip().lower()
            if march.startswith("rv32"):
                return "riscv32-unknown-elf"
            if march.startswith("rv64"):
                return "riscv64-unknown-elf"
    compiler_name = pathlib.Path(compiler).name.lower()
    if "riscv" in compiler_name:
        return "riscv32-unknown-elf"
    if "arm-none-eabi" in compiler_name:
        return "arm-none-eabi"
    return None


def sanitize_riscv_march(march: str) -> str:
    parts = march.split("_")
    if len(parts) <= 1:
        return march
    kept = [parts[0]] + [
        part for part in parts[1:] if not part.startswith("xx")
    ]
    return "_".join(kept)


_KEEP_ARG_PREFIXES = (
    "-D", "-U", "-I", "-isystem", "-iquote", "-idirafter",
    "-include", "-imacros", "-std=", "-x", "--target=", "-mabi=",
    "-fpack-struct",
)
_KEEP_ARG_EXACT = {
    "-nostdinc", "-nostdinc++", "-fshort-enums", "-fshort-wchar",
    "-funsigned-char", "-fsigned-char", "-fno-common",
}
_KEEP_ARG_PAIR_FLAGS = {
    "-D", "-U", "-I", "-isystem", "-iquote", "-idirafter",
    "-include", "-imacros", "-x", "--target",
}


def clang_args_from_entry(entry: dict, metacc_h: pathlib.Path) -> list:
    """Convert a compile_commands.json entry into libclang-compatible args."""
    raw = (
        shlex.split(entry["command"])
        if entry.get("command")
        else list(entry.get("arguments", []))
    )
    compiler = raw[0] if raw else ""
    src_file = entry.get("file", "")
    keep: list = []
    skip_next = False
    for arg in raw[1:]:
        if skip_next:
            skip_next = False
            continue
        if arg in ("-c", "-w", "-mdiv"):
            continue
        if arg.startswith("-march="):
            march = arg.split("=", 1)[1].strip()
            keep.append(f"-march={sanitize_riscv_march(march)}")
            continue
        if arg in ("-o", "-MF", "-MT", "-MQ"):
            skip_next = True
            continue
        if arg in ("-MMD", "-MD", "-MP"):
            continue
        if arg in _KEEP_ARG_PAIR_FLAGS:
            keep.append(arg)
            skip_next = True
            continue
        if arg == src_file:
            continue
        if arg in _KEEP_ARG_EXACT:
            keep.append(arg)
            continue
        if any(arg.startswith(p) for p in _KEEP_ARG_PREFIXES):
            keep.append(arg)
            continue
    for inc in compiler_system_include_dirs(compiler):
        keep += ["-isystem", inc]
    target = infer_clang_target(compiler, raw)
    if target:
        keep += [f"--target={target}"]
    keep += [
        "-D__attribute__(x)=",
        "-D__flash=",
        "-D__interrupt=",
        "-D__asm__(x)=",
        "-include",
        str(metacc_h),
    ]
    return keep


# ===========================================================================
# process worker (runs in subprocess via ProcessPoolExecutor)
# ===========================================================================

def _process_file_worker(
    src_str: str,
    entry: dict,
    project_root_str: str,
    metacc_h_str: str,
    generated_root_str: str | None,
) -> dict:
    project_root = pathlib.Path(project_root_str)
    metacc_h = pathlib.Path(metacc_h_str)
    src = pathlib.Path(src_str)

    has_metacc_text, text_deps = source_tree_might_contain_metacc_text(
        src, project_root
    )
    if not has_metacc_text:
        deps = []
        for dep_path in text_deps:
            try:
                st = dep_path.stat()
                deps.append({
                    "path": str(dep_path),
                    "mtime_ns": st.st_mtime_ns,
                    "size": st.st_size,
                })
            except OSError:
                pass
        return {
            "src": src_str,
            "annotations": [],
            "enums": {},
            "protos": {},
            "deps": deps,
            "matched": False,
        }

    index = clang.Index.create()
    args = clang_args_from_entry(entry, metacc_h)

    try:
        tu = index.parse(
            src_str,
            args=args,
            options=clang.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD,
        )
    except Exception as e:
        print(
            f"[metacc] Crash parsing {src_str}: {e}", file=sys.stderr
        )
        return {
            "src": src_str,
            "annotations": [],
            "enums": {},
            "protos": {},
            "deps": [],
            "matched": False,
        }

    if not tu:
        return {
            "src": src_str,
            "annotations": [],
            "enums": {},
            "protos": {},
            "deps": [],
            "matched": False,
        }

    has_fatal_errors = False
    for diag in tu.diagnostics:
        if diag.severity >= clang.Diagnostic.Error:
            has_fatal_errors = True
            print(
                f"[metacc] Clang Parser Error in "
                f"[{src_str}:{diag.location.line}:{diag.location.column}]: "
                f"{diag.spelling}",
                file=sys.stderr,
            )
    if has_fatal_errors:
        print(
            f"[metacc] warning: AST generation for {src.name} might be "
            f"heavily truncated due to compilation issues.",
            file=sys.stderr,
        )

    # Track dependencies (for cache invalidation), including metacc.h itself.
    deps: list = []
    try:
        st = metacc_h.stat()
        deps.append({
            "path": str(metacc_h),
            "mtime_ns": st.st_mtime_ns,
            "size": st.st_size,
        })
    except OSError:
        pass
    for inc in tu.get_includes():
        p = pathlib.Path(inc.include.name)
        try:
            st = p.stat()
            deps.append({
                "path": str(p),
                "mtime_ns": st.st_mtime_ns,
                "size": st.st_size,
            })
        except OSError:
            pass
    try:
        st = src.stat()
        deps.append({
            "path": str(src),
            "mtime_ns": st.st_mtime_ns,
            "size": st.st_size,
        })
    except OSError:
        pass

    return {
        "src": src_str,
        "annotations": collect_annotations_from_tu(tu, src),
        "enums": collect_enums(tu),
        "protos": collect_nonstatic_functions(tu),
        "deps": deps,
        "matched": True,
    }


# ===========================================================================
# ProjectModel
# ===========================================================================

class ProjectModel:
    def __init__(self, root: pathlib.Path, file_results: list):
        self.root = root
        self.enums: dict = {}
        self.protos: dict = {}
        self.annotations: list = []
        for r in file_results:
            self.enums.update(r.get("enums", {}))
            self.protos.update(r.get("protos", {}))
            for a in r.get("annotations", []):
                self.annotations.append({**a, "src": r["src"]})

    def annotations_of_kind(self, *kinds) -> list:
        return [a for a in self.annotations if a["kind"] in kinds]

    def resolve_enum_value(self, token: str) -> tuple:
        """Return (0, int_value) for numeric/enum tokens, else (1, token).

        FIXED: strips C integer suffixes (u, U, l, L, ll, LL, ul, …) before
        attempting int() conversion so that ``10u`` and ``0xFFUL`` sort as
        numbers rather than strings.
        """
        t = token.strip()
        # Strip common C integer-literal suffixes.
        stripped = re.sub(r"[uUlL]+$", "", t)
        try:
            return (0, int(stripped, 0))
        except ValueError:
            pass
        for vals in self.enums.values():
            if t in vals:
                return (0, vals[t])
        return (1, t)


# ===========================================================================
# project scanner with process-pool + incremental cache
# ===========================================================================

def scan_project(
    project_root: pathlib.Path,
    compile_commands: list,
    cache_dir: pathlib.Path,
    jobs: int,
    generated_root: pathlib.Path | None,
) -> ProjectModel:
    metacc_h = ensure_metacc_header_path()
    entries_by_src: dict = {}
    for entry in compile_commands:
        src = entry.get("file", "")
        if src.endswith((".c", ".h")):
            p = pathlib.Path(src)
            if not p.is_absolute():
                p = (
                    pathlib.Path(
                        entry.get("directory", str(project_root))
                    )
                    / p
                ).resolve()
            # Exclude previously-generated files.
            if generated_root is not None:
                try:
                    p.relative_to(generated_root)
                    continue
                except ValueError:
                    pass
            entries_by_src[str(p)] = entry

    results: list = []
    tasks_to_run: dict = {}

    for src_str, entry in entries_by_src.items():
        obj = {
            "v": CACHE_VERSION,
            "file": src_str,
            "args": entry.get("arguments", [])
            or shlex.split(entry.get("command", "")),
        }
        key = hashlib.sha256(
            json.dumps(obj, sort_keys=True).encode()
        ).hexdigest()
        cp = cache_dir / "entries" / f"{key}.json"
        cp.parent.mkdir(parents=True, exist_ok=True)

        cached = None
        if cp.exists():
            try:
                cached = json.loads(cp.read_text(encoding="utf-8"))
            except Exception:
                pass

        if cached:
            valid = True
            for dep in cached.get("deps", []):
                try:
                    st = pathlib.Path(dep["path"]).stat()
                    if (
                        st.st_mtime_ns != dep["mtime_ns"]
                        or st.st_size != dep["size"]
                    ):
                        valid = False
                        break
                except OSError:
                    valid = False
                    break
            if valid:
                results.append({"src": src_str, **cached})
                continue
        tasks_to_run[src_str] = (entry, cp)

    if tasks_to_run:
        worker_count = max(1, jobs)
        total_tasks = len(tasks_to_run)
        finished_count = 0
        with concurrent.futures.ProcessPoolExecutor(
            max_workers=worker_count
        ) as pool:
            futures = {
                pool.submit(
                    _process_file_worker,
                    src_str,
                    item[0],
                    str(project_root),
                    str(metacc_h),
                    str(generated_root) if generated_root else None,
                ): (src_str, item[1])
                for src_str, item in tasks_to_run.items()
            }
            for fut in concurrent.futures.as_completed(futures):
                src_str, cache_path = futures[fut]
                finished_count += 1
                short_name = pathlib.Path(src_str).name
                print(
                    f"[metacc] [{finished_count}/{total_tasks}] "
                    f"Parsing {short_name}...",
                    flush=True,
                )
                try:
                    res = fut.result()
                    results.append(res)
                    payload = {
                        k: v for k, v in res.items() if k != "src"
                    }
                    tmp = cache_path.with_suffix(
                        f".{os.getpid()}.tmp"
                    )
                    tmp.write_text(
                        json.dumps(payload, ensure_ascii=False),
                        encoding="utf-8",
                    )
                    tmp.replace(cache_path)
                except Exception as e:
                    print(
                        f"[metacc] process error {src_str}: {e}",
                        file=sys.stderr,
                        flush=True,
                    )

    return ProjectModel(project_root, results)


# ===========================================================================
# code-generation utilities
# ===========================================================================

def _write_if_changed(path: pathlib.Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return
    path.write_text(text, encoding="utf-8")


def companion_paths(
    owner_path: pathlib.Path,
    project_root: pathlib.Path,
    generated_root: pathlib.Path | None,
) -> tuple[pathlib.Path, pathlib.Path]:
    owner_abs = owner_path.resolve()
    if generated_root is None:
        return (
            owner_abs.parent / f"metacc_{owner_abs.stem}.h",
            owner_abs.parent / f"metacc_{owner_abs.stem}.c",
        )
    out_h_dir = generated_root / "include"
    out_c_dir = generated_root / "src"
    return (
        out_h_dir / f"metacc_{owner_abs.stem}.h",
        out_c_dir / f"metacc_{owner_abs.stem}.c",
    )


def _render_generated_header(filename: str, body: str) -> str:
    """FIXED: only includes <stdint.h> (required by uint32_t)."""
    return (
        f"/**\n"
        f"*******************************************************************************\n"
        f"* @file   {filename}\n"
        f"* @author houyuwenE@outlook.com\n"
        f"* @brief  Do not edit!\n"
        f"******************************************************************************\n"
        f"* @attention\n"
        f"*\n"
        f"* Copyright (c) {datetime.now().year} houyuwen.\n"
        f"* All rights reserved.\n"
        f"*\n"
        f"* This software is licensed under terms that can be found in the LICENSE file\n"
        f"* in the root directory of this software component.\n"
        f"* If no LICENSE file comes with this software, it is provided AS-IS.\n"
        f"*\n"
        f"******************************************************************************\n"
        f"*/\n"
        f"#pragma once\n\n"
        f"#ifdef __cplusplus\n"
        f"extern \"C\" {{\n"
        f"#endif\n\n"
        f"#include <stdint.h>\n\n"
        f"{body.strip()}\n\n"
        f"#ifdef __cplusplus\n"
        f"}}\n"
        f"#endif\n"
    )


def _render_generated_source(
    filename: str,
    owner_include: str,
    gen_header_include: str,
    body: str,
) -> str:
    return (
        f"/**\n"
        f"*******************************************************************************\n"
        f"* @file   {filename}\n"
        f"* @author houyuwenE@outlook.com\n"
        f"* @brief  Do not edit!\n"
        f"******************************************************************************\n"
        f"* @attention\n"
        f"*\n"
        f"* Copyright (c) {datetime.now().year} houyuwen.\n"
        f"* All rights reserved.\n"
        f"*\n"
        f"* This software is licensed under terms that can be found in the LICENSE file\n"
        f"* in the root directory of this software component.\n"
        f"* If no LICENSE file comes with this software, it is provided AS-IS.\n"
        f"*\n"
        f"******************************************************************************\n"
        f"*/\n"
        f"#include \"{owner_include}\"\n"
        f"#include \"{gen_header_include}\"\n"
        f"#include <string.h>\n"
        f"#include <stdio.h>\n"
        f"#include <stdlib.h>\n\n"
        f"{body.strip()}\n"
    )


def _append_companion_fragment(
    buckets: dict,
    owner_path: pathlib.Path,
    header_lines: list,
    source_lines: list,
):
    bucket = buckets.setdefault(
        str(owner_path),
        {"owner_path": owner_path, "header_parts": [], "source_parts": []},
    )
    if header_lines:
        bucket["header_parts"].append("\n".join(header_lines).rstrip())
    if source_lines:
        bucket["source_parts"].append("\n".join(source_lines).rstrip())


def _flush_companion_fragments(
    buckets: dict,
    project_root: pathlib.Path,
    generated_root: pathlib.Path | None,
):
    for bucket in buckets.values():
        owner_path = bucket["owner_path"]
        h_path, c_path = companion_paths(
            owner_path, project_root, generated_root
        )
        h_text = _render_generated_header(
            h_path.name,
            "\n\n".join(p for p in bucket["header_parts"] if p),
        )
        rel_owner = os.path.relpath(
            owner_path.resolve(), c_path.parent
        ).replace(os.sep, "/")
        rel_generated_h = os.path.relpath(
            h_path.resolve(), c_path.parent
        ).replace(os.sep, "/")
        c_text = _render_generated_source(
            c_path.name,
            rel_owner,
            rel_generated_h,
            "\n\n".join(p for p in bucket["source_parts"] if p),
        )
        _write_if_changed(h_path, h_text)
        _write_if_changed(c_path, c_text)


# ===========================================================================
# METACC_TABLE / METACC_TABLE_ITEM code generator
# ===========================================================================

def run_table(
    model: ProjectModel,
    buckets: dict,
    project_root: pathlib.Path,
    generated_root: pathlib.Path | None,
):
    tables: dict = {}
    for ann in model.annotations_of_kind("METACC_TABLE"):
        args = ann["args"]
        if len(args) < 2:
            continue
        name, type_name = args[0], args[1]
        if name in tables:
            continue
        kv = parse_kv_args(args[2:])
        sort_col_raw = kv.get("sort_col", kv.get("col"))
        sort_col = (
            int(str(sort_col_raw), 0)
            if sort_col_raw is not None
            else None
        )
        sort_desc = (
            str(kv.get("order", "asc")).strip().lower()
            in ("desc", "descending")
        )
        tables[name] = {
            "type": type_name,
            "sort_col": sort_col,
            "sort_desc": sort_desc,
            "owner_src": ann.get("file", ann["src"]),
            "items": [],
        }

    for ann in model.annotations_of_kind("METACC_TABLE_ITEM"):
        args = ann["args"]
        if not args:
            continue
        arr_name = args[0]
        payload = ", ".join(args[1:])
        if arr_name in tables:
            tables[arr_name]["items"].append({
                "payload": payload,
                "src": ann.get("file", ann["src"]),
                "line": ann["line"],
                "protos": collect_function_protos(
                    model, split_top_level_commas(payload)
                ),
            })

    for name, tbl in tables.items():
        owner_path = pathlib.Path(tbl["owner_src"])
        items = tbl["items"]
        sort_col = tbl["sort_col"]
        sort_desc = tbl["sort_desc"]

        if sort_col is None:
            items.sort(
                key=lambda x: (str(x["src"]), int(x["line"]))
            )
        else:
            def _item_sort_key(item):
                parts = [
                    p.strip()
                    for p in split_top_level_commas(item["payload"])
                ]
                key_expr = (
                    parts[sort_col].strip()
                    if sort_col < len(parts)
                    else ""
                )
                return (
                    model.resolve_enum_value(key_expr),
                    key_expr,
                    str(item["src"]),
                    int(item["line"]),
                )

            items.sort(key=_item_sort_key, reverse=sort_desc)

        proto_lines = []
        for item in items:
            for proto in item["protos"]:
                if proto not in proto_lines:
                    proto_lines.append(proto)

        h_lines = [
            f"extern const {tbl['type']} {name}[];",
            f"extern const uint32_t {name}_count;",
        ]
        c_lines = [f"{p.rstrip(';')};" for p in proto_lines]
        if proto_lines:
            c_lines.append("")
        c_lines += (
            [f"const {tbl['type']} {name}[] = {{"]
            + [format_table_item_initializer(it["payload"]) for it in items]
            + ["};", f"const uint32_t {name}_count = {len(items)}u;"]
        )
        _append_companion_fragment(
            buckets, owner_path, h_lines, c_lines
        )


# ===========================================================================
# top-level runner
# ===========================================================================

def run(
    project_root: pathlib.Path,
    compile_commands: list,
    cache_dir: pathlib.Path,
    jobs: int,
    generated_root: pathlib.Path | None,
) -> int:
    model = scan_project(
        project_root, compile_commands, cache_dir, jobs, generated_root
    )

    companion_fragments: dict = {}
    run_table(model, companion_fragments, project_root, generated_root)
    _flush_companion_fragments(
        companion_fragments, project_root, generated_root
    )

    # Detect orphan METACC_TABLE_ITEMs referencing undefined tables.
    table_names = {
        a["args"][0]
        for a in model.annotations_of_kind("METACC_TABLE")
        if a["args"]
    }
    item_table_names = {
        a["args"][0]
        for a in model.annotations_of_kind("METACC_TABLE_ITEM")
        if a["args"]
    }
    orphans = item_table_names - table_names
    for orphan in sorted(orphans):
        print(
            f"[metacc] warning: METACC_TABLE_ITEM references undefined "
            f"METACC_TABLE '{orphan}'",
            file=sys.stderr,
        )

    if orphans:
        print(
            f"[metacc] Completed with {len(orphans)} warning(s).",
            file=sys.stderr,
        )
        return 1

    print("[metacc] All code generation accomplished successfully.")
    return 0


# ===========================================================================
# CLI entry point
# ===========================================================================

def main():
    parser = argparse.ArgumentParser(
        description="METACC_TABLE / METACC_TABLE_ITEM static-array "
        "code generator for Embedded SDK",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "-c", "--compile-commands", default=None,
        help="Path to compile_commands.json. Omit for auto-discovery.",
    )
    parser.add_argument(
        "-p", "--project-root", default=".",
        help="Project Root Path Directory",
    )
    parser.add_argument(
        "-d", "--cache-dir", default=None,
        help="Cache Directory "
        "(default: project_root/build/.metacc/.cache)",
    )
    parser.add_argument(
        "-j", "--jobs", type=int, default=4,
        help="Process pool worker count",
    )
    parser.add_argument(
        "-g", "--generated-root", default=None,
        help="Target root dir for output files "
        "(default: project_root/build/metacc_files)",
    )
    args = parser.parse_args()

    project_root, raw_root, warnings = resolve_project_root_arg(
        args.project_root
    )
    for w in warnings:
        print(w)

    cc_path = None
    if args.compile_commands:
        cc_path = resolve_existing_input_path(
            args.compile_commands,
            [project_root, raw_root, pathlib.Path.cwd()],
        )
    else:
        search_candidates = [
            project_root / "compile_commands.json",
            project_root / "build" / "compile_commands.json",
            project_root / "build_gcc" / "compile_commands.json",
            pathlib.Path.cwd() / "compile_commands.json",
            pathlib.Path.cwd() / "build" / "compile_commands.json",
        ]
        for candidate in search_candidates:
            if candidate.exists():
                cc_path = candidate.resolve()
                print(
                    f"[metacc] Auto-discovered compilation database "
                    f"at: {cc_path}"
                )
                break
        if not cc_path:
            print(
                "[metacc] error: --compile-commands omitted and "
                "auto-discovery failed.",
                file=sys.stderr,
            )
            return 2

    if not cc_path.exists():
        print(
            f"[metacc] error: compilation database {cc_path} missing.",
            file=sys.stderr,
        )
        return 2

    compile_commands = json.loads(cc_path.read_text(encoding="utf-8"))

    cache_dir = (
        resolve_output_path(args.cache_dir, project_root)
        if args.cache_dir
        else default_cache_dir_for(project_root)
    )
    cache_dir.mkdir(parents=True, exist_ok=True)

    generated_root = (
        resolve_output_path(args.generated_root, project_root)
        if args.generated_root
        else default_generated_root_for(project_root)
    )
    generated_root.mkdir(parents=True, exist_ok=True)

    return run(
        project_root, compile_commands, cache_dir, args.jobs,
        generated_root,
    )


if __name__ == "__main__":
    sys.exit(main())