#!/usr/bin/env python3
"""Validate a CSV file using a JSON rules configuration."""

from __future__ import annotations

import argparse
import csv
import html
import json
import re
import sys
import tempfile
from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


CONFIG_KEYS = {
    "required_columns",
    "not_null",
    "unique",
    "columns",
    "allowed_values",
    "max_issues",
    "severity",
}

__version__ = "0.1.0"
COLUMN_RULE_KEYS = {"type", "min", "max", "format"}
SUPPORTED_TYPES = {"string", "integer", "number", "date", "boolean"}
DEFAULT_SEVERITY = "error"
SEVERITIES = {"error", "warning"}
RULE_NAMES = {
    "required_column",
    "not_null",
    "unique",
    "column_rule",
    "type_or_range",
    "allowed_values",
    "row_width",
}


class ConfigurationError(Exception):
    pass


def load_config(path: Path) -> dict[str, Any]:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"Cannot read config '{path}': {exc}") from exc

    if not isinstance(config, dict):
        raise ConfigurationError("Config must be a JSON object.")
    unknown = set(config) - CONFIG_KEYS
    if unknown:
        raise ConfigurationError(f"Unknown config keys: {', '.join(sorted(unknown))}")

    for key in ("required_columns", "not_null"):
        value = config.get(key, [])
        if not isinstance(value, list) or not all(
            isinstance(item, str) and item for item in value
        ):
            raise ConfigurationError(f"'{key}' must be a list of non-empty strings.")

    unique = config.get("unique", [])
    if not isinstance(unique, list) or not all(
        isinstance(group, list)
        and group
        and all(isinstance(column, str) and column for column in group)
        for group in unique
    ):
        raise ConfigurationError("'unique' must be a list of non-empty column lists.")

    columns = config.get("columns", {})
    if not isinstance(columns, dict):
        raise ConfigurationError("'columns' must be an object.")
    for name, rule in columns.items():
        if not isinstance(name, str) or not name or not isinstance(rule, dict):
            raise ConfigurationError("'columns' entries must map column names to objects.")
        unknown_rule_keys = set(rule) - COLUMN_RULE_KEYS
        if unknown_rule_keys:
            raise ConfigurationError(
                f"Unknown rule keys for '{name}': {', '.join(sorted(unknown_rule_keys))}"
            )
        data_type = rule.get("type")
        if not isinstance(data_type, str) or data_type not in SUPPORTED_TYPES:
            raise ConfigurationError(
                f"Column '{name}' must specify a supported type: "
                f"{', '.join(sorted(SUPPORTED_TYPES))}."
            )
        if data_type not in {"integer", "number"} and (
            "min" in rule or "max" in rule
        ):
            raise ConfigurationError(f"'min' and 'max' only apply to numeric column '{name}'.")
        if data_type == "date" and not isinstance(rule.get("format"), str):
            raise ConfigurationError(f"Date column '{name}' requires a 'format'.")
        for bound in ("min", "max"):
            if bound in rule:
                try:
                    limit = Decimal(str(rule[bound]))
                except InvalidOperation as exc:
                    raise ConfigurationError(
                        f"'{bound}' for column '{name}' must be numeric."
                    ) from exc
                if not limit.is_finite():
                    raise ConfigurationError(
                        f"'{bound}' for column '{name}' must be finite."
                    )
        if "min" in rule and "max" in rule:
            if Decimal(str(rule["min"])) > Decimal(str(rule["max"])):
                raise ConfigurationError(
                    f"'min' cannot exceed 'max' for column '{name}'."
                )

    allowed_values = config.get("allowed_values", {})
    if not isinstance(allowed_values, dict) or not all(
        isinstance(name, str)
        and name
        and isinstance(values, list)
        for name, values in allowed_values.items()
    ):
        raise ConfigurationError("'allowed_values' must map column names to lists.")

    max_issues = config.get("max_issues", 200)
    if not isinstance(max_issues, int) or isinstance(max_issues, bool) or max_issues < 0:
        raise ConfigurationError("'max_issues' must be a non-negative integer.")

    severity = config.get("severity", {})
    if not isinstance(severity, dict):
        raise ConfigurationError("'severity' must map rule names to 'error' or 'warning'.")
    for rule, level in severity.items():
        if not isinstance(rule, str) or not rule:
            raise ConfigurationError("Severity rule names must be non-empty strings.")
        base_rule = rule.split(":", 1)[0]
        if (
            base_rule not in RULE_NAMES
            or (":" in rule and base_rule != "type_or_range")
            or (
                base_rule == "type_or_range"
                and ":" in rule
                and rule.split(":", 1)[1] not in SUPPORTED_TYPES
            )
            or not isinstance(level, str)
            or level not in SEVERITIES
        ):
            raise ConfigurationError(
                f"Invalid severity entry '{rule}': use a supported rule and "
                "'error' or 'warning'."
            )

    return config


