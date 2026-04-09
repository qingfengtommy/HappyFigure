"""Data exploration tools — Claude Code Glob/Read/Grep analogs.

All functions accept a ``results_dir`` (the sandbox root) and return
JSON-serializable dicts.  Errors are returned as ``{"error": "..."}``
rather than raising exceptions, so they can be fed straight back to an LLM.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

from tools.sandbox import safe_resolve

# Extensions recognised as data files (mirrors figure_pipeline._scan_data_files)
TABULAR_EXTS = {".tsv", ".csv", ".json", ".md"}
BINARY_EXTS = {".npy", ".npz", ".pkl", ".pt", ".ckpt"}
ALL_DATA_EXTS = TABULAR_EXTS | BINARY_EXTS


# ---------------------------------------------------------------------------
# Individual tool functions
# ---------------------------------------------------------------------------

def list_data_files(
    pattern: str,
    results_dir: Path,
    directory: str | None = None,
) -> dict:
    """Glob for data files matching *pattern* inside *results_dir*.

    Returns ``{"files": [{"path": str, "size_bytes": int, "type": str}, ...]}``
    """
    base = results_dir
    if directory:
        resolved = safe_resolve(directory, results_dir)
        if resolved is None:
            return {"error": f"Directory outside sandbox: {directory}"}
        base = resolved
        if not base.is_dir():
            return {"error": f"Not a directory: {directory}"}

    # Use rglob with the pattern; fall back to listing all data files
    try:
        matches = sorted(base.rglob(pattern), key=lambda p: str(p))
    except Exception as exc:
        return {"error": f"Invalid glob pattern: {exc}"}

    files = []
    for p in matches:
        if not p.is_file():
            continue
        if p.suffix.lower() not in ALL_DATA_EXTS:
            continue
        try:
            rel = p.relative_to(results_dir)
        except ValueError:
            continue
        ext = p.suffix.lower()
        if ext in BINARY_EXTS:
            ftype = "binary"
        elif ext == ".json":
            ftype = "json"
        elif ext == ".md":
            ftype = "markdown"
        else:
            ftype = "tabular"
        files.append({
            "path": str(rel),
            "size_bytes": p.stat().st_size,
            "type": ftype,
        })

    return {"files": files, "count": len(files)}


def read_data_file(
    file_path: str,
    results_dir: Path,
    head: int = 5,
    columns: list[str] | None = None,
) -> dict:
    """Read a data file, return schema + first *head* rows.

    Tabular files (csv/tsv): columns, dtypes, row_count, first N rows.
    JSON files: raw structure snippet.
    Markdown files: text content (truncated).
    Binary files: size only.
    """
    import pandas as pd

    resolved = safe_resolve(file_path, results_dir)
    if resolved is None:
        return {"error": f"Path outside sandbox: {file_path}"}
    if not resolved.is_file():
        return {"error": f"File not found: {file_path}"}

    ext = resolved.suffix.lower()

    # Binary files — just metadata
    if ext in BINARY_EXTS:
        size_mb = resolved.stat().st_size / (1024 * 1024)
        label = {
            ".npy": "NumPy array", ".npz": "NumPy archive",
            ".pkl": "Pickle", ".pt": "PyTorch checkpoint",
            ".ckpt": "PyTorch checkpoint",
        }.get(ext, "Binary")
        return {"type": label, "size_mb": round(size_mb, 2)}

    # Markdown — return text
    if ext == ".md":
        text = resolved.read_text(encoding="utf-8")
        if len(text) > 3000:
            text = text[:3000] + "\n... (truncated)"
        return {"type": "markdown", "content": text}

    # JSON — raw structure + DataFrame view
    if ext == ".json":
        try:
            with open(resolved, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as exc:
            return {"error": f"JSON parse error: {exc}"}

        sample_obj = raw[0] if isinstance(raw, list) and raw else raw
        snippet = json.dumps(sample_obj, indent=2, default=str)
        if len(snippet) > 2000:
            snippet = snippet[:2000] + "\n... (truncated)"

        # Also try to make a DataFrame for tabular view
        try:
            df = _json_to_dataframe(resolved)
            return {
                "type": "json",
                "raw_sample": snippet,
                "total_items": len(raw) if isinstance(raw, list) else 1,
                **_df_preview(df, head, columns),
            }
        except (AttributeError, TypeError, ValueError):
            return {"type": "json", "raw_sample": snippet}

    # Tabular (csv / tsv)
    sep = "\t" if ext == ".tsv" else ","
    try:
        df = pd.read_csv(resolved, sep=sep, engine="python", on_bad_lines="skip")
    except Exception as exc:
        return {"error": f"Read error: {exc}"}

    return {"type": "tabular", **_df_preview(df, head, columns)}


def search_data(
    query: str,
    search_type: str,
    results_dir: Path,
    directory: str | None = None,
) -> dict:
    """Search across data files for column names, values, or filenames.

    *search_type* is one of ``"column"``, ``"value"``, ``"filename"``.
    """
    import pandas as pd

    base = results_dir
    if directory:
        resolved = safe_resolve(directory, results_dir)
        if resolved is None:
            return {"error": f"Directory outside sandbox: {directory}"}
        base = resolved

    if search_type not in ("column", "value", "filename"):
        return {"error": f"Invalid search_type: {search_type}. Use 'column', 'value', or 'filename'."}

    data_files = sorted(
        [p for p in base.rglob("*") if p.is_file() and p.suffix.lower() in TABULAR_EXTS],
        key=lambda p: str(p),
    )

    query_lower = query.lower()
    results = []

    for fp in data_files[:50]:  # limit scan breadth
        try:
            rel = str(fp.relative_to(results_dir))
        except ValueError:
            continue

        if search_type == "filename":
            if query_lower in fp.name.lower():
                results.append({"file": rel, "match_type": "filename"})
            continue

        # Need to read the file for column / value search
        ext = fp.suffix.lower()
        try:
            if ext == ".json":
                df = _json_to_dataframe(fp)
            elif ext == ".md":
                continue  # skip markdown for column/value search
            else:
                sep = "\t" if ext == ".tsv" else ","
                df = pd.read_csv(fp, sep=sep, engine="python", on_bad_lines="skip")
        except (pd.errors.ParserError, ValueError, UnicodeDecodeError):
            continue

        if search_type == "column":
            matching_cols = [c for c in df.columns if query_lower in c.lower()]
            if matching_cols:
                results.append({
                    "file": rel,
                    "match_type": "column",
                    "matching_columns": matching_cols,
                })
        elif search_type == "value":
            for col in df.columns:
                try:
                    mask = df[col].astype(str).str.contains(query, case=False, na=False, regex=False)
                    if mask.any():
                        count = int(mask.sum())
                        results.append({
                            "file": rel,
                            "match_type": "value",
                            "column": col,
                            "match_count": count,
                        })
                except (ValueError, TypeError):
                    continue

    return {"query": query, "search_type": search_type, "results": results}


def get_data_summary(
    file_path: str,
    results_dir: Path,
    columns: list[str] | None = None,
) -> dict:
    """Statistical summary of a data file: dtypes, ranges, uniques, missing."""
    import pandas as pd

    resolved = safe_resolve(file_path, results_dir)
    if resolved is None:
        return {"error": f"Path outside sandbox: {file_path}"}
    if not resolved.is_file():
        return {"error": f"File not found: {file_path}"}

    ext = resolved.suffix.lower()
    if ext in BINARY_EXTS:
        return {"error": "Cannot summarise binary files"}
    if ext == ".md":
        return {"error": "Cannot summarise markdown files (use read_data_file instead)"}

    try:
        if ext == ".json":
            df = _json_to_dataframe(resolved)
        else:
            sep = "\t" if ext == ".tsv" else ","
            df = pd.read_csv(resolved, sep=sep, engine="python", on_bad_lines="skip")
    except Exception as exc:
        return {"error": f"Read error: {exc}"}

    if columns:
        missing = [c for c in columns if c not in df.columns]
        if missing:
            return {"error": f"Columns not found: {missing}", "available_columns": list(df.columns)}
        df = df[columns]

    summary: dict = {
        "rows": len(df),
        "columns": {},
    }

    for col in df.columns:
        col_info: dict = {"dtype": str(df[col].dtype), "missing": int(df[col].isna().sum())}
        if pd.api.types.is_numeric_dtype(df[col]):
            col_info["min"] = _safe_scalar(df[col].min())
            col_info["max"] = _safe_scalar(df[col].max())
            col_info["mean"] = _safe_scalar(df[col].mean())
            col_info["std"] = _safe_scalar(df[col].std())
        else:
            nunique = df[col].nunique()
            col_info["nunique"] = nunique
            uniques = df[col].dropna().unique().tolist()
            if len(uniques) <= 20:
                col_info["unique_values"] = uniques
            else:
                col_info["unique_values_sample"] = uniques[:20]
                col_info["unique_values_total"] = len(uniques)
        summary["columns"][col] = col_info

    return summary


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

_TOOL_DISPATCH = {
    "list_data_files": lambda args, rd: list_data_files(
        pattern=args.get("pattern", "*"),
        results_dir=rd,
        directory=args.get("directory"),
    ),
    "read_data_file": lambda args, rd: read_data_file(
        file_path=args["file_path"],
        results_dir=rd,
        head=args.get("head", 5),
        columns=args.get("columns"),
    ),
    "search_data": lambda args, rd: search_data(
        query=args["query"],
        search_type=args["search_type"],
        results_dir=rd,
        directory=args.get("directory"),
    ),
    "get_data_summary": lambda args, rd: get_data_summary(
        file_path=args["file_path"],
        results_dir=rd,
        columns=args.get("columns"),
    ),
}


def execute_data_tool(tool_name: str, args: dict, results_dir: Path) -> dict:
    """Dispatch a data-tool call by name.  Returns a JSON-serializable dict."""
    handler = _TOOL_DISPATCH.get(tool_name)
    if handler is None:
        return {"error": f"Unknown data tool: {tool_name}"}
    try:
        return handler(args, results_dir)
    except Exception as exc:
        return {"error": f"Tool execution failed: {exc}"}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _df_preview(df, head: int, columns: list[str] | None) -> dict:
    """Build a preview dict from a DataFrame."""
    if columns:
        available = [c for c in columns if c in df.columns]
        if available:
            df = df[available]

    buf = io.StringIO()
    df.head(head).to_csv(buf, sep="\t", index=False)
    preview_text = buf.getvalue().rstrip()

    return {
        "row_count": len(df),
        "columns": [
            {"name": c, "dtype": str(df[c].dtype)}
            for c in df.columns
        ],
        "preview": preview_text,
    }


def _json_to_dataframe(fp: Path):
    """Convert a JSON file to a pandas DataFrame.

    Mirrors ``figure_pipeline._json_to_dataframe``.
    """
    import pandas as pd

    with open(fp, "r", encoding="utf-8") as f:
        raw = json.load(f)

    if isinstance(raw, list):
        if raw and isinstance(raw[0], dict):
            return pd.json_normalize(raw)
        return pd.DataFrame(raw, columns=["value"])
    if isinstance(raw, dict):
        if raw and all(isinstance(v, list) for v in raw.values()):
            lens = {len(v) for v in raw.values()}
            if len(lens) == 1:
                return pd.DataFrame(raw)
        return pd.json_normalize(raw)
    return pd.DataFrame([{"value": raw}])


def _safe_scalar(val):
    """Convert numpy scalars to Python builtins for JSON serialisation."""
    try:
        if hasattr(val, "item"):
            return val.item()
    except (AttributeError, TypeError, ValueError):
        pass
    return val
