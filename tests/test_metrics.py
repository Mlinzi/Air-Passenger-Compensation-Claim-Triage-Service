from triage.evaluation.metrics import score


def test_score_recognises_grounded_citation_and_expected_route():
    result = {
        "route": "pay_as_computed",
        "citations": [{"doc_id": "car", "clause_id": "3.4.1", "quote": "compensation shall be paid"}],
        "entitlements": [{"amount_inr": 5000}],
        "rationale": "Rs 5000",
    }
    passages = [{"doc_id": "car", "clause_id": "3.4.1", "text": "Compensation shall be paid to the passenger."}]
    expected = {"route": "pay_as_computed", "supporting": ["car:3.4.1"], "decisive": ["car:3.4.1"]}

    assert score(result, passages, expected) == {
        "groundedness": 1.0,
        "context_relevance": 1.0,
        "faithfulness": 1.0,
        "hallucination": 0.0,
        "route_correct": True,
    }


def test_score_marks_unretrieved_citation_as_hallucination():
    result = {"route": "decline_with_cited_provision", "citations": [
        {"doc_id": "car", "clause_id": "9.9", "quote": "not present"}
    ]}

    scores = score(result, [{"doc_id": "car", "clause_id": "3.4.1", "text": "actual clause"}])

    assert scores["groundedness"] == 0.0
    assert scores["hallucination"] == 1.0


def test_escalation_without_citations_is_grounded_by_definition():
    assert score({"route": "escalate_to_human"}, []) ["groundedness"] == 1.0

