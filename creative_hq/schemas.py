"""JSON schemas for each agent's structured output (the task result contract)."""


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": props,
        "required": required if required is not None else list(props),
        "additionalProperties": False,
    }


STR = {"type": "string"}
STRS = {"type": "array", "items": STR}

DIRECTOR = _obj({
    "interpretation": STR,
    "deliverable_format": {"type": "string", "enum": ["instagram_carousel", "video_script", "caption_set"]},
    "deliverable_count": {"type": "integer", "minimum": 1},
    "slides_per_carousel": {"type": "integer", "minimum": 3, "maximum": 10},
    "brief": STR,
    "assignments": _obj({
        "AG-002": STR, "AG-003": STR, "AG-004": STR, "AG-005": STR, "AG-006": STR,
    }),
    "risks": STRS,
})

STRATEGIST = _obj({
    "topics": {"type": "array", "items": _obj({
        "topic_id": STR,                       # T1, T2 ...
        "title": STR,
        "angle": STR,
        "content_pillar": STR,
        "audience_pain_point": STR,
        "why_it_matters": STR,
        "not_repetitive_because": STR,
        "questions_to_verify": STRS,
    })},
    "strategy_notes": STR,
})

RESEARCHER = _obj({
    "sources": {"type": "array", "items": _obj({
        "id": STR,                             # S1, S2 ...
        "url": STR,
        "title": STR,
        "publisher": STR,
        "accessed_via": {"type": "string", "enum": ["WebSearch", "WebFetch"]},
        "relevant_excerpt": STR,
    })},
    "topics": {"type": "array", "items": _obj({
        "topic_id": STR,
        "verified_facts": {"type": "array", "items": _obj({
            "fact": STR,
            "source_ids": STRS,
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "may_change": {"type": "boolean"},
        })},
        "unverified_claims": STRS,
        "caveats": STRS,
    })},
})

_SLIDE = _obj({
    "n": {"type": "integer"},
    "role": {"type": "string", "enum": ["hook", "content", "cta"]},
    "headline": STR,
    "body": STR,
})

COPYWRITER = _obj({
    "carousels": {"type": "array", "items": _obj({
        "topic_id": STR,
        "title": STR,
        "slides": {"type": "array", "items": _SLIDE},
        "caption": STR,
        "hashtags": STRS,
        "cta": STR,
        "fact_refs": {"type": "array", "items": _obj({
            "slide": {"type": "integer"},
            "claim": STR,
            "source_ids": STRS,
        })},
    })},
    "changes_made": STR,
})

DESIGNER = _obj({
    "concept": STR,
    "carousels": {"type": "array", "items": _obj({
        "topic_id": STR,
        "theme": {"type": "string", "enum": ["dark", "light"]},
        "slides": {"type": "array", "items": _obj({
            "n": {"type": "integer"},
            "layout": {"type": "string", "enum": ["cover", "statement", "list", "stat", "cta"]},
            "highlight": STR,
            "bullets": STR,
            "visual_note": STR,
        })},
    })},
})

SUPERVISOR = _obj({
    "verdict": {"type": "string", "enum": ["approved", "revisions_required"]},
    "summary": STR,
    "checks": {"type": "array", "items": _obj({
        "check": STR,
        "passed": {"type": "boolean"},
        "notes": STR,
    })},
    "issues": {"type": "array", "items": _obj({
        "blocking": {"type": "boolean"},
        "topic_id": STR,
        "slide": {"type": "integer"},
        "issue": STR,
        "fix": STR,
    })},
    "images_reviewed": {"type": "integer"},
})

BY_ROLE = {
    "director": DIRECTOR,
    "strategist": STRATEGIST,
    "researcher": RESEARCHER,
    "copywriter": COPYWRITER,
    "designer": DESIGNER,
    "supervisor": SUPERVISOR,
}
