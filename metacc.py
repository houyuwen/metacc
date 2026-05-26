#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: Apache-2.0
"""metacc.py.

C metaprogramming tool backed by libclang for high-performance Embedded SDK development.

Copyright (c) 2026 houyuwen.
"""
import argparse
import concurrent.futures
import datetime
import hashlib
import json
import os
import pathlib
import re
import shlex
import subprocess
import sys

try:
    import clang.cindex as clang
    _CLANG_AVAILABLE = True
except ImportError:
    _CLANG_AVAILABLE = False

# 跨平台动态库后缀自动匹配
if sys.platform.startswith("win"):
    LIB_EXT = ".dll"
elif sys.platform == "darwin":
    LIB_EXT = ".dylib"
else:
    LIB_EXT = ".so"

CACHE_VERSION   = 7
TEMPLATE_AUTHOR = "houyuwenE@outlook.com"
TEMPLATE_YEAR   = str(datetime.date.today().year)
TEMPLATE_DATE   = datetime.date.today().isoformat()

# 全局变量，记录主进程中成功加载的 libclang 真实路径，用于传递给子进程
RESOLVED_LIBCLANG_PATH = None


def configure_and_verify_libclang(explicit_path: str | None = None) -> None:
    """配置并校验 libclang 是否可用。"""
    global RESOLVED_LIBCLANG_PATH
    if not _CLANG_AVAILABLE:
        print_libclang_missing_help("Python 'clang' package is not installed.")
        sys.exit(1)

    if explicit_path:
        candidates = [pathlib.Path(explicit_path)]
    else:
        candidates = []
        # 1. 优先尊重用户显式配置的环境变量
        env_path = os.getenv("METACC_LIBCLANG")
        if env_path:
            candidates.append(pathlib.Path(env_path))

        # 2. 扁平化架构适配：直接在当前可执行文件同级目录寻找
        script_dir = pathlib.Path(__file__).resolve().parent
        candidates.append(script_dir / f"libclang{LIB_EXT}")
        # 兼容旧开发环境
        candidates.append(script_dir / "lib" / f"libclang{LIB_EXT}")

    resolved_file = None
    for candidate in candidates:
        if candidate.is_file():
            resolved_file = candidate.resolve()
            break

    if not resolved_file:
        try:
            if not clang.Config.loaded:
                clang.Index.create()
            RESOLVED_LIBCLANG_PATH = "SYSTEM_DEFAULT"
            return
        except Exception:
            print_libclang_missing_help(
                f"Could not find 'libclang{LIB_EXT}' in bin/ directory."
            )
            sys.exit(1)

    try:
        if not clang.Config.loaded:
            clang.Config.set_library_file(str(resolved_file))
            clang.Index.create()
        RESOLVED_LIBCLANG_PATH = str(resolved_file)
    except Exception as e:
        print(f"[metacc] error: Failed to load libclang at {resolved_file}: {e}", file=sys.stderr)
        sys.exit(1)


def print_libclang_missing_help(reason: str) -> None:
    print("=" * 80, file=sys.stderr)
    print(f"[metacc] CRITICAL ERROR: libclang runtime initialization failed.", file=sys.stderr)
    print(f"Reason: {reason}", file=sys.stderr)
    print("-" * 80, file=sys.stderr)
    print("确保 bin/ 目录下存在 libclang.so 即可完成自动加载。", file=sys.stderr)
    print("=" * 80, file=sys.stderr)


def _init_child_worker(libclang_path: str | None) -> None:
    """多进程并行的子进程初始化钩子，确保子进程能无缝继承主进程的 libclang 路径。"""
    if libclang_path and libclang_path != "SYSTEM_DEFAULT":
        try:
            if not clang.Config.loaded:
                clang.Config.set_library_file(libclang_path)
        except Exception:
            pass


def _metacc_dir() -> pathlib.Path:
    script_path = pathlib.Path(__file__).resolve()
    metacc_dir = script_path.parent
    if metacc_dir.name == "bin":
        return metacc_dir.parent
    return metacc_dir


METACC_DIR = _metacc_dir()
METACC_H_NAME = "include/metacc.h"
METACC_ERROR_COUNT = 0


def metacc_error(message: str) -> None:
    global METACC_ERROR_COUNT
    METACC_ERROR_COUNT += 1
    print(message, file=sys.stderr)


def resolve_project_root_arg(project_root_arg: str, cwd: pathlib.Path | None = None) -> tuple[pathlib.Path, pathlib.Path, list[str]]:
    raw = pathlib.Path(project_root_arg)
    cwd = (cwd or pathlib.Path.cwd()).resolve()
    warnings: list[str] = []

    # Try resolving relative project-root against a sensible list of anchors
    # in this order: current working directory, metacc package parent (repo
    # root), metacc package dir. This covers both running from repo root and
    # running the packaged binary from inside the metacc package directory.
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
                    # Inform user which anchor was used for resolution
                    name = "metacc dir" if anchor == METACC_DIR else "metacc package parent"
                    warnings.append(f"[metacc] warning: --project-root '{project_root_arg}' resolved relative to {name}: {candidate}")
                break
        raw_root = resolved if resolved is not None else (cwd / raw).resolve()

    if raw_root.exists() and raw_root.is_file():
        parent = raw_root.parent
        warnings.append(f"[metacc] warning: --project-root points to a file ({raw_root}); using parent dir: {parent}")
        return parent, raw_root, warnings

    # Heuristic: when callers pass '.' (or a path that resolves to the metacc
    # package dir) while running the packaged binary from inside the package,
    # users usually mean the repo/project root. If the parent of METACC_DIR
    # looks like a project root (contains CMakeLists.txt or compile_commands.json)
    # prefer that as the project root so the same -p value behaves consistently
    # between running the Python script and the packaged binary.
    if project_root_arg in (".", "./metacc") or raw_root == METACC_DIR:
        candidate_parent = METACC_DIR.parent
        if (candidate_parent / "CMakeLists.txt").exists() or (candidate_parent / "compile_commands.json").exists():
            warnings.append(f"[metacc] warning: --project-root '{project_root_arg}' interpreted as project root: {candidate_parent}")
            return candidate_parent, candidate_parent, warnings

    return raw_root, raw_root, warnings


