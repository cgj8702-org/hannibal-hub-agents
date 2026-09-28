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
uv run mypy src/webhook_agent
uv run pytest
```

### Test Results
<!-- Document test coverage and results -->

---

## 📦 Configuration Impact

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

- Closes #
- References #