"""Application-specific helpers — small modules that know hamroh's domain.

Unlike :mod:`hamroh.utils` (generic, reusable anywhere), these encode
knowledge of *this* app: the ``hamroh`` logger tree and log file
(``logging_setup``), and the conversation transcript model —
``ChatRef``/``UserRef``/``MsgRef`` on the ``hamroh.tx`` logger
(``transcript``). Imported directly
(``from hamroh.helpers.transcript import log_outbound``); re-exports nothing.
"""

from __future__ import annotations
