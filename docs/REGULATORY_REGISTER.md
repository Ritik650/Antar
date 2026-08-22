# REGULATORY_REGISTER.md

<!-- GENERATED FILE - DO NOT EDIT.
     Source: antar/policy/regulations.py
     Regenerate: python -m scripts.make_register
     CI fails if this file and the code disagree. -->

Every rule Antar obeys, as encoded. N5 requires that regulatory constraints live as data with a citation field and never as a hard-coded value inside business logic; this document is that data, rendered.

## Verification status

- **18** rules: 11 blocking, 7 advisory
- Verified against a primary regulator document: **10**
- Verified against a full-text gazette reproduction: **5**
- Resting on secondary sources (law-firm notes, trade press): **3** — all advisory
- Unverified: **0**

**A rule may only be `BLOCKING` if it was checked against a primary document or a full-text reproduction.** A regulation resting on a summary cannot stop money; it can only be reported. `test_blocking_rules_are_verified` enforces this, so the rule cannot be quietly relaxed.

## Corrections to PLAN.md

Verification was done at encoding time rather than before submission, on the grounds that a threshold which turns out to differ changes the constraint rows and everything built on them. It found **6** place(s) where the plan, compiled from secondary sources, was wrong or overstated:

- **RBI-EM-06** — Customer may modify or withdraw a mandate at any time
- **TRAI-01** — Commercial communication permitted only between 10:00 and 21:00
- **TRAI-02** — Every commercial communication carries an explicit class
- **TRAI-04** — Inferred consent lasts only for the contract; explicit consent expires
- **TRAI-06** — Preference registration (DND) blocks promotional communication
- **TRAI-07** — Auto-dialer and robocall use must be declared to the access provider

Each is detailed in its entry below. The most consequential is `TRAI-01`: the permitted contact window opens at **10:00**, not 09:00, because the 08:00-10:00 band is default-OFF for every customer. Antar has an hour a day less contact capacity than the plan assumed — which raises the shadow price on a contact slot rather than lowering it.

## Rules

### [BLOCKING] `RBI-EM-01` — Pre-transaction notification at least 24 hours before the debit

| | |
|---|---|
| Severity | `BLOCKING` |
| Source | RBI Digital Payments - E-mandate Framework, 2026 (RBI/DPSS/2026-27/396) |
| Clause | Section 6(a) |
| Effective | 2026-04-21 |
| Verification | **primary**, checked 2026-08-22 |
| Citation | <https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx?id=13374> |
| Applies to | every action, including silent retries |

> An issuer shall send a pre-transaction notification to the customer, at least 24 hours prior to the actual charge / debit.

Every debit attempt must be announced 24 hours in advance. For Antar this is a lead-time constraint on the scheduler, not a reminder: a retry decided now cannot execute until tomorrow, so the decision horizon is at least a day and the opportunity to act on fresh information is limited.

### [BLOCKING] `RBI-EM-02` — Per-transaction opt-out facility, validated by AFA

| | |
|---|---|
| Severity | `BLOCKING` |
| Source | RBI Digital Payments - E-mandate Framework, 2026 |
| Clause | Section 6(c) |
| Effective | 2026-04-21 |
| Verification | **primary**, checked 2026-08-22 |
| Citation | <https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx?id=13374> |
| Applies to | every action, including silent retries |

> The issuer shall provider a customer with a facility to opt-out of any particular transaction or the e-mandate. Any such opt-out shall be validated by the issuer using AFA.

The mandated notification carries a cancel button. This is the single most consequential fact in Antar's design: every retry we schedule generates a notification, and every notification is an opportunity for the customer to leave. Outreach is therefore not free even when it is free - it carries a churn hazard that the allocator has to price. Once an opt-out has been exercised it is absolute, with no grace window.

**Note.** The 'provider' typo is in the source text and is reproduced verbatim rather than silently corrected.

### [BLOCKING] `RBI-EM-03` — AFA-free ceiling of Rs 15,000 per recurring transaction

| | |
|---|---|
| Severity | `BLOCKING` |
| Source | RBI Digital Payments - E-mandate Framework, 2026 |
| Clause | Section 8(a) |
| Effective | 2026-04-21 |
| Verification | **primary**, checked 2026-08-22 |
| Citation | <https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx?id=13374> |
| Applies to | every action, including silent retries |

> All recurring transactions may be authorised without AFA up to Rs 15,000/- per transaction.

Above Rs 15,000 the customer must authenticate every time. Recovery above the ceiling has to route through an AFA-bearing flow, which completes materially less often - so the amount is not just a payoff, it is a difficulty. The allocator must model that, or it will over-value large cycles.

### [BLOCKING] `RBI-EM-04` — Rs 1,00,000 ceiling for insurance, mutual funds and credit card bills

