"""Fictional policy snippets for the local chatbot preview; not real HR policy."""
import re


_DOCUMENTS = (
    {
        "source": "ExampleCo New Hire Setup (fictional demo, section 1)",
        "keywords": ("new hire", "new starter", "onboard", "onboarding", "start date"),
        "answer": (
            "In this fictional ExampleCo policy, a Requestor includes the new hire's full "
            "name, start date, department, and job role, and submits the request at least "
            "three business days before the start date."
        ),
    },
    {
        "source": "ExampleCo System Access (fictional demo, section 2)",
        "keywords": ("request access", "access request", "grant access", "business reason"),
        "answer": (
            "In this fictional ExampleCo policy, an access request names the employee, "
            "system, business reason, and required access duration. A manager approves it "
            "before IT grants access."
        ),
    },
)


def mock_policy_answer(message):
    normalized = " " + " ".join(re.sub(r"[^\w\s]", " ", message.casefold()).split()) + " "
    if " mnd " in normalized and not any(
            marker in normalized for marker in (" exampleco ", " fictional ", " demo ")):
        return (
            "I don't have MND policy documents loaded, so I can't answer that as MND policy. "
            "The ExampleCo examples are fictional and not official HR guidance."
        )
    matches = [
        (sum(f" {keyword} " in normalized for keyword in document["keywords"]), document)
        for document in _DOCUMENTS
    ]
    score, document = max(matches, key=lambda match: match[0])
    if score:
        return (
            f"Demo-only mock AI (no real model): {document['answer']}\n"
            f"Source: {document['source']}. This is not official ServiceJourney or MND policy."
        )
    if "policy" in normalized or "mnd" in normalized:
        return (
            "Demo-only mock AI: I couldn't find that topic in the fictional sample policies. "
            "These examples are not official HR guidance."
        )
    return None
