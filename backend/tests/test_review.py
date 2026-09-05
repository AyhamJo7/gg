from orchestrator.review import parse_findings


def test_parse_clean_findings():
    out = 'some text\nREVIEW_FINDINGS_JSON: [{"severity": "HIGH", "category": "security", "file": "a.py", "description": "sql injection", "recommended_fix": "parameterize"}]\nmore'
    findings = parse_findings(out)
    assert len(findings) == 1
    assert findings[0]["severity"] == "HIGH"


def test_parse_empty_findings():
    assert parse_findings("REVIEW_FINDINGS_JSON: []") == []


def test_parse_garbage():
    assert parse_findings("no marker here") == []
    assert parse_findings("REVIEW_FINDINGS_JSON: {not json}") == []


def test_parse_skips_malformed_items():
    out = 'REVIEW_FINDINGS_JSON: [{"description": "ok"}, {"no_description": true}]'
    assert len(parse_findings(out)) == 1