| | |
|---|---|
| Severity | `BLOCKING` |
| Source | RBI Digital Payments - E-mandate Framework, 2026 |
| Clause | Section 8(b) |
| Effective | 2026-04-21 |
| Verification | **primary**, checked 2026-08-22 |
| Citation | <https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx?id=13374> |
| Applies to | every action, including silent retries |

> Payment of insurance premiums, subscription to mutual funds, and credit card bill payments may be made without AFA up to Rs 1,00,000/- per transaction.

The ceiling is category-dependent, not global. Merchant category is therefore a genuine feature rather than a label: the same Rs 40,000 debit is AFA-free for an insurer and AFA-bearing for a streaming service.

### [advisory] `RBI-EM-05` — Post-transaction notification after every debit

| | |
|---|---|
| Severity | `ADVISORY` |
| Source | RBI Digital Payments - E-mandate Framework, 2026 |
| Clause | Section 7 |
| Effective | 2026-04-21 |
| Verification | **primary**, checked 2026-08-22 |
| Citation | <https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx?id=13374> |
| Applies to | every action, including silent retries |

A fixed cost per successful debit, carried in the cost model rather than as a constraint. ADVISORY because the obligation falls on the issuer, not on Antar: we cannot violate it, we can only pay for it.

### [BLOCKING] `RBI-EM-06` — Customer may modify or withdraw a mandate at any time

| | |
|---|---|
| Severity | `BLOCKING` |
| Source | RBI Digital Payments - E-mandate Framework, 2026 |
| Clause | Section 4(b), 4(e) |
| Effective | 2026-04-21 |
| Verification | **primary**, checked 2026-08-22 |
| Citation | <https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx?id=13374> |
| Applies to | every action, including silent retries |

> modify the validity period or withdraw the e-mandate at any point of time

A withdrawn mandate is not a retry candidate, and neither is a paused one. Debiting either is not a bug in our retry logic - it is a debit the customer told us not to make.

**Note.** PLAN.md section 3.1 rendered this as 'modify, pause, or withdraw'. The framework text says 'modify the validity period or withdraw'; **pause is not a term the framework uses**. PAUSED survives in our mandate FSM because it is a real Razorpay subscription state, but it is a payment-processor lifecycle state rather than an RBI-conferred right, and the register should not imply otherwise.

### [advisory] `RBI-EM-07` — FASTag and NCMC auto-replenishment exempt from pre-debit notification

| | |
|---|---|
| Severity | `ADVISORY` |
| Source | RBI Digital Payments - E-mandate Framework, 2026 |
| Clause | Section 6(d) |
| Effective | 2026-04-21 |
| Verification | **primary**, checked 2026-08-22 |
| Citation | <https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx?id=13374> |
| Applies to | every action, including silent retries |

> Pre-transaction notification is not required for e-mandates registered to auto-replenish balances of FASTag, and National Common Mobility Card (NCMC).

Out of scope: Antar handles no FASTag or NCMC mandates. Encoded anyway so that the exemption is visibly considered and excluded rather than overlooked. If those mandates were ever in scope they would be the only population without the opt-out hazard, and the economics would differ.

### [BLOCKING] `TRAI-01` — Commercial communication permitted only between 10:00 and 21:00

| | |
|---|---|
| Severity | `BLOCKING` |
| Source | TRAI TCCCPR 2018, Schedule II (as amended 12 Feb 2025) |
| Clause | Schedule II, para 3(1) Note-1 |
| Effective | 2018-07-19 |
| Verification | reproduction, checked 2026-08-22 |
| Citation | <https://indiankanoon.org/doc/60694660/> |
| Applies to | contact actions only |

> Time Bands (i), (ii), (iii) and (ix) shall be default OFF for all customers irrespective of the status of registration of customer i.e. for all customers including those who have not registered any type of preference(s), anytime unless customer has registered its preference(s) and switched ON

Bands (i) 00:00-06:00, (ii) 06:00-08:00, (iii) 08:00-10:00 and (ix) 21:00-24:00 are OFF by default for every customer, registered or not. The default-permitted window is therefore 10:00-21:00 - eleven hours, not twelve.

**Note.** **This corrects PLAN.md section 3.2**, which stated the window as 09:00-21:00 from secondary sources. The 08:00-10:00 band is default OFF, so the window opens at 10:00. Antar has one hour per day less contact capacity than the plan assumed, which raises the shadow price on a contact slot rather than lowering it. The plan also said the restriction applies 'every day including weekends and public holidays'; the gazette treats public and national holidays as a *customer-registrable preference* (Schedule II, para 4(viii)), not a blanket prohibition - see TRAI-08. Ultimate authority is the gazette PDF, not this reproduction.

