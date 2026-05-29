from __future__ import annotations

import argparse
import json
import subprocess
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict


def _load_json_yaml(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _run_id() -> str:
    return f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"


def _git_commit(repo_root: Path) -> str | None:
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    value = (proc.stdout or "").strip()
    return value or None


def _manifest_doc(repo_root: Path) -> Dict[str, Any]:
    return _load_json_yaml(repo_root / "SYSTEM_MANIFEST.yaml")


def build_run_manifest(
    repo_root: Path,
    mode: str,
    profile: str,
    explicit_config: str = "",
    include_resume_execution: bool = False,
    invocation_python: str = "",
    research_python: str = "",
    status: str = "starting",
) -> Dict[str, Any]:
    manifest_doc = _manifest_doc(repo_root)
    canonical = dict(manifest_doc.get("canonical", {}) or {})
    run_id = _run_id()
    output_dir = Path(str(canonical["formal_output_root"])) / run_id
    manifest_path = output_dir / str(canonical.get("formal_run_manifest_name", "run_manifest.json"))
    git_commit = _git_commit(repo_root)
    timestamp = _now_text()
    return {
        "run_id": run_id,
        "timestamp": timestamp,
        "updated_at": timestamp,
        "status": status,
        "mode": mode,
        "profile": profile,
        "entrypoint": str(canonical["formal_operator_entry"]),
        "wrapped_business_root_entry": str(canonical["wrapped_business_root_entry"]),
        "runtime_root": str(canonical["live_runtime_root"]),
        "data_dir": str(manifest_doc["paths"]["live_data_root"]),
        "output_dir": str(output_dir),
        "run_manifest_path": str(manifest_path),
        "explicit_config": explicit_config,
        "resume_execution": bool(include_resume_execution),
        "invocation_python": invocation_python,
        "research_python": research_python,
        "git_commit": git_commit,
    }


def write_run_manifest(payload: Dict[str, Any]) -> Path:
    manifest_path = Path(str(payload["run_manifest_path"]))
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest_path


def start_registered_run(
    repo_root: Path,
    mode: str,
    profile: str,
    explicit_config: str = "",
    include_resume_execution: bool = False,
    invocation_python: str = "",
    research_python: str = "",
) -> Dict[str, Any]:
    payload = build_run_manifest(
        repo_root=repo_root,
        mode=mode,
        profile=profile,
        explicit_config=explicit_config,
        include_resume_execution=include_resume_execution,
        invocation_python=invocation_python,
        research_python=research_python,
        status="starting",
    )
    write_run_manifest(payload)
    return payload


def finalize_registered_run(payload: Dict[str, Any], status: str, exit_code: int | None = None) -> Path:
    final_payload = dict(payload)
    final_payload["status"] = status
    final_payload["updated_at"] = _now_text()
    if exit_code is not None:
        final_payload["exit_code"] = int(exit_code)
    return write_run_manifest(final_payload)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Write a canonical run_manifest.json without running the pipeline")
    parser.add_argument("--repo-root", default="", help="Repository root; defaults to the parent of this tools directory")
    parser.add_argument("--mode", required=True, help="Formal run mode")
    parser.add_argument("--profile", required=True, help="Formal run profile")
    parser.add_argument("--config", default="", help="Optional explicit runtime config path")
    parser.add_argument("--resume-execution", action="store_true", help="Record resume execution intent")
    parser.add_argument("--status", default="registered", help="Initial status stored in the manifest")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    repo_root = Path(args.repo_root).resolve() if str(args.repo_root).strip() else Path(__file__).resolve().parents[1]
    payload = build_run_manifest(
        repo_root=repo_root,
        mode=str(args.mode).strip(),
        profile=str(args.profile).strip(),
        explicit_config=str(args.config).strip(),
        include_resume_execution=bool(args.resume_execution),
        invocation_python="",
        research_python="",
        status=str(args.status).strip() or "registered",
    )
    path = write_run_manifest(payload)
    print(json.dumps({"ok": True, "run_id": payload["run_id"], "run_manifest_path": str(path)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
