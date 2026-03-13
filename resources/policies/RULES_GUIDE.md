# PDP Rules Authoring Guide

## Who This Is For

**AI rule editor (Layer 3)** — this file is injected as prompt context alongside `Rule.model_json_schema()` when the AI generates or modifies policy rules. It contains everything needed to produce a syntactically and semantically valid rule without reading evaluator source code.

**Human rule authors** — use this to write, review, and debug YAML rule files.

**Audit reviewers** — use this to understand what a rule does and why it is structured the way it is.

PDP itself contains no LLM client. The AI rule editor lives at Layer 3 (product layer) and calls the evaluator via the service API.

---

## 1. Rule Anatomy

A rule has five fields.

```yaml
- id: emp_term_wins             # Unique string identifier — must be unique across all YAML files.
  kind: lifecycle               # "lifecycle" or "risk". Affects precedence conventions only.
  when:                         # Map of condition key → expected value.
    subject.kind: employee      # All conditions are AND-ed together.
    subject.status: terminated
  then:                         # Map of outputs. Keys: abstract_state, risk_level, actions, signals.
    abstract_state: disabled
    actions:
      - revoke_all_sessions
  precedence: 100               # Integer. Higher value wins when multiple rules match.
```

### Lifecycle rule example

```yaml
- id: emp_pre_hire_not_yet
  kind: lifecycle
  when:
    subject.kind: employee
    subject.status: hired
    subject.start_date: "> now"
  then:
    abstract_state: pending
    actions:
      - schedule_evaluation_at: subject.start_date   # Parameterized action: key resolves a facts path.
  precedence: 50
```

### Risk rule example

```yaml
- id: risk_score_critical
  kind: risk
  when:
    threat.risk_score: "> 0.9"
  then:
    risk_level: critical
    abstract_state: suspended
    actions:
      - revoke_all_sessions
    signals:
      - ueba_critical
  precedence: 280
```

### `then` field details

| Key | Type | Effect |
|-----|------|--------|
| `abstract_state` | `AbstractState` enum string | Sets the canonical state for the subject/account pair. Only the highest-precedence rule that sets this wins; lower-precedence rules are overridden. |
| `risk_level` | `RiskLevel` enum string | Sets risk level. Same highest-precedence-wins logic. |
| `actions` | list of `Action` | Accumulated from **all** matched rules (not just the winner). Order is rule-match order. |
| `signals` | list of `Signal` strings | Accumulated from all matched rules. |

A rule without `abstract_state` in `then` contributes only `actions` and/or `signals`. This is intentional for additive rules (e.g., `nhi_expiring_soon`, `risk_score_high`).

---

## 2. Facts Reference

All condition keys in `when` are dotted paths resolved against the `Facts` object. Unknown paths silently resolve to `None` (the null-check operator can use this).

### `subject.*`

| Path | Type | Notes |
|------|------|-------|
| `subject.id` | `str` | Identity identifier |
| `subject.kind` | `str` | `employee` \| `nhi` \| `customer` |
| `subject.status` | `str` | Depends on kind — see below |
| `subject.org_unit` | `str \| None` | Organizational unit |
| `subject.start_date` | `datetime \| None` | Hire date (employees) |
| `subject.term_date` | `datetime \| None` | Termination date (employees) |
| `subject.nhi_kind` | `str \| None` | NHI subtype (service_account, bot, etc.) |
| `subject.owner.id` | `str \| None` | NHI owner identity ID |
| `subject.owner.status` | `str \| None` | NHI owner's current status |
| `subject.expires_at` | `datetime \| None` | NHI expiry timestamp |
| `subject.email_verified` | `bool \| None` | Customer email verification flag |
| `subject.tenant_id` | `str \| None` | Customer tenant identifier |
| `subject.tenant_role` | `str \| None` | Customer role within tenant |
| `subject.tenant_status` | `str \| None` | Status of the tenant (e.g. `suspended`) |
| `subject.plan_tier` | `str \| None` | Customer billing tier (e.g. `enterprise`, `trial`) |
| `subject.required_consents_met` | `bool` | False when required consents are withdrawn |
| `subject.mfa_enabled` | `bool` | True when MFA is active |