### [advisory] `TRAI-02` — Every commercial communication carries an explicit class

| | |
|---|---|
| Severity | `ADVISORY` |
| Source | TRAI TCCCPR 2018 as amended |
| Clause | Regulation 2(1) definitions; Second Amendment 2025 |
| Effective | 2025-02-12 |
| Verification | _secondary_, checked 2026-08-22 |
| Citation | <https://www.trai.gov.in/telecom-commercial-communications-customer-preference-second-amendment-regulations-2025> |
| Applies to | contact actions only |

Communications are classified service / transactional / promotional (and, since the Second Amendment, government), each requiring the correct registered header and number series. Antar attaches the class to every outbound action and refuses to construct a promotional one.

**Note.** ADVISORY: the classification scheme is confirmed by law-firm commentary on the Second Amendment, but we have not read the definitional clause in the gazette. Per PLAN.md section 3 an unverified rule may not block.

### [BLOCKING] `TRAI-03` — Promotional content contaminates the whole message

| | |
|---|---|
| Severity | `BLOCKING` |
| Source | TRAI TCCCPR (Second Amendment) Regulations, 2025 |
| Clause | Second Amendment, 12 February 2025 |
| Effective | 2025-02-12 |
| Verification | reproduction, checked 2026-08-22 |
| Citation | <https://www.trai.gov.in/telecom-commercial-communications-customer-preference-second-amendment-regulations-2025> |
| Applies to | contact actions only |

> if promotional content is mixed with any other type of communication (say, service or transactional) then such communication will be treated as promotional

One upsell sentence appended to a dunning message reclassifies the entire message as promotional, which changes the header, the number series, and whether DND applies. This is the rule the LLM most plausibly breaks - it is trained to be helpful, and 'we also have 20% off annual plans' is a helpful sentence. `antar/act/contamination.py` is the defence, and it runs on the model's output before the gate ever sees it.

### [BLOCKING] `TRAI-04` — Inferred consent lasts only for the contract; explicit consent expires

| | |
|---|---|
| Severity | `BLOCKING` |
| Source | TRAI TCCCPR (Second Amendment) Regulations, 2025 |
| Clause | Second Amendment, 12 February 2025 |
| Effective | 2025-02-12 |
| Verification | reproduction, checked 2026-08-22 |
| Citation | <https://www.trai.gov.in/telecom-commercial-communications-customer-preference-second-amendment-regulations-2025> |
| Applies to | contact actions only |

> inferred consent remains valid only for the duration of a contractual relationship. In other words, once the contract ends, inferred consent is automatically revoked.

A live mandate is the contractual relationship, so inferred consent covers recovery messaging while the mandate is live and stops the moment it is revoked. This is why TERMINATE means terminate: after revocation there is no consent basis left to message on.

**Note.** The seven-day explicit-consent validity window in PLAN.md section 3.2 is reported by secondary sources and is **not** confirmed here. The configured value is used only for customers on an EXPLICIT basis, of which the simulator generates none, so nothing measured depends on it.

### [advisory] `TRAI-05` — Number series must match the communication class

| | |
|---|---|
| Severity | `ADVISORY` |
| Source | TRAI TCCCPR 2018 as amended; NCPR number-series allocation |
| Clause | Second Amendment 2025; 1600-series mandate for BFSI from 1 Jan 2026 |
| Effective | 2026-01-01 |
| Verification | _secondary_, checked 2026-08-22 |
| Citation | <https://www.trai.gov.in/telecom-commercial-communications-customer-preference-second-amendment-regulations-2025> |
| Applies to | contact actions only |

Promotional voice uses the 140 series; transactional and service calls use the 1600/160 series. Channel identity is part of the action record and is validated against the message class.

**Note.** ADVISORY: confirmed only by trade press and vendor documentation. Antar does not place real calls, so no number series is ever allocated and the rule cannot be exercised end to end.

### [advisory] `TRAI-06` — Preference registration (DND) blocks promotional communication

| | |
|---|---|
| Severity | `ADVISORY` |
| Source | TRAI TCCCPR 2018 as amended |
| Clause | Regulation 20-21; Schedule II preference categories |
| Effective | 2018-07-19 |
| Verification | _secondary_, checked 2026-08-22 |
| Citation | <https://www.trai.gov.in/telecom-commercial-communications-customer-preference-second-amendment-regulations-2025> |
| Applies to | contact actions only |

DND governs *promotional* communication. Transactional and service messages are not caught by it, so a recovery message to a DND-registered customer is lawful. Antar declines voice calls to them anyway.

