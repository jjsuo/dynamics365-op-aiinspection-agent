"""
data_reader.py — D365-HealthGuard 统一数据读取工具

职责：纯数据读取，不做任何分析/聚合/阈值判断。
     所有分析逻辑交由对应 Skill 处理。

目录约定（DATA_ROOT 可配置）：
    <DATA_ROOT>/<category>/<YYYY-MM-DD>/*.csv    # 按日期归档的数据
    <DATA_ROOT>/<category>/*.csv                 # 可选的静态文件（如 index_existing.csv）

DATA_ROOT 解析顺序（优先级从高到低）：
    1. --data-root CLI 参数
    2. DATA_ROOT 环境变量
    3. 默认：<项目根目录>/data（项目根 = tools/ 的父目录）

预置类别（category）：
    sql_blocking, slow_sql, sql_index, server_per_sql,
    iis_logs, windows_health, plugin_scan
    也支持 DATA_ROOT 下任意存在的子目录作为自定义 category。

特殊文件类型：
    - .zip: 仅列出压缩包内容清单（不解压），供 plugin_scanner 等 Skill 自行处理。

用法：
    python3 tools/data_reader.py --category slow_sql --today
    python3 tools/data_reader.py --category slow_sql --yesterday
    python3 tools/data_reader.py --category slow_sql --last-3
    python3 tools/data_reader.py --category slow_sql --last-7
    python3 tools/data_reader.py --category slow_sql --last-30
    python3 tools/data_reader.py --category slow_sql --date 2026-04-29
    python3 tools/data_reader.py --category slow_sql --start 2026-04-20 --end 2026-04-29
    python3 tools/data_reader.py --category slow_sql --list-dates
    python3 tools/data_reader.py --list-categories
    python3 tools/data_reader.py --category sql_index --today --data-root /custom/path

返回结构：
    {
      "status": "ok" | "error",
      "data_root": "...",
      "category": "...",
      "date_range": "YYYY-MM-DD ~ YYYY-MM-DD",
      "loaded":  ["YYYY-MM-DD/xxx.csv", ...],
      "missing": ["YYYY-MM-DD", ...],
      "files": [
        {
          "file": "xxx.csv",
          "date": "YYYY-MM-DD",
          "path": "relative/path/to/file",
          "total_rows": N,
          "columns": [...],
          "data": [ {col: val, ..., "_date": "YYYY-MM-DD", "_file": "xxx.csv"}, ... ]
        }, ...
      ],
      "static_files": [  # category 根目录下的 csv / json（如 index_existing.csv）
        { ... 与 files 同构 ...}
      ]
    }
"""

import os
import sys
import json
import argparse
from datetime import datetime, timedelta

try:
    import pandas as pd
except ImportError:
    print(json.dumps({"error": "pip install pandas"}))
    sys.exit(1)


# -----------------------------------------------------------------------------
# DATA_ROOT 解析
# -----------------------------------------------------------------------------

def _project_root() -> str:
    """项目根目录 = tools/ 的父目录。"""
    return os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


def resolve_data_root(cli_value: str = None) -> str:
    """按优先级解析 DATA_ROOT：CLI > env > 项目默认。"""
    if cli_value:
        return os.path.abspath(cli_value)
    env_value = os.environ.get("DATA_ROOT")
    if env_value:
        return os.path.abspath(env_value)
    return os.path.join(_project_root(), "data")


# -----------------------------------------------------------------------------
# 目录 / 日期辅助
# -----------------------------------------------------------------------------

KNOWN_CATEGORIES = [
    "sql_blocking",
    "slow_sql",
    "sql_index",
    "server_per_sql",
    "iis_logs",
    "windows_health",
    "plugin_scan",
]


def list_categories(data_root: str):
    if not os.path.isdir(data_root):
        return []
    return sorted(
        d for d in os.listdir(data_root)
        if os.path.isdir(os.path.join(data_root, d)) and not d.startswith(".")
    )


def category_path(data_root: str, category: str) -> str:
    return os.path.join(data_root, category)


def available_dates(data_root: str, category: str):
    base = category_path(data_root, category)
    if not os.path.isdir(base):
        return []
    return sorted(
        d for d in os.listdir(base)
        if os.path.isdir(os.path.join(base, d)) and len(d) == 10 and d[4] == "-" and d[7] == "-"
    )


