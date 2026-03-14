"""
Boucle agentique réutilisable.
Partagée par Agent 1, Agent 2 et (si besoin) Agent 3.
"""

import anthropic
from agents.tools import execute_tool

MODEL = "claude-haiku-4-5-20251001"


def run_agent(
    system: str,
    user_message: str,
    tools: list,
    max_turns: int = 10,
    model: str = MODEL,
    progress_cb=None,
) -> str:
    """
    Boucle agentique standard.

    - Envoie user_message au modèle avec le system prompt
    - Exécute les tool_use retournés jusqu'à end_turn ou max_turns
    - Retourne le texte final de l'assistant

    progress_cb(str) : callback optionnel pour affichage temps réel.
    Retourne "__MAX_TURNS_REACHED__" si la limite est atteinte.
    """
    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": user_message}]

    for turn in range(max_turns):
        kwargs = dict(
            model=model,
            max_tokens=4096,
            system=system,
            messages=messages,
        )
        if tools:
            kwargs["tools"] = tools

        response = client.messages.create(**kwargs)

        # Afficher les blocs texte intermédiaires (pensée de l'agent)
        if progress_cb:
            for block in response.content:
                if block.type == "text" and block.text.strip():
                    # Tronquer pour ne pas noyer les logs
                    preview = block.text.strip()[:300]
                    progress_cb(f"    {preview}")

        # Fin naturelle — extraire le texte final
        if response.stop_reason == "end_turn":
            final = ""
            for block in response.content:
                if block.type == "text":
                    final += block.text
            return final.strip()

        # L'agent veut utiliser des outils
        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    if progress_cb:
                        # Afficher le premier paramètre du call pour le suivi
                        first_val = next(iter(block.input.values()), "") if block.input else ""
                        progress_cb(f"  [{block.name}] {str(first_val)[:140]}")

                    result = execute_tool(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })

            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})

        else:
            # stop_reason inattendu (max_tokens...)
            if progress_cb:
                progress_cb(f"  [warn] stop_reason={response.stop_reason}, turn={turn}")
            break

    return "__MAX_TURNS_REACHED__"
