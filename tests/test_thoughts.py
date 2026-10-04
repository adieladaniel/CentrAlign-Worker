"""clean_thought: reasoning summaries are condensed to the bold title plus the first substantive sentence."""
from __future__ import annotations

import pytest

from worker.agent.thoughts import THOUGHT_LIMIT, clean_thought

REAL = (
    "**Invoice Review for Company X Ltd**\n\nOkay, here's my take on those thoughts, tailored for an expert audience: "
    "Alright, let me see... I have the invoice list. The newest Company X invoice is CX-2026-0412."
)


def test_title_plus_first_substantive_sentences_without_filler():
    assert clean_thought(REAL) == (
        "Invoice Review for Company X Ltd: I have the invoice list; the newest Company X invoice is CX-2026-0412."
    )


@pytest.mark.parametrize(
    "preamble",
    [
        "Okay, here's my interpretation of those directives, tailored for an expert audience, and presented in the first person:",
        "Alright, here's my summary, formatted as requested:",
        "Right, so here's my thought process:",
        "OKAY. HERE'S MY TAKE ON THE ABOVE: ",
        "Assuming I'm an expert in invoicing, here is how I would proceed:",
        "Wow, ok. Let me see... ",
    ],
)
def test_boilerplate_preambles_are_stripped_case_insensitively(preamble):
    out = clean_thought(f"**Find the invoice**\n\n{preamble} I will open the invoice list and filter by company.")
    assert out == "Find the invoice: I will open the invoice list and filter by company."


@pytest.mark.parametrize(
    "preamble",
    [
        "Here's your summary:",
        "Here's the summary:",
        "Here is a summary of my thinking:",
        "Okay, here's a quick summary.",
        "Let's get down to business.",
        "Alright, let's get down to brass tacks:",
        "Time to get into the details.",
        "It's time to get into it...",
        "Okay, here's the summary: let's get down to business. Time to get into the invoice list!",
    ],
)
def test_summary_and_business_boilerplate_is_stripped(preamble):
    out = clean_thought(f"**Find the invoice**\n\n{preamble} I will open the invoice list and filter by company.")
    assert out == "Find the invoice: I will open the invoice list and filter by company."


@pytest.mark.parametrize("whole", ["Here's your summary:", "Here's the summary.", "Here is a summary...",
                                   "Okay. Here's the summary: Let's get down to business. Time to get into it."])
def test_boilerplate_that_is_the_entire_body_returns_just_the_title(whole):
    assert clean_thought(f"**Checking the invoice date**\n\n{whole}") == "Checking the invoice date"


def test_no_title_returns_only_the_sentence():
    out = clean_thought("Okay, here's my interpretation of those directives, tailored for an expert audience, and "
                        "presented in the first person: I need to open the invoice list and find the newest invoice.")
    assert out == "I need to open the invoice list and find the newest invoice."


def test_several_titles_are_joined_with_arrows():
    text = ("**Opening the list**\n\nOkay, the page loaded. The table shows twelve invoices for Company X.\n\n"
            "**Picking the newest**\n\nSo, I sort by date.")
    assert clean_thought(text) == "Opening the list → Picking the newest: the page loaded; the table shows twelve invoices for Company X."


def test_title_only_summary():
    assert clean_thought("**Checking the invoice date**") == "Checking the invoice date"
    assert clean_thought("**Checking the invoice date**\n\nOkay, ") == "Checking the invoice date"


def test_company_suffix_does_not_split_a_sentence():
    out = clean_thought("**Review**\n\nI compared invoices from Acme Ltd. against the ledger totals for this month.")
    assert out == "Review: I compared invoices from Acme Ltd. against the ledger totals for this month."


def test_result_is_cut_at_a_word_boundary_within_the_limit():
    out = clean_thought("**Long one**\n\n" + "reasoning about invoices " * 40)
    assert len(out) <= THOUGHT_LIMIT and out.endswith("…") and out.startswith("Long one: reasoning about invoices")
    assert " " not in out[-2:]
    short = clean_thought("x" * 500)  # no spaces at all: still bounded
    assert len(short) <= THOUGHT_LIMIT and short.endswith("…")


def test_real_text_that_is_not_a_summary_stays_intact():
    assert clean_thought("  I will list the files in the workspace to find the export.  ") == (
        "I will list the files in the workspace to find the export."
    )


def test_plain_summary_keeps_its_first_sentence():
    assert clean_thought("Checking the note.\nvery long reasoning " * 3).startswith("Checking the note.")


def test_empty_and_degenerate_inputs():
    assert clean_thought("") == "" and clean_thought("   \n ") == ""
    assert clean_thought("Okay.") == "Okay."  # nothing useful left: fall back to the raw text rather than nothing


def test_inline_bold_is_unwrapped():
    assert clean_thought("The **newest** invoice is the one dated 12 April.") == "The newest invoice is the one dated 12 April."