def resolve_existing_input_path(path_arg: str, anchors: list[pathlib.Path]) -> pathlib.Path:
    path = pathlib.Path(path_arg)
    if path.is_absolute():
        return path.resolve()
    for anchor in anchors:
        candidate = (anchor / path).resolve()
        if candidate.exists():
            return candidate
    return path.resolve()


def resolve_output_path(path_arg: str, project_root: pathlib.Path) -> pathlib.Path:
    path = pathlib.Path(path_arg)
    if path.is_absolute():
        return path.resolve()
    return (project_root / path).resolve()


def default_cache_dir_for(project_root: pathlib.Path) -> pathlib.Path:
    return (project_root / "build" / ".metacc" / ".cache").resolve()


def default_generated_root_for(project_root: pathlib.Path) -> pathlib.Path:
    return (project_root / "build" / "metacc_files").resolve()


def ensure_metacc_header_path() -> pathlib.Path:
    candidate = (METACC_DIR / METACC_H_NAME).resolve()
    if not candidate.exists():
        raise FileNotFoundError(f"[metacc] error: Core release header missing at: {candidate}. Please check deployment.")
    return candidate


# 核心受支持的 metacc 原生宏边界定义（不接受任何外部二次宏包装）
METACC_MACROS = {"METACC_ENUM", "METACC_TABLE", "METACC_TABLE_ITEM", "METACC_STRUCT", "METACC_SERIALIZE", "METACC_SHELL", "METACC_INTERFACE", "METACC_HASH", "METACC_GUARD"}
OWNER_MACROS = ("METACC_ENUM", "METACC_TABLE", "METACC_STRUCT", "METACC_SERIALIZE", "METACC_SHELL", "METACC_INTERFACE", "METACC_HASH", "METACC_GUARD")
METACC_TEXT_RE = re.compile(r'\b(?:' + "|".join(METACC_MACROS) + r')\b')
PROJECT_INCLUDE_RE = re.compile(r'^\s*#\s*include\s*["<]([^">]+)[">]', re.MULTILINE)


def split_top_level_commas(s: str) -> list:
    out, cur = [], []
    depth_p = depth_b = depth_c = 0
    quote = escape = False
    for ch in s:
        if quote:
            cur.append(ch)
            if escape:       escape = False
            elif ch == '\\': escape = True
            elif ch == '"':  quote = False
            continue
        if ch == '"':
            quote = True; cur.append(ch); continue
        if ch == ',' and not depth_p and not depth_b and not depth_c:
            out.append(''.join(cur)); cur = []; continue
        if ch == '(':   depth_p += 1
        elif ch == ')' and depth_p: depth_p -= 1
        elif ch == '[': depth_b += 1
        elif ch == ']' and depth_b: depth_b -= 1
        elif ch == '{': depth_c += 1
        elif ch == '}' and depth_c: depth_c -= 1
        cur.append(ch)
    if cur: out.append(''.join(cur))
    return out


def parse_kv_args(args: list) -> dict:
    result = {}
    for arg in args:
        arg = arg.strip()
        if '=' in arg:
            k, _, v = arg.partition('=')
            result[k.strip()] = v.strip()
        elif arg:
            result[arg] = True
    return result


def macro_args(cursor) -> list:
    tokens = list(cursor.get_tokens())
    collecting = False
    depth = 0
    raw_parts = []
    for tok in tokens:
        sp = tok.spelling
        if not collecting:
            if sp == '(':
                collecting = True
                depth = 1
            continue
        if sp == '(':
            depth += 1
            raw_parts.append(sp)
        elif sp == ')':
            depth -= 1
            if depth == 0:
                break
            raw_parts.append(sp)
        else:
            raw_parts.append(sp)
    raw = ''.join(raw_parts)
    return [a.strip() for a in split_top_level_commas(raw) if a.strip()]


def normalize_function_token(token: str) -> str | None:
    t = token.strip()
    if not t:
        return None
    if t.startswith("&"):
        t = t[1:].strip()
    while t.startswith("(") and t.endswith(")") and len(t) >= 2:
        t = t[1:-1].strip()
    return t or None


