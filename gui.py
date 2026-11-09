"""CSV 数据校验工具 —— 桌面图形界面（tkinter）版本。

用 Python 标准库 tkinter 做窗口，用 matplotlib 渲染图表，复用 data_check.py 的校验逻辑。

依赖说明：
- tkinter：Python 自带（部分 Linux 发行版需单独安装 python3-tk）
- matplotlib：需 pip install matplotlib
"""

import base64
import ctypes
import io
import sys
from pathlib import Path


def enable_windows_dpi_awareness() -> None:
    """让窗口在高 DPI 屏幕上清晰显示（否则 Windows 会缩放位图导致文字发虚）。"""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # system DPI aware
    except Exception:  # pragma: no cover - 老版本 Windows
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:  # pragma: no cover - 仅在缺 tkinter 的环境触发
    raise SystemExit(
        "未找到 tkinter，无法启动图形界面。\n"
        "Linux 用户可执行: sudo apt install python3-tk（或 yum install python3-tkinter）\n"
        "也可以直接使用命令行版本: python data_check.py"
    )

try:
    import matplotlib

    # 图表用 Agg 渲染成 PNG 再交给 tkinter 显示：
    # matplotlib 的 TkAgg 后端在高 DPI 下会重复放大设备像素比，导致图表被裁切
    matplotlib.use("Agg")

    from matplotlib import font_manager
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    from matplotlib.ticker import MaxNLocator
except ImportError:  # pragma: no cover - 仅在缺 matplotlib 的环境触发
    raise SystemExit(
        "图形界面需要 matplotlib，请先安装:\n"
        "    pip install matplotlib\n"
        "也可以直接使用命令行版本: python data_check.py"
    )

import data_check

# 各平台常见中文字体，按优先级尝试，选第一个系统里实际存在的
CJK_FONT_CANDIDATES = [
    "Microsoft YaHei",        # Windows 微软雅黑
    "SimHei",                 # Windows 黑体
    "PingFang SC",            # macOS
    "Hiragino Sans GB",       # macOS
    "Noto Sans CJK SC",       # Linux（Arch/Debian 常见）
    "Noto Sans SC",           # Linux（新版命名）
    "WenQuanYi Micro Hei",    # Linux 文泉驿
    "Source Han Sans SC",     # 思源黑体
]


def pick_cjk_font() -> str | None:
    """返回本机第一个可用的中文字体名，找不到则返回 None。"""
    try:
        available = {font.name for font in font_manager.fontManager.ttflist}
    except Exception:  # pragma: no cover - 字体管理器异常时兜底
        return None
    for name in CJK_FONT_CANDIDATES:
        if name in available:
            return name
    return None


def setup_matplotlib_font() -> None:
    font = pick_cjk_font()
    if font:
        matplotlib.rcParams["font.sans-serif"] = [font, "sans-serif"]
        matplotlib.rcParams["axes.unicode_minus"] = False


setup_matplotlib_font()

# 规则名 -> 中文标签（用于图表和明细表展示）
RULE_LABELS = {
    "required_column": "缺少必填列",
    "not_null": "空值",
    "unique": "重复值",
    "column_rule": "列规则缺列",
    "type_or_range": "类型/范围",
    "allowed_values": "枚举值非法",
    "row_width": "行列数不匹配",
}
TYPE_LABELS = {
    "string": "文本",
    "integer": "整数",
    "number": "数值",
    "date": "日期",
    "boolean": "布尔",
}
ENCODINGS = ["utf-8-sig", "utf-8", "gbk", "gb18030", "big5"]

SCRIPT_DIR = Path(__file__).resolve().parent


def example_path(*filenames: str) -> str:
    """返回 examples/ 下第一个存在的示例文件路径，都没有则返回空串。"""
    for filename in filenames:
        candidate = SCRIPT_DIR / "examples" / filename
        if candidate.exists():
            return str(candidate)
    return ""


def rule_label(rule: str) -> str:
    """把内部规则名翻译成中文标签，如 type_or_range:number -> 类型/范围(数值)。"""
    base, _, suffix = rule.partition(":")
    label = RULE_LABELS.get(base, base)
    if suffix:
        label = f"{label}({TYPE_LABELS.get(suffix, suffix)})"
    return label


class DataCheckApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title("CSV 数据校验工具")
        # 窗口尺寸随屏幕和 DPI 缩放自适应，避免内容被挤出可视区
        screen_w = root.winfo_screenwidth()
        screen_h = root.winfo_screenheight()
        self.scale = root.winfo_fpixels("1i") / 96.0  # 1.0=100%, 1.5=150%, 2.0=200%
        width = int(min(980 * self.scale, screen_w - 60))
        height = int(min(840 * self.scale, screen_h - 80))
        width = max(width, min(int(760 * self.scale), screen_w - 40))
        height = max(height, min(int(600 * self.scale), screen_h - 60))
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 3)
        self.window_width_px = width
        root.geometry(f"{width}x{height}+{x}+{y}")
        root.minsize(min(int(760 * self.scale), screen_w - 40), min(int(560 * self.scale), screen_h - 60))
        self._build_widgets()

    def _build_widgets(self) -> None:
        self.status_var = tk.StringVar(value="请点击「开始校验」")
        self.stat_var = tk.StringVar(value="")

        # 文件选择区
        top = ttk.LabelFrame(self.root, text="选择文件", padding=10)
        top.pack(fill="x", padx=12, pady=(12, 6))

        # 默认填 examples/ 里的演示文件（锚定脚本所在目录，不受启动目录影响）
        self.csv_var = tk.StringVar(value=example_path("sensor.csv", "data.csv"))
        self.config_var = tk.StringVar(
            value=example_path("sensor_rules.json", "rules.json")
        )

        ttk.Label(top, text="数据文件 (CSV):").grid(row=0, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.csv_var, width=60).grid(row=0, column=1, padx=6)
        ttk.Button(top, text="浏览…", command=self._pick_csv).grid(row=0, column=2)

        ttk.Label(top, text="规则文件 (JSON):").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(top, textvariable=self.config_var, width=60).grid(row=1, column=1, padx=6, pady=(8, 0))
        ttk.Button(top, text="浏览…", command=self._pick_config).grid(row=1, column=2, pady=(8, 0))

        ttk.Label(top, text="CSV 编码:").grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.encoding_var = tk.StringVar(value="utf-8-sig")
        self.encoding_box = ttk.Combobox(
            top,
            textvariable=self.encoding_var,
            values=ENCODINGS,
            state="readonly",
            width=12,
        )
        self.encoding_box.grid(row=2, column=1, sticky="w", pady=(8, 0))
        ttk.Label(
            top,
            text="Excel 另存的中文 CSV 通常是 GBK",
            foreground="#888",
        ).grid(row=2, column=1, sticky="e", padx=6, pady=(8, 0))

        # 校验按钮
        ttk.Button(self.root, text="开始校验", command=self._run).pack(anchor="w", padx=12, pady=(0, 6))

        # 统计区
        summary = ttk.LabelFrame(self.root, text="统计结果", padding=10)
        summary.pack(fill="x", padx=12, pady=6)
        self.status_label = ttk.Label(summary, textvariable=self.status_var, font=("", 14, "bold"))
        self.status_label.pack(anchor="w")
        ttk.Label(summary, textvariable=self.stat_var).pack(anchor="w", pady=(4, 0))

        # 图表区：先渲染成 PNG 再显示，避免 matplotlib 在高 DPI 下的缩放错位
        chart_frame = ttk.LabelFrame(self.root, text="问题分布图", padding=6)
        chart_frame.pack(fill="x", padx=12, pady=6)
        self.chart_label = tk.Label(chart_frame, bd=0, bg="#ffffff")
        self.chart_label.pack()
        self._chart_photo = None
        self._draw_empty_chart()

        # 问题明细区
        list_frame = ttk.LabelFrame(self.root, text="问题明细", padding=10)
        list_frame.pack(fill="both", expand=True, padx=12, pady=(6, 12))

        # 行高随字体缩放，否则高 DPI 下文字会被裁掉
        style = ttk.Style(self.root)
        style.configure("Treeview", rowheight=int(26 * getattr(self, "scale", 1.0)))

        columns = ("severity", "row", "column", "rule", "message")
        self.tree = ttk.Treeview(list_frame, columns=columns, show="headings")
        for col, text, width in (
            ("severity", "级别", 70),
            ("row", "行号", 60),
            ("column", "列", 110),
            ("rule", "规则", 150),
            ("message", "说明", 430),
        ):
            self.tree.heading(col, text=text)
            # stretch 让列宽撑满表格宽度，避免右边留下大片空白
            self.tree.column(col, width=int(width * getattr(self, "scale", 1.0)), anchor="w", stretch=True)

        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.tree.tag_configure("error", foreground="#c0392b")
        self.tree.tag_configure("warning", foreground="#e67e22")

    def _pick_csv(self) -> None:
        path = filedialog.askopenfilename(
            title="选择 CSV 数据文件",
            filetypes=[("CSV 文件", "*.csv"), ("所有文件", "*.*")],
        )
        if path:
            self.csv_var.set(path)

    def _pick_config(self) -> None:
        path = filedialog.askopenfilename(
            title="选择规则文件",
            filetypes=[("JSON 文件", "*.json"), ("所有文件", "*.*")],
        )
        if path:
            self.config_var.set(path)

    def _render_chart(self, draw) -> None:
        """用 Agg 渲染图表并显示到 Label 上（尺寸按窗口宽度和 DPI 换算）。"""
        dpi = max(72, int(round(100 * self.scale)))
        width_px = max(500, self.window_width_px - 60)
        figure = Figure(figsize=(width_px / dpi, 200 / 100), dpi=dpi)
        FigureCanvasAgg(figure)
        draw(figure.add_subplot(111))
        buffer = io.BytesIO()
        figure.savefig(buffer, format="png", dpi=dpi)
        buffer.seek(0)
        # 必须保持引用，否则 PhotoImage 会被垃圾回收导致图空白
        self._chart_photo = tk.PhotoImage(data=base64.b64encode(buffer.getvalue()))
        self.chart_label.configure(image=self._chart_photo)

    def _draw_empty_chart(self) -> None:
        def draw(ax) -> None:
            ax.text(0.5, 0.5, "点击「开始校验」后展示问题分布", ha="center", va="center", fontsize=13, transform=ax.transAxes)
            ax.axis("off")

        self._render_chart(draw)

    def _draw_chart(self, report: dict) -> None:
        def draw(ax) -> None:
            issues_by_rule = report["issues_by_rule"]
            if not issues_by_rule:
                ax.text(0.5, 0.5, "没有发现问题 (No issues)", ha="center", va="center", fontsize=15, transform=ax.transAxes)
                ax.axis("off")
                return

            rules = list(issues_by_rule.keys())
            labels = [rule_label(rule) for rule in rules]
            counts = [issues_by_rule[rule] for rule in rules]
            bars = ax.bar(range(len(rules)), counts, color="#4c72b0")
            for bar, count in zip(bars, counts):
                ax.text(
                    bar.get_x() + bar.get_width() / 2,
                    bar.get_height() + max(counts) * 0.02,
                    str(count),
                    ha="center",
                    va="bottom",
                    fontsize=11,
                )
            ax.set_xticks(range(len(rules)))
            ax.set_xticklabels(labels, rotation=20, ha="right")
            ax.set_ylabel("问题数量")
            ax.set_title("各类问题数量分布")
            ax.yaxis.set_major_locator(MaxNLocator(integer=True))
            ax.spines["top"].set_visible(False)
            ax.spines["right"].set_visible(False)
            # 给旋转后的长标签留足空间
            ax.figure.subplots_adjust(bottom=0.3, top=0.86)

        self._render_chart(draw)

    def _run(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)

        csv_path = Path(self.csv_var.get().strip())
        config_path = Path(self.config_var.get().strip())

        if not csv_path.exists():
            messagebox.showerror("错误", f"找不到数据文件：\n{csv_path}")
            return
        if not config_path.exists():
            messagebox.showerror("错误", f"找不到规则文件：\n{config_path}")
            return

        try:
            config = data_check.load_config(config_path)
        except data_check.ConfigurationError as exc:
            messagebox.showerror("配置错误", str(exc))
            return

        try:
            report = data_check.check_csv(csv_path, config, self.encoding_var.get())
        except RuntimeError as exc:
            messagebox.showerror(
                "校验失败",
                f"{exc}\n\n如果 CSV 是 Excel 另存的中文文件，请尝试把编码切换为 GBK。",
            )
            return

        if report["error_count"] == 0:
            self.status_var.set("通过 (Passed)")
            self.status_label.configure(foreground="#1e7d46")
        else:
            self.status_var.set("未通过 (Failed)")
            self.status_label.configure(foreground="#c0392b")

        self.stat_var.set(
            f"共检查 {report['record_count']} 条记录 · "
            f"错误 {report['error_count']} · 警告 {report['warning_count']}"
        )

        if report["issues"]:
            for issue in report["issues"]:
                self.tree.insert(
                    "",
                    "end",
                    values=(
                        issue["severity"].upper(),
                        "" if issue["row"] is None else issue["row"],
                        issue["column"] or "",
                        rule_label(issue["rule"]),
                        issue["message"],
                    ),
                    tags=(issue["severity"],),
                )
        else:
            self.tree.insert("", "end", values=("", "", "", "", "没有发现问题。"))

        self._draw_chart(report)


def main() -> None:
    enable_windows_dpi_awareness()
    root = tk.Tk()
    DataCheckApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
