"""Chaoxing answer-to-question cross-reference validation."""

from __future__ import annotations

from typing import Any


def validate_question_package(questions: list[dict[str, Any]]) -> list[str]:
    """Check identifier uniqueness before paying for a model request."""
    issues: list[str] = []
    question_ids = [
        question.get("question_id")
        for question in questions
        if isinstance(question, dict)
    ]
    if len(question_ids) != len(set(question_ids)):
        issues.append("question_id values must be unique")

    for question in questions:
        if not isinstance(question, dict):
            continue
        question_id = question.get("question_id", "unknown")
        option_ids = [
            option.get("option_id")
            for option in question.get("options", [])
            if isinstance(option, dict)
        ]
        if len(option_ids) != len(set(option_ids)):
            issues.append(f"question {question_id} has duplicate option_id values")

        slot_ids = [
            slot.get("slot_id")
            for slot in question.get("answer_slots", [])
            if isinstance(slot, dict)
        ]
        if len(slot_ids) != len(set(slot_ids)):
            issues.append(f"question {question_id} has duplicate slot_id values")
    return issues


def validate_answers_against_questions(
    questions: list[dict[str, Any]], answers: list[dict[str, Any]]
) -> list[str]:
    """Check IDs and answer shapes that JSON Schema cannot cross-reference."""
    issues: list[str] = []
    questions_by_id = {
        question["question_id"]: question
        for question in questions
        if isinstance(question, dict) and isinstance(question.get("question_id"), str)
    }
    seen: set[str] = set()

    for index, answer in enumerate(answers):
        if not isinstance(answer, dict):
            issues.append(f"answers[{index}] is not an object")
            continue
        question_id = answer.get("question_id")
        if question_id not in questions_by_id:
            issues.append(f"answers[{index}] references an unknown question_id")
            continue
        if question_id in seen:
            issues.append(f"question {question_id} has more than one answer")
            continue
        seen.add(question_id)

        question = questions_by_id[question_id]
        question_type = question.get("question_type")
        answer_type = answer.get("answer_type")
        if answer_type != question_type:
            issues.append(f"question {question_id} has an answer_type mismatch")
            continue

        if question_type == "single_choice":
            option_ids = {
                option.get("option_id")
                for option in question.get("options", [])
                if isinstance(option, dict)
            }
            if answer.get("value") not in option_ids:
                issues.append(f"question {question_id} answer references an unknown option_id")

        elif question_type == "fill_blank":
            expected_slots = {
                slot.get("slot_id")
                for slot in question.get("answer_slots", [])
                if isinstance(slot, dict)
            }
            values = answer.get("values", [])
            provided_slots = [
                value.get("slot_id")
                for value in values
                if isinstance(value, dict)
            ]
            if len(provided_slots) != len(values):
                issues.append(f"question {question_id} contains an invalid blank answer")
            elif len(provided_slots) != len(set(provided_slots)):
                issues.append(f"question {question_id} repeats an answer slot")
            elif set(provided_slots) != expected_slots:
                issues.append(f"question {question_id} does not answer exactly its declared slots")

        elif question_type == "short_answer":
            value = answer.get("value")
            if not isinstance(value, str) or not value.strip():
                issues.append(f"question {question_id} has an empty short answer")

    missing = set(questions_by_id) - seen
    if missing:
        issues.append(f"{len(missing)} question(s) do not have an answer")
    return issues
