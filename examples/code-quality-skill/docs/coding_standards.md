# Python Coding Standards Reference

## Formatting Rules
- **black** for all code formatting (line length 88)
- **isort** with `--profile black` for import ordering
- No trailing whitespace; every file must end with a single newline

## Quality Gates

| Metric | Threshold | Action |
|---|---|---|
| pylint score | < 4.0 | **Fail** — block release |
| Critical errors | any present | **Fail** — must fix before release |
| Warnings per file | > 10 | Flag in report, continue |
| Test coverage | < 80 % | Warn only |

## Type Hints
- Required on all public function signatures
- Use `Optional[X]` for Python < 3.10, `X | None` for 3.10+
- Return type annotations required; `-> None` must be explicit

## Docstrings
- Google style
- Required for all public classes and functions
- Format:
  ```
  """One-line summary.

  Args:
      param_name: Description.

  Returns:
      Description of return value.

  Raises:
      ValueError: When input is invalid.
  """
  ```

## Commit & Release Checklist
- [ ] pylint score ≥ 4.0
- [ ] Zero critical errors in analysis report
- [ ] black + isort applied and committed
- [ ] PR approved by ≥ 1 reviewer
- [ ] CI pipeline green
- [ ] `release_version` follows semver (MAJOR.MINOR.PATCH)
