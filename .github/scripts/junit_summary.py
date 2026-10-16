"""Summarise a pytest JUnit XML file as Markdown (for $GITHUB_STEP_SUMMARY) and optionally enforce DB coverage.

    python .github/scripts/junit_summary.py junit.xml --title "Fast suite" >> "$GITHUB_STEP_SUMMARY"
    python .github/scripts/junit_summary.py junit.xml --require-no-db-skips      # exit 1 if a DB test was skipped

The PostgreSQL fixtures skip their tests with a message that contains "PostgreSQL is not available" (tests/conftest.py);
that text is how a skipped database test is recognised.
"""
from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET

DB_SKIP_MARKER = "PostgreSQL is not available"


def summarise(path: str) -> dict:
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    stats = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0, "time": 0.0, "db_skipped": 0, "failed_ids": [], "skip_reasons": {}}
    for suite in suites:
        stats["tests"] += int(suite.get("tests", 0))
        stats["failures"] += int(suite.get("failures", 0))
        stats["errors"] += int(suite.get("errors", 0))
        stats["skipped"] += int(suite.get("skipped", 0))
        stats["time"] += float(suite.get("time", 0))
    for case in root.iter("testcase"):
        name = f"{case.get('classname', '')}::{case.get('name', '')}"
        if case.find("failure") is not None or case.find("error") is not None:
            stats["failed_ids"].append(name)
        skipped = case.find("skipped")
        if skipped is not None:
            reason = (skipped.get("message") or "").strip() or "(no reason)"
            stats["skip_reasons"][reason] = stats["skip_reasons"].get(reason, 0) + 1
            if DB_SKIP_MARKER in reason:
                stats["db_skipped"] += 1
    stats["passed"] = stats["tests"] - stats["failures"] - stats["errors"] - stats["skipped"]
    return stats


def markdown(stats: dict, title: str) -> str:
    lines = [f"### {title}", "", "| Passed | Failed | Errors | Skipped | of which DB tests skipped | Total | Time |", "|---:|---:|---:|---:|---:|---:|---:|",
             f"| {stats['passed']} | {stats['failures']} | {stats['errors']} | {stats['skipped']} | {stats['db_skipped']} | {stats['tests']} | {stats['time']:.0f}s |", ""]
    if stats["skip_reasons"]:
        lines.append("Skip reasons:")
        lines += [f"- {count} x {reason}" for reason, count in sorted(stats["skip_reasons"].items(), key=lambda kv: -kv[1])]
        lines.append("")
    if stats["failed_ids"]:
        lines.append("Failed:")
        lines += [f"- `{name}`" for name in stats["failed_ids"][:50]]
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("junit_xml")
    parser.add_argument("--title", default="Test results")
    parser.add_argument("--require-no-db-skips", action="store_true", help="fail when any PostgreSQL test was skipped")
    parser.add_argument("--min-tests", type=int, default=1, help="fail when fewer tests than this ran at all")
    args = parser.parse_args(argv)
    stats = summarise(args.junit_xml)
    print(markdown(stats, args.title))
    problems = []
    if stats["tests"] < args.min_tests:
        problems.append(f"only {stats['tests']} tests were collected (expected at least {args.min_tests})")
    if args.require_no_db_skips and stats["db_skipped"]:
        problems.append(f"{stats['db_skipped']} database tests were SKIPPED: PostgreSQL was not reachable, so the DB tests did not run")
    if args.require_no_db_skips and stats["tests"] and stats["db_skipped"] == 0 and stats["skipped"]:
        print(f"Note: {stats['skipped']} tests skipped for other reasons (see above).", file=sys.stderr)
    for problem in problems:
        print(f"**FAILED CHECK: {problem}**\n")
        print(f"::error::{problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
