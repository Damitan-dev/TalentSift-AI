ROLE_RUBRIC = {
   "Relevant Experience": {
    "weight": 30,

    "indicators": [
        "Describes a concrete backend project, work task, or practical technical context",
        "Clearly identifies the candidate's own responsibilities or contributions",
        "Describes hands-on work with backend technologies, components, or engineering practices relevant to the role",
        "Demonstrates meaningful scope or depth of responsibility in the work described",
        "Describes a concrete result, delivered functionality, or practical outcome of the work",
    ],

    "score_anchors": {
        1: (
            "Provides no meaningful evidence of relevant backend experience "
            "when the competency is explored, or gives only unsupported claims "
            "without describing actual relevant work."
        ),

        2: (
            "Mentions some relevant backend project or practical experience, "
            "but gives limited detail about the candidate's own contribution, "
            "responsibilities, technical work, or resulting outcome."
        ),

        3: (
            "Describes a concrete relevant backend project or work context, "
            "clearly identifies some of the candidate's own responsibilities "
            "or contributions, and provides specific evidence of hands-on "
            "technical work. The scope, depth, ownership, or outcome may still "
            "be limited or only partially developed."
        ),

        4: (
            "Provides clear and substantial evidence of relevant hands-on "
            "backend experience. The candidate explains their own responsibilities "
            "and contributions in specific terms, demonstrates meaningful depth "
            "or scope in relevant backend work, and describes concrete functionality, "
            "results, or outcomes from that work. A single sufficiently substantial "
            "project or work example can support this level; multiple projects or "
            "many years of experience are not required."
        ),

        5: (
            "Demonstrates all characteristics of level 4 and additionally provides "
            "explicit evidence of exceptional depth, scope, or responsibility in "
            "relevant backend work, such as substantial end-to-end ownership, "
            "responsibility for important production behavior or constraints, "
            "significant technical scope across a system, or clearly demonstrated "
            "impact beyond ordinary project contribution. These characteristics "
            "must be supported by specific candidate evidence and must not be "
            "inferred from job title, employer, years of experience, or technology names."
        ),
    },

    "non_factors": [
        "Employer or company prestige",
        "Job title prestige",
        "School or institution prestige",
        "Years of experience by itself",
        "Number of technologies named",
        "Technical jargon by itself",
        "Unrelated work experience",
        "Answer length or storytelling polish",
        "Accent",
        "Grammar accuracy",
        "Native-like English",
    ],
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
                "Demonstrates all characteristics of level 4 and additionally "
    "provides explicit evidence of exceptional depth: the candidate "
    "analyzes significant technical constraints or multiple important "
    "edge or failure cases beyond basic verification, and describes "
    "a concrete outcome or impact of the implemented solution. "
    "Do not infer these additional characteristics when they are "
    "not explicitly supported by the candidate's evidence."
            ),
        },
    },

   "Communication": {
    "weight": 20,

    "indicators": [
        "Explains technical ideas in an understandable way",
        "Organizes explanations in a logical sequence",
        "Connects technical details to the underlying problem or purpose",
        "Uses examples, comparisons, or context when they improve understanding",
        "Adapts the level of technical detail to make the explanation easier to follow",
    ],

    "score_anchors": {
        1: (
            "Provides no meaningful evidence of communicating a technical idea, "
            "or the explanation is too unclear or disconnected to understand "
            "the substantive technical meaning."
        ),

        2: (
            "Communicates some relevant technical information, but the explanation "
            "is limited, poorly structured, or relies mostly on stating or repeating "
            "technical details without making the reasoning or meaning clear."
        ),

        3: (
            "Communicates the main technical meaning understandably and provides "
            "some logical structure or explanation, but the explanation has limited "
            "context, development, or adaptation for the listener."
        ),

        4: (
            "Clearly communicates technical meaning in a logical sequence, "
            "connects details to the underlying problem or purpose, and uses "
            "appropriate context, examples, or explanation to support understanding."
        ),

        5: (
            "Demonstrates all characteristics of level 4 and additionally shows "
            "explicit evidence of exceptional communication skill, such as adapting "
            "the explanation to different levels of technical knowledge, checking "
            "understanding and changing the explanation when needed, or successfully "
            "clarifying a genuinely complex technical concept through multiple "
            "complementary explanation strategies."
        ),
    },

    "non_factors": [
        "Accent",
        "Grammar accuracy",
        "Native-like English",
        "Vocabulary sophistication",
        "Fillers or hesitations",
        "Speaking speed",
        "Sentence elegance",
    ],
},
   "Role Motivation": {
    "weight": 15,

    "indicators": [
        "Expresses interest in backend or Python engineering",
        "Identifies specific backend or Python areas of interest",
        "Connects the role to existing relevant experience",
        "Connects the role to a concrete learning or career goal",
        "Shows understanding of what the role involves",
    ],

    "score_anchors": {
        1: (
            "Provides no meaningful role-motivation evidence, "
            "or gives only an unrelated or unsupported statement."
        ),

        2: (
            "Expresses general interest in backend or Python engineering, "
            "but provides little meaningful connection to the role, "
            "existing experience, or learning and career goals."
        ),

        3: (
            "Expresses relevant interest in backend or Python engineering "
            "and connects the role to either existing relevant experience "
            "or a concrete learning or career goal, but the connection "
            "remains partial or general."
        ),

        4: (
           "Clearly identifies specific backend or Python interests and "
    "connects the role to both existing relevant experience and "
    "a concrete learning or career goal. For Role Motivation, the "
    "experience connection is satisfied when the candidate explicitly "
    "states that existing relevant project or work experience would be "
    "used, applied, built upon, or extended in the role. Detailed proof "
    "of responsibilities, technical choices, or outcomes from that "
    "experience belongs to Relevant Experience and is not required for "
    "Role Motivation. Do not reduce this score merely because the "
    "candidate expresses that connection briefly or with non-native "
    "grammar when the substantive meaning is clear. Company-specific "
    "knowledge or a detailed long-term career plan is not required for "
    "this level."
        ),

        5: (
            "Demonstrates all characteristics of level 4 plus a particularly "
            "specific understanding of the role and a well-supported connection "
            "to a deliberate longer-term development path."
        ),
    },
},

    "Culture & Values Fit": {
    "weight": 10,

    "indicators": [
        "Responds constructively to relevant feedback",
        "Takes ownership of their work, decisions, or mistakes",
        "Collaborates with others to solve problems or improve work",
        "Learns from feedback, mistakes, or new information",
        "Follows through on responsibilities or corrective actions",
    ],

    "score_anchors": {
        1: (
            "Provides no meaningful evidence of the relevant workplace "
            "behaviors when the competency is explored, or gives only "
            "unsupported claims without a concrete behavioral example."
        ),

        2: (
            "Provides some relevant evidence of collaboration, feedback, "
            "ownership, learning, or responsibility, but the example is "
            "limited, vague, mostly reactive, or shows little personal "
            "action or follow-through."
        ),

        3: (
            "Provides a concrete example demonstrating at least one "
            "meaningful behavior such as responding to feedback, taking "
            "ownership, collaborating, learning, or following through, "
            "with a clear description of the candidate's own actions."
        ),

        4: (
            "Provides a concrete example demonstrating multiple relevant "
            "behaviors, such as constructive response to feedback together "
            "with ownership, collaboration, learning, or follow-through. "
            "The candidate clearly explains their own actions and how they "
            "adapted or acted responsibly in response to the situation."
        ),

        5: (
            "Demonstrates all characteristics of level 4 and additionally "
    "provides explicit evidence of exceptional depth through broader "
    "impact beyond the candidate's own immediate task or implementation. "
    "This may include improving a shared team process, constructively "
    "resolving a difficult team disagreement, helping others adopt a "
    "better practice, or preventing recurrence across a broader team or "
    "system with a concrete outcome. Normal strong follow-through on the "
    "candidate's own work, such as correcting their implementation, "
    "updating affected code, or adding tests for that change, is consistent "
    "with level 4 and is not by itself sufficient for level 5. "
    "Do not infer broader impact when it is not explicitly supported by "
    "candidate evidence."
        ),
    },

    "non_factors": [
        "Personality similarity",
        "Extroversion or introversion",
        "Charisma",
        "Likeability",
        "Accent",
        "Grammar accuracy",
        "Native-like English",
        "Confidence of speaking style",
        "Social background or hobbies",
    ],
},
}