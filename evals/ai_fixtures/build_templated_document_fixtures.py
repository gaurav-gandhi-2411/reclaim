from __future__ import annotations

import random
from dataclasses import dataclass

from ai_fixtures.build_document_realistic_tiers import (
    RealisticDocumentVariant,
    collab_tool_paste,
    mild_whitespace_cleanup,
    moderate_paragraph_trim_and_reorder,
)

# The templated-document tier (ADR-0017 follow-up): GG flagged that measuring MinHash/LSH's
# operating point against edited literary prose can't surface the failure mode real document
# clutter actually has — resumes, invoices, reports, and decks are heavily TEMPLATED, so two
# genuinely DIFFERENT documents (different people's resumes, different clients' invoices) can
# share large blocks of identical boilerplate (section headers, standard phrasing, structural
# scaffolding) even though their actual content is completely unrelated. Word-shingle MinHash
# is exactly the kind of algorithm this could fool: high shingle overlap from shared boilerplate
# could push two unrelated documents' Jaccard similarity above a threshold tuned on prose, where
# no such shared-boilerplate structure exists at all.
#
# Three templates (resume, invoice, report-memo), each with substantial REAL boilerplate
# (structural headers, standard connective phrasing an actual template would carry) and a pool
# of varying fields large enough that two random instances share no specific personal/business
# detail — synthetic data throughout (no real names/companies/PII), deterministic given SEED.

_SEED = 42

_FIRST_NAMES = (
    "James",
    "Maria",
    "Wei",
    "Fatima",
    "Liam",
    "Aisha",
    "Noah",
    "Priya",
    "Omar",
    "Elena",
    "Kenji",
    "Grace",
    "Diego",
    "Ingrid",
    "Samuel",
    "Chidi",
    "Ana",
    "Lucas",
    "Mei",
    "Tariq",
)
_LAST_NAMES = (
    "Anderson",
    "Chen",
    "Okafor",
    "Rossi",
    "Kim",
    "Novak",
    "Silva",
    "Patel",
    "Larsen",
    "Diaz",
    "Sato",
    "Murphy",
    "Haddad",
    "Nowak",
    "Costa",
    "Ivanov",
    "Reyes",
    "Berg",
    "Osei",
    "Voss",
)
_COMPANIES = (
    "Brightline Logistics",
    "Cedar Ridge Consulting",
    "Northwind Analytics",
    "Solstice Media",
    "Pinecrest Financial",
    "Vantage Point Systems",
    "Harbor & Stone LLP",
    "Meridian Robotics",
    "Fieldstone Partners",
    "Cobalt Loop Studios",
    "Amber Trail Foods",
    "Redwood Data Co.",
)
_TITLES = (
    "Operations Manager",
    "Senior Analyst",
    "Marketing Coordinator",
    "Software Engineer",
    "Account Executive",
    "Project Lead",
    "Financial Analyst",
    "Product Designer",
)
_DEGREES = (
    "B.A. in Economics",
    "B.S. in Computer Science",
    "M.B.A.",
    "B.S. in Mechanical Engineering",
    "B.A. in Communications",
    "M.S. in Data Science",
)
_SCHOOLS = (
    "Riverdale State University",
    "Ashford College",
    "Northgate University",
    "Lakeshore Institute of Technology",
    "Barrow University",
    "Millbrook College",
)
_SKILLS_POOLS = (
    "project management, budgeting, stakeholder communication",
    "Python, SQL, data visualization",
    "salesforce, lead generation, client retention",
    "adobe creative suite, copywriting, brand strategy",
    "financial modeling, forecasting, variance analysis",
)
_ITEMS = (
    ("Consulting services", 1200.00),
    ("Widget assembly kit", 89.50),
    ("Annual software license", 499.00),
    ("Freight and handling", 65.00),
    ("Custom design work", 850.00),
    ("Replacement parts (set of 4)", 210.00),
    ("On-site training day", 1500.00),
    ("Maintenance contract (quarterly)", 375.00),
)


@dataclass(frozen=True, slots=True)
class TemplatedDocument:
    doc_id: str
    template: str  # "resume" | "invoice" | "report"
    text: str


def _resume_text(rng: random.Random, index: int) -> str:
    first, last = rng.choice(_FIRST_NAMES), rng.choice(_LAST_NAMES)
    company_a, company_b = rng.sample(_COMPANIES, 2)
    title_a, title_b = rng.sample(_TITLES, 2)
    return (
        f"{first} {last}\n"
        f"{first.lower()}.{last.lower()}{index}@example.com | (555) 010-{1000 + index:04d}\n\n"
        "OBJECTIVE\n"
        "Results-driven professional seeking to leverage proven experience and a strong "
        "track record of delivering measurable outcomes in a challenging new role.\n\n"
        "EXPERIENCE\n"
        f"{company_a} — {title_a}\n"
        f"20{18 + (index % 5)} - Present\n"
        "- Led cross-functional initiatives to improve operational efficiency and reduce costs.\n"
        "- Collaborated with stakeholders across departments to deliver projects on schedule.\n"
        "- Presented quarterly findings and recommendations to senior leadership.\n\n"
        f"{company_b} — {title_b}\n"
        f"20{13 + (index % 5)} - 20{18 + (index % 5)}\n"
        "- Managed day-to-day operations and supported team members in achieving targets.\n"
        "- Developed process improvements that increased overall team productivity.\n\n"
        "EDUCATION\n"
        f"{rng.choice(_DEGREES)}, {rng.choice(_SCHOOLS)}\n\n"
        "SKILLS\n"
        f"{rng.choice(_SKILLS_POOLS)}"
    )


