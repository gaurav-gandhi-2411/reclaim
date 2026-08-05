# 0031. Code-signing approach for the Windows installer

**Status: recommendation only. Nothing purchased. Awaiting GG's explicit go-ahead to spend.**

## Context

`reclaim-setup.exe` and the `reclaim.exe` it installs are unsigned. Reclaim deletes and
quarantines a user's files, so "unknown publisher" is not a cosmetic wart — it is the single
loudest trust signal a first-time user gets, and it appears at exactly the moment they are
deciding whether to let this program touch their disk.

PLAN.md's 2026-07-23 entries already carry a partial pass on this ("Stage 2 Part C: signing
options reported"): Azure Trusted Signing Basic at ~$9.99/month was found, the recommendation was
to defer, and GG's spend decision was never actioned either way. That note is now over a year
old on a fast-moving surface. This ADR re-derives it from the live vendor pages rather than
re-trusting it, and it found three things that were wrong or missing in the old note.

**Provenance.** Every number and quoted policy below was read from the live page on **2026-08-05**
at the URL given. Nothing here is recalled or estimated. Prices are the vendors' own list prices
in their own currency, before tax, and are "from" prices where the vendor states a range — the
real total for a specific configuration is only knowable in the vendor's cart at purchase time.

### What changed since PLAN.md's 2026-07-23 note