def is_blank(value: str | None) -> bool:
    return value is None or value.strip() == ""


def validate_value(value: str, rule: dict[str, Any]) -> str | None:
    data_type = rule["type"]
    try:
        if data_type == "string":
            parsed: Any = value
        elif data_type == "integer":
            if not re.fullmatch(r"[+-]?\d+", value.strip()):
                return "must be an integer"
            parsed = int(value)
        elif data_type == "number":
            if "_" in value:
                return "must be a valid number"
            parsed = Decimal(value.strip())
            if not parsed.is_finite():
                return "must be a finite number"
        elif data_type == "date":
            datetime.strptime(value.strip(), rule["format"])
            return None
        else:
            if value.strip().casefold() not in {"true", "false"}:
                return "must be true or false"
            return None
    except (ValueError, InvalidOperation):
        return f"must be a valid {data_type}"

    for bound in ("min", "max"):
        if bound in rule:
            limit = Decimal(str(rule[bound]))
            actual = Decimal(parsed)
            if bound == "min" and actual < limit:
                return f"must be at least {rule[bound]}"
            if bound == "max" and actual > limit:
                return f"must be at most {rule[bound]}"
    return None


def add_issue(
    report: dict[str, Any],
    rule: str,
    row: int | None,
    column: str | None,
    message: str,
) -> None:
    report["issue_count"] += 1
    report["issues_by_rule"][rule] += 1
    severity = report["severity_config"].get(rule)
    if severity is None and rule.startswith("type_or_range:"):
        severity = report["severity_config"].get("type_or_range")
    severity = severity or DEFAULT_SEVERITY
    report["issues_by_severity"][severity] += 1
    if len(report["issues"]) < report["max_issues"]:
        report["issues"].append(
            {
                "row": row,
                "column": column,
                "rule": rule,
                "severity": severity,
                "message": message,
            }
        )


