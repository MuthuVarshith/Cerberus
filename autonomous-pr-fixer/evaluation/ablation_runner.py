"""
Ablation Study Runner.
Measures the incremental reliability contribution of each architectural layer:
Config 1: Baseline (Unchecked repair loop)
Config 2: Baseline + RED Gate (Reproduction-First constraint)
Config 3: Baseline + RED Gate + Structural Blast-Radius Analysis
Config 4: Full Verification-First Harness (+ Regression Suite & Admission Controller)

The figures are derived, not written out: each configuration is applied to the
same fixed set of bug profiles and the admitted set is counted. The profiles are
stipulated inputs (see EVALUATION_STATUS), but the effect of switching a gate on
is computed from them, so a gate that stopped working would change the table.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import List, Tuple

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

EVALUATION_STATUS = (
    "*Ablation Matrix on Controlled Bug Profiles* (stipulated scenario archetypes, "
    "not live LLM generation; the gate arithmetic over them is computed)."
)


@dataclass(frozen=True)
class BugProfile:
    """Ground truth about one scenario, independent of which gates are enabled."""

    name: str
    reproduced: bool
    target_passed: bool
    regression_clean: bool
    blast_radius_clean: bool

    @property
    def is_truly_safe(self) -> bool:
        """Whether merging this patch would actually have been correct."""
        return self.target_passed and self.regression_clean and self.blast_radius_clean


#: The five failure modes the harness is designed to separate.
BUG_PROFILES: List[BugProfile] = [
    BugProfile("clean_fix", True, True, True, True),
    BugProfile("needs_retry", True, True, True, True),
    BugProfile("phantom_issue", False, False, True, True),
    BugProfile("breaks_legacy_feature", True, True, False, True),
    BugProfile("overbroad_patch", True, True, True, False),
]


@dataclass(frozen=True)
class GateConfig:
    label: str
    red_gate: bool
    blast_radius: bool
    regression: bool

    def admits(self, p: BugProfile) -> bool:
        """Whether this configuration would open a PR for the profile.

        A disabled gate cannot reject, which is the point of the study: an ungated
        loop admits everything it produced, including the patches that are wrong.
        """
        if self.red_gate and not p.reproduced:
            return False
        if self.blast_radius and not p.blast_radius_clean:
            return False
        if self.regression and not p.regression_clean:
            return False
        return True


CONFIGS: List[GateConfig] = [
    GateConfig("(1) Baseline (Free Loop)", red_gate=False, blast_radius=False, regression=False),
    GateConfig("(2) + RED Gate", red_gate=True, blast_radius=False, regression=False),
    GateConfig("(3) + RED Gate + Blast Radius", red_gate=True, blast_radius=True, regression=False),
    GateConfig("(4) Full Harness (All 3 Gates)", red_gate=True, blast_radius=True, regression=True),
]


def evaluate_config(cfg: GateConfig, profiles: List[BugProfile]) -> Tuple[float, float, int, int]:
    """Return (false_positive_pct, safe_pr_pct, unsafe_admitted, safe_admitted)."""
    n = len(profiles)
    admitted = [p for p in profiles if cfg.admits(p)]
    unsafe = [p for p in admitted if not p.is_truly_safe]
    safe = [p for p in admitted if p.is_truly_safe]
    return (
        round(len(unsafe) / n * 100.0, 1),
        round(len(safe) / n * 100.0, 1),
        len(unsafe),
        len(safe),
    )


def run_ablation_study(profiles: List[BugProfile] = BUG_PROFILES) -> str:
    print("=" * 65)
    print("🔬 Running Ablation Study: Isolating Verification Gates")
    print("=" * 65)

    n = len(profiles)
    rows = []
    results = {}
    for cfg in CONFIGS:
        fp_pct, safe_pct, unsafe_n, _ = evaluate_config(cfg, profiles)
        results[cfg.label] = (fp_pct, safe_pct, unsafe_n)
        mark = lambda on: "✅ **On**" if on else "❌ Off"  # noqa: E731
        rows.append(
            f"| **{cfg.label}** | {mark(cfg.red_gate)} | {mark(cfg.blast_radius)} | "
            f"{mark(cfg.regression)} | {fp_pct}% ({unsafe_n}/{n}) | {safe_pct}% |"
        )

    baseline_fp = results[CONFIGS[0].label][0]
    red_fp = results[CONFIGS[1].label][0]
    blast_fp = results[CONFIGS[2].label][0]
    full_fp = results[CONFIGS[3].label][0]
    full_safe = results[CONFIGS[3].label][1]
    red_reduction = round((baseline_fp - red_fp) / baseline_fp * 100.0, 1) if baseline_fp else 0.0
    blast_reduction = round((red_fp - blast_fp) / red_fp * 100.0, 1) if red_fp else 0.0

    table_rows = "\n".join(rows)
    return f"""### 🧪 Ablation Study: Impact of Verification Constraints on Repair Safety
> **Evaluation Status:** {EVALUATION_STATUS}

| Configuration | RED Gate | Blast-Radius Guard | Regression Suite | False-Positive PR Rate | True Safe PR Rate |
| :--- | :---: | :---: | :---: | :---: | :---: |
{table_rows}

#### 💡 Research Insights (RQ1 & RQ2):
1. **RQ1 (Reproduction Gate):** The hard RED gate cuts the false-positive PR rate from \
{baseline_fp}% to {red_fp}% (a {red_reduction}% relative reduction) by refusing to patch \
issues that could not be made to fail first.
2. **RQ2 (Blast-Radius Analysis):** Adding structural AST blast-radius checks takes it \
from {red_fp}% to {blast_fp}% (a further {blast_reduction}% relative reduction), preventing \
edits outside the localized fault boundary.
3. **Synthesis:** Only the full conjunction \
(`target_passed and regression_passed and blast_radius_acceptable and patch_changed`) \
reaches {full_fp}% false positives, and it does so without reducing the safe PR rate \
({full_safe}%, unchanged across all four configurations).
"""


if __name__ == "__main__":
    table = run_ablation_study()
    print(table)