def read_text_maybe(path: pathlib.Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


def resolve_project_include_path(include_name: str, including_file: pathlib.Path, project_root: pathlib.Path) -> pathlib.Path | None:
    candidates = [
        (including_file.parent / include_name).resolve(),
        (project_root / include_name).resolve(),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def source_tree_might_contain_metacc_text(src_path: pathlib.Path, project_root: pathlib.Path) -> tuple[bool, list[pathlib.Path]]:
    project_root = project_root.resolve()
    root_src = src_path.resolve()
    to_visit = [root_src]
    visited = set()
    deps = []

    while to_visit:
        path = to_visit.pop()
        if path in visited or not path.exists():
            continue
        if path != root_src:
            try:
                path.relative_to(project_root)
            except ValueError:
                continue
        visited.add(path)
        deps.append(path)
        text = read_text_maybe(path)
        if text is None:
            continue
        if "METACC_" in text and METACC_TEXT_RE.search(text):
            return True, deps
        for include_name in PROJECT_INCLUDE_RE.findall(text):
            include_path = resolve_project_include_path(include_name, path, project_root)
            if include_path is not None and include_path not in visited:
                to_visit.append(include_path)
    return False, deps


def collect_enums(tu) -> dict:
    enums = {}
    def _extract_enum_cursor(cur) -> dict:
        return {c.spelling: c.enum_value for c in cur.get_children() if c.kind == clang.CursorKind.ENUM_CONSTANT_DECL}

    for cursor in tu.cursor.walk_preorder():
        if cursor.kind == clang.CursorKind.TYPEDEF_DECL:
            for child in cursor.get_children():
                if child.kind == clang.CursorKind.ENUM_DECL:
                    vals = _extract_enum_cursor(child)
                    if vals: enums[cursor.spelling] = vals
        elif cursor.kind == clang.CursorKind.ENUM_DECL and cursor.spelling:
            vals = _extract_enum_cursor(cursor)
            if vals: enums[cursor.spelling] = vals
    return enums


def collect_structs(tu) -> dict:
    structs = {}
    def _extract_fields(record_cursor) -> list:
        fields = []
        for c in record_cursor.get_children():
            if c.kind == clang.CursorKind.FIELD_DECL:
                sz = c.type.get_size()
                fields.append({
                    "name":   c.spelling,
                    "type":   c.type.spelling,
                    "offset": c.get_field_offsetof() // 8 if c.get_field_offsetof() >= 0 else 0,
                    "size":   sz if sz > 0 else 0,
                })
        return fields

    for cursor in tu.cursor.walk_preorder():
        if cursor.kind == clang.CursorKind.TYPEDEF_DECL:
            for child in cursor.get_children():
                if child.kind in (clang.CursorKind.STRUCT_DECL, clang.CursorKind.UNION_DECL):
                    fields = _extract_fields(child)
                    if fields: structs[cursor.spelling] = fields
        elif cursor.kind in (clang.CursorKind.STRUCT_DECL, clang.CursorKind.UNION_DECL) and cursor.spelling:
            fields = _extract_fields(cursor)
            if fields: structs[cursor.spelling] = fields
    return structs


def collect_nonstatic_functions(tu) -> dict:
    main_file = tu.spelling
    protos = {}
    for cursor in tu.cursor.get_children():
        if cursor.kind != clang.CursorKind.FUNCTION_DECL:
            continue
        loc = cursor.location
        if loc.file is None or loc.file.name != main_file:
            continue
        if cursor.storage_class == clang.StorageClass.STATIC:
            continue
        ret = cursor.result_type.spelling
        
        args_list = []
        for p in cursor.get_arguments():
            args_list.append({
                "type": p.type.spelling,
                "name": p.spelling or f"arg{len(args_list)}"
            })
            
        params = ", ".join(
            (p.type.spelling + (" " + p.spelling if p.spelling else "")) for p in cursor.get_arguments()
        ) or "void"
        
        protos[cursor.spelling] = {
            "decl": f"{ret} {cursor.spelling}({params});",
            "args": args_list,
            "return_type": ret
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


_COMPILER_SYSTEM_INCLUDES = {}

def compiler_system_include_dirs(compiler: str) -> list:
    if not compiler: return []
    cached = _COMPILER_SYSTEM_INCLUDES.get(compiler)
    if cached is not None: return cached
    try:
        proc = subprocess.run([compiler, "-E", "-x", "c", "-", "-v"], input="", text=True, capture_output=True, check=False)
    except OSError:
        _COMPILER_SYSTEM_INCLUDES[compiler] = []
        return []
    includes = []
    capture = False
    for line in proc.stderr.splitlines():
        if "search starts here:" in line:
            capture = True
            continue
        if not capture: continue
        if "End of search list." in line: break
        path = line.strip()
        if not path or path.startswith("("): continue
        if path not in includes: includes.append(path)
    _COMPILER_SYSTEM_INCLUDES[compiler] = includes
    return includes


def infer_clang_target(compiler: str, raw_args: list[str]) -> str | None:
    for arg in raw_args:
        if arg.startswith("-march="):
            march = arg.split("=", 1)[1].strip().lower()
            if march.startswith("rv32"): return "riscv32-unknown-elf"
            if march.startswith("rv64"): return "riscv64-unknown-elf"
    compiler_name = pathlib.Path(compiler).name.lower()
    if "riscv" in compiler_name: return "riscv32-unknown-elf"
    if "arm-none-eabi" in compiler_name: return "arm-none-eabi"
    return None


def sanitize_riscv_march(march: str) -> str:
    parts = march.split("_")
    if len(parts) <= 1: return march
    kept = [parts[0]] + [part for part in parts[1:] if not part.startswith("xx")]
    return "_".join(kept)


_KEEP_ARG_PREFIXES = ("-D", "-U", "-I", "-isystem", "-iquote", "-idirafter", "-include", "-imacros", "-std=", "-x", "--target=", "-mabi=", "-fpack-struct")
_KEEP_ARG_EXACT = {"-nostdinc", "-nostdinc++", "-fshort-enums", "-fshort-wchar", "-funsigned-char", "-fsigned-char", "-fno-common"}
_KEEP_ARG_PAIR_FLAGS = {"-D", "-U", "-I", "-isystem", "-iquote", "-idirafter", "-include", "-imacros", "-x", "--target"}

def clang_args_from_entry(entry: dict, metacc_h: pathlib.Path) -> list:
    raw = shlex.split(entry["command"]) if entry.get("command") else list(entry.get("arguments", []))
    compiler = raw[0] if raw else ""
    src_file = entry.get("file", "")
    keep = []
    skip_next = False
    for arg in raw[1:]:
        if skip_next:
            skip_next = False
            continue
        if arg in ("-c", "-w", "-mdiv"): continue
        if arg.startswith("-march="):
            march = arg.split("=", 1)[1].strip()
            keep.append(f"-march={sanitize_riscv_march(march)}")
            continue
        if arg in ("-o", "-MF", "-MT", "-MQ"):
            skip_next = True
            continue
        if arg in ("-MMD", "-MD", "-MP"): continue
        if arg in _KEEP_ARG_PAIR_FLAGS:
            keep.append(arg)
            skip_next = True
            continue
        if arg == src_file: continue
        if arg in _KEEP_ARG_EXACT:
            keep.append(arg)
            continue
        if any(arg.startswith(prefix) for prefix in _KEEP_ARG_PREFIXES):
            keep.append(arg)
            continue
    for inc in compiler_system_include_dirs(compiler):
        keep += ["-isystem", inc]
    target = infer_clang_target(compiler, raw)
    if target: keep += [f"--target={target}"]
    
    keep += ["-D__attribute__(x)=", "-D__flash=", "-D__interrupt=", "-D__asm__(x)=", "-include", str(metacc_h)]
    return keep


def collect_annotations_from_tu(tu, src_path: pathlib.Path) -> list:
    annotations = []
    main = str(src_path.resolve())
    project_files = {main}
    for inc in tu.get_includes():
        inc_path = pathlib.Path(inc.include.name).resolve()
        if not str(inc_path).startswith(("/usr", "/lib")):
            project_files.add(str(inc_path))

    current_func = None
    for cursor in tu.cursor.walk_preorder():
        if cursor.kind == clang.CursorKind.FUNCTION_DECL:
            current_func = cursor
        if cursor.kind != clang.CursorKind.MACRO_INSTANTIATION: continue
        
        kind = cursor.spelling
        if kind not in METACC_MACROS: continue  # 拒绝任何非原生纯粹宏
        
        resolved_args = macro_args(cursor)
        loc = cursor.location
        if loc.file is None: continue
        file_path = pathlib.Path(loc.file.name).resolve()
        if str(file_path) not in project_files: continue
        
        func_name = None
        func_start = None
        func_end = None
        if current_func and current_func.extent.start.file and current_func.extent.start.file.name == loc.file.name:
            if current_func.extent.start.line <= loc.line <= current_func.extent.end.line:
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
            "func_end": func_end
        })
    return annotations


def _process_file_worker(src_str: str, entry: dict, project_root_str: str, metacc_h_str: str, generated_root_str: str | None, active_libclang: str | None) -> dict:
    import clang.cindex as clang
    if active_libclang:
        try: clang.Config.set_library_file(active_libclang)
        except Exception: pass
    
    project_root = pathlib.Path(project_root_str)
    metacc_h = pathlib.Path(metacc_h_str)
    src = pathlib.Path(src_str)

    has_metacc_text, text_deps = source_tree_might_contain_metacc_text(src, project_root)
    if not has_metacc_text:
        deps = []
        for dep_path in text_deps:
            try:
                st = dep_path.stat()
                deps.append({"path": str(dep_path), "mtime_ns": st.st_mtime_ns, "size": st.st_size})
            except OSError: pass
        return {"src": src_str, "annotations": [], "enums": {}, "structs": {}, "protos": {}, "deps": deps, "matched": False}

    index = clang.Index.create()
    args = clang_args_from_entry(entry, metacc_h)
    
    try:
        tu = index.parse(src_str, args=args, options=clang.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD | clang.TranslationUnit.PARSE_SKIP_FUNCTION_BODIES)
    except Exception:
        return {"src": src_str, "annotations": [], "enums": {}, "structs": {}, "protos": {}, "deps": [], "matched": False}

    if not tu:
        return {"src": src_str, "annotations": [], "enums": {}, "structs": {}, "protos": {}, "deps": [], "matched": False}

    deps = []
    for inc in tu.get_includes():
        p = pathlib.Path(inc.include.name)
        try:
            st = p.stat()
            deps.append({"path": str(p), "mtime_ns": st.st_mtime_ns, "size": st.st_size})
        except OSError: pass

    try:
        st = src.stat()
        deps.append({"path": str(src), "mtime_ns": st.st_mtime_ns, "size": st.st_size})
    except OSError: pass

    return {
        "src": src_str,
        "annotations": collect_annotations_from_tu(tu, src),
        "enums": collect_enums(tu),
        "structs": collect_structs(tu),
        "protos": collect_nonstatic_functions(tu),
        "deps": deps,
        "matched": True
    }


class ProjectModel:
    def __init__(self, root: pathlib.Path, file_results: list):
        self.root = root
        self.enums = {}
        self.structs = {}
        self.protos = {}
        self.annotations = []
        for r in file_results:
            self.enums.update(r.get("enums", {}))
            self.structs.update(r.get("structs", {}))
            self.protos.update(r.get("protos", {}))
            for a in r.get("annotations", []):
                self.annotations.append({**a, "src": r["src"]})

    def annotations_of_kind(self, *kinds) -> list:
        return [a for a in self.annotations if a["kind"] in kinds]

    def resolve_enum_value(self, token: str) -> tuple:
        t = token.strip()
        try: return (0, int(t, 0))
        except ValueError: pass
        for vals in self.enums.values():
            if t in vals: return (0, vals[t])
        return (1, t)


def scan_project(project_root: pathlib.Path, compile_commands: list, cache_dir: pathlib.Path, jobs: int, generated_root: pathlib.Path | None) -> ProjectModel:
    metacc_h = ensure_metacc_header_path()
    entries_by_src = {}
    for entry in compile_commands:
        src = entry.get("file", "")
        if src.endswith((".c", ".h")):
            p = pathlib.Path(src)
            if not p.is_absolute():
                p = (pathlib.Path(entry.get("directory", str(project_root))) / p).resolve()
            if generated_root is not None:
                try:
                    p.relative_to(generated_root)
                    continue
                except ValueError: pass
            entries_by_src[str(p)] = entry

    results = []
    tasks_to_run = {}
    for src_str, entry in entries_by_src.items():
        src = pathlib.Path(src_str)
        obj = {"v": CACHE_VERSION, "file": src_str, "args": entry.get("arguments", []) or shlex.split(entry.get("command", ""))}
        key = hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()
        cp = cache_dir / "entries" / f"{key}.json"
        cp.parent.mkdir(parents=True, exist_ok=True)
        
        cached = None
        if cp.exists():
            try: cached = json.loads(cp.read_text(encoding="utf-8"))
            except Exception: pass
            
        if cached:
            valid = True
            for dep in cached.get("deps", []):
                try:
                    st = pathlib.Path(dep["path"]).stat()
                    if st.st_mtime_ns != dep["mtime_ns"] or st.st_size != dep["size"]:
                        valid = False; break
                except OSError:
                    valid = False; break
            if valid:
                results.append({"src": src_str, **cached})
                continue
        tasks_to_run[src_str] = (entry, cp)

    if tasks_to_run:
        worker_count = max(1, jobs)
        total_tasks = len(tasks_to_run)
        print(f"[metacc] Spawning {worker_count} process workers to parse {total_tasks} files...", flush=True)
        
        finished_count = 0
        with concurrent.futures.ProcessPoolExecutor(max_workers=worker_count) as pool:
            futures = {
                pool.submit(
                    _process_file_worker, src_str, item[0], str(project_root), str(metacc_h), str(generated_root) if generated_root else None, RESOLVED_LIBCLANG_PATH
                ): (src_str, item[1]) for src_str, item in tasks_to_run.items()
            }
            for fut in concurrent.futures.as_completed(futures):
                src_str, cache_path = futures[fut]
                finished_count += 1
                short_name = pathlib.Path(src_str).name
                # 【流式进度条补全】加上 flush=True 强制清空缓存打印
                print(f"[metacc] [{finished_count}/{total_tasks}] Parsing {short_name}...", flush=True)
                try:
                    res = fut.result()
                    results.append(res)
                    payload = {k: v for k, v in res.items() if k != "src"}
                    tmp = cache_path.with_suffix(f".{os.getpid()}.tmp")
                    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                    tmp.replace(cache_path)
                except Exception as e:
                    print(f"[metacc] process error {src_str}: {e}", file=sys.stderr, flush=True)

    return ProjectModel(project_root, results)


_CLANG_TYPE_MAP = {
    "bool": "METACC_TYPE_BOOL", "_Bool": "METACC_TYPE_BOOL",
    "char": "METACC_TYPE_INT8", "unsigned char": "METACC_TYPE_UINT8",
    "short": "METACC_TYPE_INT16", "short int": "METACC_TYPE_INT16",
    "unsigned short": "METACC_TYPE_UINT16",
    "int": "METACC_TYPE_INT32", "unsigned int": "METACC_TYPE_UINT32",
    "long": "METACC_TYPE_INT32", "long int": "METACC_TYPE_INT32",
    "unsigned long": "METACC_TYPE_UINT32",
    "long long": "METACC_TYPE_INT64", "unsigned long long": "METACC_TYPE_UINT64",
    "int8_t": "METACC_TYPE_INT8", "uint8_t": "METACC_TYPE_UINT8",
    "int16_t": "METACC_TYPE_INT16", "uint16_t": "METACC_TYPE_UINT16",
    "int32_t": "METACC_TYPE_INT32", "uint32_t": "METACC_TYPE_UINT32",
    "int64_t": "METACC_TYPE_INT64", "uint64_t": "METACC_TYPE_UINT64",
    "float": "METACC_TYPE_FLOAT", "double": "METACC_TYPE_DOUBLE"
}

def _c_type_to_metacc(type_str: str) -> str:
    t = type_str.strip()
    if t in _CLANG_TYPE_MAP: return _CLANG_TYPE_MAP[t]
    if t.endswith("*") or "const *" in t or "* const" in t: return "METACC_TYPE_POINTER"
    if "[" in t:
        if "char" in t: return "METACC_TYPE_STRING"
        return "METACC_TYPE_ARRAY"
    return "METACC_TYPE_STRUCT"


def _write_if_changed(path: pathlib.Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") == text: return
    path.write_text(text, encoding="utf-8")


def companion_paths(owner_path: pathlib.Path, project_root: pathlib.Path, generated_root: pathlib.Path | None) -> tuple[pathlib.Path, pathlib.Path]:
    owner_abs = owner_path.resolve()
    if generated_root is None:
        return owner_abs.parent / f"metacc_{owner_abs.stem}.h", owner_abs.parent / f"metacc_{owner_abs.stem}.c"
    out_h_dir = generated_root / "include"
    out_c_dir = generated_root / "src"
    return out_h_dir / f"metacc_{owner_abs.stem}.h", out_c_dir / f"metacc_{owner_abs.stem}.c"


def _render_generated_header(filename: str, body: str) -> str:
    return f"/**\n * @file {filename}\n * @brief Automatically generated by metacc. Do not edit.\n */\n#pragma once\n\n#ifdef __cplusplus\nextern \"C\" {{\n#endif\n\n#include <stdint.h>\n#include <stdbool.h>\n#include <string.h>\n\n{body.strip()}\n\n#ifdef __cplusplus\n}}\n#endif\n"


def _render_generated_source(filename: str, owner_include: str, gen_header_include: str, body: str) -> str:
    return f"/**\n * @file {filename}\n * @brief Automatically generated by metacc. Do not edit.\n */\n#include \"{owner_include}\"\n#include \"{gen_header_include}\"\n#include <string.h>\n#include <stdio.h>\n#include <stdlib.h>\n\n{body.strip()}\n"


def _append_companion_fragment(buckets: dict, owner_path: pathlib.Path, header_lines: list, source_lines: list):
    bucket = buckets.setdefault(str(owner_path), {"owner_path": owner_path, "header_parts": [], "source_parts": []})
    if header_lines: bucket["header_parts"].append("\n".join(header_lines).rstrip())
    if source_lines: bucket["source_parts"].append("\n".join(source_lines).rstrip())


def _flush_companion_fragments(buckets: dict, project_root: pathlib.Path, generated_root: pathlib.Path | None):
    for bucket in buckets.values():
        owner_path = bucket["owner_path"]
        h_path, c_path = companion_paths(owner_path, project_root, generated_root)
        h_text = _render_generated_header(h_path.name, "\n\n".join(p for p in bucket["header_parts"] if p))
        
        rel_owner = os.path.relpath(owner_path.resolve(), c_path.parent).replace(os.sep, "/")
        rel_generated_h = os.path.relpath(h_path.resolve(), c_path.parent).replace(os.sep, "/")
        c_text = _render_generated_source(c_path.name, rel_owner, rel_generated_h, "\n\n".join(p for p in bucket["source_parts"] if p))
        
        _write_if_changed(h_path, h_text)
        _write_if_changed(c_path, c_text)


def parse_func_ptr(type_str: str, field_name: str, interface_name: str) -> dict | None:
    if "(*)" not in type_str:
        return None
    parts = type_str.split("(*)", 1)
    ret_type = parts[0].strip()
    args_inside = parts[1].strip().strip("()").strip()
    
    raw_args = split_top_level_commas(args_inside) if args_inside else []
    args_parsed = []
    for idx, arg_t in enumerate(raw_args):
        arg_t = arg_t.strip()
        if arg_t == "void" or not arg_t:
            continue
        args_parsed.append({
            "type": arg_t,
            "name": f"param{idx}"
        })
    return {
        "ret_type": ret_type,
        "name": field_name,
        "args": args_parsed
    }


def run_serialize(model: ProjectModel, buckets: dict):
    for ann in model.annotations_of_kind("METACC_SERIALIZE"):
        if not ann["args"]: continue
        struct_name = ann["args"][0].strip()
        if struct_name not in model.structs: continue
        
        owner_path = pathlib.Path(ann.get("file", ann["src"]))
        fields = model.structs[struct_name]
        
        h_lines = [
            f"uint32_t {struct_name}_Pack(const {struct_name}* src, uint8_t* out_buf);",
            f"bool {struct_name}_Unpack({struct_name}* dst, const uint8_t* in_buf);"
        ]
        
        c_lines = [
            f"uint32_t {struct_name}_Pack(const {struct_name}* src, uint8_t* out_buf) {{",
            "    if (!src || !out_buf) return 0u;",
            "    uint32_t offset = 0u;"
        ]
        for f in fields:
            c_lines.append(f"    memcpy(out_buf + offset, &src->{f['name']}, {f['size']});")
            c_lines.append(f"    offset += {f['size']};")
        c_lines += [
            "    return offset;",
            "}\n",
            f"bool {struct_name}_Unpack({struct_name}* dst, const uint8_t* in_buf) {{",
            "    if (!dst || !in_buf) return false;",
            "    uint32_t offset = 0u;"
        ]
        for f in fields:
            c_lines.append(f"    memcpy(&dst->{f['name']}, in_buf + offset, {f['size']});")
            c_lines.append(f"    offset += {f['size']};")
        c_lines += [
            "    return true;",
            "}"
        ]
        _append_companion_fragment(buckets, owner_path, h_lines, c_lines)


def run_shell(model: ProjectModel, buckets: dict):
    from collections import defaultdict
    cmds_by_owner = defaultdict(list)
    
    for ann in model.annotations_of_kind("METACC_SHELL"):
        if len(ann["args"]) < 3: continue
        cmd_name = ann["args"][0].strip().strip('"')
        func_name = ann["args"][1].strip()
        help_text = ann["args"][2].strip().strip('"')
        
        owner_path = pathlib.Path(ann.get("file", ann["src"]))
        cmds_by_owner[owner_path].append({
            "cmd": cmd_name,
            "func": func_name,
            "help": help_text
        })
        
    for owner_path, cmds in cmds_by_owner.items():
        h_lines = [
            "typedef struct {",
            "    const char* name;",
            "    void (*wrapper)(int argc, char** argv);",
            "    const char* help;",
            "} MetaccShellCmd;",
            f"extern const MetaccShellCmd metacc_shell_cmds_{owner_path.stem}[];",
            f"extern const uint32_t metacc_shell_cmd_count_{owner_path.stem};"
        ]
        
        c_lines = []
        for cmd in cmds:
            func_name = cmd["func"]
            if func_name in model.protos:
                c_lines.append(f"extern {model.protos[func_name]['decl']}")
        c_lines.append("")
        
        for cmd in cmds:
            cmd_name = cmd["cmd"]
            func_name = cmd["func"]
            
            c_lines += [
                f"void metacc_shell_wrapper_{cmd_name}(int argc, char** argv) {{",
                "    (void)argc; (void)argv;"
            ]
            
            args_meta = []
            if func_name in model.protos and isinstance(model.protos[func_name], dict):
                args_meta = model.protos[func_name].get("args", [])
                
            call_args = []
            for idx, arg in enumerate(args_meta):
                t = arg["type"].strip()
                n = arg["name"]
                v_name = f"local_{n}"
                
                if "char" in t and "*" in t:
                    c_lines.append(f"    {t} {v_name} = (argc > {idx+1}) ? argv[{idx+1}] : \"\";")
                elif "float" in t or "double" in t:
                    c_lines.append(f"    {t} {v_name} = (argc > {idx+1}) ? ({t})strtof(argv[{idx+1}], NULL) : ({t})0;")
                else:
                    c_lines.append(f"    {t} {v_name} = (argc > {idx+1}) ? ({t})strtol(argv[{idx+1}], NULL, 0) : ({t})0;")
                call_args.append(v_name)
                
            args_joined = ", ".join(call_args)
            c_lines.append(f"    {func_name}({args_joined});")
            c_lines.append("}\n")
            
        c_lines.append(f"const MetaccShellCmd metacc_shell_cmds_{owner_path.stem}[] = {{")
        for cmd in cmds:
            c_lines.append(f"    {{\"{cmd['cmd']}\", metacc_shell_wrapper_{cmd['cmd']}, \"{cmd['help']}\"}},")
        c_lines.append("};")
        c_lines.append(f"const uint32_t metacc_shell_cmd_count_{owner_path.stem} = {len(cmds)}u;")
        
        _append_companion_fragment(buckets, owner_path, h_lines, c_lines)


def run_interface(model: ProjectModel, buckets: dict):
    for ann in model.annotations_of_kind("METACC_INTERFACE"):
        if not ann["args"]: continue
        interface_name = ann["args"][0].strip()
        if interface_name not in model.structs: continue
        
        owner_path = pathlib.Path(ann.get("file", ann["src"]))
        fields = model.structs[interface_name]
        
        func_ptrs = []
        for f in fields:
            parsed = parse_func_ptr(f["type"], f["name"], interface_name)
            if parsed: func_ptrs.append(parsed)
                
        if not func_ptrs: continue
        
        h_lines = []
        c_lines = []
        
        for fp in func_ptrs:
            fname = fp["name"]
            ret = fp["ret_type"]
            
            h_lines.append(f"extern uint32_t mock_{interface_name}_{fname}_call_count;")
            c_lines.append(f"uint32_t mock_{interface_name}_{fname}_call_count = 0u;")
            
            for arg in fp["args"]:
                h_lines.append(f"extern {arg['type']} mock_{interface_name}_{fname}_{arg['name']};")
                c_lines.append(f"{arg['type']} mock_{interface_name}_{fname}_{arg['name']};")
                
            if ret != "void":
                h_lines.append(f"extern {ret} mock_{interface_name}_{fname}_return_val;")
                c_lines.append(f"{ret} mock_{interface_name}_{fname}_return_val;")
            
            params_decl = ", ".join(f"{a['type']} {a['name']}" for a in fp["args"]) or "void"
            c_lines.append(f"static {ret} mock_{interface_name}_{fname}_impl({params_decl}) {{")
            c_lines.append(f"    mock_{interface_name}_{fname}_call_count++;")
            for arg in fp["args"]:
                c_lines.append(f"    mock_{interface_name}_{fname}_{arg['name']} = {arg['name']};")
            if ret != "void":
                c_lines.append(f"    return mock_{interface_name}_{fname}_return_val;")
            c_lines.append("}\n")
            
        h_lines.append(f"{interface_name} mock_{interface_name}_Create(void);")
        h_lines.append(f"void mock_{interface_name}_Reset(void);")
        
        c_lines.append(f"{interface_name} mock_{interface_name}_Create(void) {{")
        c_lines.append(f"    {interface_name} inst = {{")
        for fp in func_ptrs:
            c_lines.append(f"        .{fp['name']} = mock_{interface_name}_{fp['name']}_impl,")
        c_lines.append("    };")
        c_lines.append("    return inst;")
        c_lines.append("}\n")
        
        c_lines.append(f"void mock_{interface_name}_Reset(void) {{")
        for fp in func_ptrs:
            c_lines.append(f"    mock_{interface_name}_{fp['name']}_call_count = 0u;")
        c_lines.append("}")
        
        _append_companion_fragment(buckets, owner_path, h_lines, c_lines)


def run_hash(model: ProjectModel, buckets: dict):
    for ann in model.annotations_of_kind("METACC_HASH"):
        args = ann["args"]
        if len(args) < 2: continue
        hash_macro_name = args[0].strip()
        raw_string = args[1].strip().strip('"')
        
        hval = 2166136261
        for ch in raw_string:
            hval = (hval ^ ord(ch)) * 16777619 & 0xFFFFFFFF
            
        owner_path = pathlib.Path(ann.get("file", ann["src"]))
        h_lines = [f"#define {hash_macro_name} (0x{hval:08X}u) /* Hashed from \"{raw_string}\" */"]
        _append_companion_fragment(buckets, owner_path, h_lines, [])


def run_guard(model: ProjectModel, generated_root: pathlib.Path | None):
    if not generated_root: return
    patched_dir = generated_root / "patched"
    patched_dir.mkdir(parents=True, exist_ok=True)
    
    guards_by_file = {}
    for ann in model.annotations_of_kind("METACC_GUARD"):
        src = ann.get("file", ann["src"])
        # Accept guards even when func_start/func_end are missing; we'll
        # attempt to locate the enclosing function body heuristically.
        guards_by_file.setdefault(src, []).append(ann)
            
    for src_str, anns in guards_by_file.items():
        src_path = pathlib.Path(src_str)
        content = read_text_maybe(src_path)
        if not content: continue

        lines = content.splitlines()
        # Process annotations from bottom to top so line indices remain valid
        for ann in sorted(anns, key=lambda x: x["line"], reverse=True):
            if not ann.get("args"): continue
            cleanup_expr = ann["args"][0].strip() + ";"
            guard_line = ann.get("line")

            # Determine function body bounds: prefer func_start/func_end if present
            func_start = ann.get("func_start")
            func_end = ann.get("func_end")

            if not func_start or not func_end:
                # Heuristic: find nearest opening brace '{' at or before guard_line,
                # then match braces forward to find function end.
                open_idx = None
                search_idx = max(0, (guard_line - 1))
                while search_idx >= 0:
                    if '{' in lines[search_idx]:
                        open_idx = search_idx
                        break
                    search_idx -= 1

                if open_idx is None:
                    # fallback: cannot determine bounds — skip this annotation
                    continue

                depth = 0
                end_idx = None
                for i in range(open_idx, len(lines)):
                    depth += lines[i].count('{')
                    depth -= lines[i].count('}')
                    if depth == 0:
                        end_idx = i
                        break
                if end_idx is None:
                    continue

                func_start = open_idx + 1  # convert to 1-based line index
                func_end = end_idx + 1

            # Extract the function body text and perform replacements in bulk to
            # avoid context-sensitive parsing issues caused by single-line
            # 'if (...) return ...;' patterns.
            start_idx = func_start - 1
            end_idx = func_end
            func_text = "\n".join(lines[start_idx:end_idx])

            # Replace inline 'if (...) return expr;' with braced form that
            # runs cleanup only when condition holds.
            pattern_if_return = re.compile(r'(\bif\b\s*\([^)]*\)\s*)return\s*([^;]+);')
            func_text = pattern_if_return.sub(lambda m: f"{m.group(1)}{{ {cleanup_expr} return {m.group(2).strip()}; }}", func_text)

            # For any remaining 'return' tokens (normal returns), insert cleanup
            func_text = re.sub(r'\breturn\b', f"{cleanup_expr} return", func_text)

            # Replace original lines with modified function text
            new_lines = func_text.splitlines()
            lines[start_idx:end_idx] = new_lines

            # Ensure cleanup at function end (before the closing brace)
            last_idx = func_end - 1
            if last_idx < len(lines):
                last_line = lines[last_idx]
                if '}' in last_line and not re.search(r'\breturn\b', last_line):
                    last_brace = last_line.rfind('}')
                    lines[last_idx] = last_line[:last_brace] + f" {cleanup_expr} " + last_line[last_brace:]

        patched_content = "\n".join(lines)
        out_path = patched_dir / f"patched_{src_path.name}"
        _write_if_changed(out_path, patched_content)


def run_table(model: ProjectModel, buckets: dict, project_root: pathlib.Path, generated_root: pathlib.Path | None):
    tables = {}
    for ann in model.annotations_of_kind("METACC_TABLE"):
        args = ann["args"]
        if len(args) < 2: continue
        name, type_name = args[0], args[1]
        if name in tables: continue
        kv = parse_kv_args(args[2:])
        sort_col_raw = kv.get("sort_col", kv.get("col"))
        sort_col = int(str(sort_col_raw), 0) if sort_col_raw is not None else None
        sort_desc = str(kv.get("order", "asc")).strip().lower() in ("desc", "descending")
        
        tables[name] = {
            "type": type_name,
            "sort_col": sort_col,
            "sort_desc": sort_desc,
            "owner_src": ann.get("file", ann["src"]),
            "items": []
        }

    for ann in model.annotations_of_kind("METACC_TABLE_ITEM"):
        args = ann["args"]
        if not args: continue
        arr_name = args[0]
        payload = ", ".join(args[1:])
        if arr_name in tables:
            tables[arr_name]["items"].append({
                "payload": payload,
                "src": ann.get("file", ann["src"]),
                "line": ann["line"],
                "protos": collect_function_protos(model, split_top_level_commas(payload))
            })

    for name, tbl in tables.items():
        owner_path = pathlib.Path(tbl["owner_src"])
        items = tbl["items"]
        sort_col = tbl["sort_col"]
        sort_desc = tbl["sort_desc"]

        if sort_col is None:
            items.sort(key=lambda x: (str(x["src"]), int(x["line"])))
        else:
            def _item_sort_key(item):
                parts = [p.strip() for p in split_top_level_commas(item["payload"])]
                key_expr = parts[sort_col].strip() if sort_col < len(parts) else ""
                return (model.resolve_enum_value(key_expr), key_expr, str(item["src"]), int(item["line"]))
            items.sort(key=_item_sort_key, reverse=sort_desc)

        proto_lines = []
        for item in items:
            for proto in item["protos"]:
                if proto not in proto_lines:
                    proto_lines.append(proto)

        h_lines = [f"extern const {tbl['type']} {name}[];", f"extern const uint32_t {name}_count;"]
        c_lines = [f"{p.rstrip(';')};" for p in proto_lines]
        if proto_lines: c_lines.append("")
        c_lines += [f"const {tbl['type']} {name}[] = {{"] + [f"    {{{it['payload']}}}," for it in items] + ["};", f"const uint32_t {name}_count = {len(items)}u;"]
        
        _append_companion_fragment(buckets, owner_path, h_lines, c_lines)


def run_enum(model: ProjectModel, buckets: dict):
    for ann in model.annotations_of_kind("METACC_ENUM"):
        if not ann["args"]: continue
        enum_name = ann["args"][0]
        if enum_name not in model.enums: continue
        
        owner_path = pathlib.Path(ann.get("file", ann["src"]))
        constants = model.enums[enum_name]

        h_lines = [
            f"const char* {enum_name}_ToString({enum_name} val);",
            f"bool {enum_name}_FromString(const char* str, {enum_name}* out_val);"
        ]
        c_lines = [f"const char* {enum_name}_ToString({enum_name} val) {{", "    switch(val) {"]
        for name in constants: c_lines.append(f"        case {name}: return \"{name}\";")
        c_lines += [
            "        default: return \"UNKNOWN\";", "    }", "}\n",
            f"bool {enum_name}_FromString(const char* str, {enum_name}* out_val) {{",
            "    if (!str || !out_val) return false;"
        ]
        for name in constants: c_lines.append(f"    if (strcmp(str, \"{name}\") == 0) {{ *out_val = {name}; return true; }}")
        c_lines += ["    return false;", "}"]
        _append_companion_fragment(buckets, owner_path, h_lines, c_lines)


def run_struct(model: ProjectModel, buckets: dict):
    for ann in model.annotations_of_kind("METACC_STRUCT"):
        if not ann["args"]: continue
        struct_name = ann["args"][0]
        if struct_name not in model.structs: continue

        owner_path = pathlib.Path(ann.get("file", ann["src"]))
        fields = model.structs[struct_name]

        h_lines = [
            f"typedef struct {{ const char* name; uint32_t kind; size_t offset; size_t size; }} MetaccFieldInfo;",
            f"extern const MetaccFieldInfo metacc_fields_{struct_name}[];",
            f"extern const uint32_t metacc_field_count_{struct_name};"
        ]
        c_lines = [f"const MetaccFieldInfo metacc_fields_{struct_name}[] = {{"]
        for f in fields:
            type_kind = _c_type_to_metacc(f["type"])
            c_lines.append(f"    {{\"{f['name']}\", 0, {f['offset']}, {f['size']}}},")
        c_lines += ["};", f"const uint32_t metacc_field_count_{struct_name} = {len(fields)}u;"]
        _append_companion_fragment(buckets, owner_path, h_lines, c_lines)


def run(project_root: pathlib.Path, compile_commands: list, cache_dir: pathlib.Path, jobs: int, generated_root: pathlib.Path | None):
    print("[metacc] Scanning project tree and parsing AST via libclang process workers...")
    model = scan_project(project_root, compile_commands, cache_dir, jobs, generated_root)
    print(f"[metacc] Scan complete. Found {len(model.annotations)} valid annotations.")
    
    companion_fragments = {}
    run_table(model, companion_fragments, project_root, generated_root)
    run_enum(model, companion_fragments)
    run_struct(model, companion_fragments)
    run_serialize(model, companion_fragments)
    run_shell(model, companion_fragments)
    run_interface(model, companion_fragments)
    run_hash(model, companion_fragments)
    
    _flush_companion_fragments(companion_fragments, project_root, generated_root)
    run_guard(model, generated_root)
    
    print("[metacc] All code generation accomplished successfully.")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="C metaprogramming tool built for Embedded SDK via libclang",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument("-c", "--compile-commands", default=None, 
                        help="Path to compile_commands.json. Omit for auto-discovery.")
    parser.add_argument("-p", "--project-root", default=".", 
                        help="Project Root Path Directory")
    parser.add_argument("-d", "--cache-dir", default=None, 
                        help="Cache Directory (default: project_root/build/.metacc/.cache)")
    parser.add_argument("-j", "--jobs", type=int, default=4, 
                        help="Process pool worker count")
    parser.add_argument("-g", "--generated-root", default=None, 
                        help="Target root dir for output files (default: project_root/build/metacc_files)")
    parser.add_argument("--libclang", default=None,
                        help="Explicit path to libclang.so / libclang.dylib / libclang.dll")
    args = parser.parse_args()

    project_root, raw_root, warnings = resolve_project_root_arg(args.project_root)
    for w in warnings:
        print(w)

    cc_path = None
    if args.compile_commands:
        cc_path = resolve_existing_input_path(args.compile_commands, [project_root, raw_root, pathlib.Path.cwd()])
    else:
        search_candidates = [
            project_root / "compile_commands.json",
            project_root / "build" / "compile_commands.json",
            project_root / "build_gcc" / "compile_commands.json",
            pathlib.Path.cwd() / "compile_commands.json",
            pathlib.Path.cwd() / "build" / "compile_commands.json"
        ]
        for candidate in search_candidates:
            if candidate.exists():
                cc_path = candidate.resolve()
                print(f"[metacc] Auto-discovered compilation database at: {cc_path}")
                break
        
        if not cc_path:
            print("[metacc] error: --compile-commands omitted and auto-discovery failed.", file=sys.stderr)
            print("Searched locations:", file=sys.stderr)
            for c in search_candidates:
                print(f"  - {c}", file=sys.stderr)
            return 2

    if not cc_path.exists():
        print(f"[metacc] error: compilation database {cc_path} missing.", file=sys.stderr)
        return 2

    compile_commands = json.loads(cc_path.read_text(encoding="utf-8"))
    
    # 启动前置强校验，不给后续多进程留任何运行隐患
    configure_and_verify_libclang(explicit_path=args.libclang)
    
    cache_dir = resolve_output_path(args.cache_dir, project_root) if args.cache_dir else default_cache_dir_for(project_root)
    cache_dir.mkdir(parents=True, exist_ok=True)
    
    generated_root = resolve_output_path(args.generated_root, project_root) if args.generated_root else default_generated_root_for(project_root)
    generated_root.mkdir(parents=True, exist_ok=True)

    return run(project_root, compile_commands, cache_dir, args.jobs, generated_root)


if __name__ == "__main__":
    sys.exit(main())