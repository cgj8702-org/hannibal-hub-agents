# 🤖 Pull Request Description Template

Use this template when creating or editing pull request descriptions in the Hannibal Hub Agents repository.

---

## 📋 Title Format

```
[type] Brief description of changes
```

**Types:**
- `feat:` New feature
- `fix:` Bug fix
- `docs:` Documentation changes
- `refactor:` Code refactoring
- `test:` Test additions/updates
- `chore:` Maintenance tasks
- `security:` Security-related changes

---

## 🗒️ Description

### What
<!-- Clear description of the changes made -->

### Why
<!-- Motivation and context for the change -->

### How
<!-- Technical approach, key implementation details -->

---

## 🧪 Testing

### Test Commands
```bash
uv sync
./scripts/ruff-all.sh
uv run pytest src/webhook_agent/tests/
```

### Test Results
<!-- Document test coverage and results -->

---

## 📦 Configuration Impact

If this PR introduces or modifies configuration:

- [ ] Update README.md environment variables
- [ ] Update pyproject.toml if adding dependencies
- [ ] Run `uv sync` after dependency changes

---

## 🔒 Security Checklist

- [ ] No secrets or credentials hardcoded
- [ ] HMAC signature verification logic unchanged (if applicable)
- [ ] Authentication/authorization behavior preserved
- [ ] Webhook payload handling validated

---

## 🔗 Related

- Closes #`_______`
- References #`_______`