ROLE_RUBRIC = {
    "Relevant Experience": {
        "weight": 30,
        "strong_answer_looks_like": (
            "Describes relevant backend projects or practical work, including "
            "responsibilities, technologies, challenges, and outcomes."
        ),
    },

    "Problem Solving": {
        "weight": 25,

        "indicators": [
            "Identifies a concrete problem",
            "Explains investigation or diagnosis",
            "Explains reasoning behind the chosen approach",
            "Considers alternatives or trade-offs",
            "Describes the implemented solution",
            "Explains how the result was verified",
        ],

        "score_anchors": {
            1: (
                "Provides no meaningful problem-solving evidence, "
                "or gives only unsupported or very general claims."
            ),
            2: (
                "Identifies a problem and some action, but provides "
                "little reasoning and little or no verification."
            ),
            3: (
                "Explains a concrete problem, some reasoning, "
                "a solution, and basic verification."
            ),
            4: (
                "Explains a concrete problem, systematic reasoning, "
                "alternatives or trade-offs, a justified solution, "
                "and meaningful verification."
            ),
            5: (
                "Demonstrates all characteristics of level 4 plus "
                "particularly strong technical depth, edge-case thinking, "
                "trade-off analysis, or clear evidence of impact."
            ),
        },
    },

    "Communication": {
        "weight": 20,
        "strong_answer_looks_like": (
            "Explains technical ideas clearly and logically. Judge clarity of "
            "meaning, not accent, grammar, fluency, fillers, or native-like English."
        ),
    },

    "Role Motivation": {
        "weight": 15,
        "strong_answer_looks_like": (
            "Connects interest in Python/backend engineering with this role "
            "and the candidate's learning or career goals."
        ),
    },

    "Culture & Values Fit": {
        "weight": 10,
        "strong_answer_looks_like": (
            "Gives evidence of collaboration, feedback, ownership, learning, "
            "or responsibility."
        ),
    },
}