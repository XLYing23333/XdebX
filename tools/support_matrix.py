#!/usr/bin/env python3
"""Convert measured Docker results into candidate-level compatibility metadata."""
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from xdebx.runtime import atomic_json


def generate(directory=ROOT / "test-results/docker"):
    cases = json.loads((directory / "matrix.json").read_text())
    results = json.loads((directory / "summary.json").read_text())
    active_ids = {case["case_id"] for case in cases}
    results = [result for result in results if result["case_id"] in active_ids]
    by_id = {r["case_id"]: r for r in results}
    metadata = {"schema_version": 1, "entries": {}, "strategy_tests": [], "software": {}}
    for case in cases:
        result = by_id.get(case["case_id"], {"status": "not-run"})
        summary = {"status": result["status"], "case_id": case["case_id"],
                   "version": result.get("resolved_version") or case["version"], "reason": result.get("reason", "")}
        if result.get("eligible_for_vim9") is False:
            summary["default_enabled"] = False
            summary["reason"] = "Package runs but does not satisfy Vim >=9"
        else:
            summary["default_enabled"] = result["status"] == "passed" or result["status"] == "skipped" and result.get("scope_excluded", False)
            if result["status"] == "skipped":
                summary["scope_excluded"] = result.get("scope_excluded", False)
                if result.get("reference_case_id"):
                    summary.update({key: result[key] for key in ("reference_case_id", "reference_target", "reference_status", "support_inferred")})
                    summary["default_enabled"] = result["support_inferred"]
        entry = case.get("entry")
        if entry:
            key = entry.get("filename") or entry["url"] + "#" + entry["version"]
            metadata["entries"].setdefault(key, {})[case["target"]] = summary
        elif case["kind"] != "integration":
            metadata["strategy_tests"].append(dict(summary, target=case["target"], software=case["software"], method=case["method"]))
        if case["kind"] != "integration":
            group = metadata["software"].setdefault(case["software"], {}).setdefault(case["target"], {"cases": [], "status": "not-run"})
            group["cases"].append(summary)
    for targets in metadata["software"].values():
        for group in targets.values():
            if any(case["status"] == "passed" and case["default_enabled"] for case in group["cases"]):
                group["status"] = "passed"
            elif any(case["status"] == "skipped" for case in group["cases"]):
                group["status"] = "not-tested"
            elif all(case["status"] != "not-run" for case in group["cases"]):
                group["status"] = "unavailable"
    atomic_json(ROOT / "data/pkgs/compatibility.json", metadata, 0o644)
    counts = Counter(r["status"] for r in results)
    lines = ["# 实测兼容矩阵", "", "结果来自一次性 Docker 容器中的串行安装与功能验证。", "",
             "统计：" + "、".join("{} {}".format(key, count) for key, count in sorted(counts.items())) + "。", "",
             "桌面测试采用 Xvfb，不包含真实音频、GPU、托盘或宿主服务集成。Vim 8 即使能够运行，也不满足 VIM9 默认候选要求。", "",
             "按用户要求，非 Ubuntu 20 桌面候选及桌面集成测试跳过（skipped），选项保留但不声明实测通过。此前已完成的结果保留，历史失败日志不删除。", "",
             "Debian 13 仅独立测试其特有安装路径；与 Debian 12 共用的候选和集成案例跳过。兼容元数据记录参考案例，参考通过仅作为推断支持，不等于 Debian 13 实测通过。", "",
             "| 系统 | 软件 | 候选版本 | 方法 | 状态 |", "|---|---|---|---|---|"]
    for case in cases:
        r = by_id.get(case["case_id"], {"status": "not-run"})
        version = r.get("resolved_version") or case["version"]
        version = str(version).replace("|", "\\|").replace("\n", " ")
        lines.append("| {} | {} | {} | {} | {} |".format(case["target"], case["software"], version, case["kind"] + "/" + case["method"], r["status"]))
    (ROOT / "docs/Compatibility.md").write_text("\n".join(lines) + "\n")
    return counts


if __name__ == "__main__":
    print(dict(generate()))