**Employee statuses (common):** `active`, `hired`, `terminated`, `on_leave`

**NHI statuses:** `active`, `locked`

**Customer statuses:** `active`, `registered`, `verified`, `suspended`, `banned`, `deletion_requested`

### `target.*`

`target` is `None` for IDP (subject-level) rules. When `target` is `None`, use `target: null` in `when` — see section on IDP rules.

| Path | Type | Notes |
|------|------|-------|
| `target.application` | `str` | Application identifier (e.g. `ad`, `jira`, `github`) |
| `target.account_status` | `str \| None` | Current account status in the application |
| `target.has_pending_attestation` | `bool` | True when an attestation task is open |
| `target.pending_reattestation` | `bool` | True when access must be re-attested after return |
| `target.privilege_level` | `str \| None` | e.g. `admin`, `readonly`, `standard` |
| `target.environment` | `str \| None` | e.g. `production`, `staging` |
| `target.data_sensitivity` | `str \| None` | Data classification level |
| `target.has_initiative` | virtual | See Operators section — checks `target.initiatives[].type` |
| `target.initiative.<type>.<field>` | virtual | Resolves a field of the first initiative matching `<type>` |

**Initiative fields** (`target.initiative.<type>.<field>`):

| Field | Type |
|-------|------|
| `type` | `str` |
| `origin` | `str \| None` |
| `valid_from` | `datetime \| None` |
| `valid_until` | `datetime \| None` |

### `threat.*`

`threat` is `None` when no threat context is present.

| Path | Type | Notes |
|------|------|-------|
| `threat.risk_score` | `float \| None` | UEBA score, typically 0.0–1.0 |
| `threat.active_indicators` | `list[str]` | Active threat indicator strings |
| `threat.days_since_last_login` | `int \| None` | Days since last successful login |
| `threat.failed_auth_count` | `int \| None` | Recent failed authentication attempts |
| `threat.has_indicator` | virtual | See Operators section — checks `threat.active_indicators` |

### Special: IDP (subject-level) rules

Use `target: null` in `when` to write a rule that applies to the IdP identity itself rather than an application account. These rules only match when `facts.target is None`. Rules without `target: null` only match when `facts.target is not None`. This is a strict partition — the two sets never overlap.

```yaml
when:
  subject.kind: employee
  subject.status: terminated
  target: null
```

---

## 3. Operators

All operators are expressed as the `value` side of a `when` condition.

### Equality (exact match)

```yaml
subject.kind: employee
subject.status: active
```

The resolved path value must equal the YAML value. Applies to strings, booleans, and integers.

### Boolean coercion

YAML string `"true"` and `"false"` are coerced to Python `bool` before comparison.

```yaml
subject.mfa_enabled: false        # YAML bool — works directly
subject.email_verified: "false"   # YAML string — also works
```

### Null check

Value `null` (YAML null) or the string `"null"` means the resolved path must be `None`.

```yaml
subject.owner: null               # NHI has no owner
target: null                      # IDP subject-level rule
```

### Temporal comparisons (relative to `facts.now`)

The resolved path must be a `datetime`. Comparison is against `facts.now`.

| Operator | Meaning |
|----------|---------|
| `"> now"` | datetime is in the future |
| `">= now"` | datetime is now or in the future |
| `"< now"` | datetime is in the past |
| `"<= now"` | datetime is now or in the past |

```yaml
subject.start_date: "> now"       # hire date is still in the future
subject.expires_at: "<= now"      # NHI has expired
```

### Temporal range

Left-inclusive, right-exclusive date range. Both bounds support offset notation.

```yaml
subject.expires_at: "now..now+30d"    # expires within the next 30 days
target.initiative.trial.valid_until: "now..now+7d"
```

Syntax: `<left_bound>..<right_bound>` where each bound is `now`, `now+Nd`, or `now-Nd`.

### `has_initiative`

Checks whether `target.initiatives` contains an initiative of the given type. Not a path — it is a special virtual key.

