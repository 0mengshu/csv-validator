import json
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
from pathlib import Path

import data_check


class DataCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)

    def write_csv(self, content):
        path = self.root / "input.csv"
        path.write_text(content, encoding="utf-8")
        return path

    def test_detects_required_type_and_duplicate_issues_without_values(self):
        path = self.write_csv(
            "id,amount\n"
            "1,3.5\n"
            "1,-2\n"
            "3,not-a-number\n"
            ",4\n"
        )
        config = {
            "not_null": ["id"],
            "unique": [["id"]],
            "columns": {"amount": {"type": "number", "min": 0}},
        }

        report = data_check.check_csv(path, config, "utf-8-sig")

        self.assertEqual(report["record_count"], 4)
        self.assertEqual(report["issue_count"], 4)
        self.assertEqual(report["issues_truncated"], 0)
        self.assertNotIn("not-a-number", json.dumps(report))

    def test_missing_columns_reported(self):
        path = self.write_csv("id\n1\n")
        report = data_check.check_csv(
            path,
            {"required_columns": ["id", "amount"]},
            "utf-8-sig",
        )
        self.assertEqual(report["issue_count"], 1)
        self.assertEqual(report["issues"][0]["column"], "amount")

    def test_rejects_duplicate_headers(self):
        path = self.write_csv("id,id\n1,2\n")
        with self.assertRaisesRegex(RuntimeError, "duplicate column names"):
            data_check.check_csv(path, {}, "utf-8-sig")

    def test_issue_sample_limit_preserves_total_count(self):
        path = self.write_csv("id,other\n,x\n,x\n,x\n")
        report = data_check.check_csv(
            path,
            {"not_null": ["id"], "max_issues": 1},
            "utf-8-sig",
        )
        self.assertEqual(report["issue_count"], 3)
        self.assertEqual(len(report["issues"]), 1)
        self.assertEqual(report["issues_truncated"], 2)

    def test_rejects_invalid_configuration(self):
        with self.assertRaises(data_check.ConfigurationError):
            data_check.load_config(self.root / "missing.json")

    def test_warning_does_not_fail_check_and_html_is_escaped(self):
        path = self.write_csv("<img src=x>\n<bad>\n")
        report = data_check.check_csv(
            path,
            {
                "allowed_values": {"<img src=x>": ["ok"]},
                "severity": {"allowed_values": "warning"},
            },
            "utf-8-sig",
        )
        self.assertEqual(report["error_count"], 0)
        self.assertEqual(report["warning_count"], 1)
        self.assertEqual(report["issues"][0]["severity"], "warning")

        html_path = self.root / "report.html"
        data_check.write_html_report(html_path, report)
        html_report = html_path.read_text(encoding="utf-8")
        self.assertIn("&lt;img src=x&gt;", html_report)
        self.assertNotIn("<img src=x>", html_report)
        self.assertNotIn("<bad>", html_report)
        self.assertIn('id="search"', html_report)
        self.assertIn("Warnings", html_report)

    def test_warning_only_cli_run_returns_success(self):
        csv_path = self.write_csv("status\nunknown\n")
        config_path = self.root / "rules.json"
        config_path.write_text(
            json.dumps(
                {
                    "allowed_values": {"status": ["ok"]},
                    "severity": {"allowed_values": "warning"},
                }
            ),
            encoding="utf-8",
        )
        output = StringIO()
        with redirect_stdout(output), redirect_stderr(StringIO()):
            result = data_check.main(
                [str(csv_path), "--config", str(config_path), "--html-report", str(self.root / "report.html")]
            )
        self.assertEqual(result, 0)
        self.assertIn("0 error(s), 1 warning(s)", output.getvalue())

    def test_invalid_severity_is_rejected(self):
        config_path = self.root / "rules.json"
        config_path.write_text(
            json.dumps({"severity": {"unknown_rule": "warning"}}),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(data_check.ConfigurationError, "Invalid severity"):
            data_check.load_config(config_path)

    def test_boolean_type_accepts_true_false_only(self):
        path = self.write_csv("flag\ntrue\nFALSE\nTrue\nyes\n1\n\n")
        report = data_check.check_csv(
            path,
            {"columns": {"flag": {"type": "boolean"}}},
            "utf-8-sig",
        )
        # "yes" 和 "1" 不合法；空值跳过类型检查
        self.assertEqual(report["issue_count"], 2)
        self.assertEqual(
            [issue["row"] for issue in report["issues"]], [5, 6]
        )

    def test_numeric_boundaries_are_inclusive(self):
        path = self.write_csv("amount\n0\n100\n-0.01\n100.01\n")
        report = data_check.check_csv(
            path,
            {"columns": {"amount": {"type": "number", "min": 0, "max": 100}}},
            "utf-8-sig",
        )
        # 边界值 0 和 100 合法，越界值报错
        self.assertEqual(report["issue_count"], 2)
        self.assertEqual(
            [issue["row"] for issue in report["issues"]], [4, 5]
        )

    def test_integer_type_rejects_floats(self):
        path = self.write_csv("qty\n3\n-2\n3.5\nabc\n")
        report = data_check.check_csv(
            path,
            {"columns": {"qty": {"type": "integer"}}},
            "utf-8-sig",
        )
        self.assertEqual(report["issue_count"], 2)
        self.assertEqual(
            [issue["row"] for issue in report["issues"]], [4, 5]
        )

    def test_date_format_parsing(self):
        path = self.write_csv(
            "created_at\n2024-01-15\n2024/01/16\n2024-02-30\n2024-12-31\n"
        )
        report = data_check.check_csv(
            path,
            {"columns": {"created_at": {"type": "date", "format": "%Y-%m-%d"}}},
            "utf-8-sig",
        )
        # 分隔符错误和不存在日期（2 月 30 日）都应报错
        self.assertEqual(report["issue_count"], 2)
        self.assertEqual(
            [issue["row"] for issue in report["issues"]], [3, 4]
        )

    def test_write_report_roundtrip(self):
        # 注意：完全空白的行会被 csv 模块跳过，这里用 ",x" 制造空的 id
        path = self.write_csv("id,other\n,x\n,y\n")
        report = data_check.check_csv(
            path, {"not_null": ["id"]}, "utf-8-sig"
        )
        report_path = self.root / "report.json"
        data_check.write_report(report_path, report)

        loaded = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(loaded["record_count"], 2)
        self.assertEqual(loaded["error_count"], 2)
        self.assertEqual(loaded["issues"][0]["rule"], "not_null")

    def test_write_report_overwrites_existing_file(self):
        report_path = self.root / "report.json"
        report_path.write_text("stale content", encoding="utf-8")
        path = self.write_csv("id\n1\n")
        report = data_check.check_csv(path, {}, "utf-8-sig")
        data_check.write_report(report_path, report)
        loaded = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(loaded["error_count"], 0)
        self.assertNotIn("stale content", report_path.read_text(encoding="utf-8"))

    def test_allowed_values_strips_whitespace(self):
        path = self.write_csv("status\n paid \n")
        report = data_check.check_csv(
            path,
            {"allowed_values": {"status": ["paid"]}},
            "utf-8-sig",
        )
        self.assertEqual(report["issue_count"], 0)

    def test_invalid_severity_type_is_rejected(self):
        config_path = self.root / "rules.json"
        config_path.write_text(
            json.dumps({"severity": {"type_or_range:numbr": "warning"}}),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(data_check.ConfigurationError, "Invalid severity"):
            data_check.load_config(config_path)

    def test_number_rejects_underscore_separators(self):
        path = self.write_csv("amount\n1_000\n")
        report = data_check.check_csv(
            path,
            {"columns": {"amount": {"type": "number"}}},
            "utf-8-sig",
        )
        self.assertEqual(report["issue_count"], 1)

    def test_missing_column_reported_once(self):
        path = self.write_csv("id\n1\n")
        report = data_check.check_csv(
            path,
            {"not_null": ["amount"], "columns": {"amount": {"type": "number"}}},
            "utf-8-sig",
        )
        self.assertEqual(report["issue_count"], 1)
        self.assertEqual(report["issues"][0]["column"], "amount")


if __name__ == "__main__":
    unittest.main()
