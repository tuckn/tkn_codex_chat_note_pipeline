"""Convert public note fixtures to the current model-facing output contract."""
from copy import deepcopy


def wire_note(data):
    value = deepcopy(data)
    if "timeline" in value:
        value.setdefault("pendingStateItems", [])
        for item in value["timeline"]:
            if "startEventId" in item:
                item["eventId"] = item.pop("startEventId")
                item.pop("endEventId", None)
    return value