def check_csv(path: Path, config: dict[str, Any], encoding: str) -> dict[str, Any]:
    report: dict[str, Any] = {
        "input_file": path.name,
        "record_count": 0,
        "issue_count": 0,
        "issues_by_rule": Counter(),
        "issues_by_severity": Counter(),
        "issues": [],
        "max_issues": config.get("max_issues", 200),
        "severity_config": config.get("severity", {}),
    }

    try:
        with path.open("r", encoding=encoding, newline="") as source:
            reader = csv.DictReader(source, strict=True)
            headers = reader.fieldnames
            if headers is None:
                raise ValueError("CSV is empty or has no header row.")

            if any(not header or not header.strip() for header in headers):
                raise ValueError("CSV contains an empty column name.")
            if len(headers) != len(set(headers)):
                raise ValueError("CSV contains duplicate column names.")

            missing_columns: set[str] = set()
            for name in config.get("required_columns", []):
                if name not in headers:
                    missing_columns.add(name)
            for name in config.get("not_null", []):
                if name not in headers:
                    missing_columns.add(name)
            for name in config.get("columns", {}):
                if name not in headers:
                    missing_columns.add(name)
            for name in config.get("allowed_values", {}):
                if name not in headers:
                    missing_columns.add(name)
            for group in config.get("unique", []):
                for name in group:
                    if name not in headers:
                        missing_columns.add(name)
            for name in sorted(missing_columns):
                add_issue(report, "required_column", None, name, "column is missing")

            unique_seen: dict[tuple[str, ...], set[tuple[str, ...]]] = {
                tuple(group): set() for group in config.get("unique", [])
            }
            row_number = 1
            for row in reader:
                row_number += 1
                if not row:
                    continue
                report["record_count"] += 1

                if None in row:
                    add_issue(
                        report,
                        "row_width",
                        row_number,
                        None,
                        "row has more values than the header",
                    )
                if any(row.get(header) is None for header in headers):
                    add_issue(
                        report,
                        "row_width",
                        row_number,
                        None,
                        "row has fewer values than the header",
                    )

                for name in config.get("not_null", []):
                    if name in headers and is_blank(row.get(name)):
                        add_issue(report, "not_null", row_number, name, "value is required")

                for name, rule in config.get("columns", {}).items():
                    value = row.get(name)
                    if name in headers and not is_blank(value):
                        message = validate_value(value or "", rule)
                        if message:
                            add_issue(report, f"type_or_range:{rule['type']}", row_number, name, message)

                for name, accepted in config.get("allowed_values", {}).items():
                    value = row.get(name)
                    if name in headers and not is_blank(value) and value.strip() not in accepted:
                        add_issue(report, "allowed_values", row_number, name, "value is not allowed")

                for group in config.get("unique", []):
                    if all(name in headers for name in group):
                        values = tuple((row.get(name) or "").strip() for name in group)
                        if any(not value for value in values):
                            continue
                        key = tuple(group)
                        if values in unique_seen[key]:
                            add_issue(
                                report,
                                "unique",
                                row_number,
                                ",".join(group),
                                "duplicate value",
                            )
                        else:
                            unique_seen[key].add(values)
    except (OSError, UnicodeError, csv.Error, ValueError) as exc:
        raise RuntimeError(f"Cannot check CSV '{path}': {exc}") from exc

    report["issues_by_rule"] = dict(sorted(report["issues_by_rule"].items()))
    report["issues_by_severity"] = dict(report["issues_by_severity"])
    report["error_count"] = report["issues_by_severity"].get("error", 0)
    report["warning_count"] = report["issues_by_severity"].get("warning", 0)
    report["issues_truncated"] = report["issue_count"] - len(report["issues"])
    report.pop("max_issues")
    report.pop("severity_config")
    return report


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            json.dump(report, temporary, ensure_ascii=False, indent=2)
            temporary.write("\n")
        Path(temporary_name).replace(path)
    except OSError:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)
        raise


def write_html_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for issue in report["issues"]:
        cells = (
            issue["severity"].upper(),
            "" if issue["row"] is None else str(issue["row"]),
            issue["rule"],
            issue["column"] or "",
            issue["message"],
        )
        rows.append(
            "<tr>"
            + "".join(f"<td>{html.escape(value, quote=True)}</td>" for value in cells)
            + "</tr>"
        )
    if not rows:
        rows.append('<tr><td colspan="5">No issues found.</td></tr>')

    rule_rows = "".join(
        "<tr>"
        f"<td>{html.escape(rule, quote=True)}</td>"
        f"<td>{count}</td>"
        "</tr>"
        for rule, count in report["issues_by_rule"].items()
    )
    if not rule_rows:
        rule_rows = '<tr><td colspan="2">No issues found.</td></tr>'

    status = "Passed" if report["error_count"] == 0 else "Failed"
    document = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Data check report</title>
