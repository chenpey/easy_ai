"""Executable B65 rules for rendering an inventory larger than 20 findings.

This is a conformance model for the audit report contract, not a scanner.  It
keeps every confirmed finding in the appendix and makes the selected body,
overflow critical count and shard coverage independently visible.
"""

from __future__ import annotations


MAX_BODY_FINDINGS = 20
SEVERITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


def render_audit(findings: list[dict], coverage: list[dict]) -> dict:
    """Render a complete inventory under the audit body limit."""
    ordered = sorted(
        findings,
        key=lambda finding: (
            SEVERITY_ORDER.get(str(finding.get("severity", "")).upper(), 99),
            str(finding.get("id", "")),
        ),
    )
    body = ordered[:MAX_BODY_FINDINGS]
    body_ids = {finding.get("id") for finding in body}
    overflow = [
        finding for finding in ordered if finding.get("id") not in body_ids
    ]
    overflow_critical = [
        finding
        for finding in overflow
        if str(finding.get("severity", "")).upper() in {"P0", "P1"}
    ]
    statuses = {
        item.get("status")
        for item in coverage
        if isinstance(item, dict)
    }

    return {
        "body_findings": body,
        "appendix_findings": ordered,
        "summary": {
            "total_findings": len(ordered),
            "body_limit": MAX_BODY_FINDINGS,
            "coverage_claim": (
                "complete"
                if statuses and not statuses & {"partial", "unreviewed"}
                else "partial"
            ),
            "overflow_critical": {
                "count": len(overflow_critical),
                "impacts": [
                    {
                        "id": finding.get("id"),
                        "consequence": finding.get("consequence"),
                    }
                    for finding in overflow_critical
                ],
                "record": "appendix",
            },
        },
        "coverage": list(coverage),
    }
