"""AI-driven residents: agents that look around, decide and act on their own.

An agent drives one resident. `runner.py` runs its loop: perceive through `Game.begin_turn`,
hand the observation (`observation.py`) to a brain, which acts through tools bound to that
resident (`tools.py`), then sleep until something worth waking for happens. Brains are
interchangeable: `scripted.py` follows a fixed plan, `openrouter.py` asks a language model.
The scheduling is event-driven rather than in rounds, see decision 0003 in the client
repository.
"""
