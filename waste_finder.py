from pathlib import Path

from classifier import Classifier, QUESTIONS


DATA_FILE = Path(__file__).parent / "data" / "waste_data.json"
UNKNOWN_LOG = Path(__file__).parent / "data" / "unknown_queries.txt"

# Loaded once when the app starts (much faster than reading the JSON on every search).
clf = Classifier(str(DATA_FILE), unknown_log=str(UNKNOWN_LOG))


# --------------------------------------------------
# Convert classifier output to the format index.html expects
# --------------------------------------------------

def _to_view(result, query):
    method = result.get("method", "exact")

    # For material-based guesses, show what the user typed as the item name.
    if method in ("keyword", "guided", "fallback"):
        item_name = query.strip().title() or result["matched"]
    else:
        item_name = result["matched"]

    return {
        "status": "found",
        "item_name": item_name,
        "material": clf.materials[result["material"]]["name"],
        "category": result["category"],
        "bin": result["bin"],
        "disposal": result.get("disposal", ""),
        "safety": result.get("safety", ""),
        "note": result.get("note", ""),
        "method": method,
        "query": query,
        # Show "Not right?" only for guesses, not after the user already answered.
        "refine": bool(result.get("refine")) and method not in ("guided", "fallback"),
    }


def guided_questions(query, message=None):
    return {
        "status": "ask",
        "query": query,
        "message": message or f'Let\'s find where "{query}" goes. Answer two quick questions.',
        "questions": QUESTIONS,
    }


# --------------------------------------------------
# Main waste finder (same name as before, so app.py barely changes)
# --------------------------------------------------

def find_waste_item(user_input):
    if not isinstance(user_input, str) or not user_input.strip():
        return {"status": "empty", "message": "Enter a waste item."}

    result = clf.classify(user_input)
    status = result["status"]

    if status == "empty":
        return {"status": "empty", "message": "Enter a waste item."}

    if status == "found":
        return _to_view(result, user_input)

    if status == "ambiguous":
        candidates = [
            _to_view(clf._item_result(option["item"], "exact", user_input), user_input)
            for option in result["options"]
        ]
        return {
            "status": "ambiguous",
            "message": result["message"] + " Check which one matches your item.",
            "candidates": candidates,
        }

    # status == "ask": nothing matched, so guide the user with questions
    return guided_questions(user_input, result["message"])


def resolve_guided(item, material, special):
    answers = {"material": material, "special": special}
    return _to_view(clf.from_answers(answers, item), item or "This item")