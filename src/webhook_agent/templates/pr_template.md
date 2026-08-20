## 🗒️ Description

### Summary
<!-- Clear description of the changes made and technical approach -->

### Motivation & Context
<!-- Why is this change necessary? What problem does it solve? -->

---

## 🧪 Testing

### Test Commands
```bash
uv sync
./scripts/ruff-all.sh
uv run pytest
```

### Validation Results
<!-- Summary of test execution and code validation -->

---

## 🔒 Security & Policy Checklist

- [ ] Secrets and credentials checked (no hardcoded keys)
- [ ] Authentication and authorization boundaries intact
- [ ] PR passes local linting (`./scripts/ruff-all.sh`) and tests (`pytest`)