def latest_date(data_root: str, category: str):
    dates = available_dates(data_root, category)
    return dates[-1] if dates else None


def resolve_date_range(args, data_root: str, category: str):
    today = datetime.now().date()
    if args.today:      return str(today), str(today)
    if args.yesterday:  d = today - timedelta(1);          return str(d), str(d)
    if args.last_3:     return str(today - timedelta(2)),  str(today)
    if args.last_7:     return str(today - timedelta(6)),  str(today)
    if args.last_30:    return str(today - timedelta(29)), str(today)
    if args.date:       return args.date, args.date
    if args.start:      return args.start, (args.end or str(today))
    # 缺省：取该 category 最新一天
    latest = latest_date(data_root, category)
    return (latest, latest) if latest else (str(today), str(today))


def iter_dates(start: str, end: str):
    cur = datetime.strptime(start, "%Y-%m-%d").date()
    end_dt = datetime.strptime(end, "%Y-%m-%d").date()
    while cur <= end_dt:
        yield str(cur)
        cur += timedelta(1)


# -----------------------------------------------------------------------------
# 文件读取（纯 IO，无业务逻辑）
# -----------------------------------------------------------------------------

def _read_csv(path: str, date_tag: str):
    try:
        df = pd.read_csv(path, encoding="utf-8-sig")
    except Exception as e:
        print(f"[skip] {path}: {e}", file=sys.stderr)
        return None
    df["_date"] = date_tag
    df["_file"] = os.path.basename(path)
    return df


def _read_w3c_log(path: str, date_tag: str):
    """解析 IIS W3C 日志为记录列表。仅做格式解析，不做统计。"""
    rows, headers = [], []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.rstrip()
                if line.startswith("#Fields:"):
                    headers = line[len("#Fields:"):].strip().split()
                elif line.startswith("#") or not line:
                    continue
                elif headers:
                    parts = line.split(" ", len(headers) - 1)
                    if len(parts) == len(headers):
                        rec = dict(zip(headers, parts))
                        rec["_date"] = date_tag
                        rec["_file"] = os.path.basename(path)
                        rows.append(rec)
    except Exception as e:
        print(f"[skip] {path}: {e}", file=sys.stderr)
    return rows


def _read_json(path: str, date_tag: str):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as e:
        print(f"[skip] {path}: {e}", file=sys.stderr)
        return None
    if isinstance(data, dict):
        data.setdefault("_date", date_tag)
        data.setdefault("_file", os.path.basename(path))
    return data


def _read_zip_manifest(path: str):
    """列出 ZIP 内条目清单，不解压。供 plugin_scanner 等 Skill 自行处理。"""
    import zipfile
    entries, err = [], None
    try:
        with zipfile.ZipFile(path, "r") as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                entries.append({
                    "name": info.filename,
                    "size": info.file_size,
                    "compressed_size": info.compress_size,
                })
    except Exception as e:
        err = str(e)
        print(f"[skip] {path}: {e}", file=sys.stderr)
    return entries, err


def _file_to_payload(abs_path: str, rel_path: str, date_tag: str):
    """统一封装文件内容到输出 payload。不对数据做分析。"""
    fname = os.path.basename(abs_path)
    low = fname.lower()

    if low.endswith(".csv"):
        df = _read_csv(abs_path, date_tag)
        if df is None:
            return None
        return {
            "file": fname,
            "date": date_tag,
            "path": rel_path,
            "kind": "csv",
            "total_rows": len(df),
            "columns": list(df.columns),
            "data": df.to_dict(orient="records"),
        }

    if low.endswith(".log"):
        rows = _read_w3c_log(abs_path, date_tag)
        cols = list(rows[0].keys()) if rows else []
        return {
            "file": fname,
            "date": date_tag,
            "path": rel_path,
            "kind": "w3c_log",
            "total_rows": len(rows),
            "columns": cols,
            "data": rows,
        }

    if low.endswith(".json"):
        data = _read_json(abs_path, date_tag)
        if data is None:
            return None
        return {
            "file": fname,
            "date": date_tag,
            "path": rel_path,
            "kind": "json",
            "data": data,
        }

    if low.endswith(".zip"):
        entries, err = _read_zip_manifest(abs_path)
        payload = {
            "file": fname,
            "date": date_tag,
            "path": rel_path,
            "kind": "zip",
            "abs_path": abs_path,                      # Skill 需要实际路径做解压
            "total_entries": len(entries),
            "total_size": sum(e["size"] for e in entries),
            "entries": entries,
            "note": "archive listed but not extracted; Skill should extract if needed",
        }
        if err:
            payload["error"] = err
        return payload

    return None  # 其他类型暂不支持


