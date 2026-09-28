"""Structured-action adapters for Pokémon's templates.

GameAPI offers Pokémon Team Preview and doubles turns as ONE legal action
whose ``input["action"]`` describes what to fill in (Agent_ACP gameapi
``pokemon_adapter/teampreview_mapper.py`` ``teampreview_action_schema`` and
``pokemon_adapter/doubles_action_mapper.py`` ``build_doubles_legal_actions``).
These adapters turn each template into a ``Choice``: the model picks from
the template's own options (roster species; per-slot option indices and
legal targets), and ``build`` enforces the rules GameAPI would otherwise
reject — the model never writes the payload itself.

Draft picks need nothing here: they're ordinary legal actions, handled by
the generic path. Fallbacks are ``examples/smoke_agent.py``'s deterministic
choice, which also validates the template before any model call.
"""

from __future__ import annotations

from altruagent import GameState, LegalAction
from examples import smoke_agent

from .base import Choice, InvalidChoice, object_schema


def lineup_choice(action: LegalAction, state: GameState) -> Choice:
    fallback = smoke_agent.choose_action(state, None)  # validates the template
    template = action.input["action"]
    roster = list(dict.fromkeys(template["roster"]))
    species = {"type": "array", "items": {"type": "string", "enum": roster}}

    def build(answer: dict) -> dict:
        bring, leads = answer.get("bring"), answer.get("leads")
        if not isinstance(bring, list) or len(bring) != 4 or len(set(bring)) != 4 or not set(bring) <= set(roster):
            raise InvalidChoice(f"bring must be 4 different species from the roster {roster}, got {bring!r}")
        if not isinstance(leads, list) or len(leads) != 2 or len(set(leads)) != 2 or not set(leads) <= set(bring):
            raise InvalidChoice(f"leads must be 2 different species from bring {bring}, got {leads!r}")
        return {"type": "select_lineup", "bring": list(bring), "leads": list(leads)}

    return Choice(
        kind="select_lineup",
        prompt={"roster": roster, "instructions": template.get("instructions")},
        schema=object_schema({"bring": species, "leads": species}),
        build=build,
        fallback=lambda: fallback,
    )


def doubles_choice(action: LegalAction, state: GameState) -> Choice:
    fallback = smoke_agent.choose_action(state, None)  # validates the template
    template = action.input["action"]
    slots = sorted(template["slots"], key=lambda slot: slot.get("slot", 0))
    options = [slot["options"] for slot in slots]
    slot_schema = object_schema(
        {"option": {"type": "integer"}, "target": {"type": ["integer", "null"]}}, reasoning=False
    )

    def build_slot(number: int, answer: object) -> dict:
        if not isinstance(answer, dict):
            raise InvalidChoice(f"slot_{number} must be an object with option and target")
        index, target = answer.get("option"), answer.get("target")
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(options[number]):
            raise InvalidChoice(f"slot_{number}.option must be an index 0..{len(options[number]) - 1}, got {index!r}")
        option = options[number][index]
        if option["type"] == "pass":
            return {"type": "pass"}
        if option["type"] == "switch":
            return {"type": "switch", "species": option["species"]}
        choice = {"type": "move", "move_id": option["move_id"]}
        targets = option.get("targets") or []
        if targets:
            if isinstance(target, bool) or target not in targets:
                raise InvalidChoice(
                    f"slot_{number}: target {target!r} is not legal for {option['move_id']}; choose one of {targets}"
                )
            choice["target"] = target
        return choice

    def build(answer: dict) -> dict:
        slot_0, slot_1 = build_slot(0, answer.get("slot_0")), build_slot(1, answer.get("slot_1"))
        if slot_0["type"] == slot_1["type"] == "switch" and slot_0["species"] == slot_1["species"]:
            raise InvalidChoice(f"both slots cannot switch in the same Pokémon ({slot_0['species']})")
        if slot_0["type"] == slot_1["type"] == "pass" and not all(
            all(option["type"] == "pass" for option in slot_options) for slot_options in options
        ):
            raise InvalidChoice("both slots cannot pass while a move or switch is available")
        return {"type": "doubles_turn", "slot_0": slot_0, "slot_1": slot_1}

    return Choice(
        kind="doubles_turn",
        prompt={
            "slots": [
                {
                    "slot": number,
                    "active": slot.get("active"),
                    "force_switch": slot.get("force_switch"),
                    "options": [{"option": index, **option} for index, option in enumerate(slot["options"])],
                }
                for number, slot in enumerate(slots)
            ],
            "target_legend": template.get("target_legend"),
            "instructions": (
                "For each of slot_0 and slot_1, pick one option index from that slot's options. "
                "For a move whose targets list is non-empty, target must be one of those integers; "
                "otherwise target is null. Both slots cannot switch into the same Pokémon. "
                f"Server instructions: {template.get('instructions')}"
            ),
        },
        schema=object_schema({"slot_0": slot_schema, "slot_1": slot_schema}),
        build=build,
        fallback=lambda: fallback,
    )


ADAPTERS = {
    "select_lineup": lineup_choice,
    "doubles_turn": doubles_choice,
}
