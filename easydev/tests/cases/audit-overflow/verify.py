#!/usr/bin/env python3
"""B65 audit overflow acceptance with independent assertions.

The subject is ``audit_model.render_audit``.  This verifier derives the
expected inventory from its own fixture, checks the rendered report, then
mutates it to prove that every required failure is observable.
"""

from __future__ import annotations

import copy
import sys

from audit_model import render_audit


MAX_BODY_FINDINGS = 20
SEVERITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}

failures = 0
checks = 0


def ok(label: str) -> None:
    global checks
    checks += 1
    print(f"  OK: {label}")


def bad(label: str, detail: str) -> None:
    global failures, checks
    failures += 1
    checks += 1
    print(f"  FAIL: {label} -> {detail}", file=sys.stderr)


def fixture_findings() -> list[dict]:
    findings = []
    for index in range(1, 24):
        severity = "P1" if index <= 21 else "P2"
        findings.append(
            {
                "id": f"F{index:02d}",
                "severity": severity,
                "location": f"service/module_{index}.py:{index}",
                "trigger": f"触发条件 {index}",
                "consequence": f"可观察后果 {index}",
                "evidence": f"复现证据 {index}",
                "shard": "core" if index <= 10 else ("api" if index <= 20 else "ops"),
            }
        )
    return findings


FIXTURE_FINDINGS = fixture_findings()
FIXTURE_COVERAGE = [
    {"shard": "core", "status": "reviewed", "scope": "核心写入与删除路径"},
    {
        "shard": "api",
        "status": "partial",
        "scope": "公开 API",
        "uncovered": "异步重试分支未审",
    },
    {
        "shard": "ops",
        "status": "unreviewed",
        "scope": "部署与运维脚本",
        "reason": "预算用尽，交用户安排后续审计",
    },
]


def audit_errors(report: dict, source_findings: list[dict]) -> list[str]:
    """Independent acceptance predicate for one rendered audit artifact."""
    errors = []
    body = report.get("body_findings")
    appendix = report.get("appendix_findings")
    summary = report.get("summary")
    coverage = report.get("coverage")

    if not isinstance(body, list):
        return ["body_findings must be a list"]
    if not isinstance(appendix, list):
        return ["appendix_findings must be a list"]
    if len(body) > MAX_BODY_FINDINGS:
        errors.append(
            f"body has {len(body)} findings; limit is {MAX_BODY_FINDINGS}"
        )

    body_severities = [
        SEVERITY_ORDER.get(finding.get("severity"), 99)
        for finding in body
        if isinstance(finding, dict)
    ]
    if body_severities != sorted(body_severities):
        errors.append("body findings are not sorted by severity")

    source_by_id = {finding["id"]: finding for finding in source_findings}
    appendix_by_id = {
        finding.get("id"): finding
        for finding in appendix
        if isinstance(finding, dict)
    }
    missing_ids = sorted(set(source_by_id) - set(appendix_by_id))
    if missing_ids:
        errors.append(
            f"appendix must preserve all {len(source_findings)} findings; "
            f"missing {missing_ids}"
        )
    for finding_id, source in source_by_id.items():
        if finding_id in appendix_by_id and appendix_by_id[finding_id] != source:
            errors.append(f"appendix evidence for {finding_id} is incomplete")
            break

    body_ids = {
        finding.get("id")
        for finding in body
        if isinstance(finding, dict)
    }
    if not body_ids <= set(source_by_id):
        errors.append("body contains findings outside the complete inventory")

    if not isinstance(summary, dict):
        errors.append("summary is missing")
    else:
        overflow = [
            finding
            for finding in source_findings
            if finding["id"] not in body_ids
        ]
        critical = [
            finding
            for finding in overflow
            if finding["severity"] in {"P0", "P1"}
        ]
        critical.sort(
            key=lambda finding: (
                SEVERITY_ORDER.get(finding["severity"], 99),
                finding["id"],
            )
        )
        disclosure = summary.get("overflow_critical")
        if not isinstance(disclosure, dict):
            errors.append("overflow P0/P1 disclosure is missing")
        else:
            if disclosure.get("count") != len(critical):
                errors.append(
                    "overflow P1 count does not match the complete inventory"
                )
            expected_impacts = [
                {
                    "id": finding["id"],
                    "consequence": finding["consequence"],
                }
                for finding in critical
            ]
            if disclosure.get("impacts") != expected_impacts:
                errors.append(
                    "overflow P1 impacts do not match every finding outside the body"
                )
            if critical and disclosure.get("record") != "appendix":
                errors.append("overflow P1 complete-record entry is missing")

    if not isinstance(coverage, list):
        errors.append("coverage record is missing")
    else:
        statuses = {
            item.get("status")
            for item in coverage
            if isinstance(item, dict)
        }
        missing_statuses = {"reviewed", "partial", "unreviewed"} - statuses
        if missing_statuses:
            errors.append(
                f"coverage must disclose {sorted(missing_statuses)}"
            )
        for item in coverage:
            if not isinstance(item, dict):
                continue
            if item.get("status") == "partial" and not item.get("uncovered"):
                errors.append("partial coverage must list uncovered scope")
            if item.get("status") == "unreviewed" and not item.get("reason"):
                errors.append("unreviewed coverage must list a reason")
        claim = (
            summary.get("coverage_claim")
            if isinstance(summary, dict)
            else None
        )
        if statuses & {"partial", "unreviewed"} and claim == "complete":
            errors.append("partial audit must not claim complete coverage")

    return errors


