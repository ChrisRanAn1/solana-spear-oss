# Shared Rules

## Notes
1. Pay close attention to the impact of all kinds of edge cases and extreme values.
2. There must be an impact target.
3. Attacker mindset.
4. Every finding must have a fix.
5. For each finding, is there an Anchor constraint or other check that would block it?
6. Any risk involving centralization, private-key leakage, or a malicious admin must not be treated as an assumption. Only what a non-privileged party can achieve counts.
7. For anything involving amounts and arithmetic, concrete numbers are always required. If it cannot be proven with numbers, it does not count as a finding.
8. Self-harm-only issues do not count as findings.
9. Keep the output concise.
10. First fully understand the codebase and establish how the protocol is supposed to operate under normal conditions.
11. Anything that requires the user to actively trigger self-harm themselves does not count as a vulnerability.


Before reporting any vulnerability involving timing/frequency/boundaries, you must first search the codebase for all:
- Constant definitions (MIN_*, MAX_*, *_INTERVAL, *_DURATION)
- Modifiers / pre-checks (require, assert, ensure)
- Other related functions (*_check, validate_*, finalize_*)
Then reference these code locations in the finding and argue why they cannot prevent the attack.

## Prohibited
A legitimate admin or other role acting maliciously does not count as a vulnerability; reporting such vulnerabilities is prohibited. Only when someone who should not have access to admin can become admin does it count as a vulnerability.


## What is a valid finding:

A finding must contain the following elements (findings that cannot be attacked may omit the attack PoC):

-- title: [defective action/mechanism] + [resulting consequence]

-- agent/function name: write whichever agent you are, and whichever function name triggers it

-- description: describe the vulnerability

-- root cause: what is the "minimal sufficient cause" of this vulnerability? (the essence of the bug; remove it and the bug ceases to exist)

-- attack step poc: if the vulnerability is exploitable, write out the attacker's concrete attack steps. Each step must include [function called + concrete parameter values + the state change after that step].

-- impact: what specific impact is caused, and who is the impact target

-- fix: how to fix it, in a short imperative sentence that is very specific. It must name the function and the exact line to add/change, e.g. "before the transfer in `withdraw()`, add `require!(ctx.accounts.recipient.key() == user.key())`".

## format

| Field | Content |
| ----- | ------- |
| title| |
| Agent/fn |  | (the fn here must strictly use the fn name from the code, and must be clean. For example provide_liquidity; never write something like handle, e.g. provide_liquidity::handle — this is strictly forbidden, it must be clean.) The Fn field may contain multiple functions.
| Description | |
| Root Cause | |
| Attack Step | |
| Impact | |
| Fix | |

If there is no attack step, the Attack Step field here may be filled with none. Keep the output short; do not dump a large block of text.
