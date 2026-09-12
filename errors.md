ASYNC240 Async functions should not perform blocking os.path operations
   --> dev/model_sync.py:130:8
    |
129 |     # Compare with existing file to avoid noisy writes
130 |     if os.path.exists(TARGET_REGISTRY):
    |        ^^^^^^^^^^^^^^
131 |         try:
    |

SIM102 Use a single `if` statement instead of nested `if` statements
   --> src/webhook_agent/formatter.py:162:9
    |
160 |           r_lower = r_text.lower()
161 |           rec_lower = rec_text.lower()
162 | /         if any(kw in r_lower or kw in rec_lower for kw in BREAKING_RISK_KEYWORDS):
163 | |             if not any(
164 | |                 r_text in c.get("description", "") or c.get("description", "") in r_text
165 | |                 for c in clean_crit
166 | |             ):
    | |______________^
167 |                   clean_crit.append(
168 |                       {
    |
help: Combine `if` statements using `and`

B005 Using `.strip()` with multi-character strings is misleading
   --> src/webhook_agent/formatter.py:637:37
    |
635 |     )
636 |     if summary_match:
637 |         data["executive_summary"] = summary_match.group(1).strip("* -•` ")
    |                                     ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
638 |     else:
639 |         lines = [
    |

B005 Using `.strip()` with multi-character strings is misleading
   --> src/webhook_agent/formatter.py:643:17
    |
641 |                 r"^(?:\*?\s*\*\*?Summary & Justification:\*\*?|\*?\s*\*\*?Executive Summary:\*\*?)\s*",
642 |                 "",
643 |                 line.strip("* -•` "),
    |                 ^^^^^^^^^^^^^^^^^^^^
644 |                 flags=re.I,
645 |             )
    |

SIM102 Use a single `if` statement instead of nested `if` statements
  --> src/webhook_agent/proactive_service.py:52:9
   |
50 |           # 1. Check Merge Conflicts
51 |           is_dirty = getattr(pr, "mergeable_state", None) == "dirty"
52 | /         if getattr(pr, "mergeable", None) is False or is_dirty:
53 | |             if not self._has_recent_comment_with_text(
54 | |                 pr, "Unable to automatically resolve merge conflicts"
55 | |             ):
   | |______________^
56 |                   logger.info(
57 |                       "Proactive Action: Detected merge conflict on PR #%d",
   |
help: Combine `if` statements using `and`

SIM102 Use a single `if` statement instead of nested `if` statements
  --> src/webhook_agent/proactive_service.py:87:9
   |
85 |           # 3. Check Failing CI Check Runs
86 |           failing_checks = self._get_failing_check_runs(pr)
87 | /         if failing_checks:
88 | |             if not self._has_recent_comment_with_text(
89 | |                 pr, "Proactive Diagnostic: Failing CI Checks"
90 | |             ):
   | |______________^
91 |                   try:
92 |                       checks_summary = "\n".join(
   |
help: Combine `if` statements using `and`

RUF002 Docstring contains ambiguous `–` (EN DASH). Did you mean `-` (HYPHEN-MINUS)?
   --> src/webhook_agent/processor.py:547:50
    |
545 |     """Handles inbound webhook events.
546 |
547 |     The processor expects a *normalized* payload – a dictionary that matches
    |                                                  ^
548 |     the GitHub webhook headers:
    |

RUF002 Docstring contains ambiguous `–` (EN DASH). Did you mean `-` (HYPHEN-MINUS)?
   --> src/webhook_agent/processor.py:550:22
    |
548 |     the GitHub webhook headers:
549 |
550 |     * ``event_name`` – the X‑GitHub‑Event header value.
    |                      ^
551 |     * ``action`` – the action field nested inside the payload.
552 |     * ``delivery_id`` – a unique id for the webhook delivery.
    |

RUF002 Docstring contains ambiguous `‑` (NON-BREAKING HYPHEN). Did you mean `-` (HYPHEN-MINUS)?
   --> src/webhook_agent/processor.py:550:29
    |
548 |     the GitHub webhook headers:
549 |
550 |     * ``event_name`` – the X‑GitHub‑Event header value.
    |                             ^
551 |     * ``action`` – the action field nested inside the payload.
552 |     * ``delivery_id`` – a unique id for the webhook delivery.
    |

RUF002 Docstring contains ambiguous `‑` (NON-BREAKING HYPHEN). Did you mean `-` (HYPHEN-MINUS)?
   --> src/webhook_agent/processor.py:550:36
    |
548 |     the GitHub webhook headers:
549 |
550 |     * ``event_name`` – the X‑GitHub‑Event header value.
    |                                    ^
551 |     * ``action`` – the action field nested inside the payload.
552 |     * ``delivery_id`` – a unique id for the webhook delivery.
    |

RUF002 Docstring contains ambiguous `–` (EN DASH). Did you mean `-` (HYPHEN-MINUS)?
   --> src/webhook_agent/processor.py:551:18
    |
550 |     * ``event_name`` – the X‑GitHub‑Event header value.
551 |     * ``action`` – the action field nested inside the payload.
    |                  ^
552 |     * ``delivery_id`` – a unique id for the webhook delivery.
553 |     * ``raw_payload`` – the original JSON body of the webhook.
    |

RUF002 Docstring contains ambiguous `–` (EN DASH). Did you mean `-` (HYPHEN-MINUS)?
   --> src/webhook_agent/processor.py:552:23
    |
550 |     * ``event_name`` – the X‑GitHub‑Event header value.
551 |     * ``action`` – the action field nested inside the payload.
552 |     * ``delivery_id`` – a unique id for the webhook delivery.
    |                       ^
553 |     * ``raw_payload`` – the original JSON body of the webhook.
554 |     """
    |

RUF002 Docstring contains ambiguous `–` (EN DASH). Did you mean `-` (HYPHEN-MINUS)?
   --> src/webhook_agent/processor.py:553:23
    |
551 |     * ``action`` – the action field nested inside the payload.
552 |     * ``delivery_id`` – a unique id for the webhook delivery.
553 |     * ``raw_payload`` – the original JSON body of the webhook.
    |                       ^
554 |     """
    |

RUF002 Docstring contains ambiguous `‑` (NON-BREAKING HYPHEN). Did you mean `-` (HYPHEN-MINUS)?
   --> src/webhook_agent/processor.py:631:22
    |
630 |     def should_process_event(self, ev: dict[str, Any]) -> bool:
631 |         """Apply loop‑avoidance, noise filtering, and duplication checks.
    |                      ^
632 |
633 |         * Duplicate deliveries are suppressed.
    |

SIM102 Use a single `if` statement instead of nested `if` statements
  --> src/webhook_agent/sanitizer_plugin.py:58:9
   |
56 |       ) -> LlmResponse | None:
57 |           """Intercept and sanitize model output before rendering or downstream processing."""
58 | /         if hasattr(llm_response, "content") and llm_response.content:
59 | |             if isinstance(llm_response.content, str):
   | |_____________________________________________________^
60 |                   llm_response.content = sanitize_markdown_text(llm_response.content)
   |
help: Combine `if` statements using `and`

B005 Using `.strip()` with multi-character strings is misleading
  --> src/webhook_agent/schemas.py:23:12
   |
21 |         flags=re.IGNORECASE,
22 |     )
23 |     return cleaned.strip("* -•` ")
   |            ^^^^^^^^^^^^^^^^^^^^^^^

SIM117 Use a single `with` statement with multiple contexts instead of nested `with` statements
  --> tests/unit/test_constants_and_secrets.py:48:5
   |
46 |       mock_sm.SecretManagerServiceClient.return_value.access_secret_version.return_value = mock_payload
47 |
48 | /     with patch.dict("sys.modules", {"google.cloud.secretmanager": mock_sm}):
49 | |         with patch.dict("logic.secret_manager._SECRET_CACHE", {}, clear=True):
   | |______________________________________________________________________________^
50 |               val = resolve_secret("WEBHOOK_FREE_KEY")
51 |               assert val == "resolved_from_secret_manager"
   |
help: Combine `with` statements

Found 17 errors.