<style>
body{{font:16px system-ui,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem;color:#222}}
h1{{margin-bottom:.25rem}} .muted{{color:#555}} .cards{{display:flex;flex-wrap:wrap;gap:1rem;margin:1.5rem 0}}
.card{{border:1px solid #ccc;border-radius:8px;padding:1rem;min-width:130px}}
.card strong{{display:block;font-size:1.6rem}} .failed{{color:#a12622}} .passed{{color:#176b36}}
table{{border-collapse:collapse;width:100%;margin:1rem 0 2rem}} th,td{{border:1px solid #ccc;padding:.6rem;text-align:left}}
th{{background:#f3f4f6}} input,select{{font:inherit;padding:.45rem;margin:.5rem .5rem .5rem 0}}
@media(prefers-color-scheme:dark){{body{{background:#181818;color:#eee}}th{{background:#333}}th,td,.card{{border-color:#666}}.muted{{color:#bbb}}}}
</style>
</head>
<body>
<h1>Data check report</h1>
<p class="muted">File: {html.escape(report["input_file"], quote=True)} · Status:
<strong class="{"passed" if report["error_count"] == 0 else "failed"}">{status}</strong></p>
<section class="cards" aria-label="Summary">
<div class="card">Records<strong>{report["record_count"]}</strong></div>
<div class="card">Errors<strong>{report["error_count"]}</strong></div>
<div class="card">Warnings<strong>{report["warning_count"]}</strong></div>
<div class="card">Total issues<strong>{report["issue_count"]}</strong></div>
</section>
<h2>Issues by rule</h2>
<table><thead><tr><th>Rule</th><th>Count</th></tr></thead><tbody>{rule_rows}</tbody></table>
<h2>Issue samples</h2>
<label for="search">Search:</label>
<input id="search" type="search" placeholder="Search issue samples">
<label for="severity">Severity:</label>
<select id="severity"><option value="">All</option><option>Error</option><option>Warning</option></select>
<p id="sample-note" class="muted">Showing up to {len(report["issues"])} issue samples.
{report["issues_truncated"]} additional issues omitted.</p>
<table id="issues"><thead><tr><th>Severity</th><th>Row</th><th>Rule</th><th>Column</th><th>Message</th></tr></thead>
<tbody>{"".join(rows)}</tbody></table>
<script>
const search = document.getElementById("search");
const severity = document.getElementById("severity");
function filterRows() {{
  const query = search.value.toLowerCase();
  const level = severity.value.toLowerCase();
  document.querySelectorAll("#issues tbody tr").forEach(row => {{
    const matchesText = row.textContent.toLowerCase().includes(query);
    const matchesLevel = !level || row.cells[0].textContent.toLowerCase() === level;
    row.hidden = !(matchesText && matchesLevel);
  }});
}}
search.addEventListener("input", filterRows);
severity.addEventListener("change", filterRows);
</script>
</body>
</html>
"""

    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(document)
        Path(temporary_name).replace(path)
    except OSError:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check CSV data against JSON rules.",
        prog="data-check",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("csv_file", type=Path, help="CSV file to check")
    parser.add_argument("--config", required=True, type=Path, help="JSON rules file")
    parser.add_argument("--report", type=Path, help="write the full JSON report to this path")
    parser.add_argument("--html-report", type=Path, help="write an interactive HTML report")
    parser.add_argument("--encoding", default="utf-8-sig", help="CSV encoding (default: utf-8-sig)")
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
        output_paths = [path.resolve() for path in (args.report, args.html_report) if path]
        if len(output_paths) != len(set(output_paths)):
            raise RuntimeError("JSON and HTML report paths must be different.")
        if any(path in {
            args.csv_file.resolve(),
            args.config.resolve(),
        } for path in output_paths):
            raise RuntimeError("Report path must not overwrite the CSV or config file.")
        report = check_csv(args.csv_file, config, args.encoding)
        if args.report:
            write_report(args.report, report)
        if args.html_report:
            write_html_report(args.html_report, report)
        print(
            f"Checked {report['record_count']} records: "
            f"{report['error_count']} error(s), {report['warning_count']} warning(s); "
            f"{report['issues_truncated']} omitted from issue samples."
        )
        if args.report:
            print(f"JSON report: {args.report}")
        if args.html_report:
            print(f"HTML report: {args.html_report}")
        return 1 if report["error_count"] else 0
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except (RuntimeError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