```yaml
target.has_initiative: grace         # any initiative with type == "grace" exists
target.has_initiative: trial
```

### `initiative.<type>.<field>`

Resolves a field on the first initiative in `target.initiatives` whose `type` matches `<type>`. Returns `None` if no matching initiative exists.

```yaml
target.initiative.grace.valid_until: "> now"
target.initiative.trial.valid_until: "<= now"
```

### `has_indicator`

Checks whether `threat.active_indicators` contains the given string. Not a path — it is a special virtual key.

```yaml
threat.has_indicator: credential_compromised
threat.has_indicator: impossible_travel
```

### Numeric comparison

Value must be of the form `"> N"` where `N` is an integer or float. The resolved path must be numeric.

**Only `>` is supported.** The evaluator does not support `< N`, `>= N`, or `<= N`. For "less than" semantics, use a numeric range (e.g., `"0..0.3"` instead of `"< 0.3"`).

```yaml
threat.risk_score: "> 0.9"
threat.failed_auth_count: "> 10"
threat.days_since_last_login: "> 90"
```

### Numeric range

Left-inclusive, right-exclusive float range.

```yaml
threat.risk_score: "0.7..0.9"       # 0.7 <= score < 0.9
```

---

## 4. AbstractState Semantics

`AbstractState` is a closed enum. No new values can be introduced in YAML.

| State | Meaning |
|-------|---------|
| `enabled` | Identity/account is fully active. |
| `suspended` | Temporarily restricted. Access is blocked but account is preserved. Reversible without data loss. |
| `disabled` | Account is deactivated. Stronger than suspended — may trigger deprovisioning. |
| `pending` | Account does not exist yet or is waiting for a prerequisite (e.g. hire day, email verification). |
| `grace` | **Dual semantic:** (1) Employee termination — extended access window before final cutoff. (2) GDPR `deletion_requested` — data export window before purge. In both cases the subject retains limited access temporarily. |

**grace semantic overload note:** A rule setting `grace` for employee termination is semantically different from a rule setting `grace` for `deletion_requested`. The mapping table (see section 6) translates `grace` into application-specific concrete states (e.g., `canceling` in Stripe, `export_only` in the customer portal).

---

## 5. Precedence Conventions

Higher precedence wins. When two rules at the **same** precedence set **different** `abstract_state` values, the result is `suspended` with signal `precedence_conflict`. Avoid this.

### Conventional bands

| Band | Range | Used for |
|------|-------|---------|
| Risk critical (ITDR) | 290–300 | Compromised credentials, session hijack, account takeover |
| Risk critical (UEBA) | 280 | UEBA score > 0.9 |
| Risk high (ITDR) | 240–260 | Impossible travel, MFA bombing, brute force, credential stuffing |
| Risk high (static) | 150–220 | Admin without MFA, prod admin access |
| Risk medium | 130–200 | Dormant reactivation, device anomaly, customer no-MFA |
| Risk low | 100–130 | Score-based low range |
| Lifecycle termination | 100 | Employee terminated, NHI owner terminated, customer banned |
| Lifecycle NHI high | 90–99 | NHI expired, locked, orphaned |
| Lifecycle customer high | 75–95 | Deletion requested, tenant suspended, trial expired, consent withdrawn |
| Lifecycle leave/grace | 50–60 | On-leave rules, reattestation, pre-hire |
| Lifecycle grace active | 110 | Grace initiative override (must beat terminate at 100) |
| Lifecycle base | 10–40 | Default active/enabled states |

**Why risk overrides lifecycle:** Risk rules use precedences 100–300, which overlap and exceed lifecycle bands. A risk rule at 300 will always beat a lifecycle `enabled` at 10. When `abstract_state` is set by both a lifecycle and a risk rule, the risk rule wins if its precedence is higher.

---

## 6. Actions Catalog

Actions are accumulated from **all** matched rules, not just the winning rule.

### Static actions (plain strings)

