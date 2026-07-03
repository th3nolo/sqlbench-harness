#!/usr/bin/env python3
"""Download/clone external SQL benchmarks with provenance records."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import http.cookiejar
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from benchmark_registry import BENCHMARK_ROOT, ROOT, SPECS, all_benchmark_names, canonical_name, get_spec

try:
    from huggingface_hub import HfApi, hf_hub_download, snapshot_download
except Exception:  # pragma: no cover - handled at runtime if HF sources are used
    HfApi = None
    hf_hub_download = None
    snapshot_download = None


SECURITY = ROOT / "security"
SOURCE_MANIFEST = SECURITY / "benchmark-sources.json"


SECRET_PATTERNS = {
    "openai_key": re.compile(r"\bsk-(?:proj-[A-Za-z0-9_-]{20,}|[A-Za-z0-9]{32,})\b"),
    "hf_token": re.compile(r"hf_[A-Za-z0-9]{20,}"),
    "generic_secret_assignment": re.compile(
        r"(?<![A-Za-z0-9])(api[_-]?key|token|secret|password)(?![A-Za-z0-9])\s*=\s*['\"]?(?!xxx|example|placeholder|$)[A-Za-z0-9_./+=-]{16,}",
        re.I,
    ),
}

SHELL_PATTERNS = {
    "curl_pipe_shell": re.compile(r"(curl|wget).*\|\s*(bash|sh)"),
    "eval": re.compile(r"(^|[;&|({]\s*)eval(\s|$)"),
    "credential_echo": re.compile(r"echo\s+.*\$(?:\{)?[A-Za-z0-9_]*(KEY|TOKEN|PASSWORD|SECRET)", re.I),
    "destructive_rm": re.compile(r"\brm\s+-[^\n]*[rf]"),
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(cmd: list[str], *, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    print("$ " + " ".join(cmd), flush=True)
    return subprocess.run(cmd, cwd=str(cwd), text=True, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def load_manifest() -> dict[str, Any]:
    if SOURCE_MANIFEST.exists():
        text = SOURCE_MANIFEST.read_text(encoding="utf-8")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            decoder = json.JSONDecoder()
            try:
                recovered, end = decoder.raw_decode(text)
            except json.JSONDecodeError:
                raise exc
            tail = text[end:].strip()
            if tail:
                print(
                    f"warning: recovered first JSON object from corrupted {SOURCE_MANIFEST.relative_to(ROOT)}; "
                    "the next write will repair it",
                    file=sys.stderr,
                )
                return recovered
            raise exc
    return {"benchmarks": {}}


def write_manifest(manifest: dict[str, Any]) -> None:
    SECURITY.mkdir(parents=True, exist_ok=True)
    manifest["updated_at_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
    tmp = SOURCE_MANIFEST.with_suffix(SOURCE_MANIFEST.suffix + ".tmp")
    tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(SOURCE_MANIFEST)
    print(f"Wrote {SOURCE_MANIFEST.relative_to(ROOT)}")


def git_head(path: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(path),
            text=True,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return result.stdout.strip()
    except Exception:
        return None


def clone_source(source: dict[str, str], target_root: Path, *, force: bool, dry_run: bool) -> dict[str, Any]:
    target = target_root / source["path"]
    record: dict[str, Any] = {"type": "git", "url": source["url"], "path": str(target.relative_to(ROOT))}
    if dry_run:
        record["dry_run"] = True
        print(f"Would clone {source['url']} -> {target}")
        return record
    if target.exists() and force:
        raise SystemExit(f"{target} already exists; remove it manually before forcing a reclone")
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        run(["git", "clone", "--depth", "1", source["url"], str(target)], cwd=ROOT)
    else:
        print(f"Using existing {target}")
    record["head"] = git_head(target)
    return record


def google_drive_url(file_id: str, *, confirm: str | None = None, uuid: str | None = None) -> str:
    base = "https://drive.google.com/uc"
    params = {"export": "download", "id": file_id}
    if confirm:
        base = "https://drive.usercontent.google.com/download"
        params["confirm"] = confirm
    if uuid:
        params["uuid"] = uuid
    return f"{base}?{urllib.parse.urlencode(params)}"


def parse_drive_confirm(html: str) -> tuple[str | None, str | None]:
    confirm = None
    uuid = None
    confirm_match = re.search(r'name="confirm"\s+value="([^"]+)"', html)
    uuid_match = re.search(r'name="uuid"\s+value="([^"]+)"', html)
    if confirm_match:
        confirm = confirm_match.group(1)
    if uuid_match:
        uuid = uuid_match.group(1)
    return confirm, uuid


def stream_response(response, destination: Path) -> None:
    tmp = destination.with_suffix(destination.suffix + ".part")
    total = int(response.headers.get("content-length") or 0)
    seen = 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tmp.open("wb") as handle:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            handle.write(chunk)
            seen += len(chunk)
            if total:
                print(f"\r{destination.name}: {seen / 1024 / 1024:.1f}/{total / 1024 / 1024:.1f} MiB", end="", flush=True)
    if total:
        print()
    if total and seen != total:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise RuntimeError(
            f"incomplete download for {destination.name}: received {seen} bytes, expected {total}"
        )
    tmp.replace(destination)


def download_url(source: dict[str, str], target_root: Path, *, force: bool, dry_run: bool) -> dict[str, Any]:
    downloads = target_root / "downloads"
    destination = downloads / source["filename"]
    record: dict[str, Any] = {
        "type": "url",
        "url": source["url"],
        "filename": source["filename"],
        "path": str(destination.relative_to(ROOT)),
    }
    if source.get("provenance"):
        record["provenance"] = source["provenance"]
    if source.get("expected_bytes"):
        record["expected_bytes"] = int(source["expected_bytes"])
    if dry_run:
        record["dry_run"] = True
        print(f"Would download {source['url']} -> {destination}")
        return record
    if destination.exists() and not force and looks_valid_download(destination):
        print(f"Using existing {destination}")
    else:
        if destination.exists():
            print(f"Existing {destination} is incomplete or invalid; downloading again.")
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                request = urllib.request.Request(source["url"], headers={"User-Agent": "beaver-benchmark-setup/1.0"})
                with urllib.request.urlopen(request, timeout=60) as response:
                    stream_response(response, destination)
                if not looks_valid_download(destination):
                    raise RuntimeError(f"downloaded file is not a valid {destination.suffix or 'file'}: {destination}")
                break
            except Exception as exc:
                last_error = exc
                print(f"download attempt {attempt} failed for {source['filename']}: {exc}", file=sys.stderr)
                if attempt < 3:
                    time.sleep(2 * attempt)
        else:
            assert last_error is not None
            raise last_error
    record["bytes"] = destination.stat().st_size
    expected = int(source["expected_bytes"]) if source.get("expected_bytes") else 0
    if expected and record["bytes"] != expected:
        raise RuntimeError(f"downloaded {destination.name} has {record['bytes']} bytes, expected {expected}")
    record["sha256"] = sha256_file(destination)
    return record


def looks_valid_download(path: Path) -> bool:
    if not path.exists() or path.stat().st_size == 0:
        return False
    if path.suffix.lower() == ".zip":
        return zipfile.is_zipfile(path)
    return True


def download_gdrive(source: dict[str, str], target_root: Path, *, force: bool, dry_run: bool) -> dict[str, Any]:
    downloads = target_root / "downloads"
    destination = downloads / source["filename"]
    record: dict[str, Any] = {
        "type": "gdrive",
        "file_id": source["file_id"],
        "filename": source["filename"],
        "path": str(destination.relative_to(ROOT)),
    }
    if dry_run:
        record["dry_run"] = True
        print(f"Would download Google Drive file {source['file_id']} -> {destination}")
        return record
    if destination.exists() and not force and looks_valid_download(destination):
        print(f"Using existing {destination}")
    else:
        if destination.exists():
            print(f"Existing {destination} is incomplete or invalid; downloading again.")
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
                first = opener.open(google_drive_url(source["file_id"]), timeout=60)
                content_type = first.headers.get("content-type", "")
                if "text/html" in content_type:
                    html = first.read(1024 * 1024).decode("utf-8", errors="ignore")
                    confirm, uuid = parse_drive_confirm(html)
                    if not confirm:
                        raise RuntimeError(f"Google Drive did not provide a download confirmation for {source['filename']}")
                    second = opener.open(google_drive_url(source["file_id"], confirm=confirm, uuid=uuid), timeout=60)
                    stream_response(second, destination)
                else:
                    stream_response(first, destination)
                if not looks_valid_download(destination):
                    raise RuntimeError(f"downloaded file is not a valid {destination.suffix or 'file'}: {destination}")
                break
            except Exception as exc:
                last_error = exc
                print(f"download attempt {attempt} failed for {source['filename']}: {exc}", file=sys.stderr)
                if attempt < 3:
                    time.sleep(2 * attempt)
        else:
            assert last_error is not None
            raise last_error
    record["bytes"] = destination.stat().st_size
    record["sha256"] = sha256_file(destination)
    return record


def download_hf_snapshot(source: dict[str, str], target_root: Path, *, force: bool, dry_run: bool) -> dict[str, Any]:
    if snapshot_download is None or HfApi is None:
        raise RuntimeError("huggingface_hub is required for hf_snapshot sources")
    destination = target_root / source["path"]
    allow_patterns = [item.strip() for item in source.get("allow_patterns", "").split(",") if item.strip()]
    record: dict[str, Any] = {
        "type": "hf_snapshot",
        "repo_id": source["repo_id"],
        "repo_type": source.get("repo_type", "dataset"),
        "path": str(destination.relative_to(ROOT)),
    }
    if allow_patterns:
        record["allow_patterns"] = allow_patterns
    if source.get("provenance"):
        record["provenance"] = source["provenance"]
    if dry_run:
        record["dry_run"] = True
        print(f"Would download HF snapshot {source['repo_id']} -> {destination}")
        return record
    if destination.exists() and force:
        raise SystemExit(f"{destination} already exists; remove it manually before forcing a re-download")
    destination.mkdir(parents=True, exist_ok=True)
    info = HfApi().repo_info(source["repo_id"], repo_type=source.get("repo_type", "dataset"))
    snapshot_download(
        repo_id=source["repo_id"],
        repo_type=source.get("repo_type", "dataset"),
        revision=source.get("revision") or getattr(info, "sha", None),
        local_dir=str(destination),
        allow_patterns=allow_patterns or None,
    )
    record["commit_sha"] = getattr(info, "sha", None)
    file_count = sum(1 for path in destination.glob("**/*") if path.is_file())
    record["files"] = file_count
    record["bytes"] = sum(path.stat().st_size for path in destination.glob("**/*") if path.is_file())
    required_glob = source.get("required_glob")
    if required_glob:
        matches = sorted(destination.glob(required_glob))
        record["required_glob"] = required_glob
        record["required_files"] = len(matches)
        minimum = int(source.get("min_required_files") or 1)
        if len(matches) < minimum:
            raise RuntimeError(
                f"HF snapshot {source['repo_id']} matched {len(matches)} files for {required_glob}, expected at least {minimum}"
            )
    return record


def parse_duckdb_path_from_profile(task_dir: Path) -> str | None:
    profile = task_dir / "profiles.yml"
    if not profile.exists():
        return None
    text = profile.read_text(encoding="utf-8", errors="ignore")
    match = re.search(r"^\s*path:\s*['\"]?\./([^'\"\n#]+\.duckdb)['\"]?\s*$", text, re.M)
    return match.group(1) if match else None


def spider2_gold_names(target_root: Path) -> dict[str, str]:
    gold_path = target_root / "repo" / "spider2-dbt" / "evaluation_suite" / "gold" / "spider2_eval.jsonl"
    mapping: dict[str, str] = {}
    if not gold_path.exists():
        return mapping
    for line in gold_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        instance_id = row.get("instance_id")
        gold = row.get("evaluation", {}).get("parameters", {}).get("gold")
        if instance_id and gold:
            mapping[str(instance_id)] = str(gold)
    return mapping


def link_or_copy(source: Path, destination: Path, *, force: bool) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if not force:
            return "exists"
        destination.unlink()
    try:
        os.link(source, destination)
        return "linked"
    except OSError:
        shutil.copy2(source, destination)
        return "copied"


def download_spider2_hf_duckdb(source: dict[str, str], target_root: Path, *, force: bool, dry_run: bool) -> dict[str, Any]:
    if hf_hub_download is None or HfApi is None:
        raise RuntimeError("huggingface_hub is required for spider2_hf_duckdb sources")
    repo_id = source["repo_id"]
    repo_type = source.get("repo_type", "dataset")
    destination = target_root / source["path"]
    examples = target_root / "repo" / "spider2-dbt" / "examples"
    if not examples.exists():
        raise RuntimeError(f"Spider2 examples directory is missing: {examples}")
    record: dict[str, Any] = {
        "type": "spider2_hf_duckdb",
        "repo_id": repo_id,
        "repo_type": repo_type,
        "path": str(destination.relative_to(ROOT)),
    }
    if source.get("provenance"):
        record["provenance"] = source["provenance"]
    if dry_run:
        record["dry_run"] = True
        print(f"Would download targeted Spider2 DuckDB files from {repo_id} -> {destination}")
        return record

    info = HfApi().repo_info(repo_id, repo_type=repo_type)
    revision = source.get("revision") or getattr(info, "sha", None)
    record["commit_sha"] = revision

    gold_names = spider2_gold_names(target_root)
    task_dirs = sorted(path for path in examples.iterdir() if path.is_dir())
    def process_task(idx: int, task_dir: Path) -> dict[str, Any]:
        task = task_dir.name
        result: dict[str, Any] = {
            "task": task,
            "env_downloaded": 0,
            "gold_downloaded": 0,
            "env_materialized": 0,
            "gold_materialized": 0,
            "missing": [],
        }
        duckdb_name = parse_duckdb_path_from_profile(task_dir)
        if not duckdb_name:
            result["missing"].append({"task": task, "kind": "profile_path", "path": str(task_dir / "profiles.yml")})
            return result
        remote_env = f"datasets/spider2-dbt/{task}/environment/dbt_project/{duckdb_name}"
        print(f"[{idx}/{len(task_dirs)}] {task}: {duckdb_name}", flush=True)
        try:
            env_path = Path(
                hf_hub_download(
                    repo_id=repo_id,
                    repo_type=repo_type,
                    revision=revision,
                    filename=remote_env,
                    local_dir=str(destination),
                )
            )
            result["env_downloaded"] += 1
            if link_or_copy(env_path, task_dir / duckdb_name, force=force) in {"exists", "linked", "copied"}:
                result["env_materialized"] += 1
        except Exception as exc:
            result["missing"].append({"task": task, "kind": "env_duckdb", "path": remote_env, "error": str(exc)})

        remote_gold = f"datasets/spider2-dbt/{task}/tests/gold.duckdb"
        gold_name = gold_names.get(task, duckdb_name)
        try:
            gold_path = Path(
                hf_hub_download(
                    repo_id=repo_id,
                    repo_type=repo_type,
                    revision=revision,
                    filename=remote_gold,
                    local_dir=str(destination),
                )
            )
            result["gold_downloaded"] += 1
            gold_dest = target_root / "repo" / "spider2-dbt" / "evaluation_suite" / "gold" / task / gold_name
            if link_or_copy(gold_path, gold_dest, force=force) in {"exists", "linked", "copied"}:
                result["gold_materialized"] += 1
        except Exception as exc:
            result["missing"].append({"task": task, "kind": "gold_duckdb", "path": remote_gold, "error": str(exc)})
        return result

    env_downloaded = 0
    gold_downloaded = 0
    env_materialized = 0
    gold_materialized = 0
    missing: list[dict[str, str]] = []
    workers = int(source.get("workers") or 4)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(process_task, idx, task_dir) for idx, task_dir in enumerate(task_dirs, start=1)]
        for future in as_completed(futures):
            result = future.result()
            env_downloaded += int(result["env_downloaded"])
            gold_downloaded += int(result["gold_downloaded"])
            env_materialized += int(result["env_materialized"])
            gold_materialized += int(result["gold_materialized"])
            missing.extend(result["missing"])

    record["tasks"] = len(task_dirs)
    record["workers"] = workers
    record["env_duckdb_files"] = env_downloaded
    record["gold_duckdb_files"] = gold_downloaded
    record["env_materialized"] = env_materialized
    record["gold_materialized"] = gold_materialized
    record["missing"] = missing[:20]
    record["missing_count"] = len(missing)
    record["bytes"] = sum(path.stat().st_size for path in destination.glob("**/*") if path.is_file())
    required_glob = source.get("required_glob")
    if required_glob:
        matches = sorted(destination.glob(required_glob))
        record["required_glob"] = required_glob
        record["required_files"] = len(matches)
        minimum = int(source.get("min_required_files") or 1)
        if len(matches) < minimum:
            raise RuntimeError(
                f"Spider2 HF mirror matched {len(matches)} files for {required_glob}, expected at least {minimum}; "
                f"missing_count={len(missing)}"
            )
    return record


def safe_extract_zip(zip_path: Path, target_root: Path) -> dict[str, Any]:
    extract_root = target_root
    extracted = 0
    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.infolist():
            destination = (extract_root / member.filename).resolve()
            if not str(destination).startswith(str(extract_root.resolve()) + os.sep):
                raise RuntimeError(f"refusing zip-slip member {member.filename}")
            if member.is_dir():
                continue
            extracted += 1
        archive.extractall(extract_root)
    return {"zip": str(zip_path.relative_to(ROOT)), "extract_root": str(extract_root.relative_to(ROOT)), "files": extracted}


def scan_tree(root: Path) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if not root.exists():
        return findings
    for path in sorted(root.glob("**/*")):
        if not path.is_file() or ".git" in path.parts or path.stat().st_size > 2_000_000:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        rel_path = str(path.relative_to(ROOT))
        for line_no, line in enumerate(text.splitlines(), start=1):
            for rule, pattern in SECRET_PATTERNS.items():
                if pattern.search(line):
                    if rule == "generic_secret_assignment" and is_placeholder_secret_line(line):
                        continue
                    findings.append({"severity": "critical", "rule": rule, "file": rel_path, "line": line_no})
            if path.suffix in {".sh", ".bash", ".zsh"}:
                for rule, pattern in SHELL_PATTERNS.items():
                    if pattern.search(line):
                        severity = "high" if rule in {"curl_pipe_shell", "eval", "credential_echo"} else "medium"
                        findings.append({"severity": severity, "rule": rule, "file": rel_path, "line": line_no})
    return findings


def is_placeholder_secret_line(line: str) -> bool:
    if "YOUR_" in line.upper() or "PLACEHOLDER" in line.upper() or "EXAMPLE" in line.upper():
        return True
    match = re.search(
        r"(api[_-]?key|token|secret|password)\s*=\s*['\"]?([A-Za-z0-9_./+=-]{3,})",
        line,
        re.I,
    )
    if not match:
        return False
    value = match.group(2).strip().strip("'\"")
    return bool(re.fullmatch(r"[A-Z][A-Z0-9_]+", value))


def setup_one(name: str, *, dry_run: bool, extract: bool, force: bool) -> dict[str, Any]:
    spec = get_spec(name)
    target_root = BENCHMARK_ROOT / spec.name
    target_root.mkdir(parents=True, exist_ok=True)
    record: dict[str, Any] = {
        "display_name": spec.display_name,
        "name": spec.name,
        "mode": spec.mode,
        "dialect": spec.dialect,
        "compressed_size": spec.compressed_size,
        "practical_disk": spec.practical_disk,
        "setup_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "artifacts": [],
        "extractions": [],
        "scan_findings": [],
    }
    for source in spec.sources:
        if source["type"] == "git":
            artifact = clone_source(source, target_root, force=force, dry_run=dry_run)
        elif source["type"] == "url":
            artifact = download_url(source, target_root, force=force, dry_run=dry_run)
            if extract and not dry_run and source["filename"].lower().endswith(".zip"):
                record["extractions"].append(safe_extract_zip(target_root / "downloads" / source["filename"], target_root))
        elif source["type"] == "gdrive":
            artifact = download_gdrive(source, target_root, force=force, dry_run=dry_run)
            if extract and not dry_run:
                record["extractions"].append(safe_extract_zip(target_root / "downloads" / source["filename"], target_root))
        elif source["type"] == "hf_snapshot":
            artifact = download_hf_snapshot(source, target_root, force=force, dry_run=dry_run)
        elif source["type"] == "spider2_hf_duckdb":
            artifact = download_spider2_hf_duckdb(source, target_root, force=force, dry_run=dry_run)
        else:
            raise RuntimeError(f"unknown source type {source['type']}")
        record["artifacts"].append(artifact)
    if not dry_run:
        record["scan_findings"] = scan_tree(target_root)
        blocking = [item for item in record["scan_findings"] if item["severity"] in {"critical", "high"}]
        if blocking:
            for item in blocking:
                print(f"{item['severity']}: {item['rule']} {item['file']}:{item['line']}", file=sys.stderr)
            raise RuntimeError(f"blocking scan findings in {target_root}")
    return record


def failure_record(name: str, error: Exception) -> dict[str, Any]:
    spec = get_spec(name)
    target_root = BENCHMARK_ROOT / spec.name
    artifacts = []
    for source in spec.sources:
        if source["type"] == "git":
            path = target_root / source["path"]
            artifacts.append(
                {
                    "type": "git",
                    "url": source["url"],
                    "path": str(path.relative_to(ROOT)),
                    "exists": path.exists(),
                    "head": git_head(path) if path.exists() else None,
                }
            )
        elif source["type"] == "url":
            path = target_root / "downloads" / source["filename"]
            artifacts.append(
                {
                    "type": "url",
                    "url": source["url"],
                    "filename": source["filename"],
                    "path": str(path.relative_to(ROOT)),
                    "exists": path.exists(),
                    "bytes": path.stat().st_size if path.exists() else 0,
                    "valid_zip": zipfile.is_zipfile(path) if path.exists() and path.suffix == ".zip" else None,
                }
            )
        elif source["type"] == "gdrive":
            path = target_root / "downloads" / source["filename"]
            artifacts.append(
                {
                    "type": "gdrive",
                    "file_id": source["file_id"],
                    "filename": source["filename"],
                    "path": str(path.relative_to(ROOT)),
                    "exists": path.exists(),
                    "bytes": path.stat().st_size if path.exists() else 0,
                    "valid_zip": zipfile.is_zipfile(path) if path.exists() and path.suffix == ".zip" else None,
                }
            )
        elif source["type"] == "hf_snapshot":
            path = target_root / source["path"]
            artifacts.append(
                {
                    "type": "hf_snapshot",
                    "repo_id": source["repo_id"],
                    "repo_type": source.get("repo_type", "dataset"),
                    "path": str(path.relative_to(ROOT)),
                    "exists": path.exists(),
                    "files": sum(1 for item in path.glob("**/*") if item.is_file()) if path.exists() else 0,
                }
            )
        elif source["type"] == "spider2_hf_duckdb":
            path = target_root / source["path"]
            artifacts.append(
                {
                    "type": "spider2_hf_duckdb",
                    "repo_id": source["repo_id"],
                    "repo_type": source.get("repo_type", "dataset"),
                    "path": str(path.relative_to(ROOT)),
                    "exists": path.exists(),
                    "files": sum(1 for item in path.glob("**/*") if item.is_file()) if path.exists() else 0,
                }
            )
    return {
        "display_name": spec.display_name,
        "name": spec.name,
        "mode": spec.mode,
        "dialect": spec.dialect,
        "status": "failed",
        "error": str(error),
        "setup_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "artifacts": artifacts,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", help="benchmark name or all")
    parser.add_argument("--list", action="store_true", help="list registered benchmarks and exit")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-extract", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    if args.list:
        payload = {
            name: {
                "display_name": spec.display_name,
                "mode": spec.mode,
                "dialect": spec.dialect,
                "description": spec.description,
                "compressed_size": spec.compressed_size,
                "practical_disk": spec.practical_disk,
            }
            for name, spec in sorted(SPECS.items())
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    if not args.name:
        parser.error("--name is required unless --list is used")

    names = all_benchmark_names() if args.name == "all" else [canonical_name(args.name)]
    manifest = load_manifest()
    exit_code = 0
    for name in names:
        print(f"\n==> Setting up {name}")
        try:
            record = setup_one(
                name,
                dry_run=args.dry_run,
                extract=not args.no_extract,
                force=args.force,
            )
            record["status"] = "dry_run" if args.dry_run else "ok"
            manifest["benchmarks"][name] = record
        except Exception as exc:
            manifest["benchmarks"][name] = failure_record(name, exc)
            print(f"setup failed for {name}: {exc}", file=sys.stderr)
            exit_code = 1
    write_manifest(manifest)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