def _invoice_text(rng: random.Random, index: int) -> str:
    client = f"{rng.choice(_FIRST_NAMES)} {rng.choice(_LAST_NAMES)}"
    company = rng.choice(_COMPANIES)
    line_items = rng.sample(_ITEMS, 3)
    subtotal = sum(price for _, price in line_items)
    tax = round(subtotal * 0.08, 2)
    total = round(subtotal + tax, 2)
    lines_text = "\n".join(f"  {name:32s} ${price:>10.2f}" for name, price in line_items)
    return (
        f"INVOICE #{10000 + index}\n"
        f"Date: 2024-{1 + (index % 12):02d}-{1 + (index % 28):02d}\n\n"
        f"Bill To: {client}\n"
        f"From: {company}\n\n"
        "Description                              Amount\n"
        f"{lines_text}\n\n"
        f"Subtotal: ${subtotal:.2f}\n"
        f"Tax (8%): ${tax:.2f}\n"
        f"Total Due: ${total:.2f}\n\n"
        "Payment is due within 30 days of the invoice date. Please remit payment to the "
        "address listed above. Late payments are subject to a 1.5% monthly finance charge. "
        "Thank you for your business."
    )


def _report_text(rng: random.Random, index: int) -> str:
    author = f"{rng.choice(_FIRST_NAMES)} {rng.choice(_LAST_NAMES)}"
    company = rng.choice(_COMPANIES)
    topic = rng.choice(
        (
            "regional sales performance",
            "customer satisfaction survey results",
            "supply chain risk assessment",
            "quarterly budget variance",
            "staffing utilization",
        )
    )
    return (
        f"MEMORANDUM\n"
        f"To: Executive Leadership Team\n"
        f"From: {author}, {company}\n"
        f"Date: 2024-{1 + (index % 12):02d}-{1 + (index % 28):02d}\n"
        f"Re: {topic.title()}\n\n"
        "EXECUTIVE SUMMARY\n"
        "This memo summarizes findings from the most recent review cycle and outlines "
        "recommended next steps for leadership consideration.\n\n"
        "FINDINGS\n"
        f"Analysis of {topic} for the current period indicates several notable trends "
        "worth further discussion at the next planning session.\n"
        "- Overall performance remained within expected variance for the period.\n"
        "- Several outlier cases were identified and are documented in the appendix.\n"
        "- Stakeholder feedback was broadly consistent with prior review cycles.\n\n"
        "RECOMMENDATIONS\n"
        "We recommend continued monitoring and a follow-up review at the start of the next "
        "quarter, with resources allocated accordingly."
    )


def build_templated_documents(n_per_template: int = 18) -> list[TemplatedDocument]:
    rng = random.Random(_SEED)  # noqa: S311 -- deterministic synthetic fixture data
    docs: list[TemplatedDocument] = []
    for index in range(n_per_template):
        docs.append(TemplatedDocument(f"resume_{index:03d}", "resume", _resume_text(rng, index)))
    for index in range(n_per_template):
        docs.append(TemplatedDocument(f"invoice_{index:03d}", "invoice", _invoice_text(rng, index)))
    for index in range(n_per_template):
        docs.append(TemplatedDocument(f"report_{index:03d}", "report", _report_text(rng, index)))
    return docs


def build_templated_variants(docs: list[TemplatedDocument]) -> list[RealisticDocumentVariant]:
    """Reuses the SAME 3 transform profiles as build_document_realistic_tiers.py — one true
    near-dup variant per templated document, for consistency with the prose tier's positive-
    pair construction."""
    rng = random.Random(_SEED)  # noqa: S311 -- deterministic synthetic fixture data
    variants: list[RealisticDocumentVariant] = []
    for doc in docs:
        variants.append(
            RealisticDocumentVariant(
                chunk_id=doc.doc_id,
                tier="mild",
                profile="mild_whitespace_cleanup",
                text=mild_whitespace_cleanup(doc.text),
            )
        )
        variants.append(
            RealisticDocumentVariant(
                chunk_id=doc.doc_id,
                tier="moderate",
                profile="moderate_paragraph_trim_and_reorder",
                text=moderate_paragraph_trim_and_reorder(doc.text, rng),
            )
        )
        variants.append(
            RealisticDocumentVariant(
                chunk_id=doc.doc_id,
                tier="collab_paste",
                profile="collab_tool_paste",
                text=collab_tool_paste(doc.text),
            )
        )
    return variants
