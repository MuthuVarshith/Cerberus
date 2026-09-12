import os
import tempfile
import subprocess
import json
import base64
import requests
import uuid
import zipfile
import io
from typing import List, Dict, Any, Tuple, Optional
from datetime import datetime, timezone

def get_github_headers(authenticated: bool = True) -> Dict[str, str]:
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "Cerberus-Autonomous-Repair"
    }
    if authenticated:
        token = os.environ.get("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
    return headers

def _github_get(url: str, timeout: int = 20) -> requests.Response:
    """GET with token, retry without token on 401 (expired/invalid token)."""
    resp = requests.get(url, headers=get_github_headers(authenticated=True), timeout=timeout)
    if resp.status_code == 401:
        resp = requests.get(url, headers=get_github_headers(authenticated=False), timeout=timeout)
    return resp

def parse_repo_url(url: str) -> Tuple[str, str]:
    """Parses a repo URL or slug into owner and repo."""
    clean = url.strip().replace("https://", "").replace("http://", "").rstrip("/")
    if clean.startswith("github.com/"):
        clean = clean[len("github.com/"):]
    parts = clean.split("/")
    if len(parts) >= 2:
        return parts[0], parts[1].replace(".git", "")
    raise ValueError(f"Invalid repository URL format: {url}")

def get_default_branch(owner: str, repo: str) -> str:
    try:
        resp = _github_get(f"https://api.github.com/repos/{owner}/{repo}")
        if resp.status_code == 200:
            return resp.json().get("default_branch", "main")
    except Exception:
        pass
    return "main"

def download_repo_archive(owner: str, repo: str, branch: str = "main") -> Tuple[Optional[str], Optional[tempfile.TemporaryDirectory]]:
    """Downloads zipball without using API rate limit points.
    Returns (extracted_folder_path, temp_dir_handle) or (None, None).
    """
    branches_to_try = [branch, "main", "master"]
    for b in branches_to_try:
        url = f"https://codeload.github.com/{owner}/{repo}/zip/refs/heads/{b}"
        try:
            r = requests.get(url, timeout=30)
            if r.status_code == 200 and len(r.content) > 100:
                tmpdir = tempfile.TemporaryDirectory()
                z = zipfile.ZipFile(io.BytesIO(r.content))
                z.extractall(tmpdir.name)
                extracted_root = os.path.join(tmpdir.name, os.listdir(tmpdir.name)[0])
                return extracted_root, tmpdir
        except Exception as e:
            print(f"Archive download failed for branch {b}: {e}")
    return None, None

def get_file_tree(owner: str, repo: str) -> Tuple[List[Dict[str, Any]], str]:
    """Returns tree and default branch name."""
    try:
        branch = get_default_branch(owner, repo)
        resp = _github_get(f"https://api.github.com/repos/{owner}/{repo}/git/trees/{branch}?recursive=1")
        if resp.status_code == 200:
            return resp.json().get("tree", []), branch
    except Exception as e:
        print(f"Error fetching tree for {owner}/{repo}: {e}")
    return [], "main"

def get_file_content(owner: str, repo: str, path: str) -> str:
    try:
        resp = _github_get(f"https://api.github.com/repos/{owner}/{repo}/contents/{path}")
        if resp.status_code == 200:
            data = resp.json()
            if "content" in data:
                return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
    except Exception:
        pass
    return ""

def detect_language_via_api(owner: str, repo: str) -> str:
    """Uses GitHub's Languages API — works on public repos without auth."""
    try:
        resp = _github_get(f"https://api.github.com/repos/{owner}/{repo}/languages")
        if resp.status_code == 200:
            langs = resp.json()
            if langs:
                primary_raw = max(langs, key=langs.get)
                if primary_raw == "Jupyter Notebook" and "Python" in langs:
                    return "python"
                
                primary = primary_raw.lower()
                mapping = {
                    "python": "python", "javascript": "javascript",
                    "typescript": "javascript", "go": "go",
                    "rust": "rust", "java": "java", "ruby": "ruby",
                    "c#": "csharp", "c++": "cpp", "c": "c",
                    "shell": "shell", "kotlin": "kotlin", "swift": "swift",
                    "jupyter notebook": "jupyter"
                }
                return mapping.get(primary, primary)
    except Exception as e:
        print(f"Language API failed: {e}")
    return "unknown"

def detect_language_from_folder(folder_path: str) -> str:
    """Detects language by counting file extensions in extracted directory."""
    counts: Dict[str, int] = {}
    for root, _, files in os.walk(folder_path):
        for file in files:
            ext = os.path.splitext(file)[1].lower()
            if ext == ".py":
                counts["python"] = counts.get("python", 0) + 1
            elif ext in [".js", ".jsx", ".ts", ".tsx"]:
                counts["javascript"] = counts.get("javascript", 0) + 1
            elif ext == ".go":
                counts["go"] = counts.get("go", 0) + 1
            elif ext == ".rs":
                counts["rust"] = counts.get("rust", 0) + 1
            elif ext == ".java":
                counts["java"] = counts.get("java", 0) + 1
    if not counts:
        return "unknown"
    return max(counts, key=counts.get)

def run_static_analysis_on_folder(folder_path: str) -> List[Dict[str, Any]]:
    findings = []
    
    # 1. Bandit (Security vulnerabilities)
    try:
        bandit_cmd = ["py", "-3", "-m", "bandit", "-r", folder_path, "-f", "json", "-q"]
        result = subprocess.run(bandit_cmd, capture_output=True, text=True, timeout=30)
        if result.stdout.strip():
            bandit_data = json.loads(result.stdout)
            for issue in bandit_data.get("results", []):
                rel_path = os.path.relpath(issue["filename"], folder_path).replace("\\", "/")
                sev_raw = issue.get("issue_severity", "MEDIUM").upper()
                sev_map = {"LOW": "low", "MEDIUM": "high", "HIGH": "critical"}
                findings.append({
                    "id": f"sec-{uuid.uuid4().hex[:6]}",
                    "severity": sev_map.get(sev_raw, "high"),
                    "category": "Security",
                    "file": rel_path,
                    "line": issue.get("line_number", 1),
                    "summary": issue.get("issue_text", "Potential security vulnerability detected"),
                    "auto_fixable": True
                })
    except Exception as e:
        print(f"Bandit execution failed: {e}")

    # 2. Pylint (Errors, Warnings, Style)
    try:
        pylint_cmd = ["py", "-3", "-m", "pylint", folder_path, "-f", "json", "--exit-zero", "--score=n"]
        result = subprocess.run(pylint_cmd, capture_output=True, text=True, timeout=40)
        if result.stdout.strip():
            pylint_data = json.loads(result.stdout)
            for issue in pylint_data:
                rel_path = os.path.relpath(issue.get("path", ""), folder_path).replace("\\", "/")
                type_name = issue.get("type", "").lower()
                symbol = issue.get("symbol", "")
                
                if type_name in ("fatal", "error"):
                    severity = "high"
                    category = "Runtime Error"
                    auto_fixable = True
                elif type_name == "warning":
                    severity = "medium"
                    category = "Reliability"
                    auto_fixable = symbol in ("dangerous-default-value", "bare-except", "broad-except", "unspecified-encoding")
                else:
                    severity = "low"
                    category = "Style"
                    auto_fixable = False
                
                findings.append({
                    "id": f"pylint-{uuid.uuid4().hex[:6]}",
                    "severity": severity,
                    "category": category,
                    "file": rel_path,
                    "line": issue.get("line", 1),
                    "summary": f"{issue.get('message', '')} ({symbol})",
                    "auto_fixable": auto_fixable
                })
    except Exception as e:
        print(f"Pylint execution failed: {e}")

    return findings

import re

MULTI_LANG_PATTERNS = [
    # JS / TS
    {
        "exts": [".js", ".jsx", ".ts", ".tsx", ".mjs"],
        "pattern": r"eval\s*\(",
        "category": "Security",
        "severity": "critical",
        "summary": "Dangerous use of eval() function allows arbitrary code execution",
        "auto_fixable": True
    },
    {
        "exts": [".js", ".jsx", ".ts", ".tsx", ".mjs"],
        "pattern": r"\.innerHTML\s*=",
        "category": "Security",
        "severity": "high",
        "summary": "Unsanitized innerHTML assignment risks Cross-Site Scripting (XSS)",
        "auto_fixable": True
    },
    {
        "exts": [".js", ".jsx", ".ts", ".tsx", ".mjs"],
        "pattern": r"\b(api_key|secret|private_key|password)\b\s*[:=]\s*['\"][A-Za-z0-9_\-]{8,}['\"]",
        "category": "Security",
        "severity": "critical",
        "summary": "Hardcoded API key or secret token detected in client code",
        "auto_fixable": True
    },
    {
        "exts": [".js", ".jsx", ".ts", ".tsx", ".mjs"],
        "pattern": r"[^=!<>]==[^=]",
        "category": "Reliability",
        "severity": "low",
        "summary": "Loose equality operator '==' used instead of strict '==='",
        "auto_fixable": True
    },
    # Go
    {
        "exts": [".go"],
        "pattern": r"exec\.Command\s*\(",
        "category": "Security",
        "severity": "critical",
        "summary": "External process execution via os/exec (possible command injection)",
        "auto_fixable": True
    },
    {
        "exts": [".go"],
        "pattern": r"_\s*,\s*err\s*:=",
        "category": "Reliability",
        "severity": "medium",
        "summary": "Ignored error return value in Go statement",
        "auto_fixable": True
    },
    # Java
    {
        "exts": [".java"],
        "pattern": r"Runtime\.getRuntime\(\)\.exec\s*\(",
        "category": "Security",
        "severity": "critical",
        "summary": "Command injection vulnerability via Runtime.getRuntime().exec()",
        "auto_fixable": True
    },
    {
        "exts": [".java"],
        "pattern": r"System\.out\.println\s*\(",
        "category": "Style",
        "severity": "low",
        "summary": "Production code contains raw System.out.println statement",
        "auto_fixable": True
    },
    # C / C++
    {
        "exts": [".c", ".cpp", ".cc", ".h", ".hpp"],
        "pattern": r"\bstrcpy\s*\(",
        "category": "Security",
        "severity": "critical",
        "summary": "Unsafe buffer operation: strcpy() does not bound string length",
        "auto_fixable": True
    },
    {
        "exts": [".c", ".cpp", ".cc", ".h", ".hpp"],
        "pattern": r"\bsprintf\s*\(",
        "category": "Security",
        "severity": "high",
        "summary": "Unsafe sprintf() usage without buffer boundary check",
        "auto_fixable": True
    },
    {
        "exts": [".c", ".cpp", ".cc", ".h", ".hpp"],
        "pattern": r"\bgets\s*\(",
        "category": "Security",
        "severity": "critical",
        "summary": "Extremely dangerous gets() function detected (removed in C11)",
        "auto_fixable": True
    },
    # Rust
    {
        "exts": [".rs"],
        "pattern": r"\.unwrap\s*\(\)",
        "category": "Reliability",
        "severity": "high",
        "summary": "Unchecked .unwrap() call risks runtime panic on None/Err",
        "auto_fixable": True
    },
    # PHP
    {
        "exts": [".php"],
        "pattern": r"\b(eval|shell_exec|exec|system)\s*\(",
        "category": "Security",
        "severity": "critical",
        "summary": "Execution of dynamic shell command or string",
        "auto_fixable": True
    },
    # Shell
    {
        "exts": [".sh", ".bash"],
        "pattern": r"rm\s+-rf\s+\$[A-Za-z0-9_]+",
        "category": "Security",
        "severity": "critical",
        "summary": "Unquoted variable expansion in recursive directory removal command",
        "auto_fixable": True
    }
]

def scan_multilanguage_folder(folder_path: str) -> List[Dict[str, Any]]:
    """Scans all files in folder using multi-language rule engines."""
    findings = []
    # 1. Python specific tools
    findings.extend(run_static_analysis_on_folder(folder_path))

    # 2. Multi-language pattern analyzer for JS, TS, Go, Rust, Java, C/C++, PHP, Shell, etc.
    for root, _, files in os.walk(folder_path):
        for file in files:
            ext = os.path.splitext(file)[1].lower()
            rel_path = os.path.relpath(os.path.join(root, file), folder_path).replace("\\", "/")
            
            # Skip node_modules, vendor, .git, etc.
            if any(x in rel_path for x in ["node_modules/", "vendor/", ".git/", "dist/", "build/", "target/"]):
                continue
                
            try:
                full_path = os.path.join(root, file)
                with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                    lines = f.readlines()
                    
                for rule in MULTI_LANG_PATTERNS:
                    if ext in rule["exts"]:
                        compiled_re = re.compile(rule["pattern"])
                        for line_idx, line_text in enumerate(lines, 1):
                            if compiled_re.search(line_text):
                                findings.append({
                                    "id": f"multilang-{uuid.uuid4().hex[:6]}",
                                    "severity": rule["severity"],
                                    "category": rule["category"],
                                    "file": rel_path,
                                    "line": line_idx,
                                    "summary": rule["summary"],
                                    "auto_fixable": rule["auto_fixable"]
                                })
                                if len(findings) >= 30:
                                    break
            except Exception as e:
                print(f"Failed scanning {rel_path}: {e}")
                
    return findings


def scan_repository(repo_url: str) -> Dict[str, Any]:
    owner, repo = parse_repo_url(repo_url)
    default_branch = get_default_branch(owner, repo)
    
    # 1. Download zero-rate-limit zip archive via codeload
    extracted_folder, temp_handle = download_repo_archive(owner, repo, default_branch)
    
    lang = detect_language_via_api(owner, repo)
    if lang == "unknown" and extracted_folder:
        lang = detect_language_from_folder(extracted_folder)
        
    findings = []
    
    if extracted_folder:
        findings = scan_multilanguage_folder(extracted_folder)
        temp_handle.cleanup()
    else:
        # Fallback if codeload zip failed
        tree, _ = get_file_tree(owner, repo)
        if tree:
            python_files = [i for i in tree if i.get("type") == "blob" and i.get("path", "").endswith(".py")]
            files_to_scan = python_files[:15]
            with tempfile.TemporaryDirectory() as tmpdir:
                for f_info in files_to_scan:
                    p = f_info["path"]
                    c = get_file_content(owner, repo, p)
                    if c:
                        lp = os.path.join(tmpdir, p)
                        os.makedirs(os.path.dirname(lp), exist_ok=True)
                        with open(lp, "w", encoding="utf-8") as f:
                            f.write(c)
                findings = scan_multilanguage_folder(tmpdir)

    areas_reviewed = set(f["category"] for f in findings) or {"General"}
    
    baseline_snapshot = {
        "areas_reviewed": len(areas_reviewed),
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "primary_language": lang,
        "is_python": (lang == "python"),
        "default_branch": default_branch
    }
    
    return {
        "status": "success",
        "repo": f"{owner}/{repo}",
        "primary_language": lang,
        "is_python": (lang == "python"),
        "issues": findings,
        "baseline_snapshot": baseline_snapshot
    }
