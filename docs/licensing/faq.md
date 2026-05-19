# Licensing FAQ

Aurelion Kernel ships under the **Business Source License 1.1 (BSL 1.1)**. This page answers the questions we get most often. If your case is not covered, contact us before drawing conclusions — BSL is straightforward, but edge cases benefit from a direct answer.

See the canonical license text in [`LICENSE`](../../LICENSE).

---

## Is Aurelion Kernel open source?

**No — it is source-available.**

You can read the source, modify it, and self-host it. However, BSL 1.1 is not recognized as an Open Source Initiative (OSI)–approved license. It restricts one specific scenario: reselling Aurelion Kernel as a competing identity platform to third parties.

After the Change Date (see below), the code automatically converts to the **GNU Affero General Public License v3.0 or later (AGPL-3.0-or-later)**, which is OSI-approved.

## What is the Change Date?

**March 13, 2030.**

On that date, every version of Aurelion Kernel released under BSL 1.1 automatically becomes available under **AGPL-3.0-or-later**. BSL's restrictions end; AGPL's obligations begin.

Each version has its own four-year clock. A version released on June 1, 2026 converts to AGPL on June 1, 2030 — even if we later ship further BSL-licensed versions. Whichever comes first applies: explicit Change Date or the four-year anniversary.

## Can I use Aurelion Kernel inside my company?

**Yes, without limitation.**

Run it in production, connect it to your HR systems, provision accounts, manage access reviews — all of that is explicitly permitted. Internal use by your employees and contractors is fine, including at a Fortune 500 scale.

The Additional Use Grant in our LICENSE file exists precisely to make this unambiguous: production use is allowed.

## Can I host Aurelion Kernel as a service for my clients?

**It depends.**

You may NOT offer Aurelion Kernel to third parties as:

- an **Identity Governance and Administration (IGA)** service
- an **Identity Provider (IDP)** service
- a **Non-Human Identity (NHI)** management service
- an **Identity Threat Detection and Response (ITDR)** service

...where that offering **substantially overlaps** with Aurelion's own commercial offerings and competes with them.

You MAY:

- self-host for your own organization
- embed Aurelion Kernel inside a product that is NOT primarily an IGA/IDP/NHI/ITDR service (e.g., using it as an internal identity layer for a domain-specific SaaS that does not compete with Aurelion)
- build tools on top of Aurelion Kernel for your own internal use
- offer managed hosting for internal corporate customers that clearly would not purchase from Aurelion

When in doubt, **ask us first**. We are happy to confirm in writing that your use case is within the Additional Use Grant.

## Can I fork Aurelion Kernel and modify it?

**Yes.** You can fork, modify, and redistribute modifications — as long as any downstream recipient receives the same BSL 1.1 terms and the same Change Date.

You cannot relicense the code (for example, you cannot rename the fork and publish it under MIT). All forks remain under BSL 1.1 until the Change Date.

## Can I contribute upstream?

**Yes, but you must sign the Contributor License Agreement (CLA) first.**

The CLA is a one-time digital signature via our GitHub bot on your first pull request. It confirms you have the right to contribute and grants Aurelion the right to incorporate your contribution. It does not transfer copyright ownership — you keep it.

The CLA is required so that Aurelion can manage the license lifecycle (e.g., issue patches under BSL even after the Change Date for older versions, or offer commercial licenses).

## Do I need to pay for a commercial license?

**Only if your use falls outside BSL 1.1 and the Additional Use Grant.**

Commercial licenses are relevant when you want to:

- offer Aurelion Kernel as a competing IGA/IDP/NHI/ITDR service to third parties
- embed Aurelion Kernel in a proprietary product under terms that differ from BSL
- get warranties, indemnification, or support obligations beyond what BSL provides

Contact licensing@aurelion.solutions for commercial licensing.

## What happens to my data/configuration after the Change Date?

**Nothing changes for you operationally.**

The license change applies to the **source code**, not to your running system. Your data, schemas, configurations, and integrations all continue to work. You get a license upgrade to AGPL for free — and with it, more freedoms (e.g., you can then relicense your own modifications under compatible terms).

## Does BSL 1.1 apply to the Aurelion CLI, Engineering Studio, or Docs?

**No. Those components are Apache 2.0.**

BSL 1.1 applies only to **Aurelion Kernel** — the core platform (REST API, database, message queue, domain logic). The following components are fully open source under Apache 2.0:

- `aurelion-cli` — CLI client
- `aurelion-engineering-studio` — VS Code extension
- `aurelion-docs` — documentation site
- `aurelion-connector-templates` — connector templates

This is intentional: the ecosystem around Kernel is permissive, so integrators can embed our tooling freely. Only the core platform is protected.

## Can I use Aurelion Kernel in an air-gapped environment?

**Yes.** Self-hosted, air-gapped, behind a VPN, on-prem, in a private cloud — all supported use cases. BSL 1.1 does not phone home, require activation, or restrict deployment topology.

## Can I use Aurelion Kernel in a government or regulated environment?

**Yes, with the same BSL 1.1 terms.** BSL does not discriminate by industry, jurisdiction, or customer type. If your agency or institution can legally accept a source-available license with a four-year FOSS conversion clause, you are free to use Aurelion Kernel under BSL.

For procurement processes that require AGPL or Apache 2.0 specifically, contact us — commercial licensing is available.

## Can I redistribute Aurelion Kernel with my own product?

**Yes, as long as:**

- you include the LICENSE file unchanged
- you display the BSL 1.1 notice conspicuously
- downstream recipients receive the same BSL 1.1 terms
- your product itself is not a competing IGA/IDP/NHI/ITDR offering

Bundling Aurelion Kernel with a non-competing product is fine. Rebranding it as a competing product is not.

## Is Aurelion Kernel FIPS/SOC2/ISO27001 certified?

License terms and certifications are different things. BSL 1.1 governs your right to use the code; certifications govern operational compliance. See our compliance documentation for the current status of relevant certifications.

---

## Still not sure?

Email **licensing@aurelion.solutions** with:

- a short description of your intended use
- whether you plan to offer the capability to third parties
- whether your offering competes with Aurelion's IGA/IDP/NHI/ITDR products

We usually respond within two business days with a clear yes/no and, if needed, a written confirmation you can share with your legal team.