**Note.** **PLAN.md section 3.2 overstated this** as an unconditional blocking predicate on all contact. As written that would be wrong law - it would block lawful transactional messaging - so the predicate encodes the actual rule plus an explicitly-labelled merchant-policy choice to avoid voice calls to preference-registered customers. Policy above the legal floor is fine; policy *presented as* the legal floor is not.

### [advisory] `TRAI-07` — Auto-dialer and robocall use must be declared to the access provider

| | |
|---|---|
| Severity | `ADVISORY` |
| Source | TRAI TCCCPR (Second Amendment) Regulations, 2025 |
| Clause | Second Amendment, 12 February 2025 |
| Effective | 2025-02-12 |
| Verification | reproduction, checked 2026-08-22 |
| Citation | <https://www.trai.gov.in/sites/default/files/2025-02/Regulation_12022025.pdf> |
| Applies to | contact actions only |

> provisions concerning autodialers and robocalls have been introduced, requiring senders to notify the Originating Access Provider (OAP) of their intent and purpose to use such means

The disclosure runs to the originating access provider, as a registration obligation, rather than to the customer inside the call.

**Note.** **Corrects PLAN.md section 3.2**, which read this as a customer-facing disclosure ('every synthetic voice action carries a disclosure flag'). It is an operator-facing registration duty. Antar keeps the flag on the action record for auditability, but describing it as a consumer disclosure would have been wrong.

### [advisory] `TRAI-08` — Customer-registered preferences narrow the window further

| | |
|---|---|
| Severity | `ADVISORY` |
| Source | TRAI TCCCPR 2018, Schedule II |
| Clause | Schedule II, paras 3-4 |
| Effective | 2018-07-19 |
| Verification | reproduction, checked 2026-08-22 |
| Citation | <https://indiankanoon.org/doc/60694660/> |
| Applies to | contact actions only |

Beyond the default-OFF bands, a customer may register preferences on specific two-hour bands, on days of the week, and on public and national holidays. Those narrow the permitted window below the 10:00-21:00 default.

**Note.** ADVISORY because Antar has no preference data to honour: the simulator generates no per-customer time-band registrations, so this rule can never bind and reporting it as BLOCKING would be claiming a compliance check we do not perform. **A production deployment must consume the preference feed before this can be called compliant.** docs/LIMITATIONS.md L11.

### [BLOCKING] `POL-BUDGET` — At most k contacts per customer per rolling 30 days

| | |
|---|---|
| Severity | `BLOCKING` |
| Source | Merchant policy (config: budgets.contacts_per_30d) |
| Clause | C-BUDGET |
| Effective | 2026-08-22 |
| Verification | **primary**, checked 2026-08-22 |
| Citation | <internal://config/default.yaml#budgets.contacts_per_30d> |
| Applies to | contact actions only |

Not a legal limit but the scarce resource the whole allocator exists to ration. Its dual is the headline shadow price: what one more compliant contact slot is worth to this merchant.

### [BLOCKING] `POL-COOLDOWN` — Minimum gap between contacts to the same customer

| | |
|---|---|
| Severity | `BLOCKING` |
| Source | Merchant policy (config: budgets.cooldown_hours) |
| Clause | C-COOLDOWN |
| Effective | 2026-08-22 |
| Verification | **primary**, checked 2026-08-22 |
| Citation | <internal://config/default.yaml#budgets.cooldown_hours> |
| Applies to | contact actions only |

Three messages inside the budget is compliant and still harassment if they arrive on consecutive days. Notification fatigue also compounds the opt-out hazard, so the cooldown is defensive as well as decent.

### [BLOCKING] `POL-PROMISE` — Stop contacting a customer who has promised to pay

| | |
|---|---|
| Severity | `BLOCKING` |
| Source | Merchant policy |
| Clause | C-STOP |
| Effective | 2026-08-22 |
| Verification | **primary**, checked 2026-08-22 |
| Citation | <internal://docs/REGULATORY_REGISTER.md#POL-PROMISE> |
| Applies to | contact actions only |

A recorded promise to pay ends the conversation until the promised date. Chasing past it is the fastest way to convert a paying customer into a complaint.

---

## What this register does not cover

- **Per-customer preference registrations** (`TRAI-08`). Antar consumes no preference feed, so registered time-band, day-of-week and holiday preferences are not honoured. A production deployment must consume that feed before any of this can be called compliant.
- **Number-series allocation** (`TRAI-05`). Antar places no real calls, so no series is ever allocated and the rule is never exercised end to end.
- **DLT template registration.** Templates here are DLT-*shaped*; none is registered with an access provider.
- **Jurisdictions other than India.** Every rule assumes an Indian customer and IST. `CustomerContext.timezone_offset_minutes` is the seam where that would change.

See `docs/LIMITATIONS.md` for the full list.