| Action | Description |
|--------|-------------|
| `revoke_all_sessions` | Terminate all active sessions for the subject. |
| `revoke_new_sessions` | Allow existing sessions to expire naturally; block new ones. |
| `revoke_session` | Revoke a specific session (context-dependent). |
| `revoke_access` | Remove subject's access to the application. |
| `revoke_all_tokens` | Revoke all OAuth/API tokens for an NHI. |
| `revoke_token` | Revoke a specific token. |
| `purge_api_keys` | Delete all API keys (customer ban scenario). |
| `disable_idp_account` | Disable the IdP (identity provider) account directly. |
| `create_idp_account` | Provision a new IdP account on hire day. |
| `send_welcome_email` | Send onboarding email to new hire. |
| `send_verification_email` | Send email verification link to customer. |
| `revoke_nhi_access` | Revoke all access for an NHI identity. |
| `rotate_credential` | Trigger credential rotation for an NHI. |
| `force_password_reset` | Force the subject to reset their password on next login. |
| `force_reauth` | Require immediate re-authentication. |
| `require_step_up_mfa` | Require an additional MFA challenge. |
| `require_mfa_enrollment` | Block access until MFA is enrolled. |
| `require_captcha` | Require CAPTCHA challenge (CIAM). |
| `kill_session` | Immediately terminate the current session. |
| `block_mfa_prompts` | Block further MFA push prompts (MFA bombing defense). |
| `temporary_lockout` | Lock account for a short time period. |
| `notify_security_team` | Send alert to security operations. |
| `notify_customer_email` | Send security notification to the customer's email. |
| `notify_manager` | Notify the subject's manager (AD mapping action). |
| `cancel_pending_attestation` | Cancel any open attestation task for this account. |
| `create_attestation_task` | Create a new attestation task. |
| `schedule_data_deletion` | Schedule GDPR data deletion job. |
| `schedule_disable` | Schedule future disable (AD mapping, grace period). |
| `rate_limit_auth` | Apply rate limiting on authentication attempts. |
| `block_request` | Block the current request immediately. |
| `restrict_to_readonly` | Restrict account to read-only mode. |
| `show_upgrade_prompt` | Show SaaS upgrade prompt to customer. |
| `show_verification_page` | Redirect customer to email verification page. |
| `enable_data_export` | Allow data export access (customer portal mapping). |
| `allow_data_export` | Allow data export (Stripe mapping). |
| `schedule_cancel` | Schedule subscription cancellation (Stripe). |
| `cancel_subscription` | Cancel the billing subscription immediately. |
| `ensure_subscription` | Ensure billing subscription is active. |
| `ensure_account` | Ensure the application account exists; create if missing. |
| `ensure_membership` | Ensure org membership exists (GitHub). |
| `ensure_access` | Ensure access record exists (customer portal). |
| `remove_membership` | Remove org membership (GitHub). |
| `set_readonly` | Set account to read-only mode. |
| `send_payment_reminder` | Send payment overdue notification. |
| `enable` | Enable the account (AD mapping: set `userAccountControl=512`). |
| `disable` | Disable the account (AD mapping: set `userAccountControl=514`). |

### Parameterized actions (dicts)

Parameterized actions are dicts with one key whose value is a dotted facts path. The evaluator resolves the path and converts the result to a string (ISO 8601 for datetimes).

```yaml
actions:
  - schedule_evaluation_at: subject.start_date    # schedules re-evaluation at hire date
  - schedule_create_idp_account: subject.start_date
```

**Important:** Parameterized path resolution happens for lifecycle and risk rules only. Mapping actions (from `mapping.yaml`) are static strings — their values are **not** resolved as facts paths.

---

## 7. Signals Catalog

Signals are accumulated from all matched rules. They are informational — they do not affect `abstract_state` or `risk_level`.

