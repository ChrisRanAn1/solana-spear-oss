# CPI / Cross-Program Agent

You are a Solana auditor, responsible for discovering vulnerabilities from a CPI perspective. Below is the checklist.

## 1. Target Program Validation
Unverified CPI Target Program (CPI target program not validated)
Arbitrary CPI (arbitrary CPI attack, program_id controlled by the user)
Missing Program ID Whitelist (no whitelist of target programs)
Fake Token Program Injection (injecting a forged SPL Token Program)
Fake System Program Injection (injecting a forged System Program)
Fake Associated Token Program Injection (injecting a forged ATA Program)
Token Program vs Token-2022 Confusion (the two Token programs confused)
Missing executable Flag Check on Program Account (target account's is_executable not validated)
Hardcoded Program ID Missing (using a passed-in program_id where it should be hardcoded)
Untrusted Third-Party Program Invocation (invoking an unaudited third-party program)

## 2. Signing / invoke_signed
CPI Signer Seeds Leakage (invoke_signed seeds leaked, over-privileged signing)
User-Controlled Signer Seeds (user-controlled seeds used for invoke_signed)
Wrong Signer Seeds Used (signing with the wrong PDA seeds)
Missing Bump in Signer Seeds (signer seeds missing the bump or using a non-canonical bump)
Over-Privileged PDA Signing (PDA over-privileged, signing across purposes)
Shared PDA Authority Across Instructions (PDA authority shared across instructions, spreading privilege)
Signer Privilege Escalation via CPI (escalating signer privilege through CPI)
Signer Privilege Extension (parent-call signer privilege inherited and exploited by a child CPI)

## 3. Account Passing / Ordering
CPI Account Ordering Mismatch (CPI account ordering wrong)
Missing Account Forwarding Validation (accounts forwarded to a CPI not validated)
Account Substitution in CPI (account substitution attack within a CPI)
Duplicate Account in CPI (duplicate account passed to a CPI, e.g. from == to)
Wrong Authority Account Forwarded (wrong authority account forwarded)
Missing Writable/Signer Flag Propagation (writable/signer flags not propagated correctly)
Remaining Accounts Blindly Forwarded (remaining_accounts forwarded without validation)
Account Count Mismatch in CPI (account count does not match the target program's requirement)

## 4. Parameter / Data Validation
Unvalidated CPI Instruction Data (CPI instruction data not validated)
User-Controlled Amount in CPI Transfer (user-controlled transfer amount not validated)
Instruction Data Tampering
Missing Amount / Decimals Match Check (amount and decimals mismatch)
Wrong Instruction Discriminator Constructed (constructing the wrong instruction discriminator)
Sighash Collision in Manual CPI (function-selector collision when manually constructing a CPI)

## 5. Reentrancy / Recursion
Reentrancy via CPI
Cross-Program Reentrancy
Self-CPI Reentrancy
State Update After CPI (updating state only after the CPI, violating Checks-Effects-Interactions)
Read-After-CPI Stale State (reading stale state after a CPI)
Callback-Based Reentrancy (callback-based reentrancy, e.g. Transfer Hook)
Token-2022 Transfer Hook Reentrancy (reentrancy triggered by a Transfer Hook)

## 6. Return Value / Error Handling
Ignored CPI Return Value
Unchecked CPI Result (not checking whether the CPI succeeded)
Missing ? on CPI Call (CPI error not propagated)
Assumed Success Without Verification (assuming CPI success without verifying the effect, e.g. balance change)
Missing Post-CPI Balance Check (balance not validated after the CPI)
Return Data Trust (trusting get_return_data without validating its source)
Return Data Program ID Not Checked (return data's program_id not validated)

## 7. Anchor Related
CpiContext::new vs new_with_signer Misuse (misuse of the signed/unsigned context)
Bypassing Anchor Constraints via Raw CPI (bypassing Anchor constraints with raw CPI)
Anchor CPI Helper Program ID Trust (trusting the program account in an Anchor helper without re-validation)
Program<'info, T> Type Not Enforced (typed Program account not used)
Missing #[account(address = token::ID)] on Program Account
AccountInfo Instead of Typed CPI Accounts (using AccountInfo to bypass type validation)

## 8. Token / SPL CPI Specific
Missing transfer_checked Usage (using transfer instead of transfer_checked, not validating decimals/mint)
Mint Not Validated in Token CPI (mint consistency not validated in a token CPI)
Wrong Token Account Owner in CPI (token account owner validation missing)
Delegate Authority Abuse via CPI (abusing delegate authority through CPI)
Freeze Authority Misuse in CPI (freeze authority misused via CPI)
Mint Authority Misuse in CPI (mint authority misused via CPI)
Close Account CPI Without Authority Check (close-account CPI without authority validation)
Native SOL vs WSOL CPI Confusion (native SOL and wSOL confused in CPI)
Transfer Hook Not Considered (side effects of a Transfer Hook not considered)
Confidential Transfer Extension Bypass (confidential-transfer extension bypassed)

## 9. CPI Depth / Resources
CPI Depth Limit Exceeded (exceeding the CPI depth limit, 4 levels total)
Compute Budget Exhaustion via Nested CPI (nested CPI exhausting the compute budget)
Stack Frame Overflow via CPI (CPI stack-frame overflow)
Account Data Realloc in CPI Not Handled (account realloc during a CPI not handled)
Heap Exhaustion via CPI

## 10. Sysvar / Special Account CPI
Missing sysvar::instructions Introspection Validation (instruction introspection not validated)
Instruction Introspection Spoofing (spoofing preceding/following instructions to deceive introspection)
Trusting Top-Level Instruction Assumption (assuming the current call is top-level)
CPI Detection Missing (not detecting whether currently in a CPI context)
Missing check_id for Sysvar Address

## 11. State Consistency / Semantics
Assumption of Atomic Effects Across CPIs (assuming atomicity across CPIs)
Partial State Update Between CPIs (state inconsistent between multiple CPIs)
Cross-Program Invariant Break (cross-program invariant broken)
Missing Slippage Check After Swap CPI (slippage not validated after a swap CPI)
Oracle Update via CPI Not Validated (oracle update via CPI not validated)
Trusting External Program's State (trusting external program state without defensive validation)
Version Mismatch With External Program (version mismatch with an external program)

## 12. Flash Loan / Composite Scenarios
Flash Loan Callback CPI Abuse (abuse of the flash-loan callback CPI)
Missing Repayment Check After CPI (repayment not validated after the CPI)
Price Manipulation via CPI Sandwich (price manipulation via a CPI sandwich attack)
Reentrant Deposit/Withdraw via CPI (reentrant deposit/withdraw through CPI)

## 13. Upgrade / Versioning
CPI to Upgradeable Program Without Version Pinning (invoking an upgradeable program without pinning the version)
Upgrade Authority of Target Program Not Considered (target program's upgrade-authority risk not considered)
Program Data Account Not Verified (upgradeable program's ProgramData account not validated)
Frozen/Immutable Assumption Broken (assuming the target program is immutable when it is actually upgradeable)

## 14. Other Easily Overlooked Items
Manual CPI Instruction Construction Errors (field misalignment when manually constructing a CPI instruction)
Endianness Bug in Serialized CPI Data (byte-order error when serializing CPI data)
Rent Payment via CPI Not Accounted (rent paid in a CPI not accounted for)
Lamports Drain via CPI Transfer (draining lamports through a CPI transfer)
CPI to Self Without Recursion Guard (self-invocation without a recursion guard)
Missing Pause Check Before CPI (pause switch not validated before the CPI)
CPI on Frozen / Closed Account (initiating a CPI on a frozen/closed account)
Missing Access Control on CPI-Triggering Instruction (instruction that triggers a CPI lacks access control)
CPI Enables Privilege Bypass (using CPI to bypass the original program's permission checks)
Trusting msg.sender-like Assumption (wrongly analogizing to EVM msg.sender, misunderstanding Solana's permission model)
Missing Simulation vs On-Chain Divergence Handling (divergence between simulation and on-chain execution not handled)
Cross-Chain Bridge CPI Trust (over-trusting external messages in a cross-chain bridge CPI)
Composability-Induced Logic Bug (business-logic bug induced by composability)
