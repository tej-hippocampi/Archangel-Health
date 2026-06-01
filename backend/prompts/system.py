SEMANTIC_ESCALATION_PROMPT = (
    "Classify the patient message for post-op escalation. "
    "Return only compact JSON object: "
    '{"tier": 0|2|3, "reason": "short reason"}. '
    "Use tier 2 for urgent same-day surgeon contact, "
    "tier 3 for navigator follow-up within 24 hours, "
    "tier 0 for no escalation."
)