| Signal | Description |
|--------|-------------|
| `no_matching_rule` | No rule matched; fallback to `suspended`. Also emitted when matched rules have no `abstract_state`. |
| `precedence_conflict` | Two rules at the same highest precedence set different `abstract_state` values. |
| `orphaned_nhi` | NHI owner is terminated — account should be reviewed. |
| `orphaned_nhi_review` | NHI has no owner assigned — requires assignment. |
| `nhi_admin_locked` | NHI was manually locked by an administrator. |
| `rotation_needed` | NHI credential is expiring within 30 days. |
| `reattest_on_return` | Employee on leave with a requested initiative — must re-attest access on return. |
| `gdpr_deletion_pending` | Customer requested account deletion — GDPR data export window is open. |
| `tenant_suspension` | Customer's tenant has been suspended. |
| `trial_expiring_soon` | Customer's trial ends within 7 days. |
| `trial_expired` | Customer's trial initiative has expired. |
| `consent_violation` | Customer has withdrawn required consents. |
| `high_risk_access` | Subject has admin access to a production environment. |
| `admin_without_mfa` | Admin-privileged account lacks MFA. |
| `recommend_mfa_enrollment` | Non-admin customer on enterprise plan without MFA (advisory). |
| `ueba_critical` | UEBA risk score above 0.9. |
| `ueba_high_review` | UEBA risk score in 0.7–0.9 range. |
| `dormant_reactivation_review` | Account dormant for more than 90 days. |
| `nhi_credential_incident` | NHI credential was exposed — incident triggered. |
| `session_hijack_incident` | Session hijack indicator detected. |
| `token_replay_incident` | Token replay attack detected. |
| `impossible_travel_review` | Impossible travel indicator — requires review. |
| `mfa_bombing_incident` | MFA push bombing attack detected. |
| `credential_stuffing_detected` | Credential stuffing attack detected against a customer. |
| `device_anomaly_review` | Unusual device detected for a customer login. |

---

## 8. Anti-Patterns

### Overlapping precedences without mutual exclusion

Two rules at the same precedence with overlapping conditions and different `abstract_state` values produce `suspended` + `precedence_conflict`. Always ensure that rules at the same precedence are mutually exclusive.

Bad:
```yaml
# Both can match an active employee with status: active
- id: rule_a
  when: { subject.kind: employee, subject.status: active }
  then: { abstract_state: enabled }
  precedence: 50

- id: rule_b
  when: { subject.kind: employee }
  then: { abstract_state: suspended }
  precedence: 50
```

Fix: add `subject.status` guard to `rule_b` so they cannot both match.

### Rules without `abstract_state` that expect to control state

A rule without `abstract_state` in `then` only adds `actions` and/or `signals`. It never sets state.

```yaml
then:
  signals:
    - rotation_needed    # Only adds a signal. Does NOT set abstract_state.
```

This is valid and intentional for additive rules. If you want state control, add `abstract_state`.

### Missing `subject.kind` guard

A rule intended only for employees can accidentally fire for NHI or customer subjects if `subject.kind` is not guarded.

Bad:
```yaml
when:
  subject.status: terminated    # matches employees AND any other kind with this status
```

Fix:
```yaml
when:
  subject.kind: employee
  subject.status: terminated
```

### Using `target.*` conditions in an IDP rule

Rules with `target: null` only match when `facts.target is None`. Conditions like `target.has_initiative: grace` will never be evaluated (the rule is rejected at the target-guard step before reaching conditions). Do not add target conditions to IDP rules.

### Precedence outside conventional bands without justification

Placing a lifecycle rule above 110 or a risk rule below 100 without a documented reason breaks the expected override hierarchy. Always verify precedence against the bands in section 5.

### Duplicate rule IDs across files

Rule IDs must be unique across `lifecycle.yaml`, `risk.yaml`, and any custom packs. Duplicates cause unpredictable behavior if the loader merges all rules into a single list.

### Parameterized action value is not a facts path

The value in a parameterized action dict is resolved as a dotted facts path. If the value does not contain a dot, it is treated as a literal string — this is silently wrong if you intended a path lookup.

Bad:
```yaml
actions:
  - schedule_evaluation_at: start_date   # missing "subject." prefix — resolves to None
```

Fix:
```yaml
actions:
  - schedule_evaluation_at: subject.start_date
```

---

## 9. Annotated Examples

### Example 1 — Employee lifecycle: terminate wins

