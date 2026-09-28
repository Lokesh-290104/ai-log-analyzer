import pytest

from app.judge import extract_numbers, judge_answer

RESULTS = {
    1: {"total_matching": 1234, "rows": [{"value": "payments", "count": 143, "share_pct": 74.5}]},
    2: {"entry": {"line": 1747, "ts": "2026-09-28T10:42:04.910Z", "message": "timeout after 5000ms"}},
}


@pytest.mark.parametrize(
    "text, numbers",
    [
        ("1,234 errors", ["1234"]),
        ("74.5% of errors", ["74.5"]),
        ("at 10:42:04 UTC", ["10", "42", "04"]),
        ("ORD-19516 and 5000ms", ["19516", "5000"]),
        ("counts: 3, 4, and 5.", ["3", "4", "5"]),
        ("no numbers here", []),
    ],
)
def test_extract_numbers(text, numbers):
    assert extract_numbers(text) == numbers


@pytest.mark.parametrize(
    "answer, evidence",
    [
        ("payments logged 143 errors, 74.5% of 1,234.", [1]),
        ("Roughly 75% came from payments.", [1]),  # rounding a tool value is allowed
        ("First timeout at 2026-09-28 10:42:04 UTC on line 1747 (5000ms).", [2]),
        ("The logs don't mention the weather.", []),
        ("Both: 143 errors, first at 10:42.", [1, 2]),
    ],
)
def test_supported_answers_pass(answer, evidence):
    assert judge_answer(answer, evidence, RESULTS, "question").ok


def test_invented_number_rejected():
    verdict = judge_answer("payments logged 150 errors.", [1], RESULTS, "q")
    assert not verdict.ok and verdict.unsupported_numbers == ["150"]
    assert "150" in verdict.problem()


def test_model_computed_number_rejected():
    # 1234 - 143 = 1091 is arithmetic the model did itself: not allowed.
    assert judge_answer("1091 errors were not from payments.", [1], RESULTS, "q").unsupported_numbers == ["1091"]


def test_numbers_must_come_from_cited_results_only():
    # 1747 exists in result 2, but the answer only cites result 1.
    assert not judge_answer("line 1747", [1], RESULTS, "q").ok


def test_numbers_without_any_evidence_rejected():
    assert not judge_answer("There were 143 errors.", [], RESULTS, "q").ok


def test_numbers_from_the_question_are_allowed():
    assert judge_answer("Between 10:00 and 11:00 nothing happened.", [], RESULTS, "what happened 10:00-11:00?").ok


def test_unknown_evidence_ids_rejected():
    verdict = judge_answer("No numbers.", [9], RESULTS, "q")
    assert not verdict.ok and verdict.unknown_evidence == [9]
    assert "9" in verdict.problem()


def test_rounding_does_not_allow_arbitrary_integers():
    # 143 is an integer result, so "140" is not a rounding of it.
    assert not judge_answer("about 140 errors", [1], RESULTS, "q").ok


TIMED = {1: {"first_ts": "2026-09-28T10:42:04.910Z", "count": 111, "filters": {"start": "2026-09-28T10:40:00Z"}}}


@pytest.mark.parametrize(
    "answer",
    [
        "The first timeout was at 10:42:04 UTC.",
        "It started at 10:42 on 2026-09-28.",
        "First seen 2026-09-28T10:42:04Z (111 times).",
        "First seen 2026-09-28 10:42:04.910 UTC.",
        "Counted from 10:40 onward.",
    ],
)
def test_times_matching_evidence_pass(answer):
    assert judge_answer(answer, [1], TIMED, "q").ok


@pytest.mark.parametrize(
    "answer, bad",
    [
        ("The first timeout was at 10:43.", "10:43"),
        ("It happened on 2026-09-29.", "2026-09-29"),
        ("There were 0 errors.", "0"),  # timestamps don't smuggle in small numbers
        ("It lasted 12 minutes.", "12"),  # a duration the model computed
        ("There were 42 of them.", "42"),  # 42 is only inside a timestamp
    ],
)
def test_times_and_timestamp_parts_not_in_evidence_fail(answer, bad):
    verdict = judge_answer(answer, [1], TIMED, "q")
    assert not verdict.ok and bad in verdict.unsupported_numbers


def test_times_in_the_question_are_allowed():
    assert judge_answer("Nothing between 9:00 and 9:30.", [], {}, "what happened 09:00 to 09:30?").ok


def test_huge_numbers_in_evidence_do_not_crash():
    results = {1: {"entry": {"message": "id " + "9" * 400}, "count": 3}}
    assert judge_answer("There were 3.", [1], results, "q").ok
    assert not judge_answer("There were 4.", [1], results, "q").ok