def expect_valid(
    label: str,
    report: dict,
    source_findings: list[dict] | None = None,
) -> None:
    errors = audit_errors(report, source_findings or FIXTURE_FINDINGS)
    if errors:
        bad(label, "; ".join(errors))
    else:
        ok(label)


def expect_invalid(
    label: str,
    report: dict,
    fragment: str,
    source_findings: list[dict] | None = None,
) -> None:
    errors = audit_errors(report, source_findings or FIXTURE_FINDINGS)
    if not errors:
        bad(label, "mutant was accepted")
    elif not any(fragment in error for error in errors):
        bad(label, f"missing expected error {fragment!r}: {errors}")
    else:
        ok(label)


def main() -> int:
    print("=== B65 audit overflow ===")
    report = render_audit(FIXTURE_FINDINGS, FIXTURE_COVERAGE)
    expect_valid("23 findings: 20 in body, all records in appendix", report)

    body = report["body_findings"]
    appendix = report["appendix_findings"]
    summary = report["summary"]
    if (
        len(body) == 20
        and len(appendix) == 23
        and sum(item["severity"] == "P1" for item in appendix) == 21
        and sum(item["severity"] == "P2" for item in appendix) == 2
    ):
        ok("fixed fixture preserves 21 P1 and 2 P2 findings")
    else:
        bad("fixture counts", f"body={len(body)} appendix={len(appendix)}")

    disclosure = summary.get("overflow_critical", {})
    if (
        disclosure.get("count") == 1
        and len(disclosure.get("impacts", [])) == 1
        and disclosure["impacts"][0]["id"] == "F21"
    ):
        ok("summary discloses the one P1 outside the body")
    else:
        bad("overflow summary", str(summary))

    two_overflow_findings = copy.deepcopy(FIXTURE_FINDINGS)
    two_overflow_findings[21]["severity"] = "P1"
    two_overflow = render_audit(two_overflow_findings, FIXTURE_COVERAGE)
    two_disclosure = two_overflow["summary"]["overflow_critical"]
    expect_valid(
        "22 P1 causes two body-overflow P1 findings",
        two_overflow,
        two_overflow_findings,
    )
    if [
        impact["id"] for impact in two_disclosure.get("impacts", [])
    ] == ["F21", "F22"]:
        ok("summary preserves every body-overflow P1 impact")
    else:
        bad("two overflow P1 impacts", str(two_disclosure))
    expect_valid(
        "unsorted source inventory does not change audit validity",
        two_overflow,
        list(reversed(two_overflow_findings)),
    )

    too_long = copy.deepcopy(report)
    too_long["body_findings"].append(copy.deepcopy(appendix[-1]))
    expect_invalid("body over 20 is rejected", too_long, "limit is 20")

    missing_record = copy.deepcopy(report)
    missing_record["appendix_findings"].pop()
    expect_invalid(
        "appendix missing one of 23 is rejected",
        missing_record,
        "missing ['F23']",
    )

    missing_overflow = copy.deepcopy(report)
    del missing_overflow["summary"]["overflow_critical"]
    expect_invalid(
        "missing overflow P1 disclosure is rejected",
        missing_overflow,
        "overflow P0/P1 disclosure is missing",
    )

    false_coverage = copy.deepcopy(report)
    false_coverage["summary"]["coverage_claim"] = "complete"
    expect_invalid(
        "partial audit claiming complete coverage is rejected",
        false_coverage,
        "must not claim complete coverage",
    )

    missing_status = copy.deepcopy(report)
    missing_status["coverage"] = [
        item
        for item in missing_status["coverage"]
        if item["status"] != "unreviewed"
    ]
    expect_invalid(
        "coverage without unreviewed shards is rejected",
        missing_status,
        "['unreviewed']",
    )

    missing_reviewed = copy.deepcopy(report)
    missing_reviewed["coverage"] = [
        item
        for item in missing_reviewed["coverage"]
        if item["status"] != "reviewed"
    ]
    expect_invalid(
        "coverage without reviewed shards is rejected",
        missing_reviewed,
        "['reviewed']",
    )

    missing_partial = copy.deepcopy(report)
    missing_partial["coverage"] = [
        item
        for item in missing_partial["coverage"]
        if item["status"] != "partial"
    ]
    expect_invalid(
        "coverage without partial shards is rejected",
        missing_partial,
        "['partial']",
    )

    partial_without_scope = copy.deepcopy(report)
    next(
        item
        for item in partial_without_scope["coverage"]
        if item["status"] == "partial"
    )["uncovered"] = ""
    expect_invalid(
        "partial coverage without uncovered scope is rejected",
        partial_without_scope,
        "partial coverage must list uncovered scope",
    )

    unreviewed_without_reason = copy.deepcopy(report)
    next(
        item
        for item in unreviewed_without_reason["coverage"]
        if item["status"] == "unreviewed"
    )["reason"] = ""
    expect_invalid(
        "unreviewed coverage without a reason is rejected",
        unreviewed_without_reason,
        "unreviewed coverage must list a reason",
    )

    wrong_impact = copy.deepcopy(two_overflow)
    wrong_impact["summary"]["overflow_critical"]["impacts"][1]["consequence"] = "错误影响"
    expect_invalid(
        "one missing overflow P1 impact is rejected",
        wrong_impact,
        "overflow P1 impacts do not match",
        two_overflow_findings,
    )

    print(f"\n=== audit overflow 复验：{checks - failures}/{checks} 通过 ===")
    if failures:
        print(f"FAIL: {failures} 项未通过", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
