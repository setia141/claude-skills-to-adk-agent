# Code Quality & Auto-Release Skill

## Description
Analyses a Python repository for quality issues, applies automated formatting
fixes, and creates a GitHub release if all quality gates pass.

## Parameters
- `repo_path` (string): Absolute path to the local repository
- `pr_number` (string): Pull request number being reviewed
- `release_version` (string): Semver string e.g. "1.4.2"
- `github_token` (string, secret): GitHub personal access token

## Steps

### Step 1: Run Static Analysis
Run lint and style checks on the repository using the analysis script.

Standards reference: [Coding Standards](./docs/coding_standards.md)

Execute: `./scripts/run_analysis.sh`

Input: `repo_path`
Output: JSON with `errors` list, `warnings` count, `score` (0–10)

### Step 2: Evaluate Quality Gates
Read the analysis JSON and decide whether to proceed.

- If `score < 4.0` → fail with reason, stop pipeline
- If `errors` is non-empty → fail, list critical issues
- If warnings > 10 per file → flag but continue

### Step 3: Apply Automated Fixes
If fixable issues exist (import ordering, formatting), apply them automatically.

Execute: `./scripts/apply_fixes.py`

Input: `repo_path`
Output: JSON with `fixes_applied` list and `failed` list

### Step 4: Generate Release Notes
Summarise the PR changes and fixes applied into a markdown changelog.

Input: `pr_number`, fix results from Step 3
Output: `release_notes` markdown string

### Step 5: Create GitHub Release
Publish the release via GitHub API. Requires human approval before executing.

Input: `github_token`, `release_version`, `release_notes`
Output: `release_url`

Human approval required before publishing the release.
