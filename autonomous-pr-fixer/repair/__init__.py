"""
Cerberus repair agent (Cerberus 2.0, Phase 1).

One tool-using model proposes a patch; the existing Cerberus gate (`main.run_pipeline`)
decides whether it is admitted. Nothing in this package can admit a patch: the only
way a run ends ADMITTED is a gate run whose run.json says so.
"""