```yaml
- id: emp_term_wins
  kind: lifecycle
  when:
    subject.kind: employee       # Guard: employees only.
    subject.status: terminated
  then:
    abstract_state: disabled     # Deactivate the application account.
    actions:
      - revoke_all_sessions      # Immediately kill all sessions.
  precedence: 100                # Beats all base lifecycle rules (10–60).
```

### Example 2 — Employee lifecycle: pre-hire with parameterized action

```yaml
- id: emp_pre_hire_not_yet
  kind: lifecycle
  when:
    subject.kind: employee
    subject.status: hired
    subject.start_date: "> now"  # Start date is still in the future.
  then:
    abstract_state: pending      # Account is not active yet.
    actions:
      - schedule_evaluation_at: subject.start_date  # Re-evaluate on hire day.
                                                    # subject.start_date is resolved to
                                                    # an ISO 8601 datetime string.
  precedence: 50
```

### Example 3 — NHI lifecycle: orphaned owner

```yaml
- id: nhi_owner_terminated
  kind: lifecycle
  when:
    subject.kind: nhi
    subject.owner.status: terminated  # Owner of this NHI was terminated.
                                      # subject.owner resolves OwnerFacts.
  then:
    abstract_state: disabled
    actions:
      - revoke_nhi_access
    signals:
      - orphaned_nhi               # Signal triggers a review workflow.
  precedence: 100
```

### Example 4 — Customer lifecycle: GDPR deletion grace period

```yaml
- id: cust_deletion_requested
  kind: lifecycle
  when:
    subject.kind: customer
    subject.status: deletion_requested
  then:
    abstract_state: grace          # Grace semantic: data export window before purge.
    actions:
      - revoke_new_sessions        # No new logins, but existing sessions finish.
      - schedule_data_deletion     # Queue the GDPR deletion job.
    signals:
      - gdpr_deletion_pending
  precedence: 95
```

### Example 5 — ITDR risk: compromised credential

```yaml
- id: risk_credential_compromised
  kind: risk
  when:
    threat.has_indicator: credential_compromised  # Matches if this string is in
                                                  # threat.active_indicators.
  then:
    abstract_state: disabled       # Immediately disable — overrides lifecycle enabled.
    risk_level: critical
    actions:
      - force_password_reset
      - revoke_all_sessions
  precedence: 300                  # Highest tier — nothing overrides this.
```

### Example 6 — UEBA risk: score range

```yaml
- id: risk_score_high
  kind: risk
  when:
    threat.risk_score: "0.7..0.9"  # Numeric range: 0.7 <= score < 0.9.
                                    # Does NOT set abstract_state — advisory only.
  then:
    risk_level: high
    signals:
      - ueba_high_review           # Triggers a human review workflow.
  precedence: 180
```

### Example 7 — IDP subject-level rule (target: null)

```yaml
- id: idp_emp_pre_hire
  kind: lifecycle
  when:
    subject.kind: employee
    subject.status: hired
    subject.start_date: "> now"
    target: null                   # This rule applies to the IdP identity, not an app account.
                                   # Matches only when facts.target is None.
  then:
    abstract_state: pending
    actions:
      - schedule_create_idp_account: subject.start_date  # Parameterized: resolves to ISO datetime.
  precedence: 40
```

### Example 8 — Grace initiative override

```yaml
- id: grace_active
  kind: lifecycle
  when:
    target.has_initiative: grace            # Account has an active grace initiative.
    target.initiative.grace.valid_until: "> now"  # And that initiative hasn't expired.
  then:
    abstract_state: grace
  precedence: 110                # Higher than emp_term_wins (100) — grace overrides termination.
```

Note: this rule has no `subject.kind` guard. It applies to any subject kind that has a grace initiative. This is intentional — the grace initiative is application-scoped.

### Example 9 — Static risk: admin without MFA

```yaml
- id: risk_admin_no_mfa
  kind: risk
  when:
    target.privilege_level: admin     # Account has admin privileges.
    subject.mfa_enabled: false        # Subject does not have MFA enabled.
  then:
    risk_level: critical
    actions:
      - require_mfa_enrollment        # Block until MFA is set up.
    signals:
      - admin_without_mfa
  precedence: 220                     # High static risk band.
```
