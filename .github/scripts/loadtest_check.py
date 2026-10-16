"""Judge a scripts/loadtest.py result file for CI and print a Markdown summary.

    python .github/scripts/loadtest_check.py results.json --profile nightly >> "$GITHUB_STEP_SUMMARY"

Exit 1 when the ground-truth pass rate is below 100%, a false success or a duplicate bill happened, a response leaked
another organization's data, or a cross-org probe was not refused.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("results_json")
    parser.add_argument("--profile", required=True)
    args = parser.parse_args(argv)
    path = Path(args.results_json)
    if not path.is_file():
        print(f"::error::{path} was not written: the load test did not finish", file=sys.stderr)
        return 1
    data = json.loads(path.read_text(encoding="utf-8"))
    result = data.get("profiles", {}).get(args.profile)
    if result is None:
        print(f"::error::profile '{args.profile}' not found in {path}", file=sys.stderr)
        return 1
    runs, iso = result["runs"], result["isolation"]
    problems = []
    if runs["ground_truth_pass"] != runs["accepted_by_api"] or runs["accepted_by_api"] == 0:
        problems.append(f"ground-truth pass rate {runs['ground_truth_pass']}/{runs['accepted_by_api']} is below 100%")
    if runs["false_success"]:
        problems.append(f"{runs['false_success']} false successes")
    if runs["invoices_with_duplicate_bills"]:
        problems.append(f"{runs['invoices_with_duplicate_bills']} invoices with duplicate bills")
    if iso["violations"]:
        problems.append(f"{iso['violations']} isolation leaks in {iso['responses_checked']} responses")
    if iso["stray_bills"]:
        problems.append(f"{iso['stray_bills']} stray bills (written to the wrong organization's company)")
    if iso["probe_failures"]:
        problems.append(f"{iso['probe_failures']} of {iso['cross_org_probes']} cross-org probes were not refused")
    print(f"### Load test `{args.profile}`\n")
    print(f"- Ground truth: {runs['ground_truth_pass']} of {runs['accepted_by_api']} passed ({runs['ground_truth_pass_rate']:.0%})")
    print(f"- Isolation: {iso['responses_checked']} responses scanned, {iso['violations']} leaks; "
          f"{iso['cross_org_probes']} cross-org probes, {iso['probe_failures']} not refused")
    print(f"- False successes: {runs['false_success']}, duplicate bills: {runs['invoices_with_duplicate_bills']}\n")
    if problems:
        print("**Failed:** " + "; ".join(problems) + "\n")
        for problem in problems:
            print(f"::error::{problem}", file=sys.stderr)
        return 1
    print("**All checks passed.**\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
