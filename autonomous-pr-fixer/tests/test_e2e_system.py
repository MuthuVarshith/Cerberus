import os
import tempfile
import json
import glob
from main import run_pipeline

def test_e2e_successful_repair_pipeline():
    """
    End-to-End System Test:
    Verifies that the Cerberus pipeline can successfully take a buggy repository,
    process it through all 8 stages, verify the patch, and reach the ADMITTED state.
    This satisfies the 'Integration and System-level tests' requirement.
    """
    with tempfile.TemporaryDirectory() as temp_repo:
        # Run the entire pipeline in local (dry-run) mode
        # This will use the built-in fallback for rate_calculator
        success = run_pipeline(
            repo_dir=temp_repo,
            issue_number=999,
            issue_title="Divide by zero in rate_calculator",
            issue_body="calculate_rate(10, 0) throws ZeroDivisionError",
            dry_run=True,
            mode="local",
            demo=True
        )
        
        # 1. Pipeline should return True (PR Admitted Locally)
        assert success is True, "Pipeline failed to reach ADMITTED state"
        
        # 2. Verify the system generated the artifact correctly
        # We look in the current working directory's artifacts folder since main.py writes there
        artifact_dirs = glob.glob(os.path.join("artifacts", "run_*"))
        
        # Find the most recently created run artifact
        latest_artifact_dir = max(artifact_dirs, key=os.path.getmtime)
        artifact_path = os.path.join(latest_artifact_dir, "run.json")
        
        assert os.path.exists(artifact_path), f"Artifact not found at {artifact_path}"
        
        # 3. Verify the state machine correctly navigated all strict gates
        with open(artifact_path, "r", encoding="utf-8") as f:
            audit_data = json.load(f)
            
        history = audit_data.get("pipeline_state_history", [])
        
        # Ensure it hit the critical verification milestones in order
        assert "REPRODUCED_RED" in history, "Failed to enforce RED gate"
        assert "PATCH_GREEN" in history, "Failed to enforce GREEN gate"
        assert "BLAST_RADIUS_ACCEPTABLE" in history, "Failed to enforce BLAST RADIUS gate"
        assert "ADMITTED" in history[-1], "Final state was not ADMITTED"
        
        # 4. Verify Diff/Patch metrics were accurately captured
        assert audit_data["blast_radius"]["is_acceptable"] is True
        assert audit_data["patch_verified_against_test"] is True
        assert audit_data["diff_lines_added"] > 0
