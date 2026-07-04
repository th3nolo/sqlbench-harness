#!/usr/bin/env python3
"""Audit downloaded benchmark trees before model/API runs.

The runner intentionally does not execute benchmark-provided scripts. This audit
documents what is present anyway: archive integrity, script/package surfaces,
prompt-injection-looking text, and obvious committed secrets.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import stat
import zipfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
BENCHMARKS = ROOT / "benchmarks"
SECURITY = ROOT / "security"
REPORTS = SECURITY / "reports"
SOURCE_MANIFEST = SECURITY / "benchmark-sources.json"

TEXT_SUFFIXES = {
    ".csv",
    ".json",
    ".jsonl",
    ".md",
    ".txt",
    ".sql",
    ".yml",
    ".yaml",
    ".toml",
    ".ini",
    ".cfg",
    ".py",
    ".sh",
    ".js",
    ".ts",
}
SCRIPT_SUFFIXES = {".sh", ".bash", ".zsh", ".py", ".ps1", ".bat", ".cmd", ".js", ".ts"}
SCRIPT_NAMES = {"setup.py", "Dockerfile", "Makefile", "package.json", "pyproject.toml"}

PROMPT_INJECTION_PATTERNS = {
    "ignore_instructions": re.compile(r"ignore\s+(all\s+)?(previous|above|prior)\s+(instructions|rules)", re.I),
    "system_prompt": re.compile(r"\bsystem\s+prompt\b", re.I),
    "developer_message": re.compile(r"\bdeveloper\s+message\b", re.I),
    "reveal_secret": re.compile(r"\b(reveal|print|show|dump|exfiltrate)\b.{0,80}\b(secret|api\s*key|token|password|prompt)\b", re.I),
    "do_not_follow": re.compile(r"\b(do\s+not|don't)\s+follow\s+(the\s+)?(instructions|rules)\b", re.I),
    "jailbreak": re.compile(r"\bjailbreak\b", re.I),
}

SECRET_PATTERNS = {
    "openai_key": re.compile(r"\bsk-(?:proj-[A-Za-z0-9_-]{20,}|[A-Za-z0-9]{32,})\b"),
    "hf_token": re.compile(r"\bhf_[A-Za-z0-9]{20,}\b"),
    "generic_secret_assignment": re.compile(
        r"(?<![A-Za-z0-9])(api[_-]?key|token|secret|password)(?![A-Za-z0-9])\s*=\s*['\"]?(?!xxx|example|placeholder|your_|dummy|sample|$)[A-Za-z0-9_./+=-]{16,}",
        re.I,
    ),
}

SHELL_PATTERNS = {
    "curl_pipe_shell": re.compile(r"(curl|wget).*\|\s*(bash|sh)"),
    "eval": re.compile(r"(^|[;&|({]\s*)eval(\s|$)"),
    "sudo": re.compile(r"\bsudo\b"),
    "destructive_rm": re.compile(r"\brm\s+-[^\n]*[rf]"),
    "chmod_777": re.compile(r"\bchmod\s+777\b"),
    "credential_echo": re.compile(r"echo\s+.*\$(?:\{)?[A-Za-z0-9_]*(KEY|TOKEN|PASSWORD|SECRET)", re.I),
}

PACKAGE_LIFECYCLE_KEYS = {
    "preinstall",
    "install",
    "postinstall",
    "prepack",
    "prepare",
    "postpack",
    "prepublish",
    "prepublishOnly",
}


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def now_id() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def add_finding(report: dict[str, Any], severity: str, rule: str, path: Path, line: int | None = None) -> None:
    finding = {"severity": severity, "rule": rule, "file": rel(path)}
    if line is not None:
        finding["line"] = line
    report.setdefault("findings", []).append(finding)


def placeholder_secret_line(line: str) -> bool:
    upper = line.upper()
    if any(marker in upper for marker in ("YOUR_", "PLACEHOLDER", "EXAMPLE", "DUMMY", "SAMPLE")):
        return True
    match = re.search(
        r"(?<![A-Za-z0-9])(api[_-]?key|token|secret|password)(?![A-Za-z0-9])\s*=\s*['\"]?([A-Za-z0-9_./+=-]{3,})",
        line,
        re.I,
    )
    if not match:
        return False
    value = match.group(2).strip().strip("'\"")
    return bool(re.fullmatch(r"[A-Z][A-Z0-9_]+", value))


def load_active_artifact_paths() -> set[str]:
    if not SOURCE_MANIFEST.exists():
        return set()
    try:
        manifest = json.loads(SOURCE_MANIFEST.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return set()
    paths: set[str] = set()
    for benchmark in manifest.get("benchmarks", {}).values():
        for artifact in benchmark.get("artifacts", []):
            path = artifact.get("path")
            if path:
                paths.add(path)
    return paths


def should_skip(path: Path) -> bool:
    skip_parts = {".git", "__pycache__", ".cache", ".huggingface"}
    return any(part in skip_parts for part in path.parts)


def audit_archives(report: dict[str, Any], *, deep_zip_test: bool) -> None:
    active_paths = load_active_artifact_paths()
    archives = []
    for path in sorted(BENCHMARKS.glob("**/*.zip")):
        if should_skip(path):
            continue
        item: dict[str, Any] = {
            "path": rel(path),
            "bytes": path.stat().st_size,
            "active_artifact": rel(path) in active_paths,
            "valid_zip": False,
            "members": None,
            "script_like_members": [],
            "zip_slip_members": [],
            "testzip_bad_member": None,
        }
        try:
            with zipfile.ZipFile(path) as archive:
                item["valid_zip"] = True
                infos = archive.infolist()
                item["members"] = len(infos)
                for member in infos:
                    name = member.filename
                    destination = (path.parent / name).resolve()
                    if not str(destination).startswith(str(path.parent.resolve()) + os.sep):
                        item["zip_slip_members"].append(name)
                    member_path = Path(name)
                    if member_path.suffix in SCRIPT_SUFFIXES or member_path.name in SCRIPT_NAMES:
                        item["script_like_members"].append(name)
                item["script_like_members"] = item["script_like_members"][:50]
                if deep_zip_test:
                    item["testzip_bad_member"] = archive.testzip()
        except Exception as exc:
            item["error"] = str(exc)
        if not item["valid_zip"]:
            add_finding(report, "high" if item["active_artifact"] else "medium", "invalid_zip", path)
        if item["zip_slip_members"]:
            add_finding(report, "critical", "zip_slip", path)
        archives.append(item)
    report["archives"] = archives


def audit_env(report: dict[str, Any]) -> None:
    env_path = ROOT / ".env"
    item: dict[str, Any] = {"present": env_path.exists(), "mode": None, "world_accessible": False}
    if env_path.exists():
        mode = stat.S_IMODE(env_path.stat().st_mode)
        item["mode"] = oct(mode)
        item["world_accessible"] = bool(mode & stat.S_IRWXO)
        if item["world_accessible"]:
            add_finding(report, "high", "env_world_accessible", env_path)
    report["env"] = item


def audit_files(report: dict[str, Any], *, max_text_bytes: int, max_findings_per_rule: int) -> None:
    script_inventory = []
    package_lifecycle = []
    requirements = []
    counts_by_suffix: dict[str, int] = {}
    per_rule_counts: dict[str, int] = {}

    def capped_add(severity: str, rule: str, path: Path, line: int | None = None) -> None:
        per_rule_counts[rule] = per_rule_counts.get(rule, 0) + 1
        if per_rule_counts[rule] <= max_findings_per_rule:
            add_finding(report, severity, rule, path, line)

    for path in sorted(BENCHMARKS.glob("**/*")):
        if not path.is_file() or should_skip(path):
            continue
        suffix = path.suffix
        counts_by_suffix[suffix or "<none>"] = counts_by_suffix.get(suffix or "<none>", 0) + 1
        if suffix in SCRIPT_SUFFIXES or path.name in SCRIPT_NAMES:
            script_inventory.append({"path": rel(path), "bytes": path.stat().st_size})

        if path.name == "package.json":
            try:
                payload = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
                scripts = payload.get("scripts") or {}
                for key in sorted(set(scripts) & PACKAGE_LIFECYCLE_KEYS):
                    package_lifecycle.append({"path": rel(path), "script": key})
                    capped_add("high", f"package_lifecycle_{key}", path)
            except json.JSONDecodeError:
                capped_add("medium", "invalid_package_json", path)

        if path.name.startswith("requirements") and path.suffix == ".txt":
            direct = []
            for line_no, raw in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), start=1):
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if any(marker in line for marker in ("git+", "http://", "https://", "file:", "-e ", "@ ")):
                    direct.append({"line": line_no})
                    capped_add("high", "direct_dependency", path, line_no)
            requirements.append({"path": rel(path), "direct_dependency_lines": direct[:20]})

        if suffix == ".sh":
            for line_no, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), start=1):
                for rule, pattern in SHELL_PATTERNS.items():
                    if pattern.search(line):
                        severity = "high" if rule in {"curl_pipe_shell", "eval", "credential_echo"} else "medium"
                        capped_add(severity, rule, path, line_no)

        if suffix in TEXT_SUFFIXES and path.stat().st_size <= max_text_bytes:
            text = path.read_text(encoding="utf-8", errors="ignore")
            for line_no, line in enumerate(text.splitlines(), start=1):
                for rule, pattern in SECRET_PATTERNS.items():
                    if pattern.search(line):
                        if rule == "generic_secret_assignment" and placeholder_secret_line(line):
                            continue
                        capped_add("critical", rule, path, line_no)
                for rule, pattern in PROMPT_INJECTION_PATTERNS.items():
                    if pattern.search(line):
                        capped_add("medium", f"prompt_injection_{rule}", path, line_no)

    report["script_inventory"] = {
        "count": len(script_inventory),
        "sample": script_inventory[:250],
    }
    report["package_lifecycle"] = package_lifecycle[:250]
    report["requirements"] = requirements[:250]
    report["file_suffix_counts"] = dict(sorted(counts_by_suffix.items()))
    report["finding_counts_by_rule"] = per_rule_counts


def classify(report: dict[str, Any]) -> None:
    blocking_rules = {
        "zip_slip",
        "env_world_accessible",
        "package_lifecycle_preinstall",
        "package_lifecycle_install",
        "package_lifecycle_postinstall",
        "package_lifecycle_prepare",
        "openai_key",
        "hf_token",
        "generic_secret_assignment",
    }
    blocking = []
    for finding in report.get("findings", []):
        if finding["severity"] == "critical" or finding["rule"] in blocking_rules:
            blocking.append(finding)
        if finding["rule"] == "invalid_zip" and finding["severity"] == "high":
            blocking.append(finding)
    report["blocking_findings"] = blocking
    report["blocking"] = bool(blocking)
    report["execution_policy"] = {
        "benchmark_scripts_executed": False,
        "model_runs_execute_upstream_code": False,
        "notes": [
            "External SQL benchmark runs read benchmark questions/schema/database files only.",
            "Do not run benchmark setup.py, package scripts, DBT package scripts, shell scripts, or Dockerfiles unless separately reviewed.",
            "Prompt templates wrap benchmark text as untrusted content and tell models not to follow instructions embedded in data.",
        ],
    }


def write_reports(report: dict[str, Any]) -> tuple[Path, Path]:
    REPORTS.mkdir(parents=True, exist_ok=True)
    stamp = now_id()
    json_path = REPORTS / f"benchmark-security-audit-{stamp}.json"
    md_path = REPORTS / f"benchmark-security-audit-{stamp}.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    lines = [
        f"# Benchmark Security Audit - {report['generated_at_utc']}",
        "",
        f"- Blocking: `{report['blocking']}`",
        f"- Findings: `{len(report.get('findings', []))}`",
        f"- Script/package-like files inventoried: `{report['script_inventory']['count']}`",
        f"- Archives checked: `{len(report['archives'])}`",
        "",
        "## Archives",
    ]
    for archive in report["archives"]:
        lines.append(
            f"- `{archive['path']}`: valid={archive['valid_zip']} active={archive['active_artifact']} "
            f"members={archive['members']} script_like={len(archive['script_like_members'])} "
            f"zip_slip={len(archive['zip_slip_members'])}"
        )
    lines.extend(["", "## Blocking Findings"])
    if report["blocking_findings"]:
        for finding in report["blocking_findings"][:50]:
            loc = f"{finding['file']}:{finding.get('line', '-')}"
            lines.append(f"- `{finding['rule']}` {finding['severity']} at `{loc}`")
    else:
        lines.append("- None")
    lines.extend(["", "## Finding Counts"])
    for rule, count in sorted(report.get("finding_counts_by_rule", {}).items()):
        lines.append(f"- `{rule}`: {count}")
    lines.extend(["", "## Execution Policy"])
    for note in report["execution_policy"]["notes"]:
        lines.append(f"- {note}")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--deep-zip-test", action="store_true")
    parser.add_argument("--max-text-bytes", type=int, default=1_000_000)
    parser.add_argument("--max-findings-per-rule", type=int, default=100)
    args = parser.parse_args()

    report: dict[str, Any] = {
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "root": str(ROOT),
        "findings": [],
    }
    audit_archives(report, deep_zip_test=args.deep_zip_test)
    audit_env(report)
    audit_files(
        report,
        max_text_bytes=args.max_text_bytes,
        max_findings_per_rule=args.max_findings_per_rule,
    )
    classify(report)
    json_path, md_path = write_reports(report)
    print(json.dumps({"blocking": report["blocking"], "json": rel(json_path), "markdown": rel(md_path)}, indent=2))
    return 1 if report["blocking"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
