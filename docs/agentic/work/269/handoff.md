# Handoff — #269

Parent: #268 patient read infrastructure, itself dependent on #267 normalization.
Adds the two nested prescription summary reads and their tests/OpenAPI/docs.
Five fields only, patient association and prescription ownership checked before
query validation; no storage access or clinical calculation.

See verify.md for actual commands and limits. One issue, one commit, one PR.
Final exact-head CI and separate Lead Tech attestation are recorded in the PR.
Keep worktrees for human review. No merge, deployment, production migration or
closure of mobile #70/#71 and parent #10.