# -----------------------------------------------------------------------------
# 主读取流程
# -----------------------------------------------------------------------------

def read(data_root: str, category: str, start: str, end: str):
    base = category_path(data_root, category)
    if not os.path.isdir(base):
        return {
            "status": "error",
            "error": f"category 目录不存在: {base}",
            "data_root": data_root,
            "category": category,
            "available_categories": list_categories(data_root),
        }

    files, loaded, missing = [], [], []

    for ds in iter_dates(start, end):
        folder = os.path.join(base, ds)
        if not os.path.isdir(folder):
            missing.append(ds)
            continue

        names = [n for n in os.listdir(folder) if not n.startswith(".")]
        day_loaded = False
        for fname in sorted(names):
            abs_path = os.path.join(folder, fname)
            if not os.path.isfile(abs_path):
                continue
            rel_path = os.path.relpath(abs_path, data_root)
            payload = _file_to_payload(abs_path, rel_path, ds)
            if payload is not None:
                files.append(payload)
                loaded.append(f"{ds}/{fname}")
                day_loaded = True
        if not day_loaded:
            missing.append(ds)

    # 静态文件（category 根目录下的 csv / json，非日期子目录）
    static_files = []
    for fname in sorted(os.listdir(base)):
        abs_path = os.path.join(base, fname)
        if not os.path.isfile(abs_path):
            continue
        rel_path = os.path.relpath(abs_path, data_root)
        payload = _file_to_payload(abs_path, rel_path, "static")
        if payload is not None:
            static_files.append(payload)

    if not files and not static_files:
        return {
            "status": "error",
            "error": "该日期范围与类别下无可读数据",
            "data_root": data_root,
            "category": category,
            "date_range": f"{start} ~ {end}",
            "missing": missing,
            "available_dates": available_dates(data_root, category),
        }

    return {
        "status": "ok",
        "data_root": data_root,
        "category": category,
        "date_range": f"{start} ~ {end}",
        "loaded": loaded,
        "missing": missing,
        "files": files,
        "static_files": static_files,
    }


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(
        description="D365-HealthGuard 统一数据读取工具（纯读取，不做任何分析）",
    )
    p.add_argument("--category", type=str, help=f"数据类别，常见值：{', '.join(KNOWN_CATEGORIES)}")
    p.add_argument("--data-root", type=str, dest="data_root",
                   help="数据根目录（默认 <项目>/data，可用 $DATA_ROOT 覆盖）")

    # 时间选择（互斥组，argparse 不强制互斥以保持现有调用兼容性）
    p.add_argument("--today",      action="store_true")
    p.add_argument("--yesterday",  action="store_true")
    p.add_argument("--last-3",     action="store_true", dest="last_3")
    p.add_argument("--last-7",     action="store_true", dest="last_7")
    p.add_argument("--last-30",    action="store_true", dest="last_30")
    p.add_argument("--date",       type=str)
    p.add_argument("--start",      type=str)
    p.add_argument("--end",        type=str)

    p.add_argument("--list-categories", action="store_true", dest="list_categories")
    p.add_argument("--list-dates",      action="store_true", dest="list_dates")
    return p


def main():
    args = build_parser().parse_args()
    data_root = resolve_data_root(args.data_root)

    if args.list_categories:
        print(json.dumps({
            "data_root": data_root,
            "categories": list_categories(data_root),
            "known_categories": KNOWN_CATEGORIES,
        }, ensure_ascii=False, indent=2))
        return

    if not args.category:
        print(json.dumps({
            "status": "error",
            "error": "必须指定 --category（或使用 --list-categories 查看可用类别）",
            "data_root": data_root,
            "available_categories": list_categories(data_root),
            "known_categories": KNOWN_CATEGORIES,
        }, ensure_ascii=False, indent=2))
        sys.exit(2)

    if args.list_dates:
        print(json.dumps({
            "data_root": data_root,
            "category": args.category,
            "available_dates": available_dates(data_root, args.category),
        }, ensure_ascii=False, indent=2))
        return

    start, end = resolve_date_range(args, data_root, args.category)
    result = read(data_root, args.category, start, end)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