1. **"Trusted Signing" is now "Artifact Signing."** The Microsoft Learn docs
   (`learn.microsoft.com/en-us/azure/artifact-signing/overview`, page footer "Last updated on
   2026-05-12") use "Artifact Signing" throughout; the old `/azure/trusted-signing/` URLs redirect
   to it and the pricing page at `azure.microsoft.com/en-us/pricing/details/trusted-signing/`
   renders "Artifact Signing" in its own table headings.
2. **The old note's ~$9.99/month is still right, but incomplete.** Basic is $9.99/month with a
   quota of **5,000 signatures/month**; Premium is $99.99/month with 100,000. Over quota, both
   bill **$0.005/signature**. Basic includes 1 of each certificate profile type. Billing starts
   the moment the account is created and is **not pro-rated** — a partial month is charged in
   full (all four facts from the pricing page's own table and its FAQ accordion).
3. **The blocker the old note never found.** The quickstart
   (`raw.githubusercontent.com/MicrosoftDocs/azure-docs/main/articles/artifact-signing/quickstart.md`,
   Prerequisites note) states verbatim: *"Public Trust certificates are available to organizations
   in the United States, Canada, the European Union, the United Kingdom, Australia, New Zealand,
   Japan, South Korea, Singapore, Switzerland, Norway, and Israel. **Individual developers must be
   located in the United States or Canada.** These geographic restrictions do not apply to Private
   Trust certificates."* Individual identity details are sourced automatically from the Azure
   billing account and are read-only in the request form, so the residency on file is the
   residency that governs. Private Trust is not a substitute: per
   `concept-trust-models.md`, the Private Trust CA hierarchy *"isn't default-trusted in any root
   program and in Windows"* — it is for App Control/WDAC policy, not for public distribution.

   This project's own build logs are timestamped IST (see PLAN.md's Wave 1 P0-B checkpoint and
   ADR-0030's measured-evidence note). **If GG's legal residence is India, Azure Artifact Signing
   Public Trust is not available to him as an individual developer at all** — not a cost question,
   an eligibility question. This is stated as a conditional, not a fact: the residency on GG's
   Azure billing account is something only GG can confirm.

### SmartScreen's current behaviour — the load-bearing correction

The old note assumed signing means "no SmartScreen warning." That is not what the current
evidence says, and it changes the entire cost/benefit.

- **Microsoft's own current wording** (`learn.microsoft.com/en-us/windows/security/
  operating-system-security/virus-and-threat-protection/microsoft-defender-smartscreen/`, footer
  "Last updated on 2026-04-23"): *"It also provides reputation checks for apps, checking
  downloaded programs and the digital signature used to sign a file. If a URL, a file, an app, or
  a certificate has an established reputation, users don't see any warnings. If there's no
  reputation, the item is marked as a higher risk and presents a warning to the user."*
- **What Microsoft does not say.** This page, the Smart App Control code-signing page
  (`learn.microsoft.com/en-us/windows/apps/develop/smart-app-control/code-signing-for-smart-app-control`,
  "Last updated on 2026-02-10"), and the Artifact Signing trust-models/certificate-management
  concept docs were all read in full on 2026-08-05. **None of them contains any claim that an EV
  certificate grants instant or automatic SmartScreen reputation.** Per rule 101a this is stated
  as "checked these specific pages, found no such claim" — not as proof that no Microsoft
  statement anywhere says otherwise.
- **What the CAs say.** SSL.com's own current code-signing pages state it directly, twice:
  *"EV and OV are treated equally since March 2024"* (`ssl.com/certificates/code-signing/` FAQ),
  and *"since March 2024, SmartScreen reputation must be built over time through download volume
  — EV-type certificates no longer receive instant bypass"*
  (`ssl.com/products/software-integrity/code-signing/ev-sole-proprietor/`). This is a CA's
  commercial claim about a competitor-relevant product distinction, i.e. a claim against its own
  interest in upselling EV, which is why it is quoted here — but it is still a vendor statement,
  not a Microsoft one. Certum's marketing across all three of its tiers likewise promises only
  *"Building a Microsoft SmartScreen Filter reputation"*, never instant trust.

**Consequence for this decision: no certificate at any price makes the SmartScreen warning
disappear on the next release.** What signing buys, immediately, is that the prompt names a
verified human instead of saying "Unknown publisher", and that a reputation starts accruing
against that identity. Because reputation attaches to the signing identity, *switching* certificate
identities later restarts much of that accrual — so the choice worth optimising is the one GG
keeps, not the one that is cheapest for one year.

### Options surveyed

All prices as listed on 2026-08-05. "Individual?" means: issuable to a natural person with no
registered business entity.

| Option | Price (list) | Individual? | Validation / turnaround | SmartScreen |
|---|---|---|---|---|
| **Azure Artifact Signing, Basic** (`azure.microsoft.com/en-us/pricing/details/trusted-signing/`) | $9.99/mo + $0.005/sig over 5,000/mo; not pro-rated | **US/Canada residents only** (quickstart Prerequisites) | Azure portal only; government photo ID via a third-party verifier (AU10TIX is the doc's worked example) + Microsoft Authenticator Verified ID; **1–20 business days**, longer if more documents are requested | Reputation built over time |
| **Certum Open Source Code Signing** (`shop.certum.eu/code-signing.html`) | **from €25** (activation code, needs your own cryptoCertum card + reader) · **from €69** (set, incl. cryptoCertum card) · from €49 (cloud/SimplySign — page rendered "Product is out of stock" on 2026-08-05) | **Yes — individuals only** | ID verification (automatic, notarial, Registration Point, or ID-in-hand photos) + a utility bill in the subscriber's name + a public open-source project URL that clearly shows the subscriber's relationship to it; **1–5 days** | Reputation built over time |
| **Certum Standard (OV) Code Signing** (same page) | **from €169** (set, incl. card) · **from €209** (in the Cloud / SimplySign, no hardware) | **Yes** — Certum's own FAQ: *"OV certificates involve a simpler validation process and can also be issued to individuals"* | Same ID methods as above + a utility bill for the individual case; **1–5 days** | Reputation built over time |
| **Certum EV Code Signing** (same page) | from €359 (set) · from €379 (cloud) | **No** — Certum FAQ: EV *"is available only to registered companies"* | 1–7 days | Same as OV since March 2024 (per SSL.com) |
| **SSL.com IV Code Signing** (`ssl.com/products/software-integrity/code-signing/iv/`) | **$129.00/yr** (1 yr; $96.75/yr at 5 yr) **plus key storage**: eSigner cloud Tier 1 **$15.00/mo** (240 signings, 1 credential) or a YubiKey one-time | **Yes** — "no business registration required" | Government-issued ID; **3–5 business day** standard validation (+$599 to expedite to 2 days) | Vendor's own page: *"SmartScreen reputation builds over time as your signed binaries circulate"* |
| **SSL.com EV Sole Proprietor** (`ssl.com/products/software-integrity/code-signing/ev-sole-proprietor/`) | **$359.00/yr** (1 yr; $201.04/yr at 5 yr) + key storage | **Yes** — EV validated as an individual, no entity | EV procedures for individuals; 3–5 business days standard | No instant bypass (vendor's own note, quoted above) |
| **Sectigo** (`sectigo.com/ssl-certificates-tls/code-signing`) | *"starts at $536.25 per year"* at the 5-year term | Their validation text names "the organization **or individual**" | Callback to a verified phone number | Reputation built over time |
| **DigiCert** (`digicert.com/signing/code-signing-certificates`) | $44/mo and $62/mo per certificate tiers, 12-month auto-renewing | Not found — no individual/sole-proprietor product on the page checked | — | — |
| **Stay unsigned** (status quo) | $0 | n/a | n/a | "Unknown publisher" warning, permanently |

Two data-quality notes, recorded rather than smoothed over: SSL.com quotes the YubiKey at **$379**
on the IV and EV-SP product pages and at **$249** on the eSigner page, on the same day — so the
hardware line item is not reliably knowable outside the cart. And Certum's store now warns that
**from 2026-02-27 a single code-signing certificate may be valid for at most 459 days**, so 2- and
3-year purchases carry mandatory free reissues mid-term rather than one long-lived certificate.

Since June 2023 the CA/Browser Forum Code Signing Baseline Requirements mandate hardware-protected
private keys for *all* code-signing certificates, OV included (stated on SSL.com's IV page under
"Compliance & Standards" and in Sectigo's FAQ). There is therefore no "just download a .pfx"
option at any price any more — every path costs either a physical token or a cloud-HSM
subscription on top of the certificate.

## Decision 1: recommend Certum Standard (OV) Code Signing in the Cloud, from €209/year

**Recommended, not purchased.** If and when GG green-lights the spend, buy
**Certum Standard Code Signing in the Cloud (SimplySign), individual subscriber** — listed from
€209.00/year at `shop.certum.eu/code-signing.html`.

Reasoning, weighed in the order rule 74 sets out:

- **Eligibility first, because it disqualifies the cheapest option outright.** Azure Artifact
  Signing at $119.88/year is the cheapest recurring price in the table and it is the one the
  previous pass recommended, but Public Trust for individual developers is restricted to US and
  Canada residents. Certum states no residency restriction on any page checked and sells in EUR,
  USD and PLN from a public store — but see the residual risks below, "no restriction found" is
  not the same as "confirmed available in India".
- **Product quality: the name in the prompt is the whole point.** Reclaim asks a stranger for
  permission to delete their files. "Gaurav Gandhi" in the publisher field is the signal being
  bought. Certum's Open Source tier is €140/year cheaper and Reclaim's MIT licence and public
  repo would qualify it today — but Certum's own requirements page states that the issued
  certificate *"will contain 'Open Source Developer' phrase added to the Common name field.
  Organization field will be set to 'Open Source Developer'"*, and that *"if Certum determines
  that the certificate is being used to sign software distributed commercially, the certificate
  will be revoked."* That trades away the exact identity signal being purchased, and it puts a
  revocation trigger on a product whose own PLAN.md discusses eventual monetisation.
- **Maintainability: reputation continuity outweighs the price delta.** SmartScreen reputation
  accrues to the signing identity. A cheap Open Source certificate that must be abandoned the day
  Reclaim charges anyone throws that accrual away and starts over. €209 vs €69 is a €140/year
  difference on a decision whose main asset — accumulated reputation — is destroyed by switching.
- **EV buys nothing here.** Certum EV (€379) and SSL.com EV Sole Proprietor ($359/yr) cost 1.7×
  and 1.7× the recommendation. Both CAs say EV no longer confers instant SmartScreen reputation,
  and EV's remaining hard advantage — kernel-mode driver signing and Windows Hardware Dev Center
  access — is irrelevant: Reclaim ships no drivers.
- **Cloud (€209) over the card set (€169) despite the €40.** The set ships a physical
  cryptoCertum card from Poland; the cloud variant uses Certum's SimplySign HSM and needs only the
  SimplySign mobile + desktop apps (per the product page's "Technical requirements"). €40/year
  buys away international hardware shipping, customs, and the standing risk of a lost or damaged
  token that has to be re-shipped mid-release. It also means the private key is never on the build
  machine, which matters for a build that has already been observed to be a 180-minute unattended
  job (ADR-0030).
- **Runner-up, named explicitly: SSL.com IV Code Signing + eSigner**, $129/yr + $15/mo = $309/yr.
  It costs roughly 30% more than the recommendation at today's rates and is the better choice on
  exactly one axis — headless CI/CD signing via the eSigner REST API / CKA, which neither Certum
  variant offers (SimplySign requires an interactive mobile-generated access code). That axis is
  not live today: `packaging/build_installer.ps1` is a manual, machine-local, ~3-hour build and
  moving it to a CI runner is a deferred roadmap item in `packaging/RELEASE_RUNBOOK.md`, not a
  current one. **If release signing ever has to run unattended in CI, switch to SSL.com IV** — and
  make that switch before, not after, meaningful reputation has accrued to a Certum identity.

## Decision 2: nothing is purchased in this pass

No certificate, subscription, or Azure account was created. Spending money is a standing
escalation trigger, and this pass had no authority to spend. Concretely, **the following are open
and require GG personally**, since all of them are tied to a legal identity and a payment method:

1. Approve (or decline) the ~€209/year recurring spend.
2. Confirm the residency question — if GG is in fact US- or Canada-resident, Azure Artifact
   Signing at $119.88/year becomes eligible and should be re-compared before buying Certum.
3. Confirm Certum will sell to and validate an India-resident individual (a pre-sales question to
   Certum, cheap to ask and cheaper than discovering it after payment).
4. Complete the identity validation: government photo ID plus a utility bill in his own name.

## Decision 3: until signed, the warning and the checksum path become first-class release steps

Staying unsigned is a legitimate interim state, but it is only honest if the consequence is
written where a release actually reads it. `packaging/RELEASE_RUNBOOK.md` gains two sections in
the same commit as this ADR:

- **What the user actually sees** — the SmartScreen unrecognised-app flow, step by step, so the
  release notes and support answers describe the real UI rather than paraphrasing it.
- **Publishing and verifying the SHA-256 checksum** — the `Get-FileHash` step that produces the
  sidecar, the exact sidecar format, and copy-pasteable verification instructions to put in front
  of end users.

At the time this ADR was written, the checksum sidecar was verified to exist and verified *not* to
be automatic: `packaging/build_installer.ps1` (590 lines) contained no `Get-FileHash`, no
`sha256`, and no checksum step — its last action was to print the installer size. The sidecar was
produced by hand at publish time. All four published releases carry one
(`api.github.com/repos/gaurav-gandhi-2411/reclaim/releases`, fetched 2026-08-05: v1.0.0, v1.1.0,
v1.2.0, v1.3.0 each have exactly `reclaim-setup.exe` and `reclaim-setup.exe.sha256`, the sidecar
84 bytes in every case), and v1.3.0's sidecar downloads as
`7f02ab7b488e51212e7bde0e686c742b448d90073df103da9ce2885f6460d7c3  reclaim-setup.exe` — matching
the hash PLAN.md's 2026-07-25 checkpoint recorded for that release. **Update, same wave:**
`build_installer.ps1` now generates this sidecar automatically as the last action of its Inno Setup
packaging step and prints the hash to stdout — see `packaging/RELEASE_RUNBOOK.md`'s "Publishing:
SHA-256 checksum sidecar" section for the current, byte-verified behaviour. The re-download and
re-hash of the *published* asset (the round-trip integrity check) remains manual by necessity — it
has to happen after the artifact reaches GitHub, which the build script cannot do.

## Consequences

- **The SmartScreen warning does not go away when GG buys a certificate.** Anyone reading this ADR
  later should not expect a signed v1.4.0 to install silently. The prompt changes from "Unknown
  publisher" to a named publisher, and reputation begins accruing from zero against that identity;
  Reclaim's download volume is the rate limiter on how fast it clears, and that is not something a
  purchase can shortcut.
- **The recurring cost is real and open-ended.** ~€209/year for as long as Reclaim is published,
  on a project with no revenue. Declining the spend is a defensible outcome of this ADR, not a
  failure of it — the honest framing is that ~€209/year buys a named publisher string and the
  start of a reputation clock, and GG is the only one who can price that against a portfolio
  project's goals.
- **A decision deferred is not a decision foreclosed.** The packaging pipeline remains
  signing-agnostic exactly as PLAN.md's 2026-07-23 note recorded: `packaging/reclaim.iss` has no
  `SignTool`/`SignedUninstaller` directive, so adding signing later is a `signtool.exe` step on
  `entry_point.dist/reclaim.exe` plus a `SignTool=` line in the `.iss`. Nothing in this ADR
  changes that, and nothing about shipping v1.4.0 unsigned makes signing v1.5.0 harder.
- **The AV false-positive risk is unchanged and is not hypothetical.** This project has already
  hit one antivirus quarantine on a freshly built unsigned binary (PLAN.md, 2026-07-23). Signing
  reduces but does not eliminate heuristic AV flags on Nuitka-compiled executables.
- **This ADR's numbers have a shelf life.** Every price and policy here was read on 2026-08-05.
  The previous pass's note went stale in about a year and was wrong on the one fact that mattered
  most (eligibility). Re-read the linked pages before acting on this, rather than trusting the
  table.

## Alternatives considered

- **Azure Artifact Signing Basic, $9.99/month.** The cheapest recurring option and the previous
  pass's recommendation. Rejected on eligibility, not price: Public Trust for individual
  developers is limited to US/Canada residents. Revisit immediately if GG's residency makes it
  available — at $119.88/year with zero-touch certificate lifecycle management (certificates
  renewed daily, valid 72 hours, per `concept-certificate-management.md`) and no token to lose, it
  would very likely win on every axis except the identity-validation renewal chore (renewable from
  60 days before expiry; letting it lapse stops certificate renewal and halts signing).
- **Certum Open Source Code Signing, from €69.** Cheapest genuinely eligible option and Reclaim's
  MIT licence qualifies it today. Rejected because the certificate subject is forced to "Open
  Source Developer" rather than GG's name, and because Certum revokes it if the software is ever
  distributed commercially — a revocation trigger plus a reputation reset on the same day Reclaim
  might start earning.
- **Any EV certificate (Certum EV €379, SSL.com EV Sole Proprietor $359/yr).** EV is genuinely
  available to individuals now via SSL.com's sole-proprietor tier, which is the one thing worth
  recording here since it used to require a registered entity. Rejected anyway: both CAs state EV
  no longer confers instant SmartScreen reputation, and Reclaim ships no kernel drivers, so the
  premium buys nothing this project can use.
- **Sectigo (from $536.25/yr) and DigiCert ($44–$62/month/cert).** Priced for organisations. No
  individual/sole-proprietor product was found on DigiCert's page. Not competitive here.
- **Stay unsigned indefinitely, checksums only.** The status quo, and still the zero-cost default
  if GG declines. Its real cost is a permanent "Unknown publisher" prompt on a tool that deletes
  files, plus the already-observed AV false-positive exposure. Decision 3 exists so that this
  option is at least documented honestly rather than silently endured.
- **Self-signed certificate.** Not considered a real option — it produces a signature no Windows
  root program trusts, so the user sees a warning naming an untrusted publisher instead of an
  unknown one. Strictly worse than unsigned for a public download.
