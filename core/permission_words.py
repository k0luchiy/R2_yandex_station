"""The sentences the user is told about a permission ask, and the bounded title.

Telegram is the only channel R2D2 has that can push, so the six strings below are
the whole user-facing contract of `core/permissions.py`: the question, the two
notices that follow an answer, the timeout notice, the notice for a server that
took no answer, and the label for an ask opencode described with nothing readable
in it. They live together because they change together -- rewording what the broker
says to its owner is a one-file change, and nothing here can send anything by
itself: the broker interpolates `{title}` and formats, and `core/routing.py`'s
`for_human` guard runs over the result on the way out.

`preview` is the other half of the same subject. The `{title}` these templates
interpolate is server-controlled text -- whatever the user asked for, or whatever a
model was told to run -- so it is bounded before it goes into a log record as well
as escaped there. A title can be kilobytes long, and a log line that repeats one is
neither readable nor cheap.
"""

from __future__ import annotations

from typing import Final

#: The question, the four answers, and the label for an ask opencode described with
#: nothing readable in it. Telegram is the only channel that can push, and these
#: sentences are the whole user-facing contract of the broker.
QUESTION: Final = "Нужно подтверждение: {title}. Ответь в телеграм «да» или «нет»."
ACCEPTED: Final = "Принято, выполняю: {title}."
REFUSED: Final = "Отклонено: {title}."
UNANSWERED: Final = "Подтверждение не получено, действие отклонено."
UNANSWERABLE: Final = "Сервер не принял ответ, действие отклонено: {title}."
#: An ask that arrived when the user already has as many open as the broker will hold. It
#: never became a question, so this is the only sentence that has to explain itself.
OVERLOADED: Final = "Запросов подтверждения слишком много, действие отклонено: {title}."
#: An ask that is NEVER a question, because its output would be a credential. Two clauses
#: because the user has to learn both: that the action did not happen, and that R2D2
#: declined to ask rather than being told «нет». `{source}` is a place
#: («окружение процесса», «файл с учётными данными») and never a value -- see
#: `core/policies.py:credential_source`, whose docstring says why a scrub of the collected
#: output would not have worked.
CREDENTIAL_REFUSED: Final = (
    "Действие отклонено, и я не буду спрашивать про него: команда «{title}» показала бы "
    "{source} — это попало бы в историю сессии, которую агент перечитывает. Если он правда "
    "нужен — выполни команду сам."
)
UNTITLED: Final = "действие opencode"
#: A logged ask is truncated as well as escaped: a title can be kilobytes long.
PREVIEW_CHARS: Final = 120


def preview(text: str) -> str:
    """A bounded title for a log record. Only the size is handled here: one line is
    guaranteed by the `%r` the log calls render the title with, which escapes the
    control characters a server-controlled string could carry."""
    return text[:PREVIEW_CHARS]